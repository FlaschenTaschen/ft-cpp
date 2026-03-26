// -*- mode: c++; c-basic-offset: 4; indent-tabs-mode: nil; -*-
//
// grayscale
// Render a JSON-defined pixel mask as grayscale
//
// Usage:
//   ./grayscale -f <filepath[,...]> [options]
//
// Options:
//   -f <filepath[,...]> : One or more JSON files with [["RRGGBB", ...], ...] (required)
//   -o <orientation>    : horizontal (default) or vertical
//   -m <mode>           : Positioning mode: bounce (default), center, left, right, top, bottom
//   -c <RRGGBB>         : Fixed color (default: rainbow palette)
//   -g <W>x<H>[+<X>+<Y>] : Output geometry
//   -l <layer>          : Layer 0-15 (default 1)
//   -t <timeout>        : Timeout in seconds
//   -h <host>           : Flaschen-Taschen display hostname
//   -d <delay>          : Delay between frames in milliseconds
//

#include "udp-flaschen-taschen.h"
#include "config.h"

#include <getopt.h>
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <string>
#include <string.h>
#include <signal.h>
#include <vector>
#include <fstream>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

// Defaults
#define Z_LAYER 1
#define DELAY 40
#define IMAGE_PADDING 3

enum class GrayscaleMode {
    kBounce,
    kCenter,
    kLeft,
    kRight,
    kTop,
    kBottom
};

enum class GrayscaleOrientation {
    kHorizontal,
    kVertical
};

struct MaskData {
    std::vector<std::vector<uint8_t>> pixels;
    int width;
    int height;
};

volatile bool interrupt_received = false;
static void InterruptHandler(int signo) {
    interrupt_received = true;
}

// Command line options
const char *opt_hostname = NULL;
int opt_layer = Z_LAYER;
double opt_timeout = 60 * 60 * 24;  // 24 hours
int opt_width = DISPLAY_WIDTH;
int opt_height = DISPLAY_HEIGHT;
int opt_xoff = 0, opt_yoff = 0;
int opt_delay = DELAY;
std::string opt_filenames;
GrayscaleMode opt_mode = GrayscaleMode::kBounce;
GrayscaleOrientation opt_orientation = GrayscaleOrientation::kHorizontal;
Color opt_logo_color;
bool opt_fixed_color = false;

int usage(const char *progname) {
    fprintf(stderr, "Grayscale - Render a JSON-defined pixel mask as grayscale\n");
    fprintf(stderr, "Usage: %s -f <filepath[,...]> [options]\n", progname);
    fprintf(stderr, "Options:\n"
        "\t-f <filepath[,...]> : One or more JSON files with [[\"RRGGBB\", ...], ...] (required)\n"
        "\t-o <orientation>    : horizontal (default) or vertical\n"
        "\t-m <mode>           : Positioning mode: bounce, center, left, right, top, bottom\n"
        "\t-c <RRGGBB>         : Fixed color (default: rainbow palette)\n"
        "\t-g <W>x<H>[+<X>+<Y>] : Output geometry. (default 45x35+0+0)\n"
        "\t-l <layer>          : Layer 0-15. (default 1)\n"
        "\t-t <timeout>        : Timeout exits after given seconds. (default 24hrs)\n"
        "\t-h <host>           : Flaschen-Taschen display hostname. (FT_DISPLAY)\n"
        "\t-d <delay>          : Delay between frames in milliseconds. (default 40)\n"
    );
    return 1;
}

bool parseHexColor(const std::string &hex_str, Color &out_color) {
    int r, g, b;
    if (sscanf(hex_str.c_str(), "%02x%02x%02x", &r, &g, &b) != 3) {
        return false;
    }
    out_color = Color(r, g, b);
    return true;
}

