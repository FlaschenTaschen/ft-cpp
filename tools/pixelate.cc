// -*- mode: c++; c-basic-offset: 4; indent-tabs-mode: nil; -*-
//
// pixelate
// Convert an image to a JSON pixel grid suitable for the grayscale demo
//
// Usage:
//   ./pixelate -i <image_path> -w <width> -h <height> [-T white|black] [-b]
//
// Options:
//   -i <image_path> : Path to image file (required)
//   -w <width>      : Target width in pixels (required)
//   -h <height>     : Target height in pixels (required)
//   -T <color>      : Transparent color: white (default) or black
//   -b              : Apply light blur (default: enabled)
//   -B              : Disable blur
//

#include <assert.h>
#include <getopt.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vector>
#include <cstring>

#include <Magick++.h>
#include <magick/image.h>

using Magick::Quantum;

enum TransparencyColor {
    kWhite,
    kBlack
};

struct RGB {
    uint8_t r, g, b;
    RGB() : r(0), g(0), b(0) {}
    RGB(uint8_t rr, uint8_t gg, uint8_t bb) : r(rr), g(gg), b(bb) {}
};

// Bilinear interpolation helper
float bilinearInterpolate(float p00, float p10, float p01, float p11, float fx, float fy) {
    float f00 = p00 * (1.0f - fx) * (1.0f - fy);
    float f10 = p10 * fx * (1.0f - fy);
    float f01 = p01 * (1.0f - fx) * fy;
    float f11 = p11 * fx * fy;
    return f00 + f10 + f01 + f11;
}

// Sample a pixel using bilinear interpolation
RGB bilinearSample(const std::vector<RGB> &pixelData, int width, int height, float x, float y) {
    int ix = (int)x;
    int iy = (int)y;
    float fx = x - ix;
    float fy = y - iy;

    int x0 = (ix < 0) ? 0 : (ix >= width ? width - 1 : ix);
    int x1 = (ix + 1 >= width) ? width - 1 : ix + 1;
    int y0 = (iy < 0) ? 0 : (iy >= height ? height - 1 : iy);
    int y1 = (iy + 1 >= height) ? height - 1 : iy + 1;

    int idx00 = y0 * width + x0;
    int idx10 = y0 * width + x1;
    int idx01 = y1 * width + x0;
    int idx11 = y1 * width + x1;

    RGB p00 = (idx00 >= 0 && idx00 < (int)pixelData.size()) ? pixelData[idx00] : RGB();
    RGB p10 = (idx10 >= 0 && idx10 < (int)pixelData.size()) ? pixelData[idx10] : RGB();
    RGB p01 = (idx01 >= 0 && idx01 < (int)pixelData.size()) ? pixelData[idx01] : RGB();
    RGB p11 = (idx11 >= 0 && idx11 < (int)pixelData.size()) ? pixelData[idx11] : RGB();

    float r = bilinearInterpolate((float)p00.r, (float)p10.r, (float)p01.r, (float)p11.r, fx, fy);
    float g = bilinearInterpolate((float)p00.g, (float)p10.g, (float)p01.g, (float)p11.g, fx, fy);
    float b = bilinearInterpolate((float)p00.b, (float)p10.b, (float)p01.b, (float)p11.b, fx, fy);

    return RGB((uint8_t)r, (uint8_t)g, (uint8_t)b);
}

// Apply light blur (3x3 kernel)
void applyLightBlur(std::vector<RGB> &pixelData, int width, int height) {
    std::vector<RGB> temp = pixelData;

    for (int y = 1; y < height - 1; ++y) {
        for (int x = 1; x < width - 1; ++x) {
            int rSum = 0, gSum = 0, bSum = 0;
            int count = 0;

            for (int dy = -1; dy <= 1; ++dy) {
                for (int dx = -1; dx <= 1; ++dx) {
                    int nx = x + dx;
                    int ny = y + dy;
                    int idx = ny * width + nx;
                    if (idx >= 0 && idx < (int)pixelData.size()) {
                        rSum += pixelData[idx].r;
                        gSum += pixelData[idx].g;
                        bSum += pixelData[idx].b;
                        count++;
                    }
                }
            }

            if (count > 0) {
                int idx = y * width + x;
                uint8_t rBlurred = (uint8_t)(rSum / count);
                uint8_t gBlurred = (uint8_t)(gSum / count);
                uint8_t bBlurred = (uint8_t)(bSum / count);

                temp[idx].r = (uint8_t)((pixelData[idx].r * 2 + rBlurred) / 3);
                temp[idx].g = (uint8_t)((pixelData[idx].g * 2 + gBlurred) / 3);
                temp[idx].b = (uint8_t)((pixelData[idx].b * 2 + bBlurred) / 3);
            }
        }
    }

    pixelData = temp;
}

