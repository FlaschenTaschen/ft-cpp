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

// ft-detect is Linux-only (requires mDNS service discovery via Avahi)
#ifdef __linux__

#include "ft-discovery.h"

#include <getopt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/wait.h>

#include <string>
#include <vector>

enum OutputFormat {
    FORMAT_TEXT,
    FORMAT_SHELL,
    FORMAT_JSON,
};

static void usage(const char *progname) {
    fprintf(stderr, "Usage: %s [options] [client-tool] [client-args...]\n", progname);
    fprintf(stderr, "Options:\n"
            "\t-l, --list              List all discovered displays and exit\n"
            "\t-q, --query <name>      Query for specific display name (case-insensitive partial match)\n"
            "\t-f, --format <format>   Output format: text, sh, json (default: text)\n"
            "\t-t, --timeout <ms>      Discovery timeout in milliseconds (default: 5000)\n"
            "\t-v, --verbose           Verbose output\n"
            "\t-h, --help              Show this help\n"
            "\n"
            "Examples:\n"
            "\t%s                          # List displays\n"
            "\t%s -q Polaris               # Query for \"Polaris\" display\n"
            "\t%s -q Polaris -f sh         # Query, output as shell variables\n"
            "\t%s send-image photo.jpg     # Discover first display, run send-image\n"
            "\t%s -v -q Polaris send-image photo.jpg  # Discover \"Polaris\", run send-image with verbose output\n",
            progname, progname, progname, progname, progname, progname);
}

static void print_service_text(const DisplayService& service) {
    printf("%s\n", service.name.c_str());
    printf("  Address: %s\n", service.address.c_str());
    printf("  Hostname: %s\n", service.hostname.c_str());
    printf("  Port: %d\n", service.port);
    printf("  Geometry: %dx%d\n", service.width, service.height);
    printf("  Backend: %s\n", service.backend.c_str());
    printf("  Platform: %s\n", service.platform.c_str());
    printf("  Version: %s\n", service.version.c_str());
    printf("  Features: %d (0x%04x)", service.features, service.features);

    // Print feature descriptions
    std::vector<const char*> feature_names;
    if (service.has_feature(0x0001)) feature_names.push_back("multi-packet");
    if (service.has_feature(0x0002)) feature_names.push_back("multi-layer");
    if (service.has_feature(0x0004)) feature_names.push_back("offset");
    if (service.has_feature(0x0008)) feature_names.push_back("layer-timeout");

    if (!feature_names.empty()) {
        printf(" [");
        for (size_t i = 0; i < feature_names.size(); ++i) {
            if (i > 0) printf(", ");
            printf("%s", feature_names[i]);
        }
        printf("]");
    }
    printf("\n");

    if (!service.url.empty()) {
        printf("  URL: %s\n", service.url.c_str());
    }
}

static void print_service_shell(const DisplayService& service) {
    printf("FT_NAME=\"%s\"\n", service.name.c_str());
    printf("FT_HOST=\"%s\"\n", service.address.c_str());
    printf("FT_PORT=\"%d\"\n", service.port);
    printf("FT_WIDTH=\"%d\"\n", service.width);
    printf("FT_HEIGHT=\"%d\"\n", service.height);
    if (!service.url.empty()) {
        printf("FT_URL=\"%s\"\n", service.url.c_str());
    }
}

static void print_service_json(const DisplayService& service) {
    printf("{\n");
    printf("  \"name\": \"%s\",\n", service.name.c_str());
    printf("  \"address\": \"%s\",\n", service.address.c_str());
    printf("  \"port\": %d,\n", service.port);
    printf("  \"width\": %d,\n", service.width);
    printf("  \"height\": %d,\n", service.height);
    printf("  \"version\": \"%s\",\n", service.version.c_str());
    printf("  \"backend\": \"%s\",\n", service.backend.c_str());
    printf("  \"platform\": \"%s\",\n", service.platform.c_str());
    printf("  \"features\": %d\n", service.features);
    if (!service.url.empty()) {
        printf("  \"url\": \"%s\",\n", service.url.c_str());
    }
    printf("}\n");
}

