"""Gallery cursor pagination: every capture exactly once, in order, no drift.

Run with tests/run.sh, or directly: python3 tests/test_gallery_paging.py

Creates its own throwaway directory. Takes NO path argument — see the note in
tests/test_gallery_ops.py for why.
"""
import os, sys, time, shutil, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP  = Path(tempfile.mkdtemp(prefix="picam-test-"))
BUF, NAS = TMP/"buffer", TMP/"nas"
shutil.rmtree(TMP, ignore_errors=True); BUF.mkdir(parents=True); NAS.mkdir(parents=True)
os.environ.update(MOTION_TARGET_DIR=str(BUF), NFS_ENABLED="true", NFS_MOUNT=str(NAS),
                  NFS_SUBDIR="picam", CAMERA_NAME="cam", AUTH_USER="", AUTH_PASS="",
                  PANTILT_ENABLED="off", PRESETS_FILE=str(TMP/"p.json"), SECRET_KEY="t",
                  GALLERY_LIMIT="100", UPLOAD_SWEEP_INTERVAL="600")
sys.path.insert(0, str(ROOT))
import uploader
uploader.nfs_is_mounted = lambda m: True
uploader.ensure_mounted = lambda m: True
import app as picam

JPEG = b"\xff\xd8\xff\xe0" + b"\x00"*200 + b"\xff\xd9"
CAM  = NAS/"picam/cam"
fails = []
def check(l, ok, extra=""):
    print(f"{'PASS' if ok else 'FAIL'}  {l} {extra}")
    if not ok: fails.append(l)

# 40 archived captures spread over 4 days, plus 3 still in the buffer
made = []
base = time.time() - 86400*10
for d in range(4):
    day = base + d*86400
    stamp = time.localtime(day)
    adir = CAM/time.strftime("%Y/%m/%d", stamp)
    adir.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        f = adir/f"a{d}{i:02d}.jpg"
        f.write_bytes(JPEG)
        t = day + i*60
        os.utime(f, (t, t))
        made.append(f"archive/{time.strftime('%Y/%m/%d', stamp)}/{f.name}")
for i in range(3):
    f = BUF/f"b{i}.jpg"; f.write_bytes(JPEG)
    t = time.time() - i
    os.utime(f, (t, t))
    made.append(f"buffer/{f.name}")
picam.media_uploader._sweep()

c = picam.app.test_client()

def page(limit, cur=None):
    q = f"/gallery?limit={limit}"
    if cur: q += f"&before={cur['before']}&before_path={cur['before_path']}"
    return c.get(q).get_json()

# ── shape ────────────────────────────────────────────────────────────────────
p1 = page(10)
check("returns an object with entries+next", set(p1) == {"entries", "next"}, f"{sorted(p1)}")
check("first page is full", len(p1["entries"]) == 10, f"({len(p1['entries'])})")
check("next cursor present", p1["next"] is not None)
check("cursor matches the last entry",
      p1["next"] == {"before": p1["entries"][-1]["ts"], "before_path": p1["entries"][-1]["path"]})

# ── walk every page ──────────────────────────────────────────────────────────
seen, cur, pages = [], None, 0
while True:
    pg = page(10, cur)
    seen += [e["path"] for e in pg["entries"]]
    pages += 1
    cur = pg["next"]
    if not cur or pages > 20: break
check("walked several pages", pages == 5, f"({pages} pages)")
check("every capture returned exactly once", sorted(seen) == sorted(made),
      f"got {len(seen)} of {len(made)}, dupes={len(seen)-len(set(seen))}")
check("no duplicates across pages", len(seen) == len(set(seen)))
check("newest first overall", seen == [p for p in sorted(
          made, key=lambda q: next((-e["ts"], e["path"]) for e in [
              {"ts": int(os.stat(
                  (BUF/q.split('/',1)[1]) if q.startswith("buffer/") else (CAM/q.split('/',1)[1])
              ).st_mtime), "path": q}]))])
check("last page has no next", cur is None)

# ── buffer entries lead, since they are newest ───────────────────────────────
check("buffer captures come first", all(e["source"] == "buffer" for e in page(3)["entries"]))

# ── same-second captures must not be skipped or repeated ─────────────────────
tie = CAM/"2026/01/01"; tie.mkdir(parents=True, exist_ok=True)
same = time.time() - 86400*40
for n in "xyz":
    f = tie/f"tie-{n}.jpg"; f.write_bytes(JPEG); os.utime(f, (same, same))
picam.media_uploader._sweep()
ties, cur = [], None
while True:
    pg = page(1, cur)
    ties += [e["path"] for e in pg["entries"] if "tie-" in e["path"]]
    cur = pg["next"]
    if not cur: break
check("identical timestamps paged exactly once", sorted(ties) == sorted(
      [f"archive/2026/01/01/tie-{n}.jpg" for n in "xyz"]), f"{ties}")

# ── cursor validation and limits ─────────────────────────────────────────────
check("bad cursor is a 400", c.get("/gallery?before=notanumber").status_code == 400)
check("limit is capped at GALLERY_LIMIT",
      len(c.get("/gallery?limit=9999").get_json()["entries"]) <= picam.GALLERY_LIMIT)
check("limit=0 falls back, not an error", c.get("/gallery?limit=0").status_code == 200)
check("a cursor past the end returns nothing",
      page(10, {"before": 1, "before_path": ""})["entries"] == [])

# ── a day dir newer than the cursor is skipped, not re-listed ────────────────
listed = []
real_iterdir = Path.iterdir
Path.iterdir = lambda self: (listed.append(str(self)), real_iterdir(self))[1]
page(10, {"before": int(base + 60), "before_path": ""})   # cursor on the oldest day
Path.iterdir = real_iterdir
newest = time.strftime("%Y/%m/%d", time.localtime(base + 86400*3))
check("skips day directories newer than the cursor",
      not any(newest in p for p in listed), f"listed {len([p for p in listed if newest in p])} newer dirs")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
