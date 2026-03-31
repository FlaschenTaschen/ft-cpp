// -*- mode: c++; c-basic-offset: 4; indent-tabs-mode: nil; -*-
//
// Debug logging utilities for FlaschenTaschen
//
// Usage:
//   #include "ft-debug.h"
//   DEBUG_LOG("message: %d\n", value);
//
// Enable with: FT_DEBUG=1 ./program
//

#ifndef FT_DEBUG_H_
#define FT_DEBUG_H_

#include <cstdio>
#include <cstdlib>

#define DEBUG_LOG(fmt, ...) do { \
    if (getenv("FT_DEBUG")) { \
        fprintf(stderr, fmt, ##__VA_ARGS__); \
    } \
} while(0)

#endif  // FT_DEBUG_H_
