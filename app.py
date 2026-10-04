import os
import math
import hashlib
import logging
import tempfile
import zipfile
import json
import queue
import time
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

import requests
from flask import (
    Flask, render_template, request, jsonify,
    Response, session, redirect, url_for, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash

import uploader

# ── Helpers ────────────────────────────────────────────────────────────────────

def clamp(value, lo, hi):
    return max(lo, min(hi, value))

# ── Soft limits (clamped to hardware ±90°) ────────────────────────────────────

PAN_MIN  = clamp(int(os.environ.get("PAN_MIN",  -90)), -90, 90)
PAN_MAX  = clamp(int(os.environ.get("PAN_MAX",   90)), -90, 90)
TILT_MIN = clamp(int(os.environ.get("TILT_MIN", -90)), -90, 90)
TILT_MAX = clamp(int(os.environ.get("TILT_MAX",  90)), -90, 90)

# Guard against inverted limits
if PAN_MIN  >= PAN_MAX:  PAN_MIN,  PAN_MAX  = -90, 90
if TILT_MIN >= TILT_MAX: TILT_MIN, TILT_MAX = -90, 90

DEFAULT_STEP = 5
PRESETS_FILE      = Path(os.environ.get("PRESETS_FILE", "/opt/picam/presets.json"))
MOTION_TARGET_DIR = Path(os.environ.get("MOTION_TARGET_DIR", "/var/lib/motion"))
GALLERY_LIMIT     = max(1, int(os.environ.get("GALLERY_LIMIT", 50)))
# How many captures one delete or download may name. Deliberately separate from
# GALLERY_LIMIT: that is a page size, and someone can load several pages before
# selecting, so tying the two together would cap bulk actions at one page.
BULK_MAX          = max(1, int(os.environ.get("BULK_MAX", 500)))
# Cap on a single zip bundle, so select-all cannot fill the SD card
ZIP_MAX_BYTES     = max(1, int(os.environ.get("ZIP_MAX_MB", 512))) * 1024 * 1024

# ── Startup position ───────────────────────────────────────────────────────────

_pan_start  = clamp(int(os.environ.get("PAN_START",  0)), PAN_MIN,  PAN_MAX)
_tilt_start = clamp(int(os.environ.get("TILT_START", 0)), TILT_MIN, TILT_MAX)

# ── Pan/tilt hardware ──────────────────────────────────────────────────────────

# auto — use the HAT if it answers, otherwise run as a fixed camera
# on   — the HAT is expected; say so loudly if it is missing
# off  — fixed camera, never touch I2C at all
PANTILT_MODE = os.environ.get("PANTILT_ENABLED", "auto").strip().lower()
if PANTILT_MODE not in ("auto", "on", "off"):
    PANTILT_MODE = "auto"

HARDWARE       = False
PANTILT_REASON = ""

if PANTILT_MODE == "off":
    PANTILT_REASON = "disabled by PANTILT_ENABLED=off"
    print("pan/tilt disabled — running as a fixed camera")
else:
    try:
        import pantilthat
        pantilthat.idle_timeout(0)  # keep servos powered between button presses
        # pan() opens the I2C bus and writes, so an absent HAT raises right here
        # (after ~10 retries) rather than failing silently on every later move.
        pantilthat.pan(_pan_start)
        pantilthat.tilt(_tilt_start)
        HARDWARE = True
    except Exception as e:
        PANTILT_REASON = f"{type(e).__name__}: {e}"
        # A servo fault must never take down the camera: the stream, snapshot,
        # gallery and motion alerts are all useful without a HAT.
        if PANTILT_MODE == "on":
            print(f"ERROR: PANTILT_ENABLED=on but the pan/tilt HAT did not "
                  f"respond ({PANTILT_REASON}) — continuing as a fixed camera")
        else:
            print(f"no pan/tilt HAT detected, running as a fixed camera "
                  f"({PANTILT_REASON})")


def _pantilt_state():
    return {"available": HARDWARE, "mode": PANTILT_MODE, "reason": PANTILT_REASON}

# ── Auth ───────────────────────────────────────────────────────────────────────

AUTH_USER    = os.environ.get("AUTH_USER", "")
AUTH_PASS    = os.environ.get("AUTH_PASS", "")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASS)
_pw_hash     = generate_password_hash(AUTH_PASS) if AUTH_ENABLED else None

# ── App ────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())
app.config['SESSION_COOKIE_SECURE']   = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ── Static asset versions ──────────────────────────────────────────────────────

