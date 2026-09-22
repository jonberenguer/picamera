#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/picam
MANIFEST=/etc/picam.manifest
MOTION_CONF=/etc/motion/motion.conf
CADDY_CONF=/etc/caddy/Caddyfile
NFS_CONFIGURED=false

DRY_RUN=false
UNINSTALL=false
ASSUME_YES=false

usage() {
    cat <<USAGE
Usage: sudo ./install.sh [options]

  --dry-run     Print every change without making one. Does not need root, so
                this is the way to review a run before it touches the Pi.
  --uninstall   Remove what a previous install put on this system, using the
                manifest at ${MANIFEST}.
  -y, --yes     Skip the uninstall confirmation prompt.
  -h, --help    This text.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)   DRY_RUN=true ;;
        --uninstall) UNINSTALL=true ;;
        -y|--yes)    ASSUME_YES=true ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

# ── Execution helpers ──────────────────────────────────────────────────────────
# Every mutating command goes through one of these, so --dry-run is a property of
# the script rather than something each step has to remember.

say() { printf '%s\n' "$*"; }

run() {
    if $DRY_RUN; then printf '       $ %s\n' "$*"; else "$@"; fi
}

# For commands whose failure is expected and harmless (disabling a unit that is
# already gone, stopping something that never started).
run_ok() {
    if $DRY_RUN; then printf '       $ %s\n' "$*"; else "$@" || true; fi
}

# Reads the file body on stdin: `write_file /etc/foo 0600 <<EOF ... EOF`
write_file() {
    local dest="$1" mode="${2:-}"
    if $DRY_RUN; then
        printf '       write %s%s\n' "$dest" "${mode:+ (mode ${mode})}"
        sed 's/^/         | /'
    else
        cat > "$dest"
        [[ -n "$mode" ]] && chmod "$mode" "$dest"
    fi
}

append_line() {   # append_line <line> <file> — only if not already present
    local line="$1" file="$2"
    if [[ -f "$file" ]] && grep -qxF "$line" "$file"; then return 0; fi
    if $DRY_RUN; then
        printf '       append to %s: %s\n' "$file" "$line"
    else
        echo "$line" >> "$file"
    fi
}

# Keep one pristine copy of any config we edit in place, so --uninstall can put
# it back. Only ever taken once; a re-run must not snapshot our own edits.
backup_once() {
    local file="$1" backup="$1.picam-orig"
    [[ -f "$file" ]] || return 0
    [[ -f "$backup" ]] && return 0
    say "    backing up $(basename "$file") -> $(basename "$backup")"
    run cp -a "$file" "$backup"
}

# ── Manifest ───────────────────────────────────────────────────────────────────
# Records what this run installed. On the next run anything the old manifest
# lists but the new one does not is removed — that is what makes changing
# MOTION_TARGET_DIR or turning NFS off actually take effect instead of leaving
# an orphaned unit mounted behind your back.

MANIFEST_ENTRIES=()
record() { MANIFEST_ENTRIES+=("$1"); }

remove_entry() {
    local entry="$1" kind="${entry%%:*}" val="${entry#*:}"
    case "$kind" in
        unit)
            say "    removing unit ${val}"
            run_ok systemctl disable --now "$val"
            run rm -f "/etc/systemd/system/${val}"
            ;;
        dropin|file) say "    removing ${val}"; run rm -f "$val" ;;
        dir)         say "    removing ${val}"; run rm -rf "$val" ;;
        *) say "    (unknown manifest entry: ${entry})" ;;
    esac
}

# ── Uninstall ──────────────────────────────────────────────────────────────────

