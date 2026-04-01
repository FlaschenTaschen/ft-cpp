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

// Flaschen Taschen Server

#include <arpa/inet.h>
#include <errno.h>
#include <getopt.h>
#include <grp.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <string>

#include "composite-flaschen-taschen.h"
#include "ft-thread.h"
#include "led-flaschen-taschen.h"
#include "servers.h"

#ifdef __linux__
#include "service-discovery.h"
#endif

#if FT_BACKEND == 0
#  include "multi-spi.h"
#  include "led-strip.h"
#endif

#if FT_BACKEND == 1
#  include "led-matrix.h"
#endif

#define DROP_PRIV_USER "daemon"
#define DROP_PRIV_GROUP "daemon"
#define FT_VERSION "1.0.0"

// Platform detection
#ifdef __APPLE__
const char* g_platform_name = "macOS";
#elif _WIN32
const char* g_platform_name = "Windows";
#else
const char* g_platform_name = "Linux";
#endif

// Backend detection
#if FT_BACKEND == 0
const char* g_backend_name = "ft";
#elif FT_BACKEND == 1
const char* g_backend_name = "rgb-matrix";
#else
const char* g_backend_name = "terminal";
#endif

#ifdef __linux__   // Apple does not have setresuid() etc.
bool drop_privs(const char *priv_user, const char *priv_group) {
    uid_t ruid, euid, suid;
    if (getresuid(&ruid, &euid, &suid) >= 0) {
        if (euid != 0)   // not root anyway. No priv dropping.
            return true;
    }

    struct group *g = getgrnam(priv_group);
    if (g == NULL) {
        perror("group lookup.");
        return false;
    }
    if (setresgid(g->gr_gid, g->gr_gid, g->gr_gid) != 0) {
        perror("setresgid()");
        return false;
    }
    struct passwd *p = getpwnam(priv_user);
    if (p == NULL) {
        perror("user lookup.");
        return false;
    }
    if (setresuid(p->pw_uid, p->pw_uid, p->pw_uid) != 0) {
        perror("setresuid()");
        return false;
    }
    return true;
}
#endif

static int usage(const char *progname) {
    fprintf(stderr, "usage: %s [options]\n", progname);
    fprintf(stderr, "Options:\n"
            "\t-D <width>x<height> : Output dimension. Default 45x35\n"
#if FT_BACKEND == 2
            "\t--hd-terminal       : Make terminal with higher resolution.\n"
#else
            "\t-d                  : Become daemon\n"
#endif
            "\t--layer-timeout <sec>: Layer timeout: clearing after non-activity (Default: 15)\n"
#ifdef __linux__
            "\t--mdns <enabled|disabled> : Enable/disable mDNS service discovery (Default: disabled)\n"
            "\t--mdns-name <name>  : Display name for mDNS announcement (Default: \"FlaschenTaschen\")\n"
            "\t--mdns-url <url>    : HTTP URL for display (optional)\n"
#endif
            );
#if FT_BACKEND == 1
    rgb_matrix::PrintMatrixFlags(stderr);
#endif
    return 1;
}

int main(int argc, char *argv[]) {
    int width = 45;
    int height = 35;
    int layer_timeout = 15;
    bool mdns_enabled = false;
    std::string mdns_name("FlaschenTaschen");
    std::string mdns_url("");
#if FT_BACKEND != 2
    bool as_daemon = false;
#endif
#if FT_BACKEND == 2
    bool hd_terminal = false;
#endif

#if FT_BACKEND == 1
    width = -1;    // Use size from matrix unless explicitly chosen.
    height = -1;
    rgb_matrix::RGBMatrix::Options matrix_options;
    rgb_matrix::RuntimeOptions runtime_opt;
    runtime_opt.daemon = -1;   // We deal with this manually belo
    runtime_opt.drop_privileges = -1;

    // First things first: extract the command line flags that contain
    // relevant matrix options.
    if (!rgb_matrix::ParseOptionsFromFlags(&argc, &argv,
                                           &matrix_options, &runtime_opt)) {
        return usage(argv[0]);
    }
#endif

    enum LongOptionsOnly {
        OPT_LAYER_TIMEOUT = 1002,
        OPT_HD_TERMINAL = 1003,
#ifdef __linux__
        OPT_MDNS_ENABLED = 1010,
        OPT_MDNS_NAME = 1011,
        OPT_MDNS_URL = 1012,
#endif
    };

    static struct option long_options[] = {
        { "dimension",          required_argument, NULL, 'D'},
#if FT_BACKEND != 2
        { "daemon",             no_argument,       NULL, 'd'},
#endif
        { "layer-timeout",      required_argument, NULL,  OPT_LAYER_TIMEOUT },
#if FT_BACKEND == 2
        { "hd-terminal",        no_argument,       NULL,  OPT_HD_TERMINAL },
#endif
#ifdef __linux__
        { "mdns",               required_argument, NULL,  OPT_MDNS_ENABLED },
        { "mdns-name",          required_argument, NULL,  OPT_MDNS_NAME },
        { "mdns-url",           required_argument, NULL,  OPT_MDNS_URL },
#endif
        { 0,                    0,                 0,    0  },
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "I:D:d", long_options, NULL)) != -1) {
        switch (opt) {
        case 'D':
            if (sscanf(optarg, "%dx%d", &width, &height) != 2) {
                fprintf(stderr, "Invalid size spec '%s'\n", optarg);
                return usage(argv[0]);
            }
            break;
#if FT_BACKEND != 2
        case 'd':
            as_daemon = true;
            break;
#endif
        case OPT_LAYER_TIMEOUT:
            layer_timeout = atoi(optarg);
            break;
#if FT_BACKEND == 2
        case OPT_HD_TERMINAL:
            hd_terminal = true;
            break;
#endif
#ifdef __linux__
        case OPT_MDNS_ENABLED:
            mdns_enabled = (strcmp(optarg, "enabled") == 0);
            break;
        case OPT_MDNS_NAME:
            mdns_name = optarg;
            break;
        case OPT_MDNS_URL:
            mdns_url = optarg;
            break;
#endif
        default:
            return usage(argv[0]);
        }
    }

    if (layer_timeout < 1) {
        layer_timeout = 1;
    }