# Hash the contents, not the mtime: the query only changes when the file really
# changes, so browsers keep a warm cache across reinstalls that touch nothing.
def _asset_version(name):
    try:
        return hashlib.sha256(
            (Path(app.static_folder) / name).read_bytes()
        ).hexdigest()[:10]
    except OSError:
        return "dev"


ASSETS = {name: _asset_version(name)
          for name in ("base.css", "app.css", "app.js", "login.css")}

# ── Logging ────────────────────────────────────────────────────────────────────

# Flask leaves app.logger at NOTSET, so it inherits root's WARNING and every
# info() is dropped. That silently cost us the uploader's status lines and, worse,
# the audit trail for gallery deletions. Make INFO the floor.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
app.logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

state = {"pan": _pan_start, "tilt": _tilt_start}
lock  = threading.Lock()

# ── Server-Sent Events ─────────────────────────────────────────────────────────

_sse_clients = set()
_sse_lock    = threading.Lock()


def _push_sse(msg):
    """Push a pre-formatted SSE message string to all connected clients."""
    with _sse_lock:
        dead = set()
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.add(q)
        _sse_clients.difference_update(dead)


def _notify_clients(pan, tilt):
    _push_sse(f"data: {json.dumps({'pan': pan, 'tilt': tilt})}\n\n")