if $UNINSTALL; then
    if ! $DRY_RUN && [[ $EUID -ne 0 ]]; then
        echo "Run as root: sudo ./install.sh --uninstall" >&2
        exit 1
    fi

    say "==> Uninstalling PiCam"
    if [[ ! -f "$MANIFEST" ]]; then
        say "    No manifest at ${MANIFEST} — falling back to the default layout."
        say "    Anything installed under non-default paths must be removed by hand."
    fi

    if ! $ASSUME_YES && ! $DRY_RUN; then
        say ""
        say "    This removes ${INSTALL_DIR}, /etc/picam.env, the picam systemd units,"
        say "    and unmounts the capture buffer and NFS archive."
        say "    Captures still in the tmpfs buffer are lost. The NFS archive is NOT touched."
        say ""
        read -rp "    Continue? [y/N] " reply
        [[ "$reply" =~ ^[Yy]$ ]] || { say "Aborted."; exit 1; }
    fi

    say "==> Stopping services..."
    run_ok systemctl disable --now picam-flask
    run_ok systemctl disable --now picam-motion

    if [[ -f "$MANIFEST" ]]; then
        say "==> Removing recorded items..."
        while IFS= read -r entry; do
            [[ -z "$entry" ]] && continue
            remove_entry "$entry"
        done < "$MANIFEST"
    else
        say "==> Removing default-layout items..."
        for unit in "$(systemd-escape -p --suffix=mount     /run/picam/media)" \
                    "$(systemd-escape -p --suffix=automount /mnt/picam-nas)" \
                    "$(systemd-escape -p --suffix=mount     /mnt/picam-nas)"; do
            run_ok systemctl disable --now "$unit"
            run rm -f "/etc/systemd/system/${unit}"
        done
        run rm -rf "$INSTALL_DIR" /etc/picam.env
    fi

    # The drop-in files are recorded individually; their directories are ours,
    # so clear them once nothing else is left in them.
    for dir in /etc/systemd/system/picam-motion.service.d \
               /etc/systemd/system/picam-flask.service.d; do
        [[ -d "$dir" ]] || $DRY_RUN || continue
        run_ok rmdir "$dir"
    done

    say "==> Removing service units..."
    run rm -f /etc/systemd/system/picam-motion.service \
              /etc/systemd/system/picam-flask.service
    run rm -f "$MANIFEST"

    say "==> Restoring configuration..."
    for conf in "$MOTION_CONF" "$CADDY_CONF"; do
        if [[ -f "${conf}.picam-orig" ]]; then
            say "    restoring ${conf}"
            run mv "${conf}.picam-orig" "$conf"
        else
            say "    no backup of ${conf} — leaving the current file in place"
        fi
    done

    run systemctl daemon-reload
    run_ok systemctl restart caddy

    say ""
    say "Done. Left alone deliberately:"
    say "  - apt packages (motion, caddy, libcamera, nfs-common) — remove by hand if you want them gone"
    say "  - the NFS archive contents"
    say "  - the i2c-dev entry in /etc/modules and dtparam in the boot config"
    say "  - the system 'motion' service, still disabled; 'sudo systemctl enable --now motion' re-enables it"
    exit 0
fi

# ── Install ────────────────────────────────────────────────────────────────────

