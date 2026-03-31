# Service Discovery on Linux with C++ — FlaschenTaschen mDNS Implementation

On Debian Linux, the closest equivalent to what you did with `NWListener` and `NWBrowser` on Apple platforms is **Avahi**. Avahi is the mDNS/DNS-SD daemon and client stack that implements Apple-compatible Bonjour/Zeroconf on Linux.

This document describes the implementation of mDNS service discovery for **FlaschenTaschen** on Debian/Raspberry Pi. For a normal app on Debian or Raspberry Pi, Debian recommends using **`libavahi-client-dev`**, which talks to `avahi-daemon`; `libavahi-core-dev` exists for embeddable use, but Debian explicitly says not to use it for non-embedded applications. ([Debian Manpages][1])

## What to install

On the Pi:

```bash
sudo apt update
sudo apt install avahi-daemon avahi-utils libavahi-client-dev libavahi-common-dev build-essential pkg-config
sudo systemctl enable --now avahi-daemon
```

Why these:

* `avahi-daemon` is the system mDNS/DNS-SD service. ([Debian Manpages][1])
* `avahi-utils` gives you tools like `avahi-browse` and `avahi-publish-service` for testing. ([Debian Manpages][2])
* `libavahi-client-dev` is the development package for integrating publish/discovery into your app. ([Debian Packages][3])

## The Linux shape

It maps pretty closely to the Swift version:

* **Advertise a service**: `AvahiClient` + `AvahiEntryGroup`
* **Browse for services**: `AvahiClient` + `AvahiServiceBrowser`
* **Resolve a service**: `AvahiServiceResolver`
* **Event loop**: `AvahiSimplePoll`

Avahi’s docs show example programs for both publishing and browsing, and the simple poll API is the standard small-program event loop. `avahi_entry_group_add_service(...)` takes TXT record strings and the service is not actually announced until you call `avahi_entry_group_commit()`. When resolving a discovered service, Avahi says to pass through the interface and protocol values you got from the browser callback. ([avahi.org][4])

## Important caveat

You can absolutely announce a service in the same **mDNS style** as AirPlay, with a service type, instance name, port, and TXT record. But that only gives you the **discovery layer**. It does **not** make your Raspberry Pi a real AirPlay receiver unless the server on that port actually speaks the AirPlay protocol.

## Example service type

Use your own service type, for example:

```text
_myreceiver._tcp
```

If you want to mimic AirPlay’s metadata pattern, put things like `model=`, `protovers=`, and `features=` in TXT records.

---

# FlaschenTaschen Implementation

The mDNS service discovery for FlaschenTaschen is implemented across three components:

## 1. Server-side Publisher: `ServiceDiscoveryThread`

Located in `server/service-discovery.h` and `server/service-discovery.cc`, this class extends `ft::Thread` to manage the Avahi event loop in a background thread.

