#!/bin/bash
# motion.env.local overlay: machine-specific settings must override the tracked
# defaults, for install.sh's own logic and for the generated /etc/picam.env.
#
# Everything runs through --dry-run in a throwaway copy of the repo, so nothing
# is installed and no root is needed.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP=$(mktemp -d -t picam-env-XXXXXX)
trap 'rm -rf "$TMP"' EXIT
fails=0
check() { printf '  %-52s ' "$1"; shift; if "$@"; then echo PASS; else echo FAIL; fails=1; fi; }
has()  { grep -qE "$1" "$TMP/out"; }
picam_env() { sed -n '/write \/etc\/picam.env/,/^       \$/p' "$TMP/out" | sed 's/^ *| *//'; }

fresh() { rm -rf "$TMP/repo"; cp -a "$REPO" "$TMP/repo"; rm -f "$TMP/repo/motion.env.local"; }
dry()   { ( cd "$TMP/repo" && ./install.sh --dry-run ) > "$TMP/out" 2>&1; }

echo "==> no local file (unchanged behaviour)"
fresh; dry
check "names motion.env as the source"        has '==> Configuration: motion.env$'
check "does not claim an overlay"             bash -c '! grep -q "overrides" "$0/out"' "$TMP"
check "picam.env has NFS_ENABLED=false"       bash -c 'picam_env() { sed -n "/write \/etc\/picam.env/,/^       \\\$/p" "$1/out" | sed "s/^ *| *//"; }; picam_env "$1" | grep -qx "NFS_ENABLED=false"' _ "$TMP"
check "footer points at the local file"       has 'put machine-specific values in motion.env.local'

echo "==> with a local overlay"
fresh
cat > "$TMP/repo/motion.env.local" <<'EOF'
# this machine only
NFS_ENABLED=true
NFS_SERVER=10.1.2.3
NFS_EXPORT=/srv/cams
CAMERA_NAME=frontdoor
EOF
dry
check "reports both files"                    has 'Configuration: motion.env \+ motion.env.local \(overrides\)'
check "NFS archive is configured"             has 'Configuring NFS archive 10.1.2.3:/srv/cams'
check "archive path uses the local name"       has 'picam/frontdoor'
check "picam.env NFS_ENABLED=true"            bash -c 'sed -n "/write \/etc\/picam.env/,/^       \\\$/p" "$1/out" | sed "s/^ *| *//" | grep -qx "NFS_ENABLED=true"' _ "$TMP"
check "picam.env CAMERA_NAME=frontdoor"       bash -c 'sed -n "/write \/etc\/picam.env/,/^       \\\$/p" "$1/out" | sed "s/^ *| *//" | grep -qx "CAMERA_NAME=frontdoor"' _ "$TMP"
check "exactly one NFS_ENABLED line emitted"  bash -c '[[ $(sed -n "/write \/etc\/picam.env/,/^       \\\$/p" "$1/out" | sed "s/^ *| *//" | grep -c "^NFS_ENABLED=") -eq 1 ]]' _ "$TMP"
check "the overridden default is gone"        bash -c '! sed -n "/write \/etc\/picam.env/,/^       \\\$/p" "$1/out" | sed "s/^ *| *//" | grep -qx "NFS_ENABLED=false"' _ "$TMP"

echo "==> the overlay reaches install.sh's own early logic"
fresh; echo "PANTILT_ENABLED=off" > "$TMP/repo/motion.env.local"; dry
check "early PANTILT peek honours the overlay" has 'Pan/tilt disabled — installing without I2C'
check "I2C setup skipped"                      has 'Skipping I2C'

echo "==> the overlay reaches the motion.conf pass"
fresh; echo "MOTION_PICTURE_OUTPUT=off" > "$TMP/repo/motion.env.local"; dry
check "motion.conf gets the overridden value"  has 'picture_output off'
check "and not the tracked default"            bash -c '! grep -q "picture_output first" "$1/out"' _ "$TMP"

echo "==> a key present only in the local file still lands"
fresh; echo "GALLERY_LIMIT=7" > "$TMP/repo/motion.env.local"; dry
check "local-only value reaches picam.env"     bash -c 'sed -n "/write \/etc\/picam.env/,/^       \\\$/p" "$1/out" | sed "s/^ *| *//" | grep -qx "GALLERY_LIMIT=7"' _ "$TMP"

echo "==> a missing motion.env is an error, not a silent empty config"
fresh; rm -f "$TMP/repo/motion.env"; dry
check "refuses to run without motion.env"      bash -c 'grep -q "ERROR: no motion.env" "$1/out"' _ "$TMP"

echo
[[ $fails -eq 0 ]] && echo "env overlay: all green" || echo "env overlay: FAILURES"
exit $fails
