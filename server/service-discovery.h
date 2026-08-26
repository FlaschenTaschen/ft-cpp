// -*- mode: c++; c-basic-offset: 4; indent-tabs-mode: nil; -*-
// This program is free software; you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation version 2.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://gnu.org/licenses/gpl-2.0.txt>

#ifndef FT_SERVICE_DISCOVERY_H
#define FT_SERVICE_DISCOVERY_H

// mDNS service discovery is Linux-only (Avahi-based)
#ifdef __linux__

#include <stdint.h>
#include <string>
#include "ft-thread.h"

#include <avahi-client/client.h>
#include <avahi-common/defs.h>

namespace ft {
class Mutex;
}

struct AvahiSimplePoll;
struct AvahiClient;
struct AvahiEntryGroup;

// ServiceDiscoveryThread extends ft::Thread and runs the Avahi event loop
// Bits in the `features` TXT field; see docs/TXT-spec.md. Clients are required
// to ignore bits they do not know, so adding one is backwards compatible.
static const uint16_t kFeatureMultiPacket     = 0x0001;
static const uint16_t kFeatureMultiLayer      = 0x0002;
static const uint16_t kFeatureOffset          = 0x0004;
static const uint16_t kFeatureLayerTimeout    = 0x0008;
static const uint16_t kFeatureDisplayControl  = 0x0010;

static const uint16_t kFeaturesWireProtocol =
    kFeatureMultiPacket | kFeatureMultiLayer | kFeatureOffset |
    kFeatureLayerTimeout;

class ServiceDiscoveryThread : public ft::Thread {
public:
    /**
     * Create a service discovery thread.
     *
     * @param instance_name  Display name (e.g., "Polaris")
     * @param port           UDP server port (typically 1337)
     * @param width          Display width in pixels
     * @param height         Display height in pixels
     * @param url            Optional HTTP URL (e.g., "https://wiki.org/wiki/Polaris", may be "")
     * @param version        Server version in semver format (e.g., "1.0.0")
     * @param backend        Backend type (e.g., "ft", "rgb-matrix", "terminal")
     * @param platform       Platform name (e.g., "Linux", "macOS")
     * @param features       Bitmask of supported features (16-bit, typically 0x000F for all current features)
     */
    ServiceDiscoveryThread(const char* instance_name,
                          uint16_t port,
                          uint16_t width,
                          uint16_t height,
                          const char* url,
                          const char* ui,
                          const char* version,
                          const char* backend,
                          const char* platform,
                          uint16_t features = kFeaturesWireProtocol);
    virtual ~ServiceDiscoveryThread();

    // Inherited from ft::Thread
    virtual void Run();

    // Cleanly shut down the thread
    void Shutdown();

private:
    // Avahi-related state
    AvahiSimplePoll* simple_poll_;
    AvahiClient* client_;
    AvahiEntryGroup* entry_group_;

    // Display metadata
    std::string instance_name_;
    uint16_t port_;
    uint16_t width_;
    uint16_t height_;
    std::string url_;
    std::string ui_;
    std::string version_;
    std::string backend_;
    std::string platform_;
    uint16_t features_;  // 16-bit bitmask for future feature expansion

    // Flag to signal shutdown
    volatile bool shutdown_requested_;

    // Callbacks for Avahi (static, with userdata pointing to 'this')
    static void ClientCallback(AvahiClient* c,
                              AvahiClientState state,
                              void* userdata);
    static void EntryGroupCallback(AvahiEntryGroup* g,
                                   AvahiEntryGroupState state,
                                   void* userdata);

    // Helper methods
    void CreateServices();
    void HandleClientState(AvahiClientState state);
    void HandleEntryGroupState(AvahiEntryGroupState state);
};

#endif // __linux__

#endif // FT_SERVICE_DISCOVERY_H
