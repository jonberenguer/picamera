# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

PiCam Controller — a Raspberry Pi camera service with a dark web UI for live MJPEG
streaming, pan/tilt control over I2C, and motion-detection alerts. Target hardware is a
Pi running Raspberry Pi OS Bookworm with a libcamera-compatible camera and a Pimoroni
Pan-Tilt HAT.

The README notes the original code was AI-generated and is kept for review; treat it as a
real project, not a scratch pad.

## Architecture

Three systemd services on the Pi:

| Service | Role |
|---|---|
| `picam-motion` | `libcamerify motion` — owns the camera, serves raw MJPEG on `:8081` |
| `picam-flask` | `app.py` — UI, stream proxy, servo control, SSE, auth on `127.0.0.1:8080` |
| `caddy` | HTTPS on `:443` (`tls internal`), `:80` → redirect, proxies to Flask |

Only Caddy is network-exposed. Flask and motion bind to localhost.

Data flow:
- Browser → Caddy → Flask `/stream` → `requests` streaming GET of `http://127.0.0.1:8081`
  → chunks relayed to the client. Single origin, so no CORS.
- `motion` fires `on_motion_detected` → `curl POST 127.0.0.1:8080/motion-event` → Flask
  pushes an SSE `event: motion` to every connected browser.
- Every servo move calls `_apply()`, which writes `state`, drives the HAT, and pushes an
  SSE `data:` frame with the new pan/tilt. The UI never polls position.
- `motion` saves captures into `MOTION_TARGET_DIR`, which is a **tmpfs** (RAM). On each
  closed file it fires `on_picture_save` / `on_movie_end` → `POST /media-saved` →
  `uploader.py` copies the file to the NFS archive and deletes the buffered copy.

## Files

- `app.py` — the backend. No blueprints, no package layout.
- `uploader.py` — the only other Python module. Owns the tmpfs→NFS offload: the worker
  thread, the sweep, high-water shedding, and the cached storage status. It exists
  separately because everything in it must stay off the request path.
- `templates/index.html` — the entire frontend: markup, CSS, and JS in one ~1100-line
  file. There is no build step and no framework. Keep it that way unless asked.
- `templates/login.html` — login form, same self-contained style.
- `static/sw.js` — service worker. `BYPASS` lists every dynamic route; **any new route
  that returns live data must be added there**, or the SW will cache it.
- `static/manifest.json`, `icon.svg`, `favicon.svg` — PWA assets.
- `motion.env` — the single source of truth for all tunables (pan/tilt, auth, camera, NFS).
- `requirements.txt` — core deps only. `pantilthat` is deliberately absent: it is useless
  without the HAT, and `install.sh` pip-installs it unless `PANTILT_ENABLED=off`.
- `motion-stream-only.env` — a complete swappable alternative to `motion.env` (live stream,
  nothing saved). Any variant must carry **every** section: `install.sh` reads only
  `motion.env`, and a fragment missing `AUTH_USER`/`AUTH_PASS` silently disables the login.
  Keep it byte-identical to `motion.env` apart from the capture settings, so a diff of the
  two is self-documenting.
- `install.sh` — root installer, with `--dry-run` (no root needed) and `--uninstall`.
  Parses `motion.env`, rewrites `/etc/motion/motion.conf`, writes `/etc/picam.env`,
  generates the mount units, copies to `/opt/picam`, builds the venv, restarts everything.
  **Every mutating command goes through `run`, `run_ok`, `write_file` or `append_line`** so
  that `--dry-run` stays a property of the script rather than something each step
  remembers — adding a bare `cp`/`sed -i`/`cat >` silently breaks it.
  None of those helpers may *end* on a bare `[[ test ]] && cmd`: a false test becomes the
  function's non-zero return, and `set -e` then kills the script at the call site. A
  `--dry-run` pass cannot catch it, because the dry-run branch returns 0 — this shipped
  once and only failed on a real install.
  Each run writes `/etc/picam.manifest` and removes anything the previous manifest lists
  that the new one does not; that is what makes a changed `MOTION_TARGET_DIR` or
  `NFS_ENABLED=false` take effect instead of orphaning a mounted unit.
- `picam-*.service`, `Caddyfile` — copied verbatim to `/etc/systemd/system` and `/etc/caddy`.

## Media offload

Two rules govern `uploader.py`, and breaking either one is how this goes wrong:

1. **Nothing touches the NFS mount on a request thread.** A wedged NAS must not hang the
   UI. The worker owns every blocking call; `/storage` returns a cached dict, and the
   gallery only walks the archive when `media_uploader.archive_root()` says the mount is
   healthy. `nfs_is_mounted()` reads `/proc/mounts` (procfs, cannot block) and requires a
   real `nfs*` filesystem — `os.path.ismount()` would report an idle automount stub as
   mounted, and a bare existence check would silently write captures into the empty local
   mountpoint on the SD card.
2. **The buffer directory is the queue.** The in-memory `queue.Queue` is only a fast path;
   the periodic sweep rescans the buffer, so a Flask restart, a missed hook or a failed
   upload all recover on their own. There is deliberately no persisted work list.