int cmdLine(int argc, char *argv[]) {
    int opt;
    while ((opt = getopt(argc, argv, "?f:o:m:c:g:l:t:h:d:")) != -1) {
        switch (opt) {
        case '?':  // help
            return usage(argv[0]);
        case 'f':  // filenames
            opt_filenames = optarg;
            break;
        case 'o':  // orientation
            if (strcmp(optarg, "horizontal") == 0) {
                opt_orientation = GrayscaleOrientation::kHorizontal;
            } else if (strcmp(optarg, "vertical") == 0) {
                opt_orientation = GrayscaleOrientation::kVertical;
            } else {
                fprintf(stderr, "Error: Unknown orientation '%s'. Valid: horizontal, vertical\n", optarg);
                return usage(argv[0]);
            }
            break;
        case 'm':  // mode
            if (strcmp(optarg, "bounce") == 0) {
                opt_mode = GrayscaleMode::kBounce;
            } else if (strcmp(optarg, "center") == 0) {
                opt_mode = GrayscaleMode::kCenter;
            } else if (strcmp(optarg, "left") == 0) {
                opt_mode = GrayscaleMode::kLeft;
            } else if (strcmp(optarg, "right") == 0) {
                opt_mode = GrayscaleMode::kRight;
            } else if (strcmp(optarg, "top") == 0) {
                opt_mode = GrayscaleMode::kTop;
            } else if (strcmp(optarg, "bottom") == 0) {
                opt_mode = GrayscaleMode::kBottom;
            } else {
                fprintf(stderr, "Error: Unknown mode '%s'. Valid: bounce, center, left, right, top, bottom\n", optarg);
                return usage(argv[0]);
            }
            break;
        case 'c':  // fixed color
            if (parseHexColor(optarg, opt_logo_color)) {
                opt_fixed_color = true;
            } else {
                fprintf(stderr, "Error: Invalid color format\n");
                return usage(argv[0]);
            }
            break;
        case 'g':  // geometry
            if (sscanf(optarg, "%dx%d%d%d", &opt_width, &opt_height, &opt_xoff, &opt_yoff) < 2) {
                fprintf(stderr, "Invalid size '%s'\n", optarg);
                return usage(argv[0]);
            }
            break;
        case 'l':  // layer
            if (sscanf(optarg, "%d", &opt_layer) != 1 || opt_layer < 0 || opt_layer >= 16) {
                fprintf(stderr, "Invalid layer '%s'\n", optarg);
                return usage(argv[0]);
            }
            break;
        case 't':  // timeout
            if (sscanf(optarg, "%lf", &opt_timeout) != 1 || opt_timeout < 0) {
                fprintf(stderr, "Invalid timeout '%s'\n", optarg);
                return usage(argv[0]);
            }
            break;
        case 'h':  // hostname
            opt_hostname = strdup(optarg);
            break;
        case 'd':  // delay
            if (sscanf(optarg, "%d", &opt_delay) != 1 || opt_delay < 1) {
                fprintf(stderr, "Invalid delay '%s'\n", optarg);
                return usage(argv[0]);
            }
            break;
        default:
            return usage(argv[0]);
        }
    }

    if (opt_filenames.empty()) {
        fprintf(stderr, "Error: -f <filepath[,...]> is required\n");
        return usage(argv[0]);
    }

    return 0;
}

void colorGradient(int start, int end, int r1, int g1, int b1, int r2, int g2, int b2, Color palette[]) {
    for (int i = 0; i <= (end - start); i++) {
        float k = (float)i / (float)(end - start);
        palette[start + i].r = (uint8_t)(r1 + (r2 - r1) * k);
        palette[start + i].g = (uint8_t)(g1 + (g2 - g1) * k);
        palette[start + i].b = (uint8_t)(b1 + (b2 - b1) * k);
    }
}