if ! $DRY_RUN && [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo ./install.sh   (or ./install.sh --dry-run to preview)" >&2
    exit 1
fi

if $DRY_RUN; then
    say "=== DRY RUN — nothing below is executed ==="
    say ""
fi

say "==> Installing system packages..."
run apt-get update -q
run apt-get install -y \
    libcamera-ipa rpicam-apps-core \
    libcamera-tools libcamera-v4l2 \
    v4l-utils motion \
    python3 python3-venv python3-smbus i2c-tools nfs-common \
    debian-keyring debian-archive-keyring apt-transport-https curl

say "==> Installing Caddy..."
if command -v caddy &>/dev/null; then
    say "    already installed"
elif $DRY_RUN; then
    say "       $ curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
    say "       $ curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | tee /etc/apt/sources.list.d/caddy-stable.list"
    say "       $ apt-get update -q && apt-get install -y caddy"
else
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y caddy
fi

# Stop and disable the default motion service — we use picam-motion instead
run_ok systemctl stop motion
run_ok systemctl disable motion

say "==> Enabling I2C..."
BOOT_CONFIG=""
for f in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$f" ]] && BOOT_CONFIG="$f" && break
done

if [[ -n "$BOOT_CONFIG" ]]; then
    if ! grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG"; then
        append_line "dtparam=i2c_arm=on" "$BOOT_CONFIG"
        say "    NOTE: I2C enabled in $BOOT_CONFIG — a reboot is required after install."
        REBOOT_REQUIRED=true
    fi
else
    say "    no Raspberry Pi boot config found — skipping"
fi

run_ok modprobe i2c-dev
append_line "i2c-dev" /etc/modules

say "==> Configuring motion..."

# Load env file, skip comments and blank lines
while IFS='=' read -r key value; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    key="${key// /}"
    value="${value%%#*}"    # strip inline comments
    value="${value// /}"
    printf -v "$key" '%s' "$value"
done < "$SCRIPT_DIR/motion.env"

if [[ ! -f "$MOTION_CONF" ]]; then
    if $DRY_RUN; then
        say "    ($MOTION_CONF is not present here — showing the edits it would receive)"
    else
        echo "ERROR: $MOTION_CONF not found. Is motion installed?" >&2
        exit 1
    fi
fi

backup_once "$MOTION_CONF"

set_conf() {   # set_conf <key> <value> — replace the line or append it
    local key="$1" value="$2"
    if [[ -f "$MOTION_CONF" ]] && grep -q "^${key} " "$MOTION_CONF"; then
        run sed -i "s|^${key} .*|${key} ${value}|" "$MOTION_CONF"
    else
        append_line "${key} ${value}" "$MOTION_CONF"
    fi
}

CONFIG_VARS="daemon log_level target_dir webcontrol_localhost webcontrol_port \
stream_localhost stream_port picture_output movie_output stream_maxrate \
framerate width height rotate threshold minimum_motion_frames event_gap pre_capture"

for var in $CONFIG_VARS; do
    env_var="MOTION_${var^^}"
    value="${!env_var}"
    [[ -z "$value" ]] && continue
    set_conf "$var" "$value"
done

say "==> Wiring motion hooks..."
# on_picture_save / on_movie_end fire once the file is closed, which is what
# makes it safe for the uploader to copy it. %f is the full path.
set_conf on_motion_detected "curl -s -X POST http://127.0.0.1:8080/motion-event"
set_conf on_picture_save    "curl -s -X POST --data-urlencode 'path=%f' http://127.0.0.1:8080/media-saved"
set_conf on_movie_end       "curl -s -X POST --data-urlencode 'path=%f' http://127.0.0.1:8080/media-saved"

say "==> Writing /etc/picam.env..."
# Preserve SECRET_KEY across re-runs so existing sessions stay valid
EXISTING_SECRET=$(grep "^SECRET_KEY=" /etc/picam.env 2>/dev/null || true)
# Any setting app.py reads must be listed here, or it silently keeps its default
FLASK_VARS="PAN_START|TILT_START|PAN_MIN|PAN_MAX|TILT_MIN|TILT_MAX|SCAN_SPEED\
|AUTH_USER|AUTH_PASS|MOTION_TARGET_DIR|GALLERY_LIMIT\
|NFS_ENABLED|NFS_MOUNT|NFS_SUBDIR|CAMERA_NAME|BUFFER_HIGH_WATER\
|UPLOAD_STABLE_AGE|UPLOAD_SWEEP_INTERVAL|UPLOAD_RETRY_MIN|UPLOAD_RETRY_MAX"
if [[ -n "$EXISTING_SECRET" ]]; then
    PICAM_SECRET="$EXISTING_SECRET"
else
    PICAM_SECRET="SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi
# Holds AUTH_PASS and SECRET_KEY in the clear — never world-readable
{
    grep -E "^(${FLASK_VARS})=" "$SCRIPT_DIR/motion.env" | sed 's/[[:space:]]*$//' || true
    if $DRY_RUN; then echo "SECRET_KEY=<generated, or preserved from the existing file>"
    else echo "$PICAM_SECRET"; fi
} | write_file /etc/picam.env 0600
record "file:/etc/picam.env"

say "==> Configuring capture buffer (tmpfs) at ${MOTION_TARGET_DIR}..."
BUFFER_UNIT=$(systemd-escape -p --suffix=mount "$MOTION_TARGET_DIR")
run mkdir -p "$MOTION_TARGET_DIR"
write_file "/etc/systemd/system/${BUFFER_UNIT}" <<EOF
[Unit]
Description=PiCam capture buffer (tmpfs)
Before=picam-motion.service

[Mount]
What=tmpfs
Where=${MOTION_TARGET_DIR}
Type=tmpfs
Options=size=${BUFFER_TMPFS_SIZE:-256M},mode=0755,nosuid,nodev

[Install]
WantedBy=multi-user.target
EOF
record "unit:${BUFFER_UNIT}"

# Both services touch the buffer, so neither should start before it is mounted
for unit in picam-motion picam-flask; do
    run mkdir -p "/etc/systemd/system/${unit}.service.d"
    write_file "/etc/systemd/system/${unit}.service.d/buffer.conf" <<EOF
[Unit]
RequiresMountsFor=${MOTION_TARGET_DIR}
EOF
    record "dropin:/etc/systemd/system/${unit}.service.d/buffer.conf"
done

if [[ "${NFS_ENABLED,,}" == "true" ]]; then
    if [[ -z "$NFS_SERVER" || -z "$NFS_EXPORT" ]]; then
        echo "    ERROR: NFS_ENABLED=true but NFS_SERVER or NFS_EXPORT is empty." >&2
        exit 1
    fi
    say "==> Configuring NFS archive ${NFS_SERVER}:${NFS_EXPORT} at ${NFS_MOUNT}..."
    run mkdir -p "$NFS_MOUNT"
    NFS_UNIT=$(systemd-escape -p --suffix=mount     "$NFS_MOUNT")
    NFS_AUTO=$(systemd-escape -p --suffix=automount "$NFS_MOUNT")
    # nofail/_netdev are fstab-generator concepts; the automount unit below is
    # what actually keeps a dead NAS from holding up boot.
    UNIT_OPTS=$(echo "$NFS_MOUNT_OPTIONS" | sed -E 's/,?(nofail|_netdev)//g; s/^,//')

    write_file "/etc/systemd/system/${NFS_UNIT}" <<EOF
[Unit]
Description=PiCam NFS archive
After=network-online.target
Wants=network-online.target

[Mount]
What=${NFS_SERVER}:${NFS_EXPORT}
Where=${NFS_MOUNT}
Type=nfs
Options=${UNIT_OPTS}
TimeoutSec=30

[Install]
WantedBy=multi-user.target
EOF
    record "unit:${NFS_UNIT}"

    write_file "/etc/systemd/system/${NFS_AUTO}" <<EOF
[Unit]
Description=PiCam NFS archive (automount)

[Automount]
Where=${NFS_MOUNT}
TimeoutIdleSec=600

[Install]
WantedBy=multi-user.target
EOF
    record "unit:${NFS_AUTO}"
    NFS_CONFIGURED=true
else
    say "==> NFS archive disabled (NFS_ENABLED is not true)"
fi

say "==> Installing app to $INSTALL_DIR..."
run mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"
run cp "$SCRIPT_DIR/app.py"           "$INSTALL_DIR/app.py"
run cp "$SCRIPT_DIR/uploader.py"      "$INSTALL_DIR/uploader.py"
run cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
run cp -r "$SCRIPT_DIR/templates/."   "$INSTALL_DIR/templates/"
run cp -r "$SCRIPT_DIR/static/."      "$INSTALL_DIR/static/"
record "dir:${INSTALL_DIR}"

say "==> Setting up Python venv..."
# --system-site-packages lets the venv see python3-smbus, which pantilthat requires
run python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
run "$INSTALL_DIR/venv/bin/pip" install --no-cache-dir -r "$INSTALL_DIR/requirements.txt"

say "==> Installing systemd services..."
backup_once "$CADDY_CONF"
run cp "$SCRIPT_DIR/picam-motion.service" /etc/systemd/system/
run cp "$SCRIPT_DIR/picam-flask.service"  /etc/systemd/system/
run cp "$SCRIPT_DIR/Caddyfile"            "$CADDY_CONF"

# Anything the previous install created that this run does not is now stale.
if [[ -f "$MANIFEST" ]]; then
    STALE=false
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        if ! printf '%s\n' "${MANIFEST_ENTRIES[@]}" | grep -qxF -- "$entry"; then
            $STALE || { say "==> Cleaning up the previous install..."; STALE=true; }
            remove_entry "$entry"
        fi
    done < "$MANIFEST"
fi
printf '%s\n' "${MANIFEST_ENTRIES[@]}" | write_file "$MANIFEST" 0644

run systemctl daemon-reload
run systemctl enable "$BUFFER_UNIT"
run systemctl restart "$BUFFER_UNIT"
if [[ "$NFS_CONFIGURED" == true ]]; then
    run systemctl enable "$NFS_AUTO"
    run systemctl restart "$NFS_AUTO"
fi
run systemctl enable picam-motion picam-flask caddy
run systemctl restart picam-motion picam-flask caddy

PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PI_IP="${PI_IP:-<pi-ip>}"
say ""
if $DRY_RUN; then
    say "=== DRY RUN complete — nothing was changed ==="
    say ""
fi
say "Done."
say "  Controller UI   : https://${PI_IP}  (accept the self-signed cert warning once)"
say "  HTTP redirects  : http://${PI_IP} → https"
say "  Trust CA cert   : /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"
say "  Capture buffer  : ${MOTION_TARGET_DIR} (tmpfs, ${BUFFER_TMPFS_SIZE:-256M})"
if [[ "$NFS_CONFIGURED" == true ]]; then
    say "  NFS archive     : ${NFS_SERVER}:${NFS_EXPORT} -> ${NFS_MOUNT}/${NFS_SUBDIR}/${CAMERA_NAME:-$(hostname)}"
    say "                    check status at https://${PI_IP}/storage"
else
    say "  NFS archive     : disabled (set NFS_ENABLED=true in motion.env)"
fi
say "  Uninstall       : sudo ./install.sh --uninstall"
if [[ "$REBOOT_REQUIRED" == true ]]; then
    say ""
    say "  I2C was just enabled — reboot the Pi before the pan/tilt HAT will respond."
fi
