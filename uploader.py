"""Offload motion captures from the tmpfs buffer to an NFS archive.

motion writes stills and movies into MOTION_TARGET_DIR, which is a tmpfs: fast,
kind to the SD card, and volatile. Every closed file is handed to this module by
the on_picture_save / on_movie_end hooks, copied to the NFS share, then dropped
from the buffer.

Two rules shape everything here:

  * Nothing touches the NFS mount on a request thread. A wedged NAS must not be
    able to hang the UI, so the worker owns every call that can block and the
    web layer only ever reads a cached status dict.
  * The buffer directory *is* the queue. The in-memory queue is a fast path; a
    periodic sweep rescans the directory, so a Flask restart, a missed hook or a
    failed upload all recover on their own without a persisted work list.

Because the buffer is RAM, a long NAS outage is a real risk to the machine. Once
usage crosses BUFFER_HIGH_WATER the sweep deletes the oldest pending captures to
keep the Pi alive, and says so loudly in the log.
"""

import os
import queue
import re
import shutil
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Settings ───────────────────────────────────────────────────────────────────

BUFFER_DIR   = Path(os.environ.get("MOTION_TARGET_DIR", "/var/lib/motion"))
NFS_ENABLED  = os.environ.get("NFS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
NFS_MOUNT    = Path(os.environ.get("NFS_MOUNT", "/mnt/picam-nas"))
NFS_SUBDIR   = os.environ.get("NFS_SUBDIR", "picam").strip("/")
CAMERA_NAME  = os.environ.get("CAMERA_NAME", "").strip() or socket.gethostname()

BUFFER_HIGH_WATER     = min(99, max(10, int(os.environ.get("BUFFER_HIGH_WATER", 80))))
UPLOAD_SWEEP_INTERVAL = max(5,  int(os.environ.get("UPLOAD_SWEEP_INTERVAL", 30)))
UPLOAD_STABLE_AGE     = max(1,  int(os.environ.get("UPLOAD_STABLE_AGE", 30)))
UPLOAD_RETRY_MIN      = max(1,  int(os.environ.get("UPLOAD_RETRY_MIN", 5)))
UPLOAD_RETRY_MAX      = max(UPLOAD_RETRY_MIN, int(os.environ.get("UPLOAD_RETRY_MAX", 300)))

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".flv", ".swf", ".m4v"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES

COPY_CHUNK = 1024 * 1024

# ── Helpers ────────────────────────────────────────────────────────────────────


def kind_for(path):
    """'image', 'video', or None if this is not a capture we handle."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def nfs_is_mounted(mount):
    """True only if a real NFS filesystem is mounted at `mount`.

    os.path.ismount() is not good enough here: with an automount unit in place
    it reports the autofs stub as a mount even when the server is unreachable,
    and a plain existence check would happily let us write captures into the
    empty local mountpoint on the SD card and never notice. /proc/mounts is on
    procfs, so reading it cannot block on a dead server.
    """
    try:
        target = os.path.realpath(str(mount))
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                # Mount points are octal-escaped in /proc/mounts (\040 for space)
                point = re.sub(r"\\([0-7]{3})",
                               lambda m: chr(int(m.group(1), 8)), parts[1])
                if os.path.realpath(point) == target and parts[2].startswith("nfs"):
                    return True
    except Exception:
        pass
    return False


def usage_pct(path):
    """Percentage of `path`'s filesystem in use, or None if it cannot be read."""
    try:
        st = os.statvfs(str(path))
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return None
    free = st.f_bavail * st.f_frsize
    return round((total - free) / total * 100, 1)


def _space(path):
    try:
        st = os.statvfs(str(path))
    except OSError:
        return {"total": None, "free": None, "used_pct": None}
    total = st.f_blocks * st.f_frsize
    free  = st.f_bavail * st.f_frsize
    return {
        "total": total,
        "free": free,
        "used_pct": round((total - free) / total * 100, 1) if total > 0 else None,
    }

# ── Uploader ───────────────────────────────────────────────────────────────────


class Uploader:
    def __init__(self, logger=None):
        self.log       = logger
        self._queue    = queue.Queue()
        self._pending  = set()          # paths already queued, to avoid duplicates
        self._lock     = threading.Lock()
        self._thread   = None
        self._retry_at = 0.0            # monotonic; no NFS attempts before this
        self._backoff  = UPLOAD_RETRY_MIN
        self._status   = {
            "enabled":     NFS_ENABLED,
            "mount":       str(NFS_MOUNT),
            "archive_dir": str(NFS_MOUNT / NFS_SUBDIR / CAMERA_NAME) if NFS_ENABLED else None,
            "mounted":     False,
            "queued":      0,
            "uploaded":    0,
            "failed":      0,
            "dropped":     0,
            "last_upload": None,
            "last_error":  None,
            "last_error_ts": None,
            "buffer":      {"path": str(BUFFER_DIR), "files": 0, "total": None,
                            "free": None, "used_pct": None},
            "archive":     {"total": None, "free": None, "used_pct": None},
        }

    # ── Logging ───────────────────────────────────────────────────────────────

    def _info(self, msg):
        if self.log:
            self.log.info(msg)
        else:
            print(f"[uploader] {msg}", flush=True)

    def _warn(self, msg):
        if self.log:
            self.log.warning(msg)
        else:
            print(f"[uploader] WARNING: {msg}", flush=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Start the worker.

        The worker runs even with NFS offload disabled. The sweep is the only
        thing that reports buffer usage and the only thing that sheds at the
        high-water mark, so gating the whole worker on NFS_ENABLED would leave
        the tmpfs with no protection at all while the share is being set up.
        Only the upload itself is gated.
        """
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="uploader", daemon=True)
        self._thread.start()
        if NFS_ENABLED:
            self._info(
                f"NFS offload started: {BUFFER_DIR} -> "
                f"{NFS_MOUNT / NFS_SUBDIR / CAMERA_NAME} (high water {BUFFER_HIGH_WATER}%)"
            )
        else:
            self._info(
                f"NFS offload disabled — watching {BUFFER_DIR} only "
                f"(high water {BUFFER_HIGH_WATER}%, captures are never archived)"
            )

    def enqueue(self, path):
        """Hand a freshly closed capture to the worker. Never blocks."""
        if not NFS_ENABLED:
            return False
        try:
            path = Path(path).resolve()
        except Exception:
            return False
        # Only accept files inside the buffer — this is reachable from a route.
        try:
            if not path.is_relative_to(BUFFER_DIR.resolve()):
                return False
        except Exception:
            return False
        if kind_for(path) is None or not path.is_file():
            return False
        with self._lock:
            if path in self._pending:
                return True
            self._pending.add(path)
        self._queue.put(path)
        return True

    def status(self):
        """Cached status. Reads no network filesystem, so it cannot block."""
        with self._lock:
            snap = dict(self._status)
            snap["queued"] = len(self._pending)
            return snap

    def archive_root(self):
        """Archive directory, or None when offload is off or the NAS is down."""
        if not NFS_ENABLED:
            return None
        with self._lock:
            if not self._status["mounted"]:
                return None
        return NFS_MOUNT / NFS_SUBDIR / CAMERA_NAME

    # ── Worker ────────────────────────────────────────────────────────────────

    def _worker(self):
        next_sweep = 0.0
        while True:
            now = time.monotonic()
            if now >= next_sweep:
                try:
                    self._sweep()
                except Exception as e:
                    self._warn(f"sweep failed: {e}")
                next_sweep = time.monotonic() + UPLOAD_SWEEP_INTERVAL
            try:
                path = self._queue.get(timeout=max(1.0, next_sweep - time.monotonic()))
            except queue.Empty:
                continue
            try:
                self._handle(path)
            except Exception as e:
                self._warn(f"upload of {path} failed: {e}")
                self._fail(str(e))
            finally:
                with self._lock:
                    self._pending.discard(path)

    def _handle(self, path):
        if time.monotonic() < self._retry_at:
            # Backing off from a NAS failure. Leave the file alone; the sweep
            # re-enqueues everything still sitting in the buffer.
            return
        if not path.exists():
            return
        if not nfs_is_mounted(NFS_MOUNT):
            self._fail(f"{NFS_MOUNT} is not an NFS mount")
            return
        self._upload(path)

    def _upload(self, src):
        st       = src.stat()
        stamp    = datetime.fromtimestamp(st.st_mtime)
        dest_dir = NFS_MOUNT / NFS_SUBDIR / CAMERA_NAME / stamp.strftime("%Y/%m/%d")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self._pick_dest(dest_dir, src, st.st_size)

        if dest is None:            # identical file already archived
            src.unlink(missing_ok=True)
            self._succeed()
            return

        tmp = dest.with_name(dest.name + ".part")
        try:
            with src.open("rb") as fsrc, tmp.open("wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, COPY_CHUNK)
                fdst.flush()
                os.fsync(fdst.fileno())
            os.utime(tmp, (st.st_atime, st.st_mtime))
            if tmp.stat().st_size != st.st_size:
                raise IOError(
                    f"size mismatch after copy: {tmp.stat().st_size} != {st.st_size}"
                )
            os.replace(tmp, dest)   # same filesystem, so this is atomic
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        src.unlink(missing_ok=True)
        self._succeed()

    def _pick_dest(self, dest_dir, src, size):
        """Destination path, or None if an identical capture is already there."""
        dest = dest_dir / src.name
        n    = 0
        while dest.exists():
            if dest.stat().st_size == size:
                return None
            n += 1
            dest = dest_dir / f"{src.stem}-{n}{src.suffix}"
        return dest

    # ── Sweep: rescan, retry, and protect the tmpfs ───────────────────────────

    def _sweep(self):
        mounted = nfs_is_mounted(NFS_MOUNT) if NFS_ENABLED else False
        buf     = _space(BUFFER_DIR)
        files   = self._buffer_files()

        with self._lock:
            self._status["mounted"] = mounted
            self._status["buffer"]  = {"path": str(BUFFER_DIR), "files": len(files), **buf}
            self._status["archive"] = _space(NFS_MOUNT) if mounted else \
                {"total": None, "free": None, "used_pct": None}

        # An outage is not a failed upload — nothing was attempted — but /storage
        # should still say what is wrong and since when, not just "not mounted".
        # With offload switched off there is no outage to report.
        if NFS_ENABLED and not mounted:
            self._note_outage()

        # The buffer is RAM. Filling it takes the whole Pi down, so shedding the
        # oldest captures beats wedging the machine — but it must be visible.
        if buf["used_pct"] is not None and buf["used_pct"] >= BUFFER_HIGH_WATER:
            self._shed(files, buf)
            files = self._buffer_files()

        if NFS_ENABLED and mounted and time.monotonic() >= self._retry_at:
            now = time.time()
            for path, fstat in files:
                # Skip anything still being written — a movie in progress has a
                # moving mtime, so this excludes it until motion closes the file.
                if now - fstat.st_mtime < UPLOAD_STABLE_AGE:
                    continue
                self.enqueue(path)

    def _buffer_files(self):
        """[(path, stat)] of media in the buffer, oldest first."""
        out = []
        try:
            for p in BUFFER_DIR.rglob("*"):
                if not p.is_file() or kind_for(p) is None:
                    continue
                try:
                    out.append((p, p.stat()))
                except OSError:
                    continue
        except OSError:
            return []
        out.sort(key=lambda pair: pair[1].st_mtime)
        return out

    def _shed(self, files, buf):
        target = BUFFER_HIGH_WATER - 10
        total  = buf["total"] or 0
        if total <= 0 or not files:
            return
        used    = total - (buf["free"] or 0)
        to_free = used - int(total * target / 100.0)
        dropped = 0
        for path, fstat in files:                  # oldest first
            if to_free <= 0:
                break
            try:
                path.unlink()
            except OSError:
                continue
            to_free -= fstat.st_size
            dropped += 1
            with self._lock:
                self._pending.discard(path)
        if dropped:
            with self._lock:
                self._status["dropped"] += dropped
            why = ("without archiving them" if NFS_ENABLED
                   else "unarchived (NFS offload is disabled)")
            self._warn(
                f"buffer {buf['used_pct']}% full (high water {BUFFER_HIGH_WATER}%) — "
                f"discarded {dropped} of the oldest captures {why}"
            )

    # ── Outcome bookkeeping ───────────────────────────────────────────────────

    def _succeed(self):
        with self._lock:
            self._status["uploaded"]   += 1
            self._status["last_upload"] = int(time.time())
            self._status["last_error"]  = None
        self._backoff  = UPLOAD_RETRY_MIN
        self._retry_at = 0.0

    def _note_outage(self):
        message = f"{NFS_MOUNT} is not an NFS mount"
        with self._lock:
            first = self._status["last_error"] != message
            self._status["last_error"]    = message
            self._status["last_error_ts"] = int(time.time())
        if first:
            self._warn(f"{message} — holding captures in {BUFFER_DIR}")

    def _fail(self, message):
        with self._lock:
            self._status["failed"]        += 1
            self._status["last_error"]     = message
            self._status["last_error_ts"]  = int(time.time())
        self._retry_at = time.monotonic() + self._backoff
        self._warn(f"{message} — retrying in {self._backoff}s")
        self._backoff = min(UPLOAD_RETRY_MAX, self._backoff * 2)