Because the buffer is RAM, a long outage would otherwise take the Pi down: past
`BUFFER_HIGH_WATER` the sweep deletes the oldest unarchived captures, logs a warning, and
counts them in `/storage` as `dropped`. Uploads go to a `.part` file then `os.replace()`,
so the share never shows a half-written capture. The sweep skips files touched within
`UPLOAD_STABLE_AGE` seconds, which is what keeps an in-progress movie from being copied.

**The worker runs even when `NFS_ENABLED` is false.** The sweep is the only thing that
reports buffer usage and the only thing that sheds, so gating the worker on `NFS_ENABLED`
would leave the tmpfs unprotected exactly while the share is still being built. Only the
upload is gated — `enqueue()` refuses, and `_sweep()` skips both the mount probe and the
enqueue pass.

## Conventions

- **Config is environment variables, read once at import time** into module-level
  constants (`PAN_MIN`, `SCAN_SPEED`, `MOTION_TARGET_DIR`, …). Add new settings the same
  way: `os.environ.get` with a default in `app.py`, an entry in `motion.env`, and — if
  Flask needs it — add the key to the `grep -E` allowlist in `install.sh` that builds
  `/etc/picam.env`. Forgetting that `FLASK_VARS` line is the most common way a new setting
  silently does nothing on the Pi.
- **Motion settings** are named `MOTION_<UPPERCASE_CONF_KEY>` and must also be listed in
  `CONFIG_VARS` in `install.sh` to be written into `motion.conf`.
- Every user-facing route is decorated `@login_required`. Exceptions are deliberate:
  `/login`, `/logout`, `/manifest.json`, `/sw.js` (the browser fetches these before a
  session exists) and `/motion-event` (localhost-only, checked via `request.remote_addr`).
- Servo state is guarded by `lock`; `_apply()` must only be called while holding it.
  Presets use a separate `presets_lock`. SSE client set uses `_sse_lock`.
- All angles are clamped through `clamp()` against the soft limits before reaching the
  hardware. Never write to `pantilthat` outside `_apply()`.
- **The HAT is optional and that is a supported mode, not a degraded one.** `HARDWARE` is
  `False` when `PANTILT_ENABLED=off` or the HAT does not answer; the app then runs as a
  fixed camera. Any new movement route needs `@pantilt_required` (returns `409`) — never
  report a success that did not reach hardware. Anything that spawns a thread to drive the
  servos must check `HARDWARE` first, the way `_set_scan()` does.
- `index()` passes `pantilt=_pantilt_state()` into the template, so the UI decides at
  render time: `<body class="no-pantilt">` hides the control surface on first paint and
  `panTiltAvailable` is seeded from Jinja. The nodes are left in the DOM until `init()`
  removes them, because the script does `getElementById(...).addEventListener(...)` at
  parse time and would throw on a missing element. Keep that ordering if you add controls.
- Filenames from the client are always reduced with `Path(filename).name` and
  extension-checked before touching disk (see `gallery_file`). Keep that pattern.
- The section-banner comments (`# ── Name ───`) in `app.py` and the CSS comment blocks in
  `index.html` are the house style; match them.

## Running and testing

There are no tests and no linter config. On a dev machine:

```bash
python3 -m venv venv && ./venv/bin/pip install flask requests
PAN_START=0 TILT_START=0 ./venv/bin/python3 app.py   # HARDWARE=False, stream 503s
```

`/stream` and `/snapshot` need `motion` on `:8081`; without it they correctly return 503.

On the Pi, after changing any file:

```bash
sudo ./install.sh          # re-copies everything and restarts the services
sudo journalctl -u picam-flask -f
```

Note: `install.sh` copies files into `/opt/picam` — editing the repo alone changes nothing
on a running Pi.

## Gotchas

- `SESSION_COOKIE_SECURE=True` means auth only works over HTTPS. Plain-HTTP access to
  `:8080` will loop on the login page.
- `scan_active` is written from route handlers without holding `lock` (the scan worker
  reads it); it is a deliberate simple flag, not an oversight to "fix" with a lock that
  the worker also holds.
- The SSE queues are `maxsize=10`; a client that stalls gets dropped from `_sse_clients`
  rather than blocking `_push_sse`.
- `/gallery` returns objects, not filenames: `{path, name, kind, source, ts, size}` where
  `path` is `"<source>/<relative path>"` and source is `buffer` or `archive`. The archive
  walk descends newest-first through `YYYY/MM/DD` and stops at `GALLERY_LIMIT` rather than
  doing a full recursive walk, which would get slower every day.
- `MOTION_TARGET_DIR` is a tmpfs mount unit generated by `install.sh` via `systemd-escape`.
  Anything still in it at reboot is gone. `MOTION_MOVIE_OUTPUT` is still `off` by default;
  the gallery and uploader already handle video, so turning it on is all that is needed.
- The NFS mount is an `.automount` unit, so a dead NAS never holds up boot and no service
  declares `RequiresMountsFor=` on it. Only the tmpfs gets that, via a drop-in.
- `motion.env` currently ships real default credentials (`picamera`/`picamera`). Do not
  copy them into examples or docs as if they were safe.
- The repo `.gitignore` is empty; `presets.json` is only ever created under `/opt/picam`.
