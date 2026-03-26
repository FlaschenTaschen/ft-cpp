// Simple checkerboard test to verify display is working
#include "api/include/udp-flaschen-taschen.h"
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char *argv[]) {
    const char *host = "localhost";
    int layer = 0;
    int size = 64;

    // Parse args
    for (int i = 1; i < argc; i++) {
        if (argv[i][0] == '-') {
            if (argv[i][1] == 'h' && i + 1 < argc) host = argv[++i];
            if (argv[i][1] == 'l' && i + 1 < argc) layer = atoi(argv[++i]);
            if (argv[i][1] == 's' && i + 1 < argc) size = atoi(argv[++i]);
        }
    }

    fprintf(stderr, "Checkerboard test: host=%s, layer=%d, size=%d\n", host, layer, size);

    const int socket = OpenFlaschenTaschenSocket(host);
    fprintf(stderr, "Socket: %d\n", socket);

    if (socket < 0) {
        fprintf(stderr, "Failed to open socket\n");
        return 1;
    }

    UDPFlaschenTaschen canvas(socket, size, size);
    canvas.Clear();
    fprintf(stderr, "Canvas created and cleared\n");

    // Create checkerboard pattern
    int square_size = 4;
    Color white(255, 255, 255);
    Color black(0, 0, 0);
    Color red(255, 0, 0);

    for (int y = 0; y < size; y++) {
        for (int x = 0; x < size; x++) {
            int sq_x = x / square_size;
            int sq_y = y / square_size;
            Color color = ((sq_x + sq_y) % 2 == 0) ? white : red;
            canvas.SetPixel(x, y, color);
        }
    }

    fprintf(stderr, "Checkerboard drawn, sending...\n");
    canvas.SetOffset(0, 0, layer);
    canvas.Send();

    fprintf(stderr, "Sent! Waiting 5 seconds...\n");
    sleep(5);

    fprintf(stderr, "Clearing canvas\n");
    canvas.Clear();
    canvas.Send();

    fprintf(stderr, "Done\n");
    return 0;
}
