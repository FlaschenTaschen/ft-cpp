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

#include "service-discovery.h"

#include <avahi-client/client.h>
#include <avahi-client/publish.h>
#include <avahi-common/alternative.h>
#include <avahi-common/error.h>
#include <avahi-common/simple-watch.h>

#include <stdio.h>
#include <string.h>
#include <stdint.h>

ServiceDiscoveryThread::ServiceDiscoveryThread(
    const char* instance_name,
    uint16_t port,
    uint16_t width,
    uint16_t height,
    const char* url,
    const char* version,
    const char* backend,
    const char* platform,
    uint16_t features)
    : simple_poll_(NULL),
      client_(NULL),
      entry_group_(NULL),
      instance_name_(instance_name),
      port_(port),
      width_(width),
      height_(height),
      url_(url ? url : ""),
      version_(version),
      backend_(backend),
      platform_(platform),
      features_(features),
      shutdown_requested_(false) {
}

ServiceDiscoveryThread::~ServiceDiscoveryThread() {
    Shutdown();
}

void ServiceDiscoveryThread::Run() {
    int error;

    // Create a simple poll object
    simple_poll_ = avahi_simple_poll_new();
    if (!simple_poll_) {
        fprintf(stderr, "avahi_simple_poll_new() failed\n");
        return;
    }

    // Create client
    client_ = avahi_client_new(avahi_simple_poll_get(simple_poll_),
                               static_cast<AvahiClientFlags>(0),
                               ClientCallback,
                               this,
                               &error);
    if (!client_) {
        fprintf(stderr, "avahi_client_new() failed: %s\n",
                avahi_strerror(error));
        avahi_simple_poll_free(simple_poll_);
        simple_poll_ = NULL;
        return;
    }

    // Run the event loop
    while (!shutdown_requested_) {
        if (avahi_simple_poll_iterate(simple_poll_, 100) != 0) {
            break;
        }
    }

    // Cleanup
    if (client_) {
        avahi_client_free(client_);
        client_ = NULL;
    }

    if (simple_poll_) {
        avahi_simple_poll_free(simple_poll_);
        simple_poll_ = NULL;
    }
}

void ServiceDiscoveryThread::Shutdown() {
    shutdown_requested_ = true;
}

void ServiceDiscoveryThread::ClientCallback(AvahiClient* c,
                                           AvahiClientState state,
                                           void* userdata) {
    ServiceDiscoveryThread* self = static_cast<ServiceDiscoveryThread*>(userdata);
    self->HandleClientState(state);
}

void ServiceDiscoveryThread::HandleClientState(AvahiClientState state) {
    int error;

    switch (state) {
    case AVAHI_CLIENT_S_RUNNING:
        // Server has startup successfully and registered its host
        // name on the network, so it's time to create our services
        if (!entry_group_) {
            CreateServices();
        }
        break;

    case AVAHI_CLIENT_FAILURE:
        fprintf(stderr, "Client failure: %s\n",
                avahi_strerror(avahi_client_errno(client_)));
        shutdown_requested_ = true;
        break;

    case AVAHI_CLIENT_S_COLLISION:
        // Let's drop our registered services. When the server is back
        // in AVAHI_SERVER_RUNNING state we will register them again.
        if (entry_group_) {
            avahi_entry_group_reset(entry_group_);
        }
        break;

    case AVAHI_CLIENT_S_REGISTERING:
        // The server records are now being established. This
        // might result in a routable local service.
        break;

    case AVAHI_CLIENT_CONNECTING:
        break;
    }
}

void ServiceDiscoveryThread::CreateServices() {
    int error;

    // If this is the first time we're called, let's create a new
    // entry group if necessary
    if (!entry_group_) {
        if (!(entry_group_ = avahi_entry_group_new(client_,
                                                     EntryGroupCallback,
                                                     this))) {
            fprintf(stderr, "avahi_entry_group_new() failed: %s\n",
                    avahi_strerror(avahi_client_errno(client_)));
            return;
        }
    }

    // Build TXT record strings
    char width_str[32];
    char height_str[32];
    char features_str[16];
    snprintf(width_str, sizeof(width_str), "width=%d", width_);
    snprintf(height_str, sizeof(height_str), "height=%d", height_);
    snprintf(features_str, sizeof(features_str), "features=0x%04x", features_);

    // Build version string
    std::string version_str = "version=" + version_;
    std::string backend_str = "backend=" + backend_;
    std::string platform_str = "platform=" + platform_;
    std::string name_str = "name=" + instance_name_;
    std::string url_str = url_.empty() ? "" : "url=" + url_;

    // Add service with TXT records
    // Note: We use varargs to add TXT records
    if (url_.empty()) {
        error = avahi_entry_group_add_service(
            entry_group_,
            AVAHI_IF_UNSPEC,
            AVAHI_PROTO_UNSPEC,
            static_cast<AvahiPublishFlags>(0),
            instance_name_.c_str(),
            "_flaschen-taschen._udp",
            NULL,
            NULL,
            port_,
            width_str,
            height_str,
            name_str.c_str(),
            version_str.c_str(),
            backend_str.c_str(),
            platform_str.c_str(),
            features_str.c_str(),
            NULL);
    } else {
        error = avahi_entry_group_add_service(
            entry_group_,
            AVAHI_IF_UNSPEC,
            AVAHI_PROTO_UNSPEC,
            static_cast<AvahiPublishFlags>(0),
            instance_name_.c_str(),
            "_flaschen-taschen._udp",
            NULL,
            NULL,
            port_,
            width_str,
            height_str,
            name_str.c_str(),
            version_str.c_str(),
            backend_str.c_str(),
            platform_str.c_str(),
            features_str.c_str(),
            url_str.c_str(),
            NULL);
    }

    if (error < 0) {
        fprintf(stderr, "Failed to add service: %s\n",
                avahi_strerror(error));
        return;
    }

    error = avahi_entry_group_commit(entry_group_);
    if (error < 0) {
        fprintf(stderr, "Failed to commit entry group: %s\n",
                avahi_strerror(error));
        avahi_entry_group_free(entry_group_);
        entry_group_ = NULL;
        return;
    }
}

void ServiceDiscoveryThread::EntryGroupCallback(AvahiEntryGroup* g,
                                               AvahiEntryGroupState state,
                                               void* userdata) {
    ServiceDiscoveryThread* self = static_cast<ServiceDiscoveryThread*>(userdata);
    self->HandleEntryGroupState(state);
}

void ServiceDiscoveryThread::HandleEntryGroupState(AvahiEntryGroupState state) {
    switch (state) {
    case AVAHI_ENTRY_GROUP_ESTABLISHED:
        // The entry group has been established successfully
        break;

    case AVAHI_ENTRY_GROUP_COLLISION:
        // A service name collision happened. Let's pick a new name
        {
            char* new_name = avahi_alternative_service_name(instance_name_.c_str());
            instance_name_ = new_name;
            avahi_free(new_name);

            fprintf(stderr, "Service name collision, renaming service to '%s'\n",
                    instance_name_.c_str());

            avahi_entry_group_reset(entry_group_);
            CreateServices();
        }
        break;

    case AVAHI_ENTRY_GROUP_FAILURE:
        fprintf(stderr, "Entry group failure: %s\n",
                avahi_strerror(avahi_client_errno(client_)));
        shutdown_requested_ = true;
        break;

    case AVAHI_ENTRY_GROUP_UNCOMMITTED:
    case AVAHI_ENTRY_GROUP_REGISTERING:
        break;
    }
}

#endif // __APPLE__
