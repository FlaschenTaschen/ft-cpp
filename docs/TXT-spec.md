# FlaschenTaschen mDNS TXT Record Specification

**Version:** 1.0
**Status:** Canonical Specification
**Applies to:** C++ and Swift implementations
**Service Type:** `_flaschen-taschen._udp`

---

## Overview

FlaschenTaschen servers **optionally** advertise their capabilities via mDNS/Bonjour using DNS-SD. Clients discover displays by browsing for `_flaschen-taschen._udp` services and parsing the TXT record fields to determine display properties and supported features.

**Important:** mDNS service discovery is **disabled by default**. Servers must be explicitly started with mDNS enabled (e.g., `--mdns enabled`) to publish their service on the network. When mDNS is disabled, the server runs normally but is not discoverable via mDNS—clients must use direct connection (hardcoded IP/hostname).

This specification defines all TXT record fields, their formats, and semantics. Both C++ and Swift implementations must follow this specification to ensure cross-platform compatibility.

---

## Service Type

```
_flaschen-taschen._udp
```

- **Protocol**: UDP (fixed, cannot vary)
- **Port**: 1337 (fixed for FlaschenTaschen UDP server)
- **Domain**: `.local` (mDNS)

---

## TXT Record Fields

### Required Fields

#### `width` (required)

**Description:** Display grid width in pixels.

**Format:** Decimal integer (unsigned 16-bit)

**Examples:**
- `width=45`
- `width=64`
- `width=128`
- `width=256`

**Constraints:**
- Minimum: 1
- Maximum: 65535
- Must match the actual display dimensions

**Notes:**
- This value tells clients the maximum X coordinate they can address
- Used by client tools to auto-populate `-g WIDTHxHEIGHT` arguments

---

#### `height` (required)

**Description:** Display grid height in pixels.

**Format:** Decimal integer (unsigned 16-bit)

**Examples:**
- `height=35`
- `height=64`
- `height=40`
- `height=192`

**Constraints:**
- Minimum: 1
- Maximum: 65535
- Must match the actual display dimensions

**Notes:**
- This value tells clients the maximum Y coordinate they can address
- Used with `width` for geometry auto-population

---

#### `name` (required)

**Description:** Human-readable display name for the server.

**Format:** UTF-8 string, up to 63 characters (DNS-SD TXT record limit per field)

**Examples:**
- `name=Polaris`
- `name=Living Room`
- `name=Kitchen Display`
- `name=Studio Main`

**Constraints:**
- Must not be empty
- Should be unique within a local network
- May contain spaces and special characters (URL-encoded if necessary per DNS-SD spec)

**Notes:**
- Used as the instance name in the mDNS advertisement
- May have `#2`, `#3` suffix appended if name collision occurs
- Intended for human identification, not programmatic matching

---

#### `version` (required)

**Description:** Server implementation version in semantic versioning format.

**Format:** `MAJOR.MINOR.PATCH` (e.g., `1.0.0`)

**Examples:**
- `version=1.0.0`
- `version=1.1.0`
- `version=2.0.0-beta`