**Features:**
- Publishes `_flaschen-taschen._udp` service with display metadata
- Advertises all required TXT records: width, height, name, version, backend, platform, features, url (optional)
- Handles service name collisions by appending counter (#2, #3, etc.)
- Gracefully shuts down with Avahi cleanup

**Integration with `server/main.cc`:**
- CLI options: `--mdns enabled/disabled`, `--mdns-name <name>`, `--mdns-url <url>`
- Thread created after UDP server init, before daemonization
- Shutdown called at program exit

## 2. Client-side Discovery Library: `ft-discovery`

Located in `api/include/ft-discovery.h` and `api/lib/ft-discovery.cc`, this library provides two functions for discovering FlaschenTaschen displays:

**Features:**
- `discover_displays(timeout_ms)` - Find all available services
- `discover_display(query, timeout_ms)` - Find service by name (case-insensitive partial match)
- Returns `DisplayService` struct with full metadata and feature support checking

## 3. Discovery Tool: `ft-detect`

Located in `client/ft-detect.cc`, this command-line tool provides three modes:

**List Mode** (default):
```bash
$ ft-detect
Living Room
  Address: 192.168.1.10
  Port: 1337
  Geometry: 64x64
  ...
```

**Query Mode**:
```bash
$ ft-detect -q Polaris -f sh
FT_NAME="Polaris"
FT_HOST="192.168.1.10"
FT_PORT="1337"
FT_WIDTH="64"
FT_HEIGHT="64"
```

**Proxy Mode** (invoke client tools with auto-populated geometry):
```bash
$ ft-detect send-image photo.jpg
# Auto-discovers first display, executes:
# send-image -h 192.168.1.10 -g 64x64 photo.jpg
```

---

# Building and Testing FlaschenTaschen mDNS

## Dependencies

On Debian/Raspberry Pi:

```bash
sudo apt update
sudo apt install avahi-daemon avahi-utils libavahi-client-dev libavahi-common-dev
sudo systemctl enable --now avahi-daemon
```

## Build

From the `ft-cpp` directory:

```bash
# Build server with mDNS support
cd server && make && cd ..

# Build client discovery tool
cd client && make && cd ..
```

The Makefiles automatically detect Avahi via pkg-config and link against it.

## Testing

### 1. Start the server with mDNS enabled (terminal backend for testing):

```bash
./build/server/ft-server --mdns enabled --mdns-name Polaris -D 64x64 \
    --mdns-url "https://wiki.org/wiki/Polaris"
```

Expected output:
```
UDP-server: ready to listen on 1337
Service discovery: Polaris (64x64) port 1337 [terminal/Linux]
```

### 2. Verify service is advertised (in another terminal):

```bash
avahi-browse _flaschen-taschen._udp --resolve --terminate
```

Expected output:
```
+   eth0 IPv4 Polaris                            _flaschen-taschen._udp   local
=   eth0 IPv4 Polaris                            _flaschen-taschen._udp   local
           Hostname = [pi.local]
           Address = [192.168.1.50]
           Port = [1337]
           TXT = ["width=64" "height=64" "name=Polaris"
                  "version=1.0.0" "backend=terminal" "platform=Linux"
                  "features=0x000F"]
```

### 3. Test discovery tool:

```bash
# List all displays
./build/client/ft-detect

# Query for specific display
./build/client/ft-detect -q Polaris

# Query with shell variable output
eval $(./build/client/ft-detect -q Polaris -f sh)
echo "Display: $FT_NAME at $FT_HOST:$FT_PORT, size ${FT_WIDTH}x${FT_HEIGHT}"

# Invoke send-image with auto-discovered geometry
./build/client/ft-detect -v -q Polaris send-image demos/content/photo.jpg
```

---

# Minimal publisher in C++ (Generic Avahi Example)

This example advertises a TCP service with TXT metadata, similar in spirit to AirPlay.

```cpp
// publisher.cpp
#include <iostream>
#include <memory>
#include <string>

extern "C" {
#include <avahi-client/client.h>
#include <avahi-client/publish.h>
#include <avahi-common/error.h>
#include <avahi-common/malloc.h>
#include <avahi-common/simple-watch.h>
}

namespace {
constexpr const char* kServiceType = "_myreceiver._tcp";
constexpr uint16_t kPort = 7000;

AvahiSimplePoll* simplePoll = nullptr;
AvahiEntryGroup* entryGroup = nullptr;
std::string serviceName = "Pi Receiver";

void createServices(AvahiClient* client);

void entryGroupCallback(AvahiEntryGroup* group, AvahiEntryGroupState state, void* /* userdata */) {
    entryGroup = group;

    switch (state) {
    case AVAHI_ENTRY_GROUP_ESTABLISHED:
        std::cout << "Service established: " << serviceName << '\n';
        break;

    case AVAHI_ENTRY_GROUP_COLLISION: {
        char* newName = avahi_alternative_service_name(serviceName.c_str());
        serviceName = newName;
        avahi_free(newName);

        std::cout << "Name collision, renaming to: " << serviceName << '\n';
        createServices(avahi_entry_group_get_client(group));
        break;
    }

    case AVAHI_ENTRY_GROUP_FAILURE:
        std::cerr << "Entry group failure: "
                  << avahi_strerror(avahi_client_errno(avahi_entry_group_get_client(group)))
                  << '\n';
        avahi_simple_poll_quit(simplePoll);
        break;

    case AVAHI_ENTRY_GROUP_UNCOMMITED:
    case AVAHI_ENTRY_GROUP_REGISTERING:
        break;
    }
}

void createServices(AvahiClient* client) {
    int result = 0;

    if (entryGroup == nullptr) {
        entryGroup = avahi_entry_group_new(client, entryGroupCallback, nullptr);
        if (entryGroup == nullptr) {
            std::cerr << "Failed to create entry group: "
                      << avahi_strerror(avahi_client_errno(client)) << '\n';
            avahi_simple_poll_quit(simplePoll);
            return;
        }
    }

    if (avahi_entry_group_is_empty(entryGroup)) {
        result = avahi_entry_group_add_service(
            entryGroup,
            AVAHI_IF_UNSPEC,
            AVAHI_PROTO_UNSPEC,
            static_cast<AvahiPublishFlags>(0),
            serviceName.c_str(),
            kServiceType,
            nullptr,
            nullptr,
            kPort,
            "model=PiReceiver1,0",
            "protovers=1.0",
            "features=audio,video",
            "srcvers=1",
            nullptr
        );

        if (result == AVAHI_ERR_COLLISION) {
            char* newName = avahi_alternative_service_name(serviceName.c_str());
            serviceName = newName;
            avahi_free(newName);

            avahi_entry_group_reset(entryGroup);
            createServices(client);
            return;
        }

        if (result < 0) {
            std::cerr << "Failed to add service: " << avahi_strerror(result) << '\n';
            avahi_simple_poll_quit(simplePoll);
            return;
        }

        result = avahi_entry_group_commit(entryGroup);
        if (result < 0) {
            std::cerr << "Failed to commit entry group: " << avahi_strerror(result) << '\n';
            avahi_simple_poll_quit(simplePoll);
        }
    }
}

void clientCallback(AvahiClient* client, AvahiClientState state, void* /* userdata */) {
    switch (state) {
    case AVAHI_CLIENT_S_RUNNING:
        createServices(client);
        break;

    case AVAHI_CLIENT_FAILURE:
        std::cerr << "Client failure: "
                  << avahi_strerror(avahi_client_errno(client)) << '\n';
        avahi_simple_poll_quit(simplePoll);
        break;

    case AVAHI_CLIENT_S_COLLISION:
    case AVAHI_CLIENT_S_REGISTERING:
        if (entryGroup != nullptr) {
            avahi_entry_group_reset(entryGroup);
        }
        break;

    case AVAHI_CLIENT_CONNECTING:
        break;
    }
}
}

int main() {
    int error = 0;

    simplePoll = avahi_simple_poll_new();
    if (simplePoll == nullptr) {
        std::cerr << "Failed to create simple poll.\n";
        return 1;
    }

    AvahiClient* client = avahi_client_new(
        avahi_simple_poll_get(simplePoll),
        static_cast<AvahiClientFlags>(0),
        clientCallback,
        nullptr,
        &error
    );

    if (client == nullptr) {
        std::cerr << "Failed to create client: " << avahi_strerror(error) << '\n';
        avahi_simple_poll_free(simplePoll);
        return 1;
    }

    std::cout << "Publishing " << serviceName << " as " << kServiceType
              << " on port " << kPort << '\n';

    avahi_simple_poll_loop(simplePoll);

    if (entryGroup != nullptr) {
        avahi_entry_group_free(entryGroup);
    }
    avahi_client_free(client);
    avahi_simple_poll_free(simplePoll);
}
```

Build it:

```bash
g++ -std=c++20 publisher.cpp -o publisher $(pkg-config --cflags --libs avahi-client avahi-common)
```

Run it:

```bash
./publisher
```

---

# Minimal browser/client in C++

This browses for `_myreceiver._tcp`, resolves each hit, and prints host, port, and TXT data.

```cpp
// browser.cpp
#include <iostream>
#include <string>

extern "C" {
#include <avahi-client/client.h>
#include <avahi-client/lookup.h>
#include <avahi-common/error.h>
#include <avahi-common/malloc.h>
#include <avahi-common/simple-watch.h>
#include <avahi-common/strlst.h>
}

namespace {
constexpr const char* kServiceType = "_myreceiver._tcp";

AvahiSimplePoll* simplePoll = nullptr;
AvahiServiceBrowser* browser = nullptr;

void printTxt(AvahiStringList* txt) {
    for (AvahiStringList* item = txt; item != nullptr; item = avahi_string_list_get_next(item)) {
        char* rendered = avahi_string_list_to_string(item);
        if (rendered != nullptr) {
            std::cout << "  TXT: " << rendered << '\n';
            avahi_free(rendered);
        }
    }
}

void resolveCallback(
    AvahiServiceResolver* resolver,
    AVAHI_GCC_UNUSED AvahiIfIndex interface,
    AVAHI_GCC_UNUSED AvahiProtocol protocol,
    AvahiResolverEvent event,
    const char* name,
    const char* type,
    const char* domain,
    const char* hostName,
    const AvahiAddress* address,
    uint16_t port,
    AvahiStringList* txt,
    AVAHI_GCC_UNUSED AvahiLookupResultFlags flags,
    void* /* userdata */
) {
    if (event == AVAHI_RESOLVER_FOUND) {
        char addressBuffer[AVAHI_ADDRESS_STR_MAX]{};
        avahi_address_snprint(addressBuffer, sizeof(addressBuffer), address);

        std::cout << "Resolved:\n";
        std::cout << "  Name: " << name << '\n';
        std::cout << "  Type: " << type << '\n';
        std::cout << "  Domain: " << domain << '\n';
        std::cout << "  Host: " << hostName << '\n';
        std::cout << "  Address: " << addressBuffer << '\n';
        std::cout << "  Port: " << port << '\n';
        printTxt(txt);
    } else {
        std::cerr << "Resolve failed for " << name << '\n';
    }

    avahi_service_resolver_free(resolver);
}

void browseCallback(
    AvahiServiceBrowser* browserHandle,
    AvahiIfIndex interface,
    AvahiProtocol protocol,
    AvahiBrowserEvent event,
    const char* name,
    const char* type,
    const char* domain,
    AVAHI_GCC_UNUSED AvahiLookupResultFlags flags,
    void* /* userdata */
) {
    AvahiClient* client = avahi_service_browser_get_client(browserHandle);

    switch (event) {
    case AVAHI_BROWSER_NEW:
        std::cout << "Discovered: " << name << '.' << type << '.' << domain << '\n';

        if (avahi_service_resolver_new(
                client,
                interface,
                protocol,
                name,
                type,
                domain,
                AVAHI_PROTO_UNSPEC,
                static_cast<AvahiLookupFlags>(0),
                resolveCallback,
                nullptr
            ) == nullptr) {
            std::cerr << "Failed to resolve " << name << ": "
                      << avahi_strerror(avahi_client_errno(client)) << '\n';
        }
        break;

    case AVAHI_BROWSER_REMOVE:
        std::cout << "Removed: " << name << '.' << type << '.' << domain << '\n';
        break;

    case AVAHI_BROWSER_FAILURE:
        std::cerr << "Browser failure: "
                  << avahi_strerror(avahi_client_errno(client)) << '\n';
        avahi_simple_poll_quit(simplePoll);
        break;

    case AVAHI_BROWSER_CACHE_EXHAUSTED:
    case AVAHI_BROWSER_ALL_FOR_NOW:
        break;
    }
}

void clientCallback(AvahiClient* client, AvahiClientState state, void* /* userdata */) {
    if (state == AVAHI_CLIENT_S_RUNNING) {
        browser = avahi_service_browser_new(
            client,
            AVAHI_IF_UNSPEC,
            AVAHI_PROTO_UNSPEC,
            kServiceType,
            nullptr,
            static_cast<AvahiLookupFlags>(0),
            browseCallback,
            nullptr
        );

        if (browser == nullptr) {
            std::cerr << "Failed to create browser: "
                      << avahi_strerror(avahi_client_errno(client)) << '\n';
            avahi_simple_poll_quit(simplePoll);
        }
    } else if (state == AVAHI_CLIENT_FAILURE) {
        std::cerr << "Client failure: " << avahi_strerror(avahi_client_errno(client)) << '\n';
        avahi_simple_poll_quit(simplePoll);
    }
}
}

int main() {
    int error = 0;

    simplePoll = avahi_simple_poll_new();
    if (simplePoll == nullptr) {
        std::cerr << "Failed to create simple poll.\n";
        return 1;
    }

    AvahiClient* client = avahi_client_new(
        avahi_simple_poll_get(simplePoll),
        static_cast<AvahiClientFlags>(0),
        clientCallback,
        nullptr,
        &error
    );

    if (client == nullptr) {
        std::cerr << "Failed to create client: " << avahi_strerror(error) << '\n';
        avahi_simple_poll_free(simplePoll);
        return 1;
    }

    std::cout << "Browsing for " << kServiceType << '\n';
    avahi_simple_poll_loop(simplePoll);

    if (browser != nullptr) {
        avahi_service_browser_free(browser);
    }
    avahi_client_free(client);
    avahi_simple_poll_free(simplePoll);
}
```

Build it:

```bash
g++ -std=c++20 browser.cpp -o browser $(pkg-config --cflags --libs avahi-client avahi-common)
```

Run it:

```bash
./browser
```

---

# Command-line testing first

Before writing code, you can prove your setup with Avahi’s tools.

Publish a temporary service:

```bash
avahi-publish-service "Pi Receiver" _myreceiver._tcp 7000 model=PiReceiver1,0 protovers=1.0 features=audio,video
```

Browse for it:

```bash
avahi-browse _myreceiver._tcp --resolve --terminate
```

Those tools are the Linux equivalent of using `dns-sd` on macOS: `avahi-publish-service` registers a service, and `avahi-browse` browses and optionally resolves it. ([Debian Manpages][2])

---

# Static service file option

If you want the Pi to advertise a service without writing code at all, Avahi also supports static XML service files under `/etc/avahi/services/*.service`. Debian documents those as static DNS-SD service definitions. ([Debian Manpages][5])

Example:

```xml
<!-- /etc/avahi/services/myreceiver.service -->
<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">

<service-group>
  <name replace-wildcards="yes">Pi Receiver</name>

  <service>
    <type>_myreceiver._tcp</type>
    <port>7000</port>
    <txt-record>model=PiReceiver1,0</txt-record>
    <txt-record>protovers=1.0</txt-record>
    <txt-record>features=audio,video</txt-record>
  </service>
</service-group>
```

Then restart Avahi:

```bash
sudo systemctl restart avahi-daemon
```

This is useful when the service metadata is fixed and your actual TCP server is separate.

---

# Debian / Pi notes that matter

If service publication does not work, check `avahi-daemon.conf`. Debian’s man page notes:

* `disable-user-service-publishing=no` is required for apps to publish services.
* `publish-addresses=yes` is recommended, and specifically required if you plan to register local services. ([Debian Manpages][6])

Also, remember mDNS is link-local. Discovery normally stays on the local subnet unless you deliberately configure a reflector. Avahi supports a reflector mode in `avahi-daemon.conf`, but that is a network-wide decision, not an app-level one. ([Debian Manpages][6])

---

# Mapping from the Swift version to C++ with Avahi

Swift (Apple):

* `NWListener` → advertise
* `NWBrowser` → discover
* `NWConnection` → connect

C++ with Avahi (Linux/Raspberry Pi):

* `AvahiEntryGroup` (in `ServiceDiscoveryThread`) → advertise
* `AvahiServiceBrowser` (in `ft-discovery.cc`) → discover
* `AvahiServiceResolver` (in `ft-discovery.cc`) → resolve to host/port/TXT
* UDP server (existing `udp-server.cc`) → actual FlaschenTaschen protocol connection

That last line is the key difference: **Avahi handles discovery, not your application protocol**.

---

# TXT Record Specification

FlaschenTaschen advertises the following TXT records (see `TXT-spec.md` for full details):

**Required:**
- `width=<int>` - Display width in pixels
- `height=<int>` - Display height in pixels
- `name=<string>` - Display name (human-readable)
- `version=<semver>` - Server version (e.g., "1.0.0")
- `backend=<string>` - Backend type (ft, rgb-matrix, terminal)
- `platform=<string>` - Platform (Linux, macOS, etc.)
- `features=<hex>` - 16-bit feature bitmask (0x000F for all current features)

**Optional:**
- `url=<string>` - HTTP URL for documentation or web interface

**Feature Bitmask:**
- `0x0001` - Multi-packet support
- `0x0002` - Multi-layer support
- `0x0004` - Offset/partial updates
- `0x0008` - Layer timeout/garbage collection

The C++ server advertises `features=0x000F` (all features) on actual hardware.

---

# Notes

The implementation in `server/service-discovery.*` and `api/lib/ft-discovery.cc` encapsulates all Avahi complexity. The main server simply creates a `ServiceDiscoveryThread` with display metadata, and clients use `discover_displays()` and `discover_display()` without worrying about Avahi details.

For more details on the protocol and TXT record format, see:
- `TXT-spec.md` - Complete TXT record specification
- `mDNS-implementation-cpp.md` - Detailed implementation design

[1]: https://manpages.debian.org/avahi-daemon "avahi-daemon(8) — avahi-daemon — Debian trixie — Debian Manpages"
[2]: https://manpages.debian.org/testing/avahi-utils/avahi-browse.1.en.html "avahi-browse(1) — avahi-utils — Debian testing — Debian Manpages"
[3]: https://packages.debian.org/bookworm/libavahi-client-dev "Debian -- Details of package libavahi-client-dev in bookworm"
[4]: https://avahi.org/doxygen/html/client-publish-service_8c-example.html "avahi: client-publish-service.c"
[5]: https://manpages.debian.org/testing/avahi-daemon/avahi.service.5.en.html "avahi.service(5) — avahi-daemon — Debian testing — Debian Manpages"
[6]: https://manpages.debian.org/testing/avahi-daemon/avahi-daemon.conf.5.en.html "avahi-daemon.conf(5) — avahi-daemon — Debian testing — Debian Manpages"