MaskData combineMasks(const std::vector<MaskData> &masks, GrayscaleOrientation orientation) {
    if (masks.empty()) {
        return MaskData{{}, 0, 0};
    }
    if (masks.size() == 1) {
        return masks[0];
    }

    if (orientation == GrayscaleOrientation::kHorizontal) {
        int combined_width = 0;
        int combined_height = 0;

        // Calculate dimensions
        for (const auto &mask : masks) {
            combined_width += mask.width;
            combined_height = std::max(combined_height, mask.height);
        }
        combined_width += (masks.size() - 1) * IMAGE_PADDING;

        // Create combined mask
        std::vector<std::vector<uint8_t>> combined(combined_height, std::vector<uint8_t>(combined_width, 255));

        int x_cursor = 0;
        for (const auto &mask : masks) {
            int y_offset = (combined_height - mask.height) / 2;
            for (int y = 0; y < mask.height; y++) {
                for (int x = 0; x < mask.width; x++) {
                    combined[y_offset + y][x_cursor + x] = mask.pixels[y][x];
                }
            }
            x_cursor += mask.width + IMAGE_PADDING;
        }

        return MaskData{combined, combined_width, combined_height};
    } else {  // vertical
        int combined_width = 0;
        int combined_height = 0;

        // Calculate dimensions
        for (const auto &mask : masks) {
            combined_width = std::max(combined_width, mask.width);
            combined_height += mask.height;
        }
        combined_height += (masks.size() - 1) * IMAGE_PADDING;

        // Create combined mask
        std::vector<std::vector<uint8_t>> combined(combined_height, std::vector<uint8_t>(combined_width, 255));

        int y_cursor = 0;
        for (const auto &mask : masks) {
            int x_offset = (combined_width - mask.width) / 2;
            for (int y = 0; y < mask.height; y++) {
                for (int x = 0; x < mask.width; x++) {
                    combined[y_cursor + y][x_offset + x] = mask.pixels[y][x];
                }
            }
            y_cursor += mask.height + IMAGE_PADDING;
        }

        return MaskData{combined, combined_width, combined_height};
    }
}

struct PositionState {
    int x = 0;
    int y = 0;
    int sx = 1;
    int sy = 1;
};

std::pair<int, int> computeOffset(
    GrayscaleMode mode,
    PositionState &state,
    int color_index,
    int display_width,
    int display_height,
    int image_width,
    int image_height
) {
    switch (mode) {
    case GrayscaleMode::kBounce: {
        // Animate position, bouncing off edges every 8 frames
        if ((color_index % 8) == 0) {
            int next_x = state.x + state.sx;
            int next_y = state.y + state.sy;

            // Bounce off left/right edges
            if (next_x < 0) {
                state.x = 0;
                state.sx = 1;
            } else if (next_x + image_width > display_width) {
                state.x = display_width - image_width;
                state.sx = -1;
            } else {
                state.x = next_x;
            }

            // Bounce off top/bottom edges
            if (next_y < 0) {
                state.y = 0;
                state.sy = 1;
            } else if (next_y + image_height > display_height) {
                state.y = display_height - image_height;
                state.sy = -1;
            } else {
                state.y = next_y;
            }
        }
        return {state.x, state.y};
    }
    case GrayscaleMode::kCenter:
        return {
            std::max(0, (display_width - image_width) / 2),
            std::max(0, (display_height - image_height) / 2)
        };
    case GrayscaleMode::kLeft:
        return {
            0,
            std::max(0, (display_height - image_height) / 2)
        };
    case GrayscaleMode::kRight:
        return {
            std::max(0, display_width - image_width),
            std::max(0, (display_height - image_height) / 2)
        };
    case GrayscaleMode::kTop:
        return {
            std::max(0, (display_width - image_width) / 2),
            0
        };
    case GrayscaleMode::kBottom:
        return {
            std::max(0, (display_width - image_width) / 2),
            std::max(0, display_height - image_height)
        };
    }
    return {0, 0};
}

