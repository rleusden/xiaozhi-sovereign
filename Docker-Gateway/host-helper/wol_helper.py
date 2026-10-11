import os
import socket

SOCKET_PATH = "/volume1/docker/xiaozhi-gateway-2/run/wol.sock"
MAC = "68:84:7e:a0:82:98"

try:
    os.unlink(SOCKET_PATH)
except FileNotFoundError:
    pass

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(SOCKET_PATH)
os.chmod(SOCKET_PATH, 0o666)
server.listen(4)

print(f"WOL helper listening on {SOCKET_PATH}")

packet = bytes.fromhex("FF" * 6 + MAC.replace(":", "") * 16)

while True:
    conn, _ = server.accept()
    try:
        command = conn.recv(32).decode("ascii", errors="ignore").strip()

        if command != "WAKE":
            conn.sendall(b"ERROR\n")
            continue

        wol = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            wol.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            wol.sendto(packet, ("255.255.255.255", 9))
        finally:
            wol.close()

        conn.sendall(b"OK\n")

    finally:
        conn.close()
