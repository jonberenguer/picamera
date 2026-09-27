#!/bin/bash
# Run every suite. Creates a venv on first use; needs no Pi and no hardware.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."
VENV=${VENV:-.testvenv}
[[ -x "$VENV/bin/python3" ]] || {
    echo "==> creating $VENV"
    python3 -m venv "$VENV" >/dev/null
    "$VENV/bin/pip" install -q flask requests
}
PY="$VENV/bin/python3"
fail=0
run() { printf '  %-34s ' "$1"; shift; "$PY" "$@" >/dev/null 2>&1 && echo PASS || { echo FAIL; fail=1; }; }

echo "==> static checks"
run "python syntax"        -c "import ast;[ast.parse(open(f).read()) for f in ('app.py','uploader.py')]"
printf '  %-34s ' "bash syntax"; bash -n install.sh && echo PASS || { echo FAIL; fail=1; }
printf '  %-34s ' "install.sh --dry-run";  ./install.sh --dry-run >/dev/null 2>&1 && echo PASS || { echo FAIL; fail=1; }

echo "==> suites"
run "automount trigger"    tests/test_automount.py
run "offload (NFS on)"     tests/test_offload.py on
run "offload (NFS off)"    tests/test_offload.py off
run "gallery delete + zip" tests/test_gallery_ops.py

echo
[[ $fail -eq 0 ]] && echo "all green" || echo "FAILURES — rerun a suite directly to see which check"
exit $fail
