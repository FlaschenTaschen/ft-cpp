#!/usr/bin/env python3
"""Hold each brightness level long enough to actually look at it.

  python3 ftstairs.py              # 100 60 30 15 5, eight seconds each
  python3 ftstairs.py 40 20 10     # whatever levels you like
  python3 ftstairs.py --hold 20    # longer holds

Freezing the rotation first makes low levels much easier to judge: banding shows
up in a flat gradient held still, and not at all in something that is moving.

  python3 ftstairs.py --freeze sunset
"""
import json, socket, sys, time, urllib.request

def cmd(line):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(3)
    s.connect("/run/ft/control.sock")
    s.sendall((line + "\n").encode())
    out = b""
    while True:
        c = s.recv(4096)
        if not c: break
        out += c
    s.close(); return out.decode().strip()

def sched(op, **kw):
    body = dict(kw); body["op"] = op
    req = urllib.request.Request("http://127.0.0.1:8081/api/command",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=3).read()

def state():
    with urllib.request.urlopen("http://127.0.0.1:8081/api/state", timeout=3) as f:
        return json.load(f)

args = sys.argv[1:]
hold = 8.0
freeze = None
levels = []
i = 0
while i < len(args):
    if args[i] == "--hold":
        hold = float(args[i + 1]); i += 2
    elif args[i] == "--freeze":
        freeze = args[i + 1] if i + 1 < len(args) and not args[i+1].isdigit() else ""
        i += 2 if freeze else 1
    else:
        levels.append(int(args[i])); i += 1
levels = levels or [100, 60, 30, 15, 5]

was_paused = state().get("paused")
if freeze is not None:
    if freeze:
        rot = state()["rotation"]
        match = [e for e in rot if e["name"] == freeze]
        if not match:
            print("no demo called %r" % freeze); sys.exit(1)
        sched("jump", index=match[0]["position"])
        time.sleep(4)                       # let it get somewhere worth looking at
    sched("pause")
    print("rotation paused on %r" % state()["now"]["name"])

try:
    for b in levels:
        cmd("brightness %d" % b)
        print("  %3d%%  holding %.0fs   (get: %s)"
              % (b, hold, cmd("get").split("\n")[0]), flush=True)
        time.sleep(hold)
finally:
    cmd("brightness 80")
    if freeze is not None and not was_paused:
        sched("resume")
    print("back to 80%%%s" % ("" if was_paused else ", rotation resumed"))
