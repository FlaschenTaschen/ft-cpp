# ft-web — a browser preview for FlaschenTaschen

A FlaschenTaschen server that renders to a `<canvas>` instead of hardware, for
developing demos without a wall in front of you.

```console
$ ./ft-web-server.py -D 320x64
UDP-server: ready to listen on 1337
Viewer: http://localhost:8080/   (320x64, 16 layers)
```

Open the viewer, then point any demo at `127.0.0.1:1337` as usual.

## Why not the terminal backend

`make -C server FT_BACKEND=terminal` already gives you a preview, and for
static images or slow demos it is the quicker thing to reach for — no browser,
no second window. But it re-emits a truecolor escape sequence for every
character cell on every frame, so the terminal emulator, not the demo, decides
the frame rate. That makes it the wrong instrument for judging smoothness:
you end up measuring your terminal.

Measured here on a 320x64 canvas, driven by `demos/scroller.py`:

| | offered | delivered | frame interval p99 / max | CPU |
|---|---|---|---|---|
| ft-web | 60 fps | 60.0 fps | 16.8 / 17.0 ms | — |
| ft-web | 240 fps | 240.1 fps | 5.1 / 5.2 ms | 8.7% of one core |

118 Mbit/s over loopback at the top end. The preview is not the bottleneck.

## What the viewer shows

* **render / frames in / datagrams / wire** — what the server pushed to the
  browser, what arrived over UDP, and how much of it there was. If `frames in`
  is well below what your demo prints, the loss is on the wire, not in the
  renderer.
* **frame interval p50 / p99 / max**, with a rolling trace. This is the number
  that predicts visible flicker: a stall shows up as a spike here long before
  it moves the average, which is exactly the failure mode that is hard to
  judge by eye.
* **layer strip** — which of the 16 layers currently hold pixels. Handy when a
  demo is invisible because something else is sitting on top of it, or when
  you are waiting for a layer to age out.
* **zoom**, and an **LED grid** overlay that inserts gaps between pixels, which
  makes it much easier to tell real dithering from resampling artifacts.
* **freeze** stops the canvas updating while the stats keep running, for
  reading a single frame closely.

## Fidelity

The protocol and compositing are transcribed from the C++ server so what you
see matches the wall:

| behaviour | source |
|---|---|
| P6 parsing, `#FT: x y z` header comments, trailing-offset extension | `server/ppm-reader.cc` |
| 16 layers, black transparent above layer 0, topmost non-black wins | `server/composite-flaschen-taschen.cc` |
| 15 second per-layer inactivity timeout | `server/main.cc` |
| receive buffer sized to three full frames | `server/udp-server.cc` |

The layer stack is composited top-down on demand rather than maintained as an
incremental z-buffer. The two are equivalent — in both, a pixel shows the
highest layer that is non-black there, over an opaque layer 0 — but the
whole-stack form vectorizes and the per-pixel walk does not.

Two deliberate differences:

* A datagram carrying less pixel data than its header promises is dropped and
  counted under **bad packets**. The C++ reads the declared number of bytes
  regardless, running off the end of the receive buffer.
* Frames are pushed to the browser at `--push-fps` (default 60) and only when
  the composite has actually changed, so the browser is decoupled from the
  demo's rate. **frames in** still reports every frame that arrived, so you can
  see a demo running at 240 fps even while the canvas updates at 60.

This tool does not model the panel itself. Refresh jitter, PWM/BCM colour
quantisation and multiplexing artifacts are properties of the HUB75 hardware
and only show up on the wall.

## Options

```
-D, --dimension WxH     output dimension, like ft-server -D   (default 45x35)
    --ft-port PORT      UDP port for FlaschenTaschen images   (default 1337)
    --http-port PORT    TCP port for the viewer               (default 8080)
    --bind ADDR         viewer bind address; '::1' for local only (default '::')
    --layer-timeout SEC clear a layer after this much inactivity  (default 15)
    --push-fps N        cap on frames pushed to the browser   (default 60)
-v, --verbose           log HTTP requests and WebSocket upgrade headers
```

WebSocket connect and disconnect are always logged, with how long the
connection lasted and how many frames it received. A browser that reconnects
every second with `disconnected after 0.00s` is failing the handshake; one
with a rising `skipped` count cannot keep up with `--push-fps`.

`--bind` defaults to all interfaces so you can watch from another machine; use
`--bind ::1` to keep it local.

## Tests

```console
$ python3 test_ft_web.py
```

Covers the PPM parser against the cases `ppm-reader.cc` accepts, the
compositing and layer-expiry rules, WebSocket framing, and a loopback
round-trip that pushes a datagram in one side and reads the rendered frame out
the other.

Needs numpy; everything else is standard library.
