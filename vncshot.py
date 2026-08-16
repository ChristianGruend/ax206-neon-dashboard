#!/usr/bin/env python3
"""Minimaler RFB-Client: verbindet auf einen VNC-Server, holt genau einen
Framebuffer-Update im Raw-Encoding und schreibt ihn als PNG.

Nur fuer die lcd4linux-Layoutvorschau gedacht - kein Auth, kein Retry-Zauber.
"""
import socket
import struct
import sys
import time

from PIL import Image


def recv_exactly(sock, count):
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError(f"Verbindung zu, {len(buf)}/{count} Bytes gelesen")
        buf.extend(chunk)
    return bytes(buf)


def screenshot(host, port, out_path, settle=2.0):
    sock = socket.create_connection((host, port), timeout=15)
    sock.settimeout(15)

    # --- Handshake ---
    server_version = recv_exactly(sock, 12)
    if not server_version.startswith(b"RFB "):
        raise RuntimeError(f"Kein RFB-Server: {server_version!r}")
    sock.sendall(b"RFB 003.008\n")

    n_sec = recv_exactly(sock, 1)[0]
    if n_sec == 0:
        reason_len = struct.unpack(">I", recv_exactly(sock, 4))[0]
        raise RuntimeError(recv_exactly(sock, reason_len).decode(errors="replace"))
    sec_types = recv_exactly(sock, n_sec)
    if 1 not in sec_types:
        raise RuntimeError(f"Server verlangt Auth, Typen: {list(sec_types)}")
    sock.sendall(bytes([1]))                      # None
    if struct.unpack(">I", recv_exactly(sock, 4))[0] != 0:
        raise RuntimeError("SecurityResult != OK")

    sock.sendall(bytes([1]))                      # ClientInit, shared
    width, height = struct.unpack(">HH", recv_exactly(sock, 4))
    recv_exactly(sock, 16)                        # Server-PixelFormat, wird ueberschrieben
    name_len = struct.unpack(">I", recv_exactly(sock, 4))[0]
    recv_exactly(sock, name_len)

    # --- Pixelformat auf 32bpp BGRX festnageln ---
    pixel_format = struct.pack(
        ">BBBBHHHBBBxxx",
        32,    # bits-per-pixel
        24,    # depth
        0,     # big-endian
        1,     # true-colour
        255, 255, 255,
        16, 8, 0,
    )
    sock.sendall(struct.pack(">Bxxx", 0) + pixel_format)
    sock.sendall(struct.pack(">BxHi", 2, 1, 0))   # SetEncodings: nur Raw

    # lcd4linux zeichnet erst nach und nach - kurz warten, dann Vollbild anfordern
    time.sleep(settle)
    sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    deadline = time.time() + 20
    painted = 0
    while painted < width * height and time.time() < deadline:
        msg_type = recv_exactly(sock, 1)[0]
        if msg_type != 0:                          # nur FramebufferUpdate interessiert
            continue
        recv_exactly(sock, 1)
        n_rects = struct.unpack(">H", recv_exactly(sock, 2))[0]
        for _ in range(n_rects):
            x, y, w, h, enc = struct.unpack(">HHHHi", recv_exactly(sock, 12))
            if enc != 0:
                raise RuntimeError(f"Unerwartetes Encoding {enc}")
            if w == 0 or h == 0:
                continue
            data = recv_exactly(sock, w * h * 4)
            canvas.paste(Image.frombytes("RGB", (w, h), data, "raw", "BGRX"), (x, y))
            painted += w * h

    canvas.save(out_path)
    sock.close()
    print(f"{out_path} geschrieben ({width}x{height}, {painted} Pixel gemalt)")


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5900
    out = sys.argv[3] if len(sys.argv) > 3 else "/tmp/lcdshot.png"
    settle = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
    screenshot(host, port, out, settle)
