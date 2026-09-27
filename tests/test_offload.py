"""Both NFS offload paths: uploading when enabled, buffer-watch when not.

Run with tests/run.sh, or directly: python3 tests/test_offload.py

These suites create their own throwaway directory and delete it on the way in.
They deliberately take NO path argument: an earlier version accepted one, was
handed an empty string, resolved Path("") to "." and rmtree'd the repository.
Never reintroduce a caller-supplied path here.
"""
import os, sys, time, shutil
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
MODE = (sys.argv[1] if len(sys.argv) > 1 else "on")                     # "on" | "off"
TMP = Path(tempfile.mkdtemp(prefix="picam-test-")); BUF = TMP/"buffer"; NAS = TMP/"nas"
shutil.rmtree(TMP, ignore_errors=True); BUF.mkdir(parents=True); NAS.mkdir(parents=True)
os.environ.update(MOTION_TARGET_DIR=str(BUF), NFS_ENABLED=("true" if MODE=="on" else "false"),
                  NFS_MOUNT=str(NAS), NFS_SUBDIR="picam", CAMERA_NAME="cam",
                  BUFFER_HIGH_WATER="80", UPLOAD_STABLE_AGE="1",
                  UPLOAD_RETRY_MIN="1", UPLOAD_SWEEP_INTERVAL="5")
sys.path.insert(0, str(ROOT))
import uploader

fails=[]
def check(l, ok, extra=""):
    print(f"{'PASS' if ok else 'FAIL'}  {l} {extra}")
    if not ok: fails.append(l)
def mk(n, size=1024, age=0):
    p = BUF/n; p.write_bytes(b"\xff\xd8"+os.urandom(size))
    if age: t=time.time()-age; os.utime(p,(t,t))
    return p
def wait(pred, secs=14):
    end=time.time()+secs
    while time.time()<end:
        if pred(): return True
        time.sleep(0.2)
    return False
def dest_for(p, name=None):
    return NAS/"picam/cam"/time.strftime("%Y/%m/%d", time.localtime(p.stat().st_mtime))/(name or p.name)

touches = {"n": 0}
real_listdir = os.listdir
def counting_listdir(p):
    if str(p) == str(NAS): touches["n"] += 1
    return real_listdir(p)
uploader.os.listdir = counting_listdir
mounted=[True]
uploader.nfs_is_mounted = lambda m: mounted[0]

u = uploader.Uploader(); u.start()
check("worker runs", u._thread.is_alive())

if MODE == "off":
    for i in range(10):
        p = BUF/f"c{i:02d}.jpg"; p.write_bytes(os.urandom(9000))
        t=time.time()-(100-i*5); os.utime(p,(t,t))
    TOTAL=100_000
    uploader._space = lambda path: ({"total":TOTAL,"free":TOTAL-sum(f.stat().st_size for f in BUF.rglob("*") if f.is_file()),
        "used_pct":round(sum(f.stat().st_size for f in BUF.rglob("*") if f.is_file())/TOTAL*100,1)}
        if Path(path)==BUF else {"total":None,"free":None,"used_pct":None})
    time.sleep(7)
    s=u.status()
    check("buffer status reported", s["buffer"]["total"]==TOTAL and s["buffer"]["used_pct"] is not None,
          f"({s['buffer']['used_pct']}%)")
    check("shedding ran", s["dropped"]>0, f"(dropped={s['dropped']})")
    check("archive never probed", touches["n"]==0, f"(touches={touches['n']})")
    check("enabled false", s["enabled"] is False)
    check("no phantom error", s["last_error"] is None)
    check("archive_root None", u.archive_root() is None)
else:
    p = mk("pic-01.jpg"); d = dest_for(p); u.enqueue(p)
    check("hook upload reaches archive", wait(lambda: d.exists()))
    check("buffer cleared", not p.exists())
    check("no .part left", not list(NAS.rglob("*.part")))
    o = mk("pic-02.jpg", age=5); od = dest_for(o)
    check("sweep rescues un-hooked file", wait(lambda: od.exists()))
    live = mk("mov.mkv"); time.sleep(3)
    check("in-progress file skipped", live.exists() and not list(NAS.rglob("mov.mkv")))
    mounted[0]=False
    held = mk("pic-03.jpg", age=5); hd = dest_for(held); u.enqueue(held); time.sleep(2)
    check("held while down", held.exists())
    check("failure recorded", bool(u.status()["last_error"]))
    check("ensure_mounted attempted a trigger", touches["n"]>0, f"(touches={touches['n']})")
    mounted[0]=True
    check("uploads after recovery", wait(lambda: hd.exists(), 16))
    dup = mk("pic-01.jpg", size=2048, age=5); dd = dest_for(dup,"pic-01-1.jpg")
    check("collision suffixed", wait(lambda: dd.exists()))
    check("enqueue rejects outside buffer", u.enqueue(TMP/"x.jpg") is False)

print(f"\n{len(fails)} failure(s)" + (": "+", ".join(fails) if fails else ""))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
