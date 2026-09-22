import os
import math
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
GALLERY_LIMIT     = max(1, int(os.environ.get("GALLERY_LIMIT", 200)))

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
    with _scan_lock:
        scan_active = enabled
        if enabled and (_scan_thread is None or not _scan_thread.is_alive()):
            _scan_thread = threading.Thread(target=_scan_worker, daemon=True)
            _scan_thread.start()

# ── Presets ────────────────────────────────────────────────────────────────────

presets_lock = threading.Lock()


def load_presets():
    if PRESETS_FILE.exists():
        try:
            return json.loads(PRESETS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_presets_file(data):
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(data, indent=2))


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
    return render_template("login.html", error=error)


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
    return render_template("index.html", pantilt=_pantilt_state())


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
    with lock:
        _apply(pan, tilt)
        return jsonify(state)


@app.route("/home", methods=["POST"])
@login_required
@pantilt_required
def home():
    global scan_active
    scan_active = False
    with lock:
        _apply(_pan_start, _tilt_start)
        return jsonify(state)


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
        presets[name] = pos
        save_presets_file(presets)
    return jsonify(presets)


@app.route("/presets/<name>", methods=["DELETE"])
@login_required
def delete_preset(name):
    with presets_lock:
        presets.pop(name, None)
        save_presets_file(presets)
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


def _archive_entries(root, limit):
    """Captures from the newest YYYY/MM/DD directories only.

    The archive grows forever, so a full recursive walk would get slower every
    day. Descending newest-first and stopping at `limit` keeps the cost flat.
    """
    out = []
    for year in _subdirs(root):
        for month in _subdirs(year):
            for day in _subdirs(month):
                try:
                    found = [_entry(f, root, "archive")
                             for f in day.iterdir()
                             if f.is_file() and uploader.kind_for(f)]
                except OSError:
                    continue
                out.extend(found)
                if len(out) >= limit:
                    return out
    return out


@app.route("/gallery")
@login_required
def gallery_list():
    entries = []
    roots   = _gallery_roots()

    buffer_root = roots["buffer"]
    if buffer_root.exists():
        try:
            for f in buffer_root.rglob("*"):
                if f.is_file() and uploader.kind_for(f):
                    entries.append(_entry(f, buffer_root, "buffer"))
        except OSError:
            app.logger.exception("buffer scan failed")

    if "archive" in roots:
        try:
            entries.extend(_archive_entries(roots["archive"], GALLERY_LIMIT))
        except OSError:
            app.logger.exception("archive scan failed")

    entries.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify(entries[:GALLERY_LIMIT])


@app.route("/gallery/<path:relpath>")
@login_required
def gallery_file(relpath):
    source, _, rest = relpath.partition("/")
    root = _gallery_roots().get(source)
    if root is None or not rest:
        return "", 404

    # Resolve then confirm containment — `rest` comes straight from the client.
    try:
        base   = root.resolve()
        target = (base / rest).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return "", 404
    except OSError:
        return "", 404

    if uploader.kind_for(target) is None:
        return "", 404
    return send_file(target, conditional=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, threaded=True)
