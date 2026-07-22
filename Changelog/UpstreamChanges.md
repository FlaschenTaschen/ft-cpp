# Changes Since Forking Upstream

This repository consolidates the upstream [hzeller/flaschen-taschen](https://github.com/hzeller/flaschen-taschen)
and [cgorringe/ft-demos](https://github.com/cgorringe/ft-demos) projects into a single
tree (see [Consolidate.md](Consolidate.md)), then layers on three areas of work:
**bug fixes**, **performance improvements**, and a new **mDNS service discovery**
subsystem. This document summarizes those changes.

## Performance Improvements

### Frame buffering / double buffering for the RGB matrix (`4a02e38`)

Upstream `RGBMatrixFlaschenTaschen::SetPixel()` wrote directly to the live
framebuffer while the refresh thread was scanning it out to GPIO, and `Send()`
was a no-op. Pixel writes raced with frame scans, causing tearing and flicker.

- Each frame is now written to a back buffer (`FrameCanvas`); `Send()` calls
  `SwapOnVSync()` to present the completed frame atomically at the next vsync.
- `CompositeFlaschenTaschen::Send()` re-composites the full scene from the
  z-buffer and layer screens into the back buffer before each swap — required
  because the double-buffered backend hands back a stale canvas. This
  eliminates flicker when multiple layers are active (e.g. plasma on layer 0
  with text on a higher layer).
- Measured results: ~5x reduction in process context switches under a
  3-minute multi-layer load, tearing effectively eliminated, at the cost of
  ~1 refresh period (~7 ms at 140 Hz) of latency.

See [docs/PerformanceImprovements.md](../docs/PerformanceImprovements.md) for
verified results and further (not yet implemented) options: bulk `SetPixels()`
encoding, the `--led-no-busy-waiting` flag, reduced mutex hold time, and DMA
SPI for the crate path.

### Packet chunking for large canvases (`f2635a1`)

Large displays (e.g. 1920x2160 ~ 12 MB/frame) exceed the ~65 KB UDP packet
limit. The client library now splits a frame into multiple PPM-headered
chunks, each carrying an offset so the server reassembles them correctly.
Chunk size is derived from the socket's `SO_SNDBUF` (overridable via the
`FT_UDP_SIZE` environment variable). See [PacketChunking.md](PacketChunking.md).

## Bug Fixes

- **Clean shutdown / GC-thread lifetime** (`4a02e38`): `main()` was
  restructured so the `CompositeFlaschenTaschen` lives in an explicit scope —
  its destructor now runs and stops the layer garbage-collection thread
  cleanly on exit.
- **Cross-platform build** (`4a02e38`): `drop_privs()` in
  `server/rgb-matrix/lib/led-matrix.cc` is guarded with `#ifdef __linux__`
  (no-op fallback) so the tree builds on macOS for development.
- **Optional build dependencies** (`4a3b851`): avahi and nlohmann-json are
  detected via `pkg-config` and degrade gracefully with a notice when missing,
  instead of failing mid-build with cryptic header errors. Removed a
  hardcoded Homebrew include path from the demos Makefile. mDNS code is gated
  on `HAVE_MDNS` rather than `__linux__`.
- **mDNS build fixes** (`8169dcc`): resolved compile/link issues from the
  service-discovery work across the API lib, client, and server Makefiles.
- **send-video** (`719c878`): fixed the send-video build
  (see [SendVideoFix.md](SendVideoFix.md)).

## mDNS Service Discovery (new subsystem)

Implemented in `ec6e377` using **Avahi** (Linux Bonjour/Zeroconf):

- **Server publisher** — `server/service-discovery.{h,cc}`: a
  `ServiceDiscoveryThread` runs the Avahi event loop in the background and
  advertises a `_flaschen-taschen._udp` service with TXT metadata (width,
  height, name, version, backend, platform, features, optional URL). Enabled
  with `--mdns enabled`, started after hardware init so it reports correct
  dimensions.
- **Client browser** — `client/ft-detect.cc`: a discovery tool to find
  FlaschenTaschen displays on the LAN.
- **Shared API** — `api/lib/ft-discovery.{cc,h}`: reusable discovery logic
  for client apps.
- **Documentation** — [docs/mDNS.md](../docs/mDNS.md) (implementation and
  setup), [docs/TXT-spec.md](../docs/TXT-spec.md) (TXT record specification),
  plus investigation notes on Avahi 0.8 Pi-to-Pi discovery and a hybrid
  discovery strategy for clients (`67822bd`, `4514fec`).

Building with mDNS requires `libavahi-client-dev` and `pkg-config`; running
it requires `avahi-daemon`. Without them the server builds without mDNS
support (Linux) and `ft-detect` is skipped.

## Tooling and Demos

- **grayscale demo** (`1f9f03c`, `2323682`): new demo with JSON-driven
  features and test/debug infrastructure (see [Grayscale.md](Grayscale.md)
  and [Testing.md](Testing.md)).
- **pixelate tool** (`6596119`): converts images to JSON output for use with
  the grayscale demo.
- **Unified build** (`61ba49b`, `72b824a`): single top-level Makefile routing
  to subdirectories, with all build products under `build/`.
