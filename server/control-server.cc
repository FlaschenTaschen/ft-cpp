// -*- mode: c++; c-basic-offset: 4; indent-tabs-mode: nil; -*-
//
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
//
// A control channel for state that belongs to the display as a whole rather
// than to any one client: how bright it is, whether it is showing anything,
// and clearing it out.
//
// Deliberately NOT part of the pixel protocol. That one is meant to stay
// simple enough that an ESP8266 can implement all of it, and a display that
// answers questions is a different kind of thing from one that accepts frames.
// So this is a separate socket speaking lines of text, which also means the
// entire client is `nc -U`:
//
//   $ echo get | nc -U /run/ft/control.sock
//   brightness 100
//   blanked 0
//   ...
//   $ echo "brightness 40" | nc -U /run/ft/control.sock
//   ok
//
// One command per connection, like HTTP/1.0 and for the same reason: there is
// then no session to keep, no half-read line to remember, and a client that
// connects and says nothing is bounded by the receive timeout rather than
// hanging around. With two consumers on the same machine, a connect per
// command is cheaper than any of the machinery needed to avoid it.
//
// A Unix socket rather than a port on loopback, for one specific reason: a
// page in any browser on the network can be made to POST at 127.0.0.1. It
// cannot read the reply, but the bytes arrive, and a line-oriented protocol
// would happily find "blank on" in the middle of them.

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "composite-flaschen-taschen.h"
#include "ft-thread.h"
#include "led-flaschen-taschen.h"
#include "servers.h"

// Set by whichever server front-end installed the handler; see udp-server.cc.
extern volatile bool interrupt_received;

namespace {
static const int kMaxCommand = 256;      // longest line we will look at
static const int kIoTimeoutSeconds = 2;  // per read and per write
static const int kAcceptPollMillis = 250;

// Bumped by nothing: it identifies this run of the daemon, so that a client
// keeping desired state can notice a restart with one cheap read instead of
// guessing from symptoms.
static long generation = 0;

static int listen_socket = -1;
static char socket_path[108] = {0};      // sun_path is this big

class ControlServer : public ft::Thread {
public:
    ControlServer(CompositeFlaschenTaschen *display,
                  ServerFlaschenTaschen *backend, ft::Mutex *mutex)
        : display_(display), backend_(backend), mutex_(mutex) {}

    virtual void Run() {
        // Signals are the main thread's business. Without this, a SIGTERM
        // delivered here would be swallowed while we sit in accept() and the
        // shutdown the main loop is waiting for would never be noticed.
        sigset_t block_all;
        sigfillset(&block_all);
        pthread_sigmask(SIG_BLOCK, &block_all, NULL);

        while (!interrupt_received) {
            const int fd = accept(listen_socket, NULL, NULL);
            if (fd < 0) {
                // The accept timeout is what lets us poll interrupt_received.
                if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
                if (errno == EINTR) continue;
                if (interrupt_received) break;
                perror("control-server: accept");
                continue;
            }
            Serve(fd);
            close(fd);
        }
    }

private:
    void Serve(int fd) {
        struct timeval tv;
        tv.tv_sec = kIoTimeoutSeconds;
        tv.tv_usec = 0;
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

        char buffer[kMaxCommand + 1];
        const ssize_t len = read(fd, buffer, kMaxCommand);
        if (len <= 0) return;            // gone, or said nothing in time
        buffer[len] = '\0';
        char *end = strpbrk(buffer, "\r\n");
        if (end) *end = '\0';

        char reply[512];
        Dispatch(buffer, reply, sizeof(reply));
        // Best effort: a client that hung up before reading is not our
        // problem, and MSG_NOSIGNAL keeps it from being fatal.
        send(fd, reply, strlen(reply), MSG_NOSIGNAL);
    }