**Constraints:**
- Must follow semantic versioning (https://semver.org)
- Clients may use this for protocol compatibility checks

**Notes:**
- Allows clients to detect incompatibilities or missing features
- Should be incremented when protocol-breaking changes occur
- Prerelease/build metadata allowed per semver spec

**Platform-Specific Versions:**
- C++ server: Version defined in `server/main.cc` via `FT_VERSION` macro
- Swift server: Version defined in the Swift implementation

---

#### `backend` (required)

**Description:** The rendering backend used by the server.

**Format:** Single-word string, case-sensitive

**Valid Values:**

- `ft` — Direct FlaschenTaschen hardware interface (LED strips with SPI drivers)
  - Used by C++ with `FT_BACKEND=0` (spixels/SPI)
  - Used by Swift on macOS/tvOS with Apple hardware

- `rgb-matrix` — RGB LED matrix backend
  - Used by C++ with `FT_BACKEND=1` (rpi-rgb-led-matrix)
  - Common on Raspberry Pi with LED matrices

- `terminal` — Terminal/console output
  - Used by C++ with `FT_BACKEND=2` (ANSI color output)
  - Used for development/testing without hardware

**Examples:**
- `backend=ft`
- `backend=rgb-matrix`
- `backend=terminal`

**Notes:**
- Informational only; does not affect protocol or capabilities
- Helps clients understand the display type and potential performance characteristics
- May be used for logging or UI purposes

---

#### `platform` (required)

**Description:** The operating system and/or platform running the server.

**Format:** Single-word string, case-sensitive

**Valid Values:**

**Operating Systems:**
- `Linux` — Linux operating system
- `macOS` — Apple macOS
- `iOS` — Apple iOS
- `iPadOS` — Apple iPadOS
- `tvOS` — Apple tvOS
- `Windows` — Microsoft Windows

**Examples:**
- `platform=Linux` (C++ on Raspberry Pi or desktop Linux)
- `platform=macOS` (C++ or Swift on Apple Silicon/Intel Macs)
- `platform=tvOS` (Swift on Apple TV)

**Notes:**
- Informational; does not affect protocol or capabilities
- Useful for debugging and understanding deployment environments
- Clients may filter or prioritize based on platform preference

---

#### `features` (required)

**Description:** Bitmask of supported server capabilities.

**Format:** Hexadecimal string with `0x` prefix (e.g., `0x000F`), representing a 16-bit unsigned integer

**Feature Bits (Currently Defined):**

| Bit | Hex | Feature | Description |
|-----|-----|---------|-------------|
| 0 | 0x0001 | Multi-packet support | Server can accumulate/reassemble frames split across multiple UDP packets |
| 1 | 0x0002 | Multi-layer support | Server supports 16 layers with compositing and `offset_z` |
| 2 | 0x0004 | Offset/partial updates | Server supports `offset_x`, `offset_y` per packet for partial updates |
| 3 | 0x0008 | Layer timeout/GC | Server has layer garbage collection (automatic clearing after inactivity) |
| 4-15 | — | Reserved | Reserved for future feature definitions |

**Constraints:**
- Hexadecimal format with `0x` prefix (case-insensitive, e.g., `0x000F` or `0x000f`)
- 16-bit value range (0x0000 to 0xFFFF)
- Bits 4-15 reserved for future use
- Clients must ignore unknown bits gracefully (assume not supported)

**Examples:**

```
features=0x000F  # All currently-defined features (bits 0-3)
features=0x0007  # No layer timeout (bits 0-2 only)
features=0x0003  # Multi-packet and multi-layer only (bits 0-1)
features=0x0001  # Multi-packet only (bit 0)
features=0x0000  # No features (minimal server)
features=0x001F  # All currently-defined + 1 hypothetical future feature (bits 0-4)
```

**Why Hexadecimal?**

Hex format makes the bitmask structure immediately visible to developers:
- `0x000F` instantly shows bits 0-3 are set
- `0x0007` instantly shows bits 0-2 are set
- Easier to visually combine flags (e.g., `0x0001 | 0x0002 = 0x0003`)
- Standard for representing capability flags in systems programming

**C++ Server:** Always advertises `features=15` (0x000F) when running on actual hardware, as the C++ codebase supports all currently-defined features:
- Multi-packet: `udp-flaschen-taschen.cc` splits large displays
- Multi-layer: `CompositeFlaschenTaschen` with 16 layers
- Offset/partial: Packet header includes `offset_x`, `offset_y`, `offset_z`
- Layer timeout: `CompositeFlaschenTaschen::StartLayerGarbageCollection()`

**Swift Server:** Advertises features based on implementation; typically `features=15` for full implementations.

**Future-Proofing:**

The 16-bit format allows for up to 16 different capability flags. When new features are added:
1. Define a new bit position (e.g., Bit 4 = 0x0010)
2. Increment the version in TXT `version` field
3. Servers advertising the new feature set the bit
4. Clients checking for the feature test the bit and gracefully degrade if not present

**Parsing Guidance:**

```cpp
// C++ example: parse hex string (with 0x prefix)
uint16_t features = static_cast<uint16_t>(std::stoul(features_txt, nullptr, 16));
bool supports_multipacking = (features & 0x0001) != 0;
bool supports_layers = (features & 0x0002) != 0;
bool supports_offset = (features & 0x0004) != 0;
bool supports_layer_gc = (features & 0x0008) != 0;
// Safely ignore unknown bits
```

```swift
// Swift example: parse hex string (with 0x prefix)
let features = UInt16(featuresTxt, radix: 16) ?? 0
let supportsMultipacking = (features & 0x0001) != 0
let supportsLayers = (features & 0x0002) != 0
let supportsOffset = (features & 0x0004) != 0
let supportsLayerGC = (features & 0x0008) != 0
// Safely ignore unknown bits
```

---

### Optional Fields

#### `url` (optional)

**Description:** Web URL for accessing the server interface or documentation.

**Format:** Full URL string (http or https)

**Examples:**
- `url=http://192.168.1.50:8080`
- `url=https://wiki.org/wiki/Polaris`
- `url=http://polaris.local`

**Constraints:**
- Must be a valid URL (http or https)
- May be empty if no HTTP server is running
- Length limited by DNS-SD TXT record field size (up to 255 bytes, but typically keep under 63 chars per field)

**Notes:**
- Informational; helps users access documentation or control panel
- Optional; clients must handle missing or empty `url` gracefully
- If omitted, clients assume no HTTP interface available

---

## Complete Examples

### Example 1: Real Hardware (C++, `backend=ft`)

```
width=64
height=64
name=Polaris
version=1.0.0
backend=ft
platform=Linux
features=0x000F
url=https://wiki.org/wiki/Polaris
```

**Context:** FlaschenTaschen hardware with LED strips, running on Raspberry Pi. All features supported.

---

### Example 2: RGB LED Matrix (C++, `backend=rgb-matrix`)

```
width=128
height=40
name=Kitchen Display
version=1.0.0
backend=rgb-matrix
platform=Linux
features=0x000F
url=http://kitchen.local
```

**Context:** RGB LED matrix (e.g., rpi-rgb-led-matrix), running on Raspberry Pi. All features supported.

---

### Example 3: Terminal/Development (C++, `backend=terminal`)

```
width=45
height=35
name=Demo
version=1.0.0
backend=terminal
platform=macOS
features=0x000F
```

**Context:** Terminal output for development/testing on macOS. All features supported.

---

### Example 4: Minimal Server (Hypothetical)

```
width=128
height=64
name=BasicDisplay
version=1.0.0
backend=terminal
platform=Linux
features=0x0001
```

**Context:** Minimal server with only multi-packet support (bit 0), other features disabled.

---

### Example 5: Swift on tvOS

```
width=256
height=128
name=Living Room TV
version=1.0.0
backend=ft
platform=tvOS
features=0x000F
url=https://example.com/living-room
```

**Context:** Swift FlaschenTaschen on Apple TV with LED hardware. All features supported.

---

## Enabling mDNS Service Discovery

### Optional Feature

mDNS service discovery is **optional** and **disabled by default**. Servers may choose to advertise themselves via mDNS or operate as direct-connection-only.

### Enabling mDNS

**C++ Server:**

Start the server with mDNS enabled via command-line flag:

```bash
./ft-server --mdns enabled --mdns-name "Polaris" -D 64x64 \
    --mdns-url "https://wiki.org/wiki/Polaris"
```

mDNS-related command-line options:
- `--mdns <enabled|disabled>` — Enable/disable mDNS service discovery (default: disabled)
- `--mdns-name <name>` — Display name for service announcement (default: "FlaschenTaschen")
- `--mdns-url <url>` — Optional HTTP URL for documentation or web interface
- `--mdns-geometry <WxH>` — Display geometry override (alternative to `-D`)

When mDNS is **disabled** (default):
- Server starts normally and listens on UDP port 1337
- No service advertisement on the network
- Clients must connect via explicit hostname/IP (e.g., `send-image -h 192.168.1.50 ...`)

When mDNS is **enabled**:
- Server publishes `_flaschen-taschen._udp` service on the local network
- All TXT record fields are populated and advertised
- Clients can discover the display using `ft-detect` or other mDNS-aware tools

**Swift Server:**

Consult the Swift implementation documentation for enabling/disabling mDNS. The same principle applies: mDNS is optional and must be explicitly enabled at startup.

### Default Behavior

Both C++ and Swift implementations default to mDNS **disabled** for the following reasons:

1. **Security:** Reduces network exposure for private/controlled installations
2. **Simplicity:** Direct connection doesn't require mDNS daemon or network setup
3. **Compatibility:** Works on any network without mDNS prerequisites
4. **Performance:** Avoids multicast overhead if not needed

---

## Implementation Guidance

### Server Implementation (Publishing)

**C++ (server/service-discovery.cc):**

1. Determine values for all required fields at startup
2. Build TXT record strings using `avahi_entry_group_add_service()` with varargs:
   ```cpp
   avahi_entry_group_add_service(
       entry_group,
       AVAHI_IF_UNSPEC,
       AVAHI_PROTO_UNSPEC,
       static_cast<AvahiPublishFlags>(0),
       instance_name,           // e.g., "Polaris"
       "_flaschen-taschen._udp",
       nullptr,
       nullptr,
       1337,                    // port
       "width=64",
       "height=64",
       "name=Polaris",
       "version=1.0.0",
       "backend=ft",
       "platform=Linux",
       "features=15",
       "url=https://wiki.org/wiki/Polaris",
       nullptr  // NULL terminator
   );
   ```

2. Use `snprintf()` for runtime values:
   ```cpp
   char width_str[32], height_str[32], features_str[8];
   snprintf(width_str, sizeof(width_str), "width=%d", width);
   snprintf(height_str, sizeof(height_str), "height=%d", height);
   snprintf(features_str, sizeof(features_str), "features=%d", features);
   ```

**Swift:**

1. Use `NetServiceDelegate` or Bonjour APIs
2. Build TXT record dictionary with all required fields
3. Publish via `NetService` with TXT record data

---

### Client Implementation (Discovering)

**C++ (api/lib/ft-discovery.cc):**

1. Browse for `_flaschen-taschen._udp`
2. Resolve each discovered service
3. Parse TXT records:
   ```cpp
   // Iterate AvahiStringList*
   for (AvahiStringList* item = txt; item != nullptr; item = avahi_string_list_get_next(item)) {
       char* str = nullptr;
       avahi_string_list_get_pair(item, &key, &value, nullptr);
       // Parse key=value pairs
       if (strcmp(key, "width") == 0) {
           service.width = std::stoi(std::string(reinterpret_cast<char*>(value)));
       }
       // ... repeat for other fields
   }
   ```

4. Store in `DisplayService` struct
5. Return to caller

**Swift:**

1. Use `NetServiceBrowser` to find `_flaschen-taschen._udp` services
2. Resolve each service
3. Parse TXT record dictionary
4. Store in data model for clients to use

---

## Backward Compatibility

### Protocol Versions

The TXT record `version` field uses semantic versioning to signal compatibility:

- **Major version bump** (1.0.0 → 2.0.0): Protocol-breaking change. Clients should be cautious.
- **Minor version bump** (1.0.0 → 1.1.0): New optional features added. Clients can safely upgrade.
- **Patch version bump** (1.0.0 → 1.0.1): Bug fixes only. No feature changes.

Clients should check `version` and may refuse to connect to incompatible versions.

### Feature-Based Fallback

Clients discovering `features` bitmask should:

1. Check if required features are supported
2. Gracefully degrade if optional features are missing
3. Example: If a client needs multi-layer support (0x2) and it's not advertised, use single-layer only

---

## Validation and Error Handling

### Server Side (Publishing)

- **Must** provide all required fields
- **Should** validate geometry (width/height > 0)
- **Should** validate `version` is valid semver
- **Should** validate `backend` is a recognized value
- **May** omit `url` if no HTTP interface available

### Client Side (Discovering)

- **Must** handle missing optional fields (e.g., no `url`)
- **Should** handle malformed TXT records gracefully (default to safe values)
- **Should** validate geometry is sensible (prevent huge requests)
- **May** warn if `version` is newer than expected
- **May** filter displays by `platform` or `backend` if desired

---

## Testing

### Manual Testing (Command-line)

**List services with TXT records:**
```bash
avahi-browse _flaschen-taschen._udp --resolve --terminate
```

**Publish a test service:**
```bash
avahi-publish-service "Test Display" _flaschen-taschen._udp 1337 \
    width=64 height=64 name="Test" version=1.0.0 backend=terminal \
    platform=Linux features=15
```

**Query with mDNS (on macOS):**
```bash
dns-sd -B _flaschen-taschen._udp local
dns-sd -L "Polaris" _flaschen-taschen._udp local
```

---

## References

- **DNS-SD (DNS Service Discovery):** RFC 6763
- **mDNS (Multicast DNS):** RFC 6762
- **Avahi Documentation:** https://avahi.org
- **Semantic Versioning:** https://semver.org