void drawMask(
    int offset_x,
    int offset_y,
    const Color &color,
    int display_width,
    int display_height,
    const std::vector<std::vector<uint8_t>> &mask,
    UDPFlaschenTaschen &canvas
) {
    int pixels_drawn = 0;
    for (int y = 0; y < (int)mask.size(); y++) {
        for (int x = 0; x < (int)mask[y].size(); x++) {
            uint8_t gray_value = mask[y][x];

            // Skip white pixels (background/transparency)
            if (gray_value >= 240) continue;

            // Apply color based on grayscale intensity
            // Dark pixels (low grayscale) get full color, light pixels get darker version
            float intensity = (255.0f - gray_value) / 255.0f;
            Color pixel_color(
                (uint8_t)(color.r * intensity),
                (uint8_t)(color.g * intensity),
                (uint8_t)(color.b * intensity)
            );

            // Place pixel on canvas with offset
            int screen_x = offset_x + x;
            int screen_y = offset_y + y;

            if (screen_x >= 0 && screen_x < display_width && screen_y >= 0 && screen_y < display_height) {
                canvas.SetPixel(screen_x, screen_y, pixel_color);
                pixels_drawn++;
            }
        }
    }
    fprintf(stderr, "Drew %d pixels (color rgb(%d,%d,%d), offset %d,%d, mask size %dx%d, display %dx%d)\n",
            pixels_drawn, color.r, color.g, color.b, offset_x, offset_y,
            (int)mask[0].size(), (int)mask.size(), display_width, display_height);
}