#if FT_BACKEND == 0
    using spixels::MultiSPI;
    using spixels::CreateWS2801Strip;
    static const int kLedsPerCol = 7 * 25;
    MultiSPI *const spi = spixels::CreateDMAMultiSPI();
    ColumnAssembly *display = new ColumnAssembly(spi);

#define MAKE_COLUMN(port) new CrateColumnFlaschenTaschen(CreateWS2801Strip(spi, port, kLedsPerCol))

    // Looking from the back of the display: leftmost column first.
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P8));
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P7));
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P6));
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P5));

    // Center column. Connected to front part
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P13));

    // Rest: continue on the back part
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P4));
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P3));
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P2));
    display->AddColumn(MAKE_COLUMN(MultiSPI::SPI_P1));
#undef MAKE_COLUMN
#elif FT_BACKEND == 1
    ServerFlaschenTaschen *display
        = new RGBMatrixFlaschenTaschen(
            CreateMatrixFromOptions(matrix_options, runtime_opt),
            width, height);
#elif FT_BACKEND == 2
    ServerFlaschenTaschen *display =
        hd_terminal
        ? new HDTerminalFlaschenTaschen(STDOUT_FILENO, width, height)
        : new TerminalFlaschenTaschen(STDOUT_FILENO, width, height);
#endif

    // Start all the services and report problems (such as sockets already
    // bound to) before we become a daemon
    if (!udp_server_init(1337)) {
        return 1;
    }

#ifdef __linux__
    ServiceDiscoveryThread* discovery_thread = NULL;
#endif

#if FT_BACKEND != 2  // terminal thing can not run in background.
    // Commandline parsed, immediate errors reported. Time to become daemon.
    if (as_daemon && daemon(0, 0) != 0) {  // Become daemon.
        perror("Failed to become daemon");
    }
#endif

    // Only after we have become a daemon, we can do all the things that
    // require starting threads. These can be various realtime priorities,
    // we so need to stay root until all threads are set up.
    display->PostDaemonInit();

    display->Send();  // Clear screen.

    // Get actual display dimensions from the display object (in case they were -1)
    width = display->width();
    height = display->height();

    ft::Mutex mutex;

    // The display we expose to the user provides composite layering which can
    // be used by the UDP server.
    CompositeFlaschenTaschen layered_display(display, 16);
    layered_display.StartLayerGarbageCollection(&mutex, layer_timeout);

#ifdef __linux__
    // Create service discovery thread if enabled (after hardware is initialized
    // so we have the correct width/height from the display)
    if (mdns_enabled) {
        discovery_thread = new ServiceDiscoveryThread(
            mdns_name.c_str(),
            1337,
            width,
            height,
            mdns_url.empty() ? "" : mdns_url.c_str(),
            FT_VERSION,
            g_backend_name,
            g_platform_name,
            0x000F  // features: all currently-defined capabilities
        );
        discovery_thread->Start(0, 0);  // 0 priority = not real-time, 0 affinity = any CPU
        fprintf(stderr, "Service discovery: %s (%dx%d) port 1337 [%s/%s]\n",
                mdns_name.c_str(), width, height, g_backend_name, g_platform_name);
    }
#endif

#ifdef __linux__
    // After hardware is set up, all servers are listening and all
    // threads are started with their respective priorities, we can drop
    // privileges.
    if (!drop_privs(DROP_PRIV_USER, DROP_PRIV_GROUP))
        return 1;
#endif

    udp_server_run_blocking(&layered_display, &mutex);  // last server blocks.

#ifdef __linux__
    // Shutdown service discovery thread gracefully
    if (discovery_thread != NULL) {
        discovery_thread->Shutdown();
        discovery_thread->WaitStopped();
        delete discovery_thread;
    }
#endif

    delete display;
}
