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

// mDNS service discovery is Linux-only (Avahi-based)
#ifdef __linux__

#include "ft-discovery.h"

#include <avahi-client/client.h>
#include <avahi-client/lookup.h>
#include <avahi-common/error.h>
#include <avahi-common/simple-watch.h>

#include <algorithm>
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>

namespace {

struct DiscoveryContext {
    std::vector<DisplayService> services;
    AvahiSimplePoll* simple_poll;
    bool all_for_now;
    int timeout_ms;
    long start_time_ms;

    DiscoveryContext()
        : simple_poll(NULL), all_for_now(false), timeout_ms(0), start_time_ms(0) {}
};

// Case-insensitive substring match
bool case_insensitive_contains(const std::string& haystack,
                               const std::string& needle) {
    std::string h_lower = haystack;
    std::string n_lower = needle;

    for (auto& c : h_lower) c = std::tolower(c);
    for (auto& c : n_lower) c = std::tolower(c);

    return h_lower.find(n_lower) != std::string::npos;
}

// Parse TXT record value for a given key
std::string parse_txt_value(AvahiStringList* txt, const char* key) {
    size_t key_len = strlen(key);
    char key_with_equals[256];
    snprintf(key_with_equals, sizeof(key_with_equals), "%s=", key);

    for (AvahiStringList* item = txt; item != NULL;
         item = avahi_string_list_get_next(item)) {
        char* str = reinterpret_cast<char*>(item->text);
        if (strncmp(str, key_with_equals, strlen(key_with_equals)) == 0) {
            return std::string(str + strlen(key_with_equals));
        }
    }
    return "";
}

// Resolve callback
static void resolve_callback(AvahiServiceResolver* r,
                             AvahiIfIndex interface,
                             AvahiProtocol protocol,
                             AvahiResolverEvent event,
                             const char* name,
                             const char* type,
                             const char* domain,
                             const char* host_name,
                             const AvahiAddress* address,
                             uint16_t port,
                             AvahiStringList* txt,
                             AvahiLookupResultFlags flags,
                             void* userdata) {
    DiscoveryContext* ctx = static_cast<DiscoveryContext*>(userdata);

    switch (event) {
    case AVAHI_RESOLVER_FOUND: {
        DisplayService service;
        service.instance_name = name;
        service.hostname = host_name;
        service.port = port;

        // Convert IP address
        char address_str[AVAHI_ADDRESS_STR_MAX];
        avahi_address_snprint(address_str, sizeof(address_str), address);
        service.address = address_str;

        // Parse TXT records
        service.width = std::atoi(parse_txt_value(txt, "width").c_str());
        service.height = std::atoi(parse_txt_value(txt, "height").c_str());
        service.name = parse_txt_value(txt, "name");
        service.url = parse_txt_value(txt, "url");
        service.version = parse_txt_value(txt, "version");
        service.backend = parse_txt_value(txt, "backend");
        service.platform = parse_txt_value(txt, "platform");

        std::string features_str = parse_txt_value(txt, "features");
        service.features = static_cast<uint16_t>(strtoul(features_str.c_str(), NULL, 16));

        ctx->services.push_back(service);
        break;
    }

    case AVAHI_RESOLVER_FAILURE:
        // Just continue, this service couldn't be resolved
        break;
    }

    avahi_service_resolver_free(r);
}

// Browse callback
static void browse_callback(AvahiServiceBrowser* b,
                            AvahiIfIndex interface,
                            AvahiProtocol protocol,
                            AvahiBrowserEvent event,
                            const char* name,
                            const char* type,
                            const char* domain,
                            AvahiLookupResultFlags flags,
                            void* userdata) {
    AvahiClient* c = avahi_service_browser_get_client(b);
    DiscoveryContext* ctx = static_cast<DiscoveryContext*>(userdata);

    switch (event) {
    case AVAHI_BROWSER_NEW: {
        // Resolve the service
        AvahiServiceResolver* resolver =
            avahi_service_resolver_new(c, interface, protocol, name, type, domain,
                                       AVAHI_PROTO_UNSPEC, static_cast<AvahiLookupFlags>(0),
                                       resolve_callback, ctx);
        if (!resolver) {
            fprintf(stderr, "Failed to create resolver: %s\n",
                    avahi_strerror(avahi_client_errno(c)));
        }
        break;
    }

    case AVAHI_BROWSER_REMOVE:
        // A service was removed - we could track this, but for now just ignore
        break;

    case AVAHI_BROWSER_CACHE_EXHAUSTED:
        ctx->all_for_now = true;
        break;

    case AVAHI_BROWSER_ALL_FOR_NOW:
        ctx->all_for_now = true;
        break;

    case AVAHI_BROWSER_FAILURE:
        fprintf(stderr, "Browse failure: %s\n",
                avahi_strerror(avahi_client_errno(c)));
        ctx->all_for_now = true;
        break;
    }
}

// Get current time in milliseconds
long get_time_ms() {
#ifdef _WIN32
    return GetTickCount();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (ts.tv_sec * 1000) + (ts.tv_nsec / 1000000);
#endif
}

}  // namespace

std::vector<DisplayService> discover_displays(int timeout_ms) {
    DiscoveryContext ctx;
    ctx.timeout_ms = timeout_ms;
    ctx.start_time_ms = get_time_ms();

    int error;
    AvahiSimplePoll* simple_poll = avahi_simple_poll_new();
    if (!simple_poll) {
        fprintf(stderr, "Failed to create simple poll\n");
        return ctx.services;
    }
    ctx.simple_poll = simple_poll;

    AvahiClient* client = avahi_client_new(avahi_simple_poll_get(simple_poll),
                                           static_cast<AvahiClientFlags>(0),
                                           NULL, &ctx, &error);
    if (!client) {
        fprintf(stderr, "Failed to create client: %s\n", avahi_strerror(error));
        avahi_simple_poll_free(simple_poll);
        return ctx.services;
    }

    AvahiServiceBrowser* sb = avahi_service_browser_new(
        client, AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC, "_flaschen-taschen._udp", NULL,
        static_cast<AvahiLookupFlags>(0), browse_callback, &ctx);

    if (!sb) {
        fprintf(stderr, "Failed to create service browser: %s\n",
                avahi_strerror(avahi_client_errno(client)));
        avahi_client_free(client);
        avahi_simple_poll_free(simple_poll);
        return ctx.services;
    }

    // Run the event loop with timeout
    while (!ctx.all_for_now) {
        long elapsed = get_time_ms() - ctx.start_time_ms;
        if (elapsed > timeout_ms) {
            break;
        }

        int remaining = timeout_ms - elapsed;
        if (avahi_simple_poll_iterate(simple_poll, remaining) != 0) {
            break;
        }
    }

    avahi_service_browser_free(sb);
    avahi_client_free(client);
    avahi_simple_poll_free(simple_poll);

    return ctx.services;
}

DisplayService discover_display(const std::string& query, int timeout_ms) {
    std::vector<DisplayService> all_services = discover_displays(timeout_ms);

    // Find first service matching query (case-insensitive substring match)
    for (size_t i = 0; i < all_services.size(); ++i) {
        const DisplayService& service = all_services[i];
        if (case_insensitive_contains(service.name, query) ||
            case_insensitive_contains(service.instance_name, query)) {
            return service;
        }
    }

    // Return empty service if not found
    DisplayService empty;
    empty.port = 0;
    empty.width = 0;
    empty.height = 0;
    empty.features = 0;
    return empty;
}

#endif // __linux__
