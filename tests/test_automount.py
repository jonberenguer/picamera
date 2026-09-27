"""ensure_mounted() must trigger an autofs mount, not merely observe /proc/mounts.

Run with tests/run.sh, or directly: python3 tests/test_automount.py

These suites create their own throwaway directory and delete it on the way in.
They deliberately take NO path argument: an earlier version accepted one, was
handed an empty string, resolved Path("") to "." and rmtree'd the repository.
Never reintroduce a caller-supplied path here.
"""
import os, sys, shutil
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; TMP = Path(tempfile.mkdtemp(prefix="picam-test-"))
shutil.rmtree(TMP, ignore_errors=True); (TMP/"buf").mkdir(parents=True); (TMP/"nas").mkdir()
os.environ.update(MOTION_TARGET_DIR=str(TMP/"buf"), NFS_ENABLED="true",
                  NFS_MOUNT=str(TMP/"nas"), NFS_SUBDIR="picam", CAMERA_NAME="cam",
                  UPLOAD_STABLE_AGE="1", UPLOAD_RETRY_MIN="1", UPLOAD_SWEEP_INTERVAL="5")
sys.path.insert(0, str(ROOT))
import uploader

fails=[]
def check(l, ok, extra=""):
    print(f"{'PASS' if ok else 'FAIL'}  {l} {extra}")
    if not ok: fails.append(l)

# Model autofs: /proc/mounts shows nfs ONLY after the path has been touched.
state = {"mounted": False, "touched": 0, "listdir": 0}
def fake_is_mounted(m):
    return state["mounted"]
real_listdir = os.listdir
def fake_listdir(p):
    if str(p) == str(TMP/"nas"):
        state["listdir"] += 1
        state["mounted"] = True          # the automount fires
    return real_listdir(p)
uploader.nfs_is_mounted = fake_is_mounted
uploader.os.listdir = fake_listdir

# 1. idle automount: observing alone would fail, touching must fix it
state["mounted"] = False
check("returns True by triggering the mount", uploader.ensure_mounted(TMP/"nas") is True)
check("it actually touched the path", state["listdir"] == 1, f"(listdir calls={state['listdir']})")

# 2. already mounted: must NOT touch the path needlessly
before = state["listdir"]
check("already mounted -> True", uploader.ensure_mounted(TMP/"nas") is True)
check("no needless touch when mounted", state["listdir"] == before)

# 3. dead server: touch raises, stays unmounted, no exception escapes
state["mounted"] = False
def exploding_listdir(p):
    state["listdir"] += 1
    raise OSError("Stale file handle")
uploader.os.listdir = exploding_listdir
check("dead server -> False, no raise", uploader.ensure_mounted(TMP/"nas") is False)
uploader.os.listdir = fake_listdir

# 4. the worker recovers on its own from an idle automount
state["mounted"] = False; state["listdir"] = 0
u = uploader.Uploader(); u.start()
p = TMP/"buf"/"cap.jpg"; p.write_bytes(b"\xff\xd8" + os.urandom(2048))
import time
t = time.time() - 5; os.utime(p, (t, t))
dest = TMP/"nas"/"picam/cam"/time.strftime("%Y/%m/%d", time.localtime(p.stat().st_mtime))/"cap.jpg"
ok = False
for _ in range(80):
    if dest.exists(): ok = True; break
    time.sleep(0.25)
check("worker mounts and uploads with no outside help", ok)
check("buffer cleared", not p.exists())
s = u.status()
check("status reports mounted", s["mounted"] is True)
check("no failure recorded", s["failed"] == 0, f"(failed={s['failed']}, err={s['last_error']})")

print(f"\n{len(fails)} failure(s)" + (": "+", ".join(fails) if fails else ""))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
