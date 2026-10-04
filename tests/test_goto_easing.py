"""Eased absolute moves: a preset or Home must ramp, not slam the servos.

Run with tests/run.sh, or directly: python3 tests/test_goto_easing.py [speed]

A fake pantilthat module is injected before app.py is imported, so HARDWARE is
genuinely True and every servo write is recorded — the ramp is checked at the
hardware boundary, not just in app state.

Creates its own throwaway directory. Takes no path argument.
"""
import os, sys, time, types, shutil, tempfile, threading
from pathlib import Path

ROOT  = Path(__file__).resolve().parent.parent
SPEED = sys.argv[1] if len(sys.argv) > 1 else "450"
TMP   = Path(tempfile.mkdtemp(prefix="picam-test-"))
(TMP/"buffer").mkdir(parents=True)

# ── fake HAT, installed before app.py looks for one ─────────────────────────
writes = []
fake = types.ModuleType("pantilthat")
fake.idle_timeout = lambda *a, **k: None
fake.pan  = lambda a: writes.append(("pan", a))
fake.tilt = lambda a: writes.append(("tilt", a))
sys.modules["pantilthat"] = fake

os.environ.update(MOTION_TARGET_DIR=str(TMP/"buffer"), NFS_ENABLED="false",
                  AUTH_USER="", AUTH_PASS="", PANTILT_ENABLED="auto",
                  PRESETS_FILE=str(TMP/"p.json"), SECRET_KEY="t",
                  PAN_START="0", TILT_START="0", GOTO_SPEED=SPEED,
                  UPLOAD_SWEEP_INTERVAL="600")
sys.path.insert(0, str(ROOT))
import app as picam

fails = []
def check(l, ok, extra=""):
    print(f"{'PASS' if ok else 'FAIL'}  {l} {extra}")
    if not ok: fails.append(l)

c    = picam.app.test_client()
STEP = max(1, int(round(picam.GOTO_SPEED * picam.GLIDE_INTERVAL)))
print(f"--- GOTO_SPEED={picam.GOTO_SPEED} deg/s -> {STEP} deg per {picam.GLIDE_INTERVAL}s tick")

check("the fake HAT was detected", picam.HARDWARE is True)
check("startup position written to the servos", ("pan", 0) in writes)

# ── record every angle the app applies ──────────────────────────────────────
applied = []
real_apply = picam._apply
def spy(pan, tilt):
    applied.append((pan, tilt))
    return real_apply(pan, tilt)
picam._apply = spy

def settle(target, secs=6):
    end = time.time() + secs
    while time.time() < end:
        with picam.lock:
            if (picam.state["pan"], picam.state["tilt"]) == target: return True
        time.sleep(0.02)
    return False

# ── GOTO_SPEED=0: easing off, straight there, no thread ────────────────────
if picam.GOTO_SPEED == 0:
    applied.clear()
    r = c.post("/goto", json={"pan": 70, "tilt": 50}).get_json()
    check("reports no easing", r["gliding"] is False, f"{r}")
    check("arrives immediately", (picam.state["pan"], picam.state["tilt"]) == (70, 50),
          f"({picam.state['pan']},{picam.state['tilt']})")
    check("exactly one application", len(applied) == 1, f"({len(applied)})")
    check("no glide thread started",
          not any(t.name == "glide" and t.is_alive() for t in threading.enumerate()))
    h = c.post("/home").get_json()
    check("Home is instant too", h["gliding"] is False and
          (picam.state["pan"], picam.state["tilt"]) == (0, 0), f"{h}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
    sys.exit(1 if fails else 0)

# ── a preset jump ramps instead of jumping ─────────────────────────────────
applied.clear()
r = c.post("/goto", json={"pan": 80, "tilt": 60})
body = r.get_json()
check("/goto returns the target", (body["pan"], body["tilt"]) == (80, 60), f"{body}")
check("/goto reports it is easing", body["gliding"] is True)
check("/goto returns before arriving",
      (picam.state["pan"], picam.state["tilt"]) != (80, 60),
      f"(already at {picam.state['pan']},{picam.state['tilt']})")
check("arrives at the target", settle((80, 60)), f"(got {picam.state['pan']},{picam.state['tilt']})")
check("took more than one step", len(applied) > 1, f"({len(applied)} steps)")

deltas = [max(abs(b[0]-a[0]), abs(b[1]-a[1])) for a, b in zip(applied, applied[1:])]
check("no step exceeds the configured rate", deltas and max(deltas) <= STEP,
      f"(max {max(deltas) if deltas else 'n/a'} deg, limit {STEP})")
check("every step reached the servos",
      len([w for w in writes if w[0] == "pan"]) >= len(applied),
      f"({len(writes)} servo writes for {len(applied)} steps)")

# ── a second move supersedes the first; they must not fight ────────────────
c.post("/goto", json={"pan": -80, "tilt": -60})
time.sleep(0.05)
c.post("/goto", json={"pan": 10, "tilt": 10})
check("second target wins", settle((10, 10)), f"(got {picam.state['pan']},{picam.state['tilt']})")
time.sleep(0.2)
check("only one glide thread survives",
      sum(1 for t in threading.enumerate() if t.name == "glide" and t.is_alive()) <= 1)

# ── a manual nudge cancels a travelling preset ─────────────────────────────
c.post("/goto", json={"pan": 85, "tilt": 85})
time.sleep(0.06)
c.post("/move", json={"direction": "left", "step": 5})
time.sleep(0.3)
with picam.lock:
    frozen = (picam.state["pan"], picam.state["tilt"])
time.sleep(0.3)
with picam.lock:
    check("manual move stops the glide", (picam.state["pan"], picam.state["tilt"]) == frozen,
          f"(held at {frozen[0]},{frozen[1]})")

# ── auto-scan also takes over ──────────────────────────────────────────────
c.post("/goto", json={"pan": -85, "tilt": 0})
time.sleep(0.06)
c.post("/scan", json={"enabled": True})
time.sleep(0.2)
c.post("/scan", json={"enabled": False})
check("scan cancelled the glide", picam._glide_seq >= 4, f"(seq={picam._glide_seq})")

# ── never walks past a soft limit, and always terminates ───────────────────
picam._cancel_glide()
with picam.lock:
    real_apply(0, 0)
t = threading.Thread(target=picam._glide_worker, args=(9999, 9999, picam._glide_seq), daemon=True)
t.start(); t.join(timeout=5)
check("an out-of-range target terminates", not t.is_alive())
check("and stops at the soft limit",
      picam.state["pan"] == picam.PAN_MAX and picam.state["tilt"] == picam.TILT_MAX,
      f"({picam.state['pan']},{picam.state['tilt']} vs {picam.PAN_MAX},{picam.TILT_MAX})")

# ── /position advertises the setting ───────────────────────────────────────
check("/position reports goto_speed", c.get("/position").get_json()["goto_speed"] == picam.GOTO_SPEED)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