int main(int argc, char *argv[]) {
    bool list_mode = false;
    std::string query("");
    OutputFormat format = FORMAT_TEXT;
    int timeout_ms = 5000;
    bool verbose = false;

    struct option long_options[] = {
        { "list",    no_argument,       NULL, 'l' },
        { "query",   required_argument, NULL, 'q' },
        { "format",  required_argument, NULL, 'f' },
        { "timeout", required_argument, NULL, 't' },
        { "verbose", no_argument,       NULL, 'v' },
        { "help",    no_argument,       NULL, 'h' },
        { 0,         0,                 0,    0   },
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "lq:f:t:vh", long_options, NULL)) != -1) {
        switch (opt) {
        case 'l':
            list_mode = true;
            break;
        case 'q':
            query = optarg;
            list_mode = true;  // Query implies list mode first
            break;
        case 'f':
            if (strcmp(optarg, "sh") == 0 || strcmp(optarg, "shell") == 0) {
                format = FORMAT_SHELL;
            } else if (strcmp(optarg, "json") == 0) {
                format = FORMAT_JSON;
            } else {
                format = FORMAT_TEXT;
            }
            break;
        case 't':
            timeout_ms = atoi(optarg);
            break;
        case 'v':
            verbose = true;
            break;
        case 'h':
            usage(argv[0]);
            return 0;
        default:
            usage(argv[0]);
            return 1;
        }
    }

    // If no client tool specified, list mode is implied
    if (optind >= argc) {
        list_mode = true;
    }

    if (list_mode) {
        // List or query mode
        if (!query.empty()) {
            // Query for specific display
            DisplayService service = discover_display(query, timeout_ms);
            if (service.port == 0) {
                if (verbose) {
                    fprintf(stderr, "No display found matching \"%s\"\n", query.c_str());
                }
                return 1;
            }

            switch (format) {
            case FORMAT_SHELL:
                print_service_shell(service);
                break;
            case FORMAT_JSON:
                print_service_json(service);
                break;
            case FORMAT_TEXT:
            default:
                print_service_text(service);
                break;
            }
        } else {
            // List all displays
            std::vector<DisplayService> services = discover_displays(timeout_ms);
            if (services.empty()) {
                if (verbose) {
                    fprintf(stderr, "No displays found\n");
                }
                return 1;
            }

            for (size_t i = 0; i < services.size(); ++i) {
                if (i > 0) printf("\n");
                switch (format) {
                case FORMAT_SHELL:
                    printf("# Display %zu\n", i);
                    print_service_shell(services[i]);
                    break;
                case FORMAT_JSON:
                    if (i == 0) printf("[\n");
                    printf("  ");
                    print_service_json(services[i]);
                    if (i < services.size() - 1) printf(",\n");
                    if (i == services.size() - 1) printf("]\n");
                    break;
                case FORMAT_TEXT:
                default:
                    print_service_text(services[i]);
                    break;
                }
            }
        }
        return 0;
    }

    // Proxy mode: discover a display and invoke the client tool
    DisplayService service;
    if (!query.empty()) {
        service = discover_display(query, timeout_ms);
        if (service.port == 0) {
            fprintf(stderr, "Error: No display found matching \"%s\"\n", query.c_str());
            return 1;
        }
    } else {
        // Use first available display
        std::vector<DisplayService> services = discover_displays(timeout_ms);
        if (services.empty()) {
            fprintf(stderr, "Error: No displays found\n");
            return 1;
        }
        service = services[0];
    }

    if (verbose) {
        fprintf(stderr, "Discovered: %s (%s:%d, %dx%d",
                service.name.c_str(), service.address.c_str(), service.port,
                service.width, service.height);
        if (!service.url.empty()) {
            fprintf(stderr, ", %s", service.url.c_str());
        }
        fprintf(stderr, ")\n");
    }

    // Build argument array for the client tool
    const char* tool_name = argv[optind];
    int client_argc = argc - optind + 4;  // tool + "-h" + host + "-g" + geom + remaining args
    char** client_argv = new char*[client_argc + 1];

    char geometry[32];
    snprintf(geometry, sizeof(geometry), "%dx%d", service.width, service.height);

    int arg_index = 0;
    client_argv[arg_index++] = const_cast<char*>(tool_name);
    client_argv[arg_index++] = const_cast<char*>("-h");
    client_argv[arg_index++] = const_cast<char*>(service.address.c_str());
    client_argv[arg_index++] = const_cast<char*>("-g");
    client_argv[arg_index++] = geometry;

    // Copy remaining arguments
    for (int i = optind + 1; i < argc; ++i) {
        client_argv[arg_index++] = argv[i];
    }
    client_argv[arg_index] = NULL;

    if (verbose) {
        fprintf(stderr, "Executing: %s", tool_name);
        for (int i = 1; i < arg_index; ++i) {
            fprintf(stderr, " %s", client_argv[i]);
        }
        fprintf(stderr, "\n");
    }

    // Execute the client tool
    pid_t pid = fork();
    if (pid == -1) {
        perror("fork");
        delete[] client_argv;
        return 1;
    }

    if (pid == 0) {
        // Child process: execute the client tool
        execvp(tool_name, client_argv);
        perror("execvp");
        exit(1);
    }

    // Parent process should not reach here; the child's exec() replaces the process
    // But if exec fails, we wait for the child
    int status;
    waitpid(pid, &status, 0);
    delete[] client_argv;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

#else // __linux__

#include <stdio.h>

int main() {
    fprintf(stderr, "Error: ft-detect is only available on Linux (requires mDNS/Avahi)\n");
    return 1;
}

#endif // __linux__