def _notify_motion():
    _push_sse(f"event: motion\ndata: {json.dumps({'ts': int(time.time())})}\n\n")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED and not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def pantilt_required(f):
    """Refuse movement when there is no HAT, instead of reporting a fake success."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not HARDWARE:
            return jsonify({
                "error":   "pan/tilt is not available on this camera",
                "pantilt": _pantilt_state(),
            }), 409
        return f(*args, **kwargs)
    return decorated


def _apply(pan, tilt):
    """Write pan/tilt to state and hardware. Must be called under lock."""
    state["pan"]  = pan
    state["tilt"] = tilt
    if HARDWARE:
        pantilthat.pan(pan)
        pantilthat.tilt(tilt)
    try:
        _notify_clients(pan, tilt)
    except Exception:
        app.logger.exception("SSE notify failed")

# ── Auto-scan ──────────────────────────────────────────────────────────────────

# Sweep speed in radians/sec — full left-to-right takes ~(π / SCAN_SPEED) seconds
SCAN_SPEED   = float(os.environ.get("SCAN_SPEED", "0.4"))
scan_active  = False
_scan_thread = None
_scan_lock   = threading.Lock()


def _scan_worker():
    t0 = time.monotonic()
    pan_amp    = (PAN_MAX - PAN_MIN) / 2.0
    pan_center = (PAN_MAX + PAN_MIN) / 2.0
    while scan_active:
        t   = time.monotonic() - t0
        pan = int(pan_center + pan_amp * math.sin(t * SCAN_SPEED))
        with lock:
            if scan_active:
                _apply(clamp(pan, PAN_MIN, PAN_MAX), state["tilt"])
        time.sleep(0.05)


def _set_scan(enabled):
    global scan_active, _scan_thread
    if not HARDWARE:
        return          # nothing to sweep; the thread would burn CPU and SSE for nothing
    if enabled:
        _cancel_glide()     # the sweep and a travelling preset would fight over the servo
    with _scan_lock:
        scan_active = enabled
        if enabled and (_scan_thread is None or not _scan_thread.is_alive()):
            _scan_thread = threading.Thread(target=_scan_worker, daemon=True)
            _scan_thread.start()

# ── Eased absolute moves ───────────────────────────────────────────────────────

# Degrees per second for a jump to an absolute position — a preset or Home.
# A bare pantilthat.pan() call slews the servo at full speed, and the jolt is
# enough to walk the whole rig across a table. Stepping the angle bounds the
# angular velocity instead. 0 disables the easing and jumps straight there.
GOTO_SPEED    = max(0.0, float(os.environ.get("GOTO_SPEED", "45")))
GLIDE_INTERVAL = 0.02          # 50 Hz; step size falls out of the speed

_glide_lock = threading.Lock()
_glide_seq  = 0                # bumped to retire whatever glide is in flight


def _cancel_glide():
    """Retire any glide in progress. The worker notices by its sequence number."""
    global _glide_seq
    with _glide_lock:
        _glide_seq += 1
        return _glide_seq


def _glide_worker(target_pan, target_tilt, seq):
    step = max(1, int(round(GOTO_SPEED * GLIDE_INTERVAL)))
    while True:
        with _glide_lock:
            if seq != _glide_seq:
                return                  # a newer move superseded this one
        with lock:
            cur_pan, cur_tilt = state["pan"], state["tilt"]
            nxt_pan  = clamp(cur_pan  + max(-step, min(step, target_pan  - cur_pan)),
                             PAN_MIN,  PAN_MAX)
            nxt_tilt = clamp(cur_tilt + max(-step, min(step, target_tilt - cur_tilt)),
                             TILT_MIN, TILT_MAX)
            # No change means either arrived or a soft limit blocks the rest —
            # either way there is nothing left to do, so this always terminates.
            if (nxt_pan, nxt_tilt) == (cur_pan, cur_tilt):
                return
            _apply(nxt_pan, nxt_tilt)
        time.sleep(GLIDE_INTERVAL)


def _goto(pan, tilt):
    """Move to an absolute position, eased unless GOTO_SPEED is 0.

    Returns immediately: the stepping runs on its own thread so the request is
    not held open for the length of the travel, and the UI follows along over
    SSE because every step goes through _apply().
    """
    seq = _cancel_glide()
    if GOTO_SPEED <= 0 or not HARDWARE:
        with lock:
            _apply(pan, tilt)
        return False
    threading.Thread(target=_glide_worker, args=(pan, tilt, seq),
                     daemon=True, name="glide").start()
    return True

# ── Presets ────────────────────────────────────────────────────────────────────

presets_lock = threading.Lock()


def load_presets():
    """Read saved positions from disk.

    An unreadable file is moved aside rather than ignored: returning {} silently
    would let the next save overwrite the damaged file and destroy whatever
    could still have been recovered from it.
    """
    if not PRESETS_FILE.exists():
        return {}
    try:
        data = json.loads(PRESETS_FILE.read_text())
    except Exception as e:
        damaged = PRESETS_FILE.with_name(PRESETS_FILE.name + ".corrupt")
        try:
            PRESETS_FILE.replace(damaged)
            print(f"presets file unreadable ({e}); moved to {damaged}")
        except OSError:
            print(f"presets file unreadable ({e}) and could not be moved aside")
        return {}
    if not isinstance(data, dict):
        print(f"presets file is {type(data).__name__}, expected object — ignoring")
        return {}
    return data


def save_presets_file(data):
    """Write presets atomically.

    write_text() truncates before writing, so losing power in between leaves an
    empty or half-written file and every preset is gone. Write a sibling temp
    file, flush it to disk, then rename: on POSIX a rename within one filesystem
    is atomic, so a reader only ever sees the whole old file or the whole new
    one. The directory is fsynced too, or the rename itself can be lost.
    """
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_FILE.with_name(PRESETS_FILE.name + ".tmp")
    try:
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PRESETS_FILE)
        dir_fd = os.open(PRESETS_FILE.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


presets = load_presets()

# ── Media offload ──────────────────────────────────────────────────────────────

# motion writes captures into MOTION_TARGET_DIR (a tmpfs); the uploader copies
# them to the NFS archive and clears the buffer. It is a no-op unless NFS_ENABLED.
media_uploader = uploader.Uploader(logger=app.logger)
media_uploader.start()

# ── Public assets (no auth — needed by browser before login) ──────────────────

@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


@app.route("/sw.js")
def service_worker():
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"]        = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == AUTH_USER and check_password_hash(_pw_hash, password):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Invalid username or password"
    return render_template("login.html", error=error, assets=ASSETS)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ── App routes ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    # The template needs this at render time, not after /position resolves —
    # otherwise the pan/tilt controls flash up before being removed.
    return render_template("index.html", pantilt=_pantilt_state(), assets=ASSETS)


@app.route("/limits")
@login_required
def limits():
    return jsonify({
        "pan":  {"min": PAN_MIN,  "max": PAN_MAX},
        "tilt": {"min": TILT_MIN, "max": TILT_MAX},
    })


@app.route("/position")
@login_required
def position():
    with lock:
        return jsonify({
            **state,
            "hardware": HARDWARE,          # kept for older clients
            "pantilt":  _pantilt_state(),
            "scan":     scan_active,
            "goto_speed": GOTO_SPEED,
        })


@app.route("/scan", methods=["POST"])
@login_required
@pantilt_required
def scan():
    enabled = bool(request.json.get("enabled", False))
    _set_scan(enabled)
    return jsonify({"scan": scan_active})


@app.route("/move", methods=["POST"])
@login_required
@pantilt_required
def move():
    global scan_active
    scan_active = False          # any manual move cancels the scan
    _cancel_glide()              # ...and overrides a preset still travelling
    data      = request.json
    direction = data.get("direction")
    step      = clamp(int(data.get("step", DEFAULT_STEP)), 1, 90)
    with lock:
        if direction == "up":
            _apply(state["pan"], clamp(state["tilt"] + step, TILT_MIN, TILT_MAX))
        elif direction == "down":
            _apply(state["pan"], clamp(state["tilt"] - step, TILT_MIN, TILT_MAX))
        elif direction == "left":
            _apply(clamp(state["pan"] - step, PAN_MIN, PAN_MAX), state["tilt"])
        elif direction == "right":
            _apply(clamp(state["pan"] + step, PAN_MIN, PAN_MAX), state["tilt"])
        else:
            return jsonify({"error": "invalid direction"}), 400
        return jsonify(state)


@app.route("/goto", methods=["POST"])
@login_required
@pantilt_required
def goto():
    global scan_active
    scan_active = False
    data = request.json
    pan  = clamp(int(data.get("pan",  state["pan"])),  PAN_MIN,  PAN_MAX)
    tilt = clamp(int(data.get("tilt", state["tilt"])), TILT_MIN, TILT_MAX)
    gliding = _goto(pan, tilt)
    # The target, not the current angle: with easing on, the camera is still on
    # its way. Live position keeps arriving over SSE.
    return jsonify({"pan": pan, "tilt": tilt, "gliding": gliding})


@app.route("/home", methods=["POST"])
@login_required
@pantilt_required
def home():
    global scan_active
    scan_active = False
    gliding = _goto(_pan_start, _tilt_start)
    return jsonify({"pan": _pan_start, "tilt": _tilt_start, "gliding": gliding})


@app.route("/presets", methods=["GET"])
@login_required
def get_presets():
    with presets_lock:
        return jsonify(presets)


@app.route("/presets", methods=["POST"])
@login_required
@pantilt_required
def save_preset():
    name = request.json.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with lock:
        pos = {"pan": state["pan"], "tilt": state["tilt"]}
    with presets_lock:
        updated = {**presets, name: pos}
        save_presets_file(updated)        # only adopt it once it is on disk
        presets.clear()
        presets.update(updated)
        return jsonify(presets)


@app.route("/presets/<name>", methods=["DELETE"])
@login_required
def delete_preset(name):
    with presets_lock:
        updated = {k: v for k, v in presets.items() if k != name}
        save_presets_file(updated)
        presets.clear()
        presets.update(updated)
        return jsonify(presets)


@app.route("/snapshot")
@login_required
def snapshot():
    try:
        resp = requests.get("http://127.0.0.1:8081", stream=True, timeout=5)
        buf  = b""
        for chunk in resp.iter_content(chunk_size=4096):
            buf  += chunk
            start = buf.find(b"\xff\xd8")
            end   = buf.find(b"\xff\xd9", start + 2 if start != -1 else 0)
            if start != -1 and end != -1:
                jpeg  = buf[start:end + 2]
                fname = f"picam-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
                resp.close()
                return Response(
                    jpeg,
                    content_type="image/jpeg",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'},
                )
        resp.close()
        return Response("Could not capture frame", status=503)
    except Exception:
        return Response("Snapshot failed", status=503)


@app.route("/stream")
@login_required
def stream():
    try:
        upstream     = requests.get("http://127.0.0.1:8081", stream=True, timeout=5)
        content_type = upstream.headers.get(
            "content-type", "multipart/x-mixed-replace; boundary=BoundaryString"
        )

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=4096):
                    yield chunk
            except Exception:
                pass
            finally:
                upstream.close()

        return Response(generate(), content_type=content_type)
    except requests.exceptions.RequestException:
        return Response("Camera stream unavailable", status=503)


def _is_local():
    return request.remote_addr in ("127.0.0.1", "::1")


@app.route("/motion-event", methods=["POST"])
def motion_event():
    if not _is_local():
        return "", 403
    _notify_motion()
    return "", 204


@app.route("/media-saved", methods=["POST"])
def media_saved():
    """Webhook called by motion's on_picture_save / on_movie_end hooks.

    Hands the path to the uploader and returns immediately — motion runs these
    hooks from its capture loop, so this must never wait on the network.
    """
    if not _is_local():
        return "", 403
    path = request.form.get("path") or (request.get_json(silent=True) or {}).get("path", "")
    if not path:
        return "", 400
    media_uploader.enqueue(path)
    return "", 204


@app.route("/storage")
@login_required
def storage():
    return jsonify(media_uploader.status())


@app.route("/events")
@login_required
def events():
    def generate():
        q = queue.Queue(maxsize=10)
        with _sse_lock:
            _sse_clients.add(q)
        try:
            with lock:
                initial = f"data: {json.dumps({'pan': state['pan'], 'tilt': state['tilt']})}\n\n"
            yield initial
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield msg
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _sse_lock:
                _sse_clients.discard(q)

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _gallery_roots():
    """Named roots the gallery serves from, most transient first.

    'buffer' is the tmpfs holding captures not yet archived. 'archive' is the
    NFS share, and is absent whenever the uploader reports the mount as down —
    that check is what keeps a dead NAS from hanging a request thread.
    """
    roots = {"buffer": MOTION_TARGET_DIR}
    archive = media_uploader.archive_root()
    if archive is not None:
        roots["archive"] = archive
    return roots


def _entry(path, root, source):
    st = path.stat()
    return {
        "path":   f"{source}/{path.relative_to(root).as_posix()}",
        "name":   path.name,
        "kind":   uploader.kind_for(path),
        "source": source,
        "ts":     int(st.st_mtime),
        "size":   st.st_size,
    }


def _subdirs(path):
    """Immediate subdirectories, newest name first, tolerating an I/O error."""
    try:
        return sorted((d for d in path.iterdir() if d.is_dir()),
                      key=lambda d: d.name, reverse=True)
    except OSError:
        return []


def _sort_key(entry):
    """Newest first, then path, giving a total order with no ties."""
    return (-entry["ts"], entry["path"])


def _after_cursor(entry, cursor):
    """True when `entry` falls strictly after `cursor` in _sort_key order."""
    if cursor is None:
        return True
    ts, path = cursor
    return entry["ts"] < ts or (entry["ts"] == ts and entry["path"] > path)


def _archive_entries(root, limit, cursor=None):
    """Captures from the newest YYYY/MM/DD directories only.

    The archive grows forever, so a full recursive walk would get slower every
    day. Descending newest-first and stopping at `limit` keeps the cost flat.

    When paging, the cursor's date also lets whole day directories newer than it
    be skipped by name, without listing them. Without that, every page would
    re-list all the days it had already returned and paging deep into the
    archive would cost O(pages²) directory reads.
    """
    cutoff = None
    if cursor is not None:
        cutoff = tuple(datetime.fromtimestamp(cursor[0]).strftime("%Y/%m/%d").split("/"))

    out = []
    for year in _subdirs(root):
        if cutoff and (year.name,) > cutoff[:1]:
            continue
        for month in _subdirs(year):
            if cutoff and (year.name, month.name) > cutoff[:2]:
                continue
            for day in _subdirs(month):
                if cutoff and (year.name, month.name, day.name) > cutoff:
                    continue
                try:
                    found = [e for e in (_entry(f, root, "archive")
                                         for f in day.iterdir()
                                         if f.is_file() and uploader.kind_for(f))
                             if _after_cursor(e, cursor)]
                except OSError:
                    continue
                out.extend(found)
                if len(out) >= limit:
                    return out
    return out


@app.route("/gallery")
@login_required
def gallery_list():
    """One page of captures, newest first.

    Paging is by cursor, not offset: `before`/`before_path` carry the last entry
    of the previous page. Captures arrive and get deleted while someone browses,
    so an offset would silently skip or repeat rows as the list shifts under it.
    """
    limit = clamp(int(request.args.get("limit") or GALLERY_LIMIT), 1, GALLERY_LIMIT)

    cursor = None
    if request.args.get("before"):
        try:
            cursor = (int(request.args["before"]), request.args.get("before_path", ""))
        except ValueError:
            return jsonify({"error": "bad cursor"}), 400

    entries = []
    roots   = _gallery_roots()

    buffer_root = roots["buffer"]
    if buffer_root.exists():
        try:
            for f in buffer_root.rglob("*"):
                if f.is_file() and uploader.kind_for(f):
                    e = _entry(f, buffer_root, "buffer")
                    if _after_cursor(e, cursor):
                        entries.append(e)
        except OSError:
            app.logger.exception("buffer scan failed")

    if "archive" in roots:
        try:
            # One extra, so "is there another page" needs no second query
            entries.extend(_archive_entries(roots["archive"], limit + 1, cursor))
        except OSError:
            app.logger.exception("archive scan failed")

    entries.sort(key=_sort_key)
    page = entries[:limit]
    nxt  = None
    if len(entries) > limit and page:
        nxt = {"before": page[-1]["ts"], "before_path": page[-1]["path"]}
    return jsonify({"entries": page, "next": nxt})


def _prune_empty_dirs(start, stop):
    """Walk up from `start` removing empty directories, never past `stop`.

    Deleting the last capture of a day otherwise leaves an empty YYYY/MM/DD
    behind on the share forever. rmdir only succeeds on an empty directory, so
    this cannot take anything that still holds captures.
    """
    d = start
    while d != stop and d.is_relative_to(stop):
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def _resolve_capture(relpath):
    """Map a client-supplied "<source>/<relative path>" onto a real capture.

    Returns None for anything that is not a media file inside one of the gallery
    roots. Every route that touches a capture goes through here — fetch, delete
    and zip — so the containment check exists in exactly one place.
    """
    if not isinstance(relpath, str):
        return None
    source, _, rest = relpath.partition("/")
    root = _gallery_roots().get(source)
    if root is None or not rest:
        return None
    try:
        base   = root.resolve()
        target = (base / rest).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return None
    except OSError:
        return None
    if uploader.kind_for(target) is None:
        return None
    return target


@app.route("/gallery/<path:relpath>")
@login_required
def gallery_file(relpath):
    target = _resolve_capture(relpath)
    if target is None:
        return "", 404
    return send_file(target, conditional=True)


@app.route("/gallery-delete", methods=["POST"])
@login_required
def gallery_delete():
    """Delete one or more captures.

    Deliberately bulk-only: the preview's single delete posts a one-item list,
    so there is one path-resolution and one unlink to get right instead of two.
    """
    paths = (request.get_json(silent=True) or {}).get("paths")
    if not isinstance(paths, list) or not paths:
        return jsonify({"error": "paths required"}), 400
    if len(paths) > BULK_MAX:
        return jsonify({"error": f"at most {BULK_MAX} at a time"}), 400

    roots = _gallery_roots()
    deleted, failed = [], []
    for rel in paths:
        target = _resolve_capture(rel)
        if target is None:
            failed.append({"path": rel, "error": "not found"})
            continue
        try:
            target.unlink()
            deleted.append(rel)
        except OSError as e:
            failed.append({"path": rel, "error": e.strerror or "could not delete"})
            continue
        root = roots.get(rel.partition("/")[0])
        if root is not None:
            try:
                _prune_empty_dirs(target.parent, root.resolve())
            except OSError:
                pass

    if deleted:
        shown = ", ".join(deleted[:10]) + (" …" if len(deleted) > 10 else "")
        app.logger.info("gallery: deleted %d capture(s): %s", len(deleted), shown)
    if failed:
        app.logger.warning("gallery: %d deletion(s) refused", len(failed))
    return jsonify({"deleted": deleted, "failed": failed})


@app.route("/gallery-download", methods=["POST"])
@login_required
def gallery_download():
    """Bundle several captures into one zip.

    Driven by a form POST rather than fetch(), so the browser streams the result
    straight to disk instead of holding the whole bundle in page memory.
    ZIP_STORED because JPEG and MKV are already compressed — deflating them
    costs CPU on a Pi and saves almost nothing.
    """
    rels = request.form.getlist("path")
    if not rels:
        return "no paths given", 400
    if len(rels) > BULK_MAX:
        return f"at most {BULK_MAX} at a time", 400

    targets, total = [], 0
    for rel in rels:
        target = _resolve_capture(rel)
        if target is None:
            continue
        try:
            total += target.stat().st_size
        except OSError:
            continue
        # Bounded so a careless select-all cannot fill the SD card with a temp file
        if total > ZIP_MAX_BYTES:
            return "selection too large to bundle", 413
        targets.append((rel, target))

    if not targets:
        return "nothing to download", 404

    tmp = tempfile.NamedTemporaryFile(prefix="picam-", suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for rel, target in targets:
                # Keep the YYYY/MM/DD structure; drops the "<source>/" prefix
                z.write(target, arcname=rel.partition("/")[2] or target.name)
        tmp.flush()
        # Unlink while the handle is still open: the data stays readable until
        # send_file closes it, and nothing is left behind on any exit path.
        os.unlink(tmp.name)
        tmp.seek(0)
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name=f"picam-{stamp}.zip")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, threaded=True)
