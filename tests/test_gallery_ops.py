"""Gallery delete and zip download, including the path-traversal defences.

Run with tests/run.sh, or directly: python3 tests/test_gallery_ops.py

These suites create their own throwaway directory and delete it on the way in.
They deliberately take NO path argument: an earlier version accepted one, was
handed an empty string, resolved Path("") to "." and rmtree'd the repository.
Never reintroduce a caller-supplied path here.
"""
import os, sys, io, json, shutil, zipfile, time
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; TMP = Path(tempfile.mkdtemp(prefix="picam-test-"))
BUF = TMP/"buffer"; NAS = TMP/"nas"; ARCH = NAS/"picam/cam/2026/09/27"
shutil.rmtree(TMP, ignore_errors=True); BUF.mkdir(parents=True); ARCH.mkdir(parents=True)
(NAS/"secret.txt").write_bytes(b"do not touch")
(TMP/"outside.jpg").write_bytes(b"\xff\xd8outside")
os.environ.update(MOTION_TARGET_DIR=str(BUF), NFS_ENABLED="true", NFS_MOUNT=str(NAS),
                  NFS_SUBDIR="picam", CAMERA_NAME="cam", AUTH_USER="", AUTH_PASS="",
                  PANTILT_ENABLED="off", PRESETS_FILE=str(TMP/"p.json"), SECRET_KEY="t",
                  UPLOAD_SWEEP_INTERVAL="600")
sys.path.insert(0, str(ROOT))
import uploader
uploader.nfs_is_mounted = lambda m: True
uploader.ensure_mounted = lambda m: True
import app as picam

JPEG = b"\xff\xd8\xff\xe0" + b"\x00"*300 + b"\xff\xd9"
def seed():
    ARCH.mkdir(parents=True, exist_ok=True)   # pruning may have removed it
    (BUF/"pending.jpg").write_bytes(JPEG)
    (ARCH/"a.jpg").write_bytes(JPEG)
    (ARCH/"b.jpg").write_bytes(JPEG)
    (ARCH/"clip.mkv").write_bytes(b"\x1aE\xdf\xa3" + b"\x00"*80)
seed()
picam.media_uploader._sweep()
c = picam.app.test_client()
fails=[]
def check(l, ok, extra=""):
    print(f"{'PASS' if ok else 'FAIL'}  {l} {extra}")
    if not ok: fails.append(l)

def names(): return sorted(e["name"] for e in c.get("/gallery").get_json()["entries"])
check("gallery lists 4", names() == ["a.jpg","b.jpg","clip.mkv","pending.jpg"], f"{names()}")

# ── delete: refusals ─────────────────────────────────────────────────────────
for body, label in [
    ({}, "missing paths"),
    ({"paths": []}, "empty list"),
    ({"paths": "archive/2026/09/27/a.jpg"}, "string instead of list"),
]:
    check(f"delete rejects {label}", c.post("/gallery-delete", json=body).status_code == 400)
check("delete rejects over the cap",
      c.post("/gallery-delete", json={"paths": ["x"]*(picam.BULK_MAX+1)}).status_code == 400)

# ── delete: traversal must not escape ────────────────────────────────────────
evil = ["archive/../secret.txt", "archive/../../outside.jpg", "buffer/../../outside.jpg",
        "archive/2026/09/27/../../../../secret.txt", "nosuch/a.jpg", "archive", 12345]
r = c.post("/gallery-delete", json={"paths": evil})
check("traversal all rejected", r.status_code == 200 and r.get_json()["deleted"] == [],
      f"deleted={r.get_json()['deleted']}")
check("secret.txt survived", (NAS/"secret.txt").exists())
check("outside.jpg survived", (TMP/"outside.jpg").exists())
check("non-media path rejected", len(r.get_json()["failed"]) == len(evil))

# ── delete: the real thing ───────────────────────────────────────────────────
r = c.post("/gallery-delete", json={"paths": ["archive/2026/09/27/a.jpg"]})
check("single delete works", r.get_json()["deleted"] == ["archive/2026/09/27/a.jpg"])
check("file really gone", not (ARCH/"a.jpg").exists())
check("siblings untouched", (ARCH/"b.jpg").exists() and (ARCH/"clip.mkv").exists())

r = c.post("/gallery-delete", json={"paths": ["archive/2026/09/27/b.jpg", "buffer/pending.jpg"]})
d = r.get_json()
check("multi delete across sources", sorted(d["deleted"]) == ["archive/2026/09/27/b.jpg","buffer/pending.jpg"])
check("buffer file gone", not (BUF/"pending.jpg").exists())
check("partial failure reported", c.post("/gallery-delete",
      json={"paths": ["archive/2026/09/27/clip.mkv", "archive/2026/09/27/gone.jpg"]}
      ).get_json() == {"deleted": ["archive/2026/09/27/clip.mkv"],
                       "failed": [{"path": "archive/2026/09/27/gone.jpg", "error": "not found"}]})
