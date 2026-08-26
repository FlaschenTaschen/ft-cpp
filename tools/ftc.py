#!/usr/bin/env python3
"""Minimal control-socket client: ftc.py <command...>"""
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(3)
s.connect("/run/ft/control.sock")
s.sendall((" ".join(sys.argv[1:]) + "\n").encode())
out = b""
while True:
    c = s.recv(4096)
    if not c: break
    out += c
sys.stdout.write(out.decode())
