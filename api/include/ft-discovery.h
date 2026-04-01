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

#ifndef FT_DISCOVERY_H
#define FT_DISCOVERY_H

// mDNS service discovery is Linux-only (Avahi-based)
#ifdef __linux__

#include <stdint.h>
#include <string>
#include <vector>

struct DisplayService {
    std::string instance_name;  // e.g., "Polaris"
    std::string hostname;       // e.g., "pi.local"
    std::string address;        // IP address, e.g., "192.168.1.50"
    uint16_t port;              // 1337
    uint16_t width;             // From TXT: width
    uint16_t height;            // From TXT: height
    std::string name;           // From TXT: display name
    std::string url;            // From TXT: HTTP URL (optional)
    std::string version;        // From TXT: server version (e.g., "1.0.0")
    std::string backend;        // From TXT: backend type (e.g., "ft", "rgb-matrix", "terminal")
    std::string platform;       // From TXT: platform (e.g., "Linux", "macOS")
    uint16_t features;          // From TXT: feature bitmask (16-bit, 0-65535)

    // Helper: check if feature is supported
    bool has_feature(uint16_t feature_bit) const {
        return (features & feature_bit) != 0;
    }
};

/**
 * Discover all available FlaschenTaschen displays.
 * Blocks until discovery completes (waits for cache exhaustion).
 *
 * @param timeout_ms  Max wait time in milliseconds
 * @return Vector of discovered DisplayService instances
 */
std::vector<DisplayService> discover_displays(int timeout_ms = 5000);

/**
 * Discover a single display by instance name (case-insensitive partial match).
 *
 * @param query       Display name or partial name
 * @param timeout_ms  Max wait time in milliseconds
 * @return First matching DisplayService, or empty struct on not found
 */
DisplayService discover_display(const std::string& query, int timeout_ms = 5000);

#endif // __linux__

#endif // FT_DISCOVERY_H
