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
#include "ft-debug.h"
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

enum class RenderMode {
    kGrayscale,
    kOriginal
};

enum class GrayscaleOrientation {
    kHorizontal,
    kVertical
};

enum class SequenceMode {
    kForward,
    kReverse
};

enum class TransparencyColor {
    kWhite,
    kBlack
};

struct MaskData {
    std::vector<std::vector<uint8_t>> grayscale_pixels;
    std::vector<std::vector<Color>> original_colors;
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
int opt_frame_duration = -1;  // -1 means not set (use default 200 if animation enabled)
bool opt_animation_enabled = false;  // True if -D was provided
std::string opt_filenames;
GrayscaleMode opt_mode = GrayscaleMode::kBounce;
GrayscaleOrientation opt_orientation = GrayscaleOrientation::kHorizontal;
RenderMode opt_render_mode = RenderMode::kGrayscale;
SequenceMode opt_sequence_mode = SequenceMode::kForward;
TransparencyColor opt_transparency = TransparencyColor::kWhite;
Color opt_logo_color;
bool opt_fixed_color = false;

int usage(const char *progname) {
    fprintf(stderr, "Grayscale - Render JSON-defined pixel masks as grayscale or original colors\n");
    fprintf(stderr, "Usage: %s -f <filepath[,...]> [options]\n", progname);
    fprintf(stderr, "Options:\n"
        "\t-f <filepath[,...]> : One or more JSON files with [[\"RRGGBB\", ...], ...] (required)\n"
        "\t-m <mode>           : Positioning mode: bounce, center, left, right, top, bottom\n"
        "\t-c <RRGGBB>         : Fixed color (default: rainbow palette, ignored in original mode)\n"
        "\t-r <mode>           : Render mode: grayscale (default) or original\n"
        "\t-o <orientation>    : horizontal (default) or vertical (ignored if -D is used)\n"
        "\t-D <ms>             : Frame duration before advancing to next image (enables sequential mode)\n"
        "\t-y <mode>           : Sequence mode: forward (default) or reverse (only with -D)\n"
        "\t-T <color>          : Transparency color: white (default) or black\n"
        "\t-g <W>x<H>[+<X>+<Y>] : Output geometry. (default 45x35+0+0)\n"
        "\t-l <layer>          : Layer 0-15. (default 1)\n"
        "\t-t <timeout>        : Timeout exits after given seconds. (default 24hrs)\n"
        "\t-h <host>           : Flaschen-Taschen display hostname. (FT_DISPLAY)\n"
        "\t-d <delay>          : Delay between rendering frames in milliseconds. (default 40)\n"
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
    while ((opt = getopt(argc, argv, "?f:m:c:r:o:D:y:T:g:l:t:h:d:")) != -1) {
        switch (opt) {
        case '?':  // help
            return usage(argv[0]);
        case 'f':  // filenames
            opt_filenames = optarg;
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
        case 'r':  // render mode
            if (strcmp(optarg, "grayscale") == 0) {
                opt_render_mode = RenderMode::kGrayscale;
            } else if (strcmp(optarg, "original") == 0) {
                opt_render_mode = RenderMode::kOriginal;
            } else {
                fprintf(stderr, "Error: Unknown render mode '%s'. Valid: grayscale, original\n", optarg);
                return usage(argv[0]);
            }
            break;
        case 'D':  // frame duration (enables animation mode)
            if (sscanf(optarg, "%d", &opt_frame_duration) != 1 || opt_frame_duration < 1) {
                fprintf(stderr, "Invalid frame duration '%s'\n", optarg);
                return usage(argv[0]);
            }
            opt_animation_enabled = true;
            break;
        case 'y':  // sequence mode
            if (strcmp(optarg, "forward") == 0) {
                opt_sequence_mode = SequenceMode::kForward;
            } else if (strcmp(optarg, "reverse") == 0) {
                opt_sequence_mode = SequenceMode::kReverse;
            } else {
                fprintf(stderr, "Error: Unknown sequence mode '%s'. Valid: forward, reverse\n", optarg);
                return usage(argv[0]);
            }
            break;
        case 'T':  // transparency color
            if (strcmp(optarg, "white") == 0) {
                opt_transparency = TransparencyColor::kWhite;
            } else if (strcmp(optarg, "black") == 0) {
                opt_transparency = TransparencyColor::kBlack;
            } else {
                fprintf(stderr, "Error: Unknown transparency color '%s'. Valid: white, black\n", optarg);
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
        return MaskData{{}, {}, 0, 0};
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
        std::vector<std::vector<uint8_t>> combined_gray(combined_height, std::vector<uint8_t>(combined_width, 255));
        std::vector<std::vector<Color>> combined_colors(combined_height, std::vector<Color>(combined_width, Color(255, 255, 255)));

        int x_cursor = 0;
        for (const auto &mask : masks) {
            int y_offset = (combined_height - mask.height) / 2;
            for (int y = 0; y < mask.height; y++) {
                for (int x = 0; x < mask.width; x++) {
                    combined_gray[y_offset + y][x_cursor + x] = mask.grayscale_pixels[y][x];
                    combined_colors[y_offset + y][x_cursor + x] = mask.original_colors[y][x];
                }
            }
            x_cursor += mask.width + IMAGE_PADDING;
        }

        return MaskData{combined_gray, combined_colors, combined_width, combined_height};
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
        std::vector<std::vector<uint8_t>> combined_gray(combined_height, std::vector<uint8_t>(combined_width, 255));
        std::vector<std::vector<Color>> combined_colors(combined_height, std::vector<Color>(combined_width, Color(255, 255, 255)));

        int y_cursor = 0;
        for (const auto &mask : masks) {
            int x_offset = (combined_width - mask.width) / 2;
            for (int y = 0; y < mask.height; y++) {
                for (int x = 0; x < mask.width; x++) {
                    combined_gray[y_cursor + y][x_offset + x] = mask.grayscale_pixels[y][x];
                    combined_colors[y_cursor + y][x_offset + x] = mask.original_colors[y][x];
                }
            }
            y_cursor += mask.height + IMAGE_PADDING;
        }

        return MaskData{combined_gray, combined_colors, combined_width, combined_height};
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
    RenderMode render_mode,
    TransparencyColor transparency_color,
    const std::vector<std::vector<uint8_t>> &grayscale_mask,
    const std::vector<std::vector<Color>> &original_colors,
    UDPFlaschenTaschen &canvas
) {
    int pixels_drawn = 0;
    for (int y = 0; y < (int)grayscale_mask.size(); y++) {
        for (int x = 0; x < (int)grayscale_mask[y].size(); x++) {
            uint8_t gray_value = grayscale_mask[y][x];

            // Skip transparent pixels based on transparency color
            if (transparency_color == TransparencyColor::kWhite) {
                if (gray_value >= 240) continue;  // Skip bright pixels
            } else {
                if (gray_value <= 15) continue;   // Skip dark pixels
            }

            Color pixel_color;
            if (render_mode == RenderMode::kOriginal) {
                // Use original color from JSON
                pixel_color = original_colors[y][x];
            } else {
                // Apply color based on grayscale intensity
                // Dark pixels (low grayscale) get full color, light pixels get darker version
                float intensity = (255.0f - gray_value) / 255.0f;
                pixel_color = Color(
                    (uint8_t)(color.r * intensity),
                    (uint8_t)(color.g * intensity),
                    (uint8_t)(color.b * intensity)
                );
            }

            // Place pixel on canvas with offset
            int screen_x = offset_x + x;
            int screen_y = offset_y + y;

            if (screen_x >= 0 && screen_x < display_width && screen_y >= 0 && screen_y < display_height) {
                canvas.SetPixel(screen_x, screen_y, pixel_color);
                pixels_drawn++;
            }
        }
    }
}

int main(int argc, char *argv[]) {
    DEBUG_LOG("grayscale: starting, argc=%d\n", argc);

    // Parse command line
    if (int e = cmdLine(argc, argv)) {
        fprintf(stderr, "grayscale: cmdLine failed with code %d\n", e);
        return e;
    }
    DEBUG_LOG("grayscale: cmdLine OK, loading JSON from: %s\n", opt_filenames.c_str());

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

            // Convert hex strings to grayscale and store original colors
            std::vector<std::vector<uint8_t>> grayscale_pixels;
            std::vector<std::vector<Color>> original_colors;

            for (const auto &row : hex_array) {
                std::vector<uint8_t> gray_row;
                std::vector<Color> color_row;

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

                    // Store original color
                    color_row.push_back(color);

                    // Luminance formula: 0.299*R + 0.587*G + 0.114*B
                    uint8_t gray = (uint8_t)(0.299f * color.r + 0.587f * color.g + 0.114f * color.b);
                    gray_row.push_back(gray);
                }
                grayscale_pixels.push_back(gray_row);
                original_colors.push_back(color_row);
            }

            masks.push_back(MaskData{grayscale_pixels, original_colors, width, height});
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
    DEBUG_LOG("grayscale: opening socket to %s\n", opt_hostname ? opt_hostname : "(default)");
    const int socket = OpenFlaschenTaschenSocket(opt_hostname);
    DEBUG_LOG("grayscale: socket=%d, creating %dx%d canvas\n", socket, opt_width, opt_height);
    UDPFlaschenTaschen canvas(socket, opt_width, opt_height);
    canvas.Clear();
    DEBUG_LOG("grayscale: canvas created and cleared\n");

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

    // Handle break
    signal(SIGTERM, InterruptHandler);
    signal(SIGINT, InterruptHandler);

    // Determine mode: animation or combining
    MaskData display_mask;
    std::vector<MaskData> animation_masks;
    int image_width, image_height;

    if (opt_animation_enabled) {
        // Animation mode: use masks sequentially
        animation_masks = masks;
        image_width = masks[0].width;
        image_height = masks[0].height;
    } else {
        // Combining mode: combine masks into single display
        display_mask = combineMasks(masks, opt_orientation);
        image_width = display_mask.width;
        image_height = display_mask.height;
    }

    PositionState pos_state;

    // Initialize bounce mode with random position and direction
    if (opt_mode == GrayscaleMode::kBounce) {
        srand((unsigned int)(time(NULL) + getpid()));

        // Random position within bounds
        int max_x = std::max(0, opt_width - image_width);
        int max_y = std::max(0, opt_height - image_height);

        pos_state.x = max_x > 0 ? rand() % (max_x + 1) : 0;
        pos_state.y = max_y > 0 ? rand() % (max_y + 1) : 0;

        // Random direction (1 or -1)
        pos_state.sx = (rand() % 2 == 0) ? 1 : -1;
        pos_state.sy = (rand() % 2 == 0) ? 1 : -1;
    }

    time_t starttime = time(NULL);
    int color_index = rand() % 256;  // Randomize starting color
    int current_mask_index = 0;
    int mask_direction = 1;  // 1 for forward, -1 for backward
    int frame_elapsed = 0;   // Elapsed time in current frame in milliseconds

    do {
        // Determine if we should advance to next mask (only in animation mode)
        if (opt_animation_enabled && frame_elapsed >= opt_frame_duration) {
            frame_elapsed = 0;

            if (opt_sequence_mode == SequenceMode::kForward) {
                // Forward: loop from 0 to N-1 then back to 0
                current_mask_index++;
                if (current_mask_index >= (int)masks.size()) {
                    current_mask_index = 0;
                }
            } else {
                // Reverse: bounce 0 -> 1 -> 2 -> 1 -> 0
                current_mask_index += mask_direction;

                if (current_mask_index >= (int)masks.size()) {
                    // Hit end, bounce back
                    mask_direction = -1;
                    current_mask_index = masks.size() - 2;
                } else if (current_mask_index < 0) {
                    // Hit start, bounce forward
                    mask_direction = 1;
                    current_mask_index = 1;
                }
            }
        }

        // Get current color (fixed or from palette, ignored in original render mode)
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

        // Draw current mask at computed offset
        const MaskData &current_mask = opt_animation_enabled ?
            animation_masks[current_mask_index] : display_mask;
        drawMask(offset_x, offset_y, current_color, opt_width, opt_height,
                 opt_render_mode, opt_transparency, current_mask.grayscale_pixels,
                 current_mask.original_colors, canvas);

        // Send to display
        canvas.SetOffset(opt_xoff + DISPLAY_XOFF, opt_yoff + DISPLAY_YOFF, opt_layer);
        canvas.Send();
        usleep(opt_delay * 1000);

        frame_elapsed += opt_delay;
        color_index++;
    } while ((difftime(time(NULL), starttime) <= opt_timeout) && !interrupt_received);

    // Clear canvas on exit
    canvas.Clear();
    canvas.Send();
    usleep(100); // give the packet a moment to be sent

    if (interrupt_received) return 1;
    return 0;
}