check("gallery now empty", names() == [], f"{names()}")

# ── zip download ─────────────────────────────────────────────────────────────
seed(); picam.media_uploader._sweep()
r = c.post("/gallery-download", data={"path": ["archive/2026/09/27/a.jpg",
                                               "archive/2026/09/27/b.jpg",
                                               "buffer/pending.jpg"]})
check("zip returns 200", r.status_code == 200, f"({r.status_code})")
check("zip mimetype", r.mimetype == "application/zip", r.mimetype)
check("zip is an attachment", "attachment" in r.headers.get("Content-Disposition",""))
z = zipfile.ZipFile(io.BytesIO(r.get_data()))
check("zip has 3 members", len(z.namelist()) == 3, f"{z.namelist()}")
check("zip keeps date structure", "2026/09/27/a.jpg" in z.namelist(), f"{z.namelist()}")
check("zip content intact", z.read("2026/09/27/a.jpg") == JPEG)
check("zip is stored not deflated",
      all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
check("zip skips traversal entries",
      len(zipfile.ZipFile(io.BytesIO(c.post("/gallery-download", data={
          "path": ["archive/2026/09/27/a.jpg", "archive/../secret.txt"]
      }).get_data())).namelist()) == 1)
check("zip 400 with no paths", c.post("/gallery-download", data={}).status_code == 400)
check("zip 404 when nothing resolves",
      c.post("/gallery-download", data={"path": ["nosuch/x.jpg"]}).status_code == 404)
picam.ZIP_MAX_BYTES = 10
check("zip refuses an oversized selection",
      c.post("/gallery-download", data={"path": ["archive/2026/09/27/a.jpg",
                                                 "archive/2026/09/27/b.jpg"]}).status_code == 413)
picam.ZIP_MAX_BYTES = 512*1024*1024
import tempfile as _tf
_leftover = [f for f in os.listdir(_tf.gettempdir())
             if f.startswith("picam-") and f.endswith(".zip")]
check("no temp files left behind", not _leftover, f"{_leftover}")

# ── uploader tolerates a capture deleted from under it ───────────────────────
p = BUF/"race.jpg"; p.write_bytes(JPEG)
t = time.time()-60; os.utime(p,(t,t))
picam.media_uploader._pending.clear()
p.unlink()
try:
    picam.media_uploader._upload(p)
    check("uploader survives a vanished source", True)
except Exception as e:
    check("uploader survives a vanished source", False, f"({type(e).__name__}: {e})")
check("no spurious failure recorded", picam.media_uploader.status()["failed"] == 0)

# ── empty date dirs are pruned, the camera root is not ──────────────────────
seed(); picam.media_uploader._sweep()
day = ARCH
r = c.post("/gallery-delete", json={"paths": [
    "archive/2026/09/27/a.jpg", "archive/2026/09/27/b.jpg", "archive/2026/09/27/clip.mkv"]})
check("all three archive files deleted", len(r.get_json()["deleted"]) == 3)
check("empty day dir pruned", not day.exists(), f"({day} still there)" if day.exists() else "")
check("empty month+year pruned too", not (NAS/"picam/cam/2026").exists())
check("camera root kept", (NAS/"picam/cam").exists())
check("buffer file untouched", (BUF/"pending.jpg").exists())

# a day dir that still holds a capture must survive
ARCH.mkdir(parents=True, exist_ok=True)
(ARCH/"keep.jpg").write_bytes(JPEG); (ARCH/"go.jpg").write_bytes(JPEG)
picam.media_uploader._sweep()
c.post("/gallery-delete", json={"paths": ["archive/2026/09/27/go.jpg"]})
check("day dir with survivors kept", ARCH.exists() and (ARCH/"keep.jpg").exists())

# ── deletions are actually logged now ───────────────────────────────────────
import logging, io
buf = io.StringIO()
h = logging.StreamHandler(buf); h.setLevel(logging.INFO)
picam.app.logger.addHandler(h)
c.post("/gallery-delete", json={"paths": ["archive/2026/09/27/keep.jpg"]})
picam.app.logger.removeHandler(h)
logged = buf.getvalue()
check("deletion is logged at INFO", "deleted 1 capture" in logged, repr(logged.strip()[:80]))
check("logged line names the path", "keep.jpg" in logged)
check("app.logger level is INFO or lower", picam.app.logger.level <= logging.INFO,
      f"(level={logging.getLevelName(picam.app.logger.level)})")

print(f"\n{len(fails)} failure(s)" + (": "+", ".join(fails) if fails else ""))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