int main(int argc, char *argv[]) {
    fprintf(stderr, "grayscale: starting, argc=%d\n", argc);

    // Parse command line
    if (int e = cmdLine(argc, argv)) {
        fprintf(stderr, "grayscale: cmdLine failed with code %d\n", e);
        return e;
    }
    fprintf(stderr, "grayscale: cmdLine OK, loading JSON from: %s\n", opt_filenames.c_str());

    // Load and parse JSON files
    std::vector<MaskData> masks;

    // Split filenames by comma
    std::vector<std::string> file_paths;
    size_t start = 0;
    size_t end = opt_filenames.find(',');
    while (end != std::string::npos) {
        file_paths.push_back(opt_filenames.substr(start, end - start));
        start = end + 1;
        end = opt_filenames.find(',', start);
    }
    file_paths.push_back(opt_filenames.substr(start));

    for (const auto &file_path : file_paths) {
        try {
            std::ifstream file(file_path);
            if (!file.is_open()) {
                fprintf(stderr, "Error: Cannot open file '%s'\n", file_path.c_str());
                return 1;
            }

            json hex_array = json::parse(file);
            if (!hex_array.is_array() || hex_array.empty()) {
                fprintf(stderr, "Error: JSON array is empty in %s\n", file_path.c_str());
                return 1;
            }

            int height = hex_array.size();
            int width = 0;

            // Find max width
            for (const auto &row : hex_array) {
                if (row.is_array()) {
                    width = std::max(width, (int)row.size());
                }
            }

            if (width == 0) {
                fprintf(stderr, "Error: Invalid JSON structure in %s\n", file_path.c_str());
                return 1;
            }

            // Convert hex strings to grayscale
            std::vector<std::vector<uint8_t>> pixels;
            for (const auto &row : hex_array) {
                std::vector<uint8_t> gray_row;
                for (const auto &hex_str : row) {
                    if (!hex_str.is_string()) {
                        fprintf(stderr, "Error: Invalid JSON structure in %s\n", file_path.c_str());
                        return 1;
                    }
                    Color color;
                    if (!parseHexColor(hex_str.get<std::string>(), color)) {
                        fprintf(stderr, "Error: Invalid hex color in %s\n", file_path.c_str());
                        return 1;
                    }
                    // Luminance formula: 0.299*R + 0.587*G + 0.114*B
                    uint8_t gray = (uint8_t)(0.299f * color.r + 0.587f * color.g + 0.114f * color.b);
                    gray_row.push_back(gray);
                }
                pixels.push_back(gray_row);
            }

            masks.push_back(MaskData{pixels, width, height});
        } catch (const std::exception &e) {
            fprintf(stderr, "Error: Failed to load or parse JSON file: %s\n", e.what());
            return 1;
        }
    }

    if (masks.empty()) {
        fprintf(stderr, "Error: No masks loaded\n");
        return 1;
    }

    // Open socket and create canvas
    fprintf(stderr, "grayscale: opening socket to %s\n", opt_hostname ? opt_hostname : "(default)");
    const int socket = OpenFlaschenTaschenSocket(opt_hostname);
    fprintf(stderr, "grayscale: socket=%d, creating %dx%d canvas\n", socket, opt_width, opt_height);
    UDPFlaschenTaschen canvas(socket, opt_width, opt_height);
    canvas.Clear();
    fprintf(stderr, "grayscale: canvas created and cleared\n");

    // Create rainbow palette
    Color palette[256];
    colorGradient(  0,  31, 255,   0, 255,   0,   0, 255, palette);
    colorGradient( 32,  63,   0,   0, 255,   0, 255, 255, palette);
    colorGradient( 64,  95,   0, 255, 255,   0, 255,   0, palette);
    colorGradient( 96, 127,   0, 255,   0, 127, 255,   0, palette);
    colorGradient(128, 159, 127, 255,   0, 255, 255,   0, palette);
    colorGradient(160, 191, 255, 255,   0, 255, 127,   0, palette);
    colorGradient(192, 223, 255, 127,   0, 255,   0,   0, palette);
    colorGradient(224, 255, 255,   0,   0, 255,   0, 255, palette);

    // Combine masks
    MaskData combined = combineMasks(masks, opt_orientation);
    int image_width = combined.width;
    int image_height = combined.height;
    auto mask = combined.pixels;

    // For non-bounce modes, pad the combined mask to display geometry
    if (opt_mode != GrayscaleMode::kBounce && !mask.empty()) {
        if (image_width < opt_width || image_height < opt_height) {
            // Pad horizontally if needed
            if (image_width < opt_width) {
                int pad_width = opt_width - image_width;
                int pad_left = pad_width / 2;
                int pad_right = pad_width - pad_left;

                for (auto &row : mask) {
                    std::vector<uint8_t> padded_row(pad_left, 255);
                    padded_row.insert(padded_row.end(), row.begin(), row.end());
                    padded_row.insert(padded_row.end(), pad_right, 255);
                    row = padded_row;
                }
                image_width = opt_width;
            }

            // Pad vertically if needed
            if (image_height < opt_height) {
                int pad_height = opt_height - image_height;
                int pad_top = pad_height / 2;
                int pad_bottom = pad_height - pad_top;

                std::vector<uint8_t> empty_row(image_width, 255);
                mask.insert(mask.begin(), pad_top, empty_row);
                mask.insert(mask.end(), pad_bottom, empty_row);
                image_height = opt_height;
            }
        }
    }

    // Handle break
    signal(SIGTERM, InterruptHandler);
    signal(SIGINT, InterruptHandler);

    PositionState pos_state;
    time_t starttime = time(NULL);
    int color_index = 0;

    do {
        // Get current color (fixed or from palette)
        Color current_color = opt_fixed_color ? opt_logo_color : palette[color_index % 256];

        // Determine position based on mode
        std::pair<int, int> offset = computeOffset(
            opt_mode, pos_state, color_index,
            opt_width, opt_height, image_width, image_height
        );
        int offset_x = offset.first;
        int offset_y = offset.second;

        // Clear canvas
        canvas.Clear();

        // Draw mask at computed offset
        drawMask(offset_x, offset_y, current_color, opt_width, opt_height, mask, canvas);

        // Send to display
        canvas.SetOffset(opt_xoff + DISPLAY_XOFF, opt_yoff + DISPLAY_YOFF, opt_layer);
        canvas.Send();
        usleep(opt_delay * 1000);

        color_index++;
    } while ((difftime(time(NULL), starttime) <= opt_timeout) && !interrupt_received);

    // Clear canvas on exit
    canvas.Clear();
    canvas.Send();

    if (interrupt_received) return 1;
    return 0;
}
