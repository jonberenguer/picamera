#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/picam

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo ./install.sh"
    exit 1
fi

echo "==> Installing system packages..."
apt-get update -q
apt-get install -y \
    libcamera-ipa rpicam-apps-core \
    libcamera-tools libcamera-v4l2 \
    v4l-utils motion \
    python3 python3-venv python3-smbus i2c-tools \
    debian-keyring debian-archive-keyring apt-transport-https curl

echo "==> Installing Caddy..."
if ! command -v caddy &>/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y caddy
fi

# Stop and disable the default motion service — we use picam-motion instead
systemctl stop motion   2>/dev/null || true
systemctl disable motion 2>/dev/null || true

echo "==> Enabling I2C..."
BOOT_CONFIG=""
for f in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$f" ]] && BOOT_CONFIG="$f" && break
done

if [[ -n "$BOOT_CONFIG" ]]; then
    if ! grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG"; then
        echo "dtparam=i2c_arm=on" >> "$BOOT_CONFIG"
        echo "    NOTE: I2C enabled in $BOOT_CONFIG — a reboot is required after install."
        REBOOT_REQUIRED=true
    fi
fi

modprobe i2c-dev 2>/dev/null || true
if ! grep -q "^i2c-dev" /etc/modules 2>/dev/null; then
    echo "i2c-dev" >> /etc/modules
fi

echo "==> Configuring motion..."
MOTION_CONF=/etc/motion/motion.conf

# Load env file, skip comments and blank lines
while IFS='=' read -r key value; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    key="${key// /}"
    value="${value%%#*}"    # strip inline comments
    value="${value// /}"
    printf -v "$key" '%s' "$value"
done < "$SCRIPT_DIR/motion.env"

CONFIG_VARS="daemon log_level target_dir webcontrol_localhost webcontrol_port \
stream_localhost stream_port picture_output movie_output stream_maxrate \
framerate width height rotate threshold minimum_motion_frames event_gap pre_capture"

for var in $CONFIG_VARS; do
    env_var="MOTION_${var^^}"
    value="${!env_var}"
    [[ -z "$value" ]] && continue

    if grep -q "^${var} " "$MOTION_CONF"; then
        sed -i "s|^${var} .*|${var} ${value}|" "$MOTION_CONF"
    else
        echo "${var} ${value}" >> "$MOTION_CONF"
    fi
done

echo "==> Wiring motion detection hook..."
MOTION_HOOK="curl -s -X POST http://127.0.0.1:8080/motion-event"
if grep -q "^on_motion_detected " "$MOTION_CONF"; then
    sed -i "s|^on_motion_detected .*|on_motion_detected ${MOTION_HOOK}|" "$MOTION_CONF"
else
    echo "on_motion_detected ${MOTION_HOOK}" >> "$MOTION_CONF"
fi

echo "==> Writing /etc/picam.env..."
# Preserve SECRET_KEY across re-runs so existing sessions stay valid
EXISTING_SECRET=$(grep "^SECRET_KEY=" /etc/picam.env 2>/dev/null || true)
grep -E "^(PAN_START|TILT_START|PAN_MIN|PAN_MAX|TILT_MIN|TILT_MAX|SCAN_SPEED|AUTH_USER|AUTH_PASS)=" "$SCRIPT_DIR/motion.env" > /etc/picam.env || true
if [[ -n "$EXISTING_SECRET" ]]; then
    echo "$EXISTING_SECRET" >> /etc/picam.env
else
    echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> /etc/picam.env
fi

echo "==> Installing app to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"
cp "$SCRIPT_DIR/app.py"               "$INSTALL_DIR/app.py"
cp "$SCRIPT_DIR/requirements.txt"     "$INSTALL_DIR/requirements.txt"
cp -r "$SCRIPT_DIR/templates/."       "$INSTALL_DIR/templates/"
cp -r "$SCRIPT_DIR/static/."          "$INSTALL_DIR/static/"

echo "==> Setting up Python venv..."
# --system-site-packages lets the venv see python3-smbus, which pantilthat requires
python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --no-cache-dir -r "$INSTALL_DIR/requirements.txt"

echo "==> Installing systemd services..."
cp "$SCRIPT_DIR/picam-motion.service" /etc/systemd/system/
cp "$SCRIPT_DIR/picam-flask.service"  /etc/systemd/system/
cp "$SCRIPT_DIR/Caddyfile"            /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable picam-motion picam-flask caddy
systemctl restart picam-motion picam-flask caddy

PI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "Done."
echo "  Controller UI   : https://${PI_IP}  (accept the self-signed cert warning once)"
echo "  HTTP redirects  : http://${PI_IP} → https"
echo "  Trust CA cert   : /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"
[[ "$REBOOT_REQUIRED" == true ]] && echo ""
[[ "$REBOOT_REQUIRED" == true ]] && echo "  I2C was just enabled — reboot the Pi before the pan/tilt HAT will respond."
