# Packet Chunking in Flaschen Taschen

## Overview

Packet chunking solves the problem of sending large canvas data that exceeds UDP packet size limits. Instead of sending the entire image in one packet, the implementation splits it into multiple chunks, each with its own PPM header containing offset information so the server can reassemble them correctly.

## Why Chunking is Needed

- UDP has a practical size limit of ~65KB (typically 65507 bytes after IP/UDP headers)
- Flaschen Taschen displays can be very large (e.g., 1920×2160 pixels)
- A full 1920×2160 RGB image requires 1920 × 2160 × 3 = 12,441,600 bytes
- This cannot fit in a single UDP packet, so chunking is required

## How It Works

### 1. Calculate Maximum Rows Per Packet

```
rowSize = width × 3  (3 bytes per pixel for RGB)
maxRowsPerPacket = (maxUDPSize - headerReserve) / rowSize
```

Where:
- `maxUDPSize` is determined by querying the socket's `SO_SNDBUF` setting (or can be overridden via `FT_UDP_SIZE` environment variable)
- `headerReserve` is 64 bytes, reserved for the PPM header and overhead

**Example**: For a 1920-wide display with maxUDPSize = 65507:
- rowSize = 1920 × 3 = 5760 bytes
- maxRowsPerPacket = (65507 - 64) / 5760 ≈ 11 rows per packet

### 2. Split Canvas Into Chunks

Loop through the canvas height, creating chunks of `maxRowsPerPacket` rows. The last chunk may be smaller if the height is not evenly divisible.

```
For each chunk from row 0 to height:
  rowsThisChunk = min(maxRowsPerPacket, height - currentRow)
  [process this chunk]
  currentRow += rowsThisChunk
```

### 3. Create Per-Packet PPM Header With Offset

For each chunk, generate a PPM header with the chunk's dimensions and offset coordinates:

```
Header = "P6\n{width} {rowsThisChunk}\n#FT: {offsetX} {offsetY + chunkRowOffset} {offsetZ}\n255\n"
```

**Key Detail**: The `#FT:` comment tells the server where to place this chunk:
- `offsetX`: Horizontal position (usually 0, no chunking in X direction)
- `offsetY + chunkRowOffset`: Vertical position of this specific chunk
- `offsetZ`: Layer/depth (for layered displays)

### 4. Extract and Send Pixel Data

For each chunk:
1. Extract the relevant pixel rows from the buffer: `buffer[pixelStart : pixelEnd]`
2. Construct packet: `[PPM header] + [pixel data]`
3. Send via UDP

## Packet Structure

Each packet contains:
```
[PPM Header with offset comment]
[Pixel data for chunk rows]
```

**Example packet for rows 100-110 of a 1920-wide image:**
```
P6
1920 11
#FT: 0 100 0
255
[RGB pixels for 11 rows × 1920 pixels = 63,360 bytes]
```

## Server-Side Reassembly

The server receives each packet independently and:
1. Parses the PPM header to determine chunk dimensions
2. Extracts the offset coordinates from the `#FT:` comment
3. Places the pixel data at the correct position on the display using those offsets

This way, chunk order doesn't matter—each packet is self-contained with positioning information.

## Implementation Details

### UDP Size Detection

The `maxUDPSize` is determined by (in priority order):
1. **Environment variable override**: `FT_UDP_SIZE` env var (if set and > 0)
2. **Socket query**: `getsockopt(fd, SOL_SOCKET, SO_SNDBUF)` to read the socket's send buffer size
3. **Fallback**: 65507 bytes (standard UDP max)

### Error Handling

- Validate that at least 1 row fits per packet (fail fast if canvas is too wide for max UDP size)
- Log detailed packet information when sending multiple chunks
- Handle write errors with descriptive messages (connection refused, etc.)

## Performance Notes

- **Header Overhead**: Each packet's PPM header is typically 20-30 bytes; minimal compared to pixel data
- **Number of Packets**: For a 1920×2160 display on a typical 65KB UDP limit, expect ~200 packets
- **Latency**: Sequential packet sending; no parallelization needed since UDP handles one packet at a time
- **Bandwidth**: Total bytes sent = (number of packets × header overhead) + (width × height × 3 bytes of pixel data)

## Variations and Extensions

This chunking method can support:
- **Partial canvas updates**: Send only a subset of rows by adjusting the Y offset
- **Layered displays**: Use the Z offset for displays with multiple layers
- **Different color depths**: Adjust `rowSize` calculation if using formats other than RGB8

## C++ Implementation Fix

### Issue

The `UDPFlaschenTaschen` class in `api/lib/udp-flaschen-taschen.cc` already implements packet chunking correctly in its `Send()` method (lines 158-189). However, UDP packet size detection was incomplete:

- Only used constructor parameter (default 65507) or `FT_UDP_SIZE` environment variable
- Did not query the actual kernel socket buffer limit via `SO_SNDBUF`
- On macOS, the kernel limit is ~9000 bytes, not 65507
- Result: Canvases like 64×64 would fail with "Message too long" error because the calculated `max_send_height` assumed 65KB limit when the kernel only allowed ~9KB

**Example**: For a 64-wide canvas on macOS:
- `rowSize = 64 × 3 = 192 bytes`
- Expected: `max_send_height = (9000 - 64) / 192 ≈ 46 rows` (should work in 2 packets)
- Actual: Code used 65507, calculated `max_send_height = 340 rows`, then kernel rejected oversized packet

### Solution

Update `UDPFlaschenTaschen` constructor and `SetMaxUDPPacketSize()` to:

1. Query the socket's `SO_SNDBUF` when socket is valid
2. Detect OS-specific limits (macOS ~9000, Linux ~65507)
3. Apply priority order from "UDP Size Detection" section above:
   - `FT_UDP_SIZE` environment variable (if set and > 0)
   - `getsockopt(fd, SOL_SOCKET, SO_SNDBUF)` on valid socket
   - Fallback to 65507

This ensures the existing chunking logic correctly divides canvases into appropriately-sized packets for the actual kernel limit.