int main(int argc, char *argv[]) {
    const char *imagePath = NULL;
    int targetWidth = -1, targetHeight = -1;
    int transparentColor = kWhite;
    int applyBlur = 1;

    int opt;
    while ((opt = getopt(argc, argv, "?i:w:h:T:bB")) != -1) {
        switch (opt) {
        case '?':
            fprintf(stderr, "Usage: %s -i <image_path> -w <width> -h <height> [-T white|black] [-b] [-B]\n", argv[0]);
            fprintf(stderr, "Options:\n");
            fprintf(stderr, "  -i <image_path> : Path to image file (required)\n");
            fprintf(stderr, "  -w <width>      : Target width in pixels (required)\n");
            fprintf(stderr, "  -h <height>     : Target height in pixels (required)\n");
            fprintf(stderr, "  -T <color>      : Transparent color: white (default) or black\n");
            fprintf(stderr, "  -b              : Apply light blur (default: enabled)\n");
            fprintf(stderr, "  -B              : Disable blur\n");
            return 1;
        case 'i':
            imagePath = optarg;
            break;
        case 'w':
            targetWidth = atoi(optarg);
            break;
        case 'h':
            targetHeight = atoi(optarg);
            break;
        case 'T':
            if (strcmp(optarg, "white") == 0) {
                transparentColor = kWhite;
            } else if (strcmp(optarg, "black") == 0) {
                transparentColor = kBlack;
            } else {
                fprintf(stderr, "Error: Unknown transparency color '%s'. Valid: white, black\n", optarg);
                return 1;
            }
            break;
        case 'b':
            applyBlur = 1;
            break;
        case 'B':
            applyBlur = 0;
            break;
        default:
            return 1;
        }
    }

    if (!imagePath || targetWidth <= 0 || targetHeight <= 0) {
        fprintf(stderr, "Error: -i, -w, and -h are required\n");
        return 1;
    }

    try {
        Magick::InitializeMagick(NULL);

        // Load image
        Magick::Image image;
        fprintf(stderr, "Loading image: %s\n", imagePath);
        image.read(imagePath);

        int sourceWidth = (int)image.columns();
        int sourceHeight = (int)image.rows();
        fprintf(stderr, "Source dimensions: %d×%d\n", sourceWidth, sourceHeight);

        // Extract RGB pixel data
        fprintf(stderr, "Extracting pixel data...\n");
        std::vector<RGB> sourcePixels;
        for (int y = 0; y < sourceHeight; ++y) {
            for (int x = 0; x < sourceWidth; ++x) {
                Magick::Color c = image.pixelColor(x, y);
                sourcePixels.push_back(RGB(
                    (uint8_t)ScaleQuantumToChar(c.redQuantum()),
                    (uint8_t)ScaleQuantumToChar(c.greenQuantum()),
                    (uint8_t)ScaleQuantumToChar(c.blueQuantum())
                ));
            }
        }

        // Scale using bilinear interpolation
        fprintf(stderr, "Scaling to %d×%d...\n", targetWidth, targetHeight);
        std::vector<RGB> scaledPixels;
        for (int y = 0; y < targetHeight; ++y) {
            for (int x = 0; x < targetWidth; ++x) {
                float srcX = (float)x * sourceWidth / targetWidth;
                float srcY = (float)y * sourceHeight / targetHeight;
                RGB pixel = bilinearSample(sourcePixels, sourceWidth, sourceHeight, srcX, srcY);
                scaledPixels.push_back(pixel);
            }
        }

        // Apply blur if requested
        if (applyBlur) {
            fprintf(stderr, "Applying light blur...\n");
            applyLightBlur(scaledPixels, targetWidth, targetHeight);
        }

        // Output JSON
        fprintf(stderr, "Generating JSON output...\n");
        printf("[");

        for (int y = 0; y < targetHeight; ++y) {
            if (y > 0) printf(",");
            printf("[");

            for (int x = 0; x < targetWidth; ++x) {
                if (x > 0) printf(",");

                int idx = y * targetWidth + x;
                RGB pixel = scaledPixels[idx];

                // Check if pixel matches transparent color
                int isTransparent = 0;
                if (transparentColor == kWhite) {
                    // White is (255, 255, 255) or close to it
                    isTransparent = (pixel.r >= 240 && pixel.g >= 240 && pixel.b >= 240);
                } else {
                    // Black is (0, 0, 0) or close to it
                    isTransparent = (pixel.r <= 15 && pixel.g <= 15 && pixel.b <= 15);
                }

                // Output as black if transparent, otherwise as the actual color
                if (isTransparent) {
                    printf("\"000000\"");
                } else {
                    printf("\"%02x%02x%02x\"", pixel.r, pixel.g, pixel.b);
                }
            }

            printf("]");
        }

        printf("]\n");
        fprintf(stderr, "Done!\n");
        return 0;

    } catch (Magick::Error &error) {
        fprintf(stderr, "ImageMagick error: %s\n", error.what());
        return 1;
    } catch (std::exception &error) {
        fprintf(stderr, "Error: %s\n", error.what());
        return 1;
    }
}
