#!/bin/sh

BASE="/volume1/docker/xiaozhi-gateway-2"
SOCKET="$BASE/run/wol.sock"
LOG="$BASE/host-helper/wol-helper.log"
PYTHON="/bin/python3"

mkdir -p "$BASE/run"

# Als de socket bestaat, testen of de helper nog leeft.
if [ -S "$SOCKET" ]; then
    "$PYTHON" - <<PY >/dev/null 2>&1
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(1)
s.connect("$SOCKET")
s.sendall(b"PING\n")
try:
    s.recv(16)
except Exception:
    pass
s.close()
PY
    if [ $? -eq 0 ]; then
        exit 0
    fi
fi

rm -f "$SOCKET"

nohup "$PYTHON" "$BASE/host-helper/wol_helper.py" \
    >> "$LOG" 2>&1 &

exit 0