    // Everything below runs with the mutex held for as short as possible, and
    // never across a syscall on the client's socket. Holding it around a read
    // would let anything that can open the socket stop the display simply by
    // connecting and going quiet.
    void Dispatch(const char *line, char *reply, size_t reply_size) {
        const char *arg = strchr(line, ' ');
        while (arg && *arg == ' ') ++arg;

        if (strcmp(line, "get") == 0) {
            int brightness, width, height;
            bool blanked, dimmer;
            {
                ft::MutexLock l(mutex_);
                brightness = backend_->global_brightness();
                blanked = backend_->blanked();
                dimmer = backend_->SupportsGlobalDimmer();
                width = display_->width();
                height = display_->height();
            }
            snprintf(reply, reply_size,
                     "brightness %d\n"
                     "blanked %d\n"
                     "dimmer %d\n"
                     "width %d\n"
                     "height %d\n"
                     "generation %ld\n"
                     "\n",
                     brightness, blanked ? 1 : 0, dimmer ? 1 : 0,
                     width, height, generation);
            return;
        }

        if (strncmp(line, "brightness", 10) == 0 && arg) {
            char *parse_end = NULL;
            const long percent = strtol(arg, &parse_end, 10);
            if (parse_end == arg || percent < 1 || percent > 100) {
                snprintf(reply, reply_size,
                         "err brightness takes 1..100\n");
                return;
            }
            bool ok;
            {
                ft::MutexLock l(mutex_);
                ok = backend_->SetGlobalBrightness((int)percent);
            }
            snprintf(reply, reply_size, ok
                     ? "ok\n" : "err this display cannot dim\n");
            return;
        }

        if (strncmp(line, "blank", 5) == 0 && arg) {
            bool on;
            if (strcmp(arg, "on") == 0 || strcmp(arg, "1") == 0) {
                on = true;
            } else if (strcmp(arg, "off") == 0 || strcmp(arg, "0") == 0) {
                on = false;
            } else {
                snprintf(reply, reply_size, "err blank takes on or off\n");
                return;
            }
            bool ok;
            {
                ft::MutexLock l(mutex_);
                ok = backend_->SetBlanked(on);
            }
            snprintf(reply, reply_size, ok
                     ? "ok\n" : "err this display cannot blank\n");
            return;
        }

        if (strcmp(line, "wipe") == 0) {
            ft::MutexLock l(mutex_);
            display_->Clear();
            snprintf(reply, reply_size, "ok\n");
            return;
        }

        snprintf(reply, reply_size, "err unknown command\n");
    }

    CompositeFlaschenTaschen *const display_;
    ServerFlaschenTaschen *const backend_;
    ft::Mutex *const mutex_;
};

static ControlServer *server = NULL;
}  // namespace

bool control_server_init(const char *path) {
    if (path == NULL || *path == '\0') return false;
    if (strlen(path) >= sizeof(socket_path)) {
        fprintf(stderr, "control-server: path too long: %s\n", path);
        return false;
    }
    strcpy(socket_path, path);

    if ((listen_socket = socket(AF_UNIX, SOCK_STREAM, 0)) < 0) {
        perror("control-server: socket");
        return false;
    }

    // bind() on an existing path is EADDRINUSE, unconditionally, and this
    // machine is usually stopped by having its power removed. Take the stale
    // one out of the way rather than requiring a tidy shutdown to have
    // happened.
    unlink(socket_path);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strcpy(addr.sun_path, socket_path);
    if (bind(listen_socket, (struct sockaddr *) &addr, sizeof(addr)) < 0) {
        perror("control-server: bind");
        close(listen_socket);
        listen_socket = -1;
        return false;
    }
    if (listen(listen_socket, 8) < 0) {
        perror("control-server: listen");
        close(listen_socket);
        listen_socket = -1;
        return false;
    }

    // World-writable, and called out rather than hidden: any local process can
    // dim or blank the display. That is not a step down from where we started
    // -- anything on the network can already paint the whole display black by
    // sending frames, unauthenticated, all day. The reason this is a socket in
    // the filesystem is to keep browsers from reaching it, not to keep local
    // users out. Do it while we still can; after the privilege drop this
    // process owns nothing.
    if (chmod(socket_path, 0666) < 0) {
        perror("control-server: chmod");
    }

    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = kAcceptPollMillis * 1000;
    setsockopt(listen_socket, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    generation = (long) time(NULL);
    fprintf(stderr, "Control-server: ready on %s\n", socket_path);
    return true;
}

void control_server_run_thread(CompositeFlaschenTaschen *display,
                               ServerFlaschenTaschen *backend,
                               ft::Mutex *mutex) {
    if (listen_socket < 0 || server != NULL) return;
    server = new ControlServer(display, backend, mutex);
    // Same affinity as the frame pusher: off the core driving the panel.
    server->Start(0, (1 << 0) | (1 << 1) | (1 << 2));
}

void control_server_shutdown() {
    if (server != NULL) {
        // Run() polls interrupt_received between accept() timeouts, so there is
        // nothing to signal; it will be gone within kAcceptPollMillis.
        server->WaitStopped();
        delete server;
        server = NULL;
    }
    if (listen_socket >= 0) {
        close(listen_socket);
        listen_socket = -1;
    }
    if (socket_path[0]) unlink(socket_path);
}
