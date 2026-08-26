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

#ifndef LED_FLASCHEN_TASCHEN_H_
#define LED_FLASCHEN_TASCHEN_H_

#include "flaschen-taschen.h"

#include <pthread.h>

#include <vector>
#include <string>

namespace spixels {
class MultiSPI;
class LEDStrip;
}

namespace ft {
class Mutex;
}

class ServerFlaschenTaschen : public FlaschenTaschen {
public:
    virtual ~ServerFlaschenTaschen() {}

    // Server-side FlaschenTaschen displays might need to do some initialization
    // after they have become a daemon. This can be used for general init
    // tasks, but in particular to start threads (Threads must not be started
    // before becoming a daemon).
    virtual void PostDaemonInit() {}

    // Backends whose Send() would otherwise block on hardware present
    // asynchronously instead: Send() only marks the frame ready, and a
    // dedicated thread does the blocking part with the writer lock released.
    // "lock" is the same mutex that serialises writers.
    // Default is a no-op for backends that present synchronously.
    virtual void StartDisplayThread(ft::Mutex * /*lock*/) {}
    virtual void StopDisplayThread() {}

    // -- Global display state.
    //
    // Brightness and blanking are properties of the whole display rather than
    // of any one client, so they belong to whoever owns the panel: a client
    // that dimmed itself would just be drawing darker pixels, and any other
    // client could undo it by drawing on top. Backends that cannot do this
    // return false rather than pretending, so a UI can say so instead of
    // offering a control that does nothing.
    //
    // All of these are called with the writer mutex held.

    // Brightness in percent, 1..100, clamped. Panel-wide, and independent of
    // what is being displayed.
    virtual bool SetGlobalBrightness(int /*percent*/) { return false; }

    // Present black without discarding the composite, so that unblanking
    // restores the image immediately rather than waiting for a client to draw
    // its next frame. This is "off" for a display; it is not a wipe.
    virtual bool SetBlanked(bool /*blanked*/) { return false; }

    virtual bool SupportsGlobalDimmer() const { return false; }
    virtual int global_brightness() const { return 100; }
    virtual bool blanked() const { return false; }
};

// Column helps assembling the various columns of width 5 (the width of a crate)
// to one big display. Since all SPI based strips are necessary upated in
// parallel, that SPI send command is triggered within here.
class ColumnAssembly : public ServerFlaschenTaschen {
public:
    ColumnAssembly(spixels::MultiSPI *spi);
    ~ColumnAssembly();

    // Add column. Takes over ownership of column.
    // Columns have been added right to left, or, if standing
    // behind the display: leftmost column first.
    void AddColumn(FlaschenTaschen *taschen);

    int width() const { return width_; }
    int height() const { return height_; }

    void SetPixel(int x, int y, const Color &col);
    void Send();

private:
    spixels::MultiSPI *const spi_;
    std::vector<FlaschenTaschen*> columns_;
    int width_;
    int height_;
};

// This represents one column. Unlike the final display,
// x-coordinates go right-to-left, and bottom to up.
// The final assembly will turn things around.
class CrateColumnFlaschenTaschen : public FlaschenTaschen {
public:
    // Given a simple LED strip, create a column of crates that behaves
    // the way we are snaking the strip.
    // Takes ownership of the LED strip.
    CrateColumnFlaschenTaschen(spixels::LEDStrip *strip);
    ~CrateColumnFlaschenTaschen();

    int width() const { return 5; }
    int height() const { return height_; }

    void SetPixel(int x, int y, const Color &col);
    void Send() {}  // This happens in SPI sending in ColumnAssembly

private:
    spixels::LEDStrip *strip_;
    const int height_;
};

// -- FlaschenTaschen implementation using rpi-rgb-led-matrix
namespace rgb_matrix {
class RGBMatrix;
class FrameCanvas;
}

class RGBMatrixFlaschenTaschen : public ServerFlaschenTaschen {
public:
    RGBMatrixFlaschenTaschen(rgb_matrix::RGBMatrix *matrix,
                             int width, int height);
    virtual ~RGBMatrixFlaschenTaschen();

    virtual void PostDaemonInit();  // Starting threads.
    virtual void StartDisplayThread(ft::Mutex *lock);
    virtual void StopDisplayThread();

    int width() const { return width_; }
    int height() const { return height_; }

    // Both are called on writer threads with the writer mutex held. Neither
    // touches the matrix, so neither can block on hardware.
    void SetPixel(int x, int y, const Color &col);
    void Send();

    virtual bool SetGlobalBrightness(int percent);
    virtual bool SetBlanked(bool blanked);
    virtual bool SupportsGlobalDimmer() const { return true; }
    virtual int global_brightness() const { return pending_brightness_; }
    virtual bool blanked() const { return blanked_; }

private:
    class DisplayPusher;
    friend class DisplayPusher;

    // Mark a frame ready and wake the pusher. Send() is one caller; a control
    // command is the other, because it has to be presented even when no client
    // is sending anything.
    void WakePusher();

    // Copy the accumulated dirty region of fb_ into the offscreen canvas.
    // Called by the pusher with the writer mutex HELD. False if no work.
    bool TakeDirtyRegion();

    // Present the offscreen canvas and resync the spare. Blocks until vsync,
    // so it is called by the pusher with the writer mutex RELEASED.
    void SwapAndResync();

    rgb_matrix::RGBMatrix *const matrix_;

    // Only ever touched by the pusher thread, so it needs no locking.
    rgb_matrix::FrameCanvas *back_buffer_;

    // Source of truth for what should be on screen. Writers mutate this and
    // nothing else; guarded by the writer mutex.
    Color *fb_;
    int dirty_x0_, dirty_y0_, dirty_x1_, dirty_y1_;  // half-open; empty if x1<=x0
    bool frame_ready_;
    pthread_cond_t frame_ready_cond_;

    // Global state. Written by control threads under the writer mutex, read
    // and acted on by the pusher. The pending/applied pairs let the pusher
    // notice a change on whichever frame it next gets to, so a burst of
    // commands (a dragged brightness slider) coalesces into one repaint.
    int pending_brightness_, applied_brightness_;
    bool blanked_, blank_applied_;
    bool force_full_;      // next take covers the whole frame, not the diff

    DisplayPusher *pusher_;

    int width_;
    int height_;
};

class TerminalFlaschenTaschen : public ServerFlaschenTaschen {
public:
    TerminalFlaschenTaschen(int terminal_fd, int width, int heigh);
    virtual ~TerminalFlaschenTaschen();
    virtual void PostDaemonInit();

    int width() const { return width_; }
    int height() const { return height_; }

    void SetPixel(int x, int y, const Color &col);
    void Send();

protected:
    static inline void WriteByteDecimal(char *buf, uint8_t val) {
        buf[2] = (val % 10) + '0'; val /= 10;
        buf[1] = (val % 10) + '0'; val /= 10;
        buf[0] = val + '0';
    }

    const int terminal_fd_;
    const int width_;
    const int height_;
    size_t initial_offset_;
    size_t pixel_offset_;
    size_t fps_offset_;
    bool is_first_;
    std::string buffer_;
    int64_t last_time_usec_;
};

// Similar, but higher res.
class HDTerminalFlaschenTaschen : public TerminalFlaschenTaschen {
public:
    HDTerminalFlaschenTaschen(int terminal_fd, int width, int heigh);
    virtual void PostDaemonInit();

    void SetPixel(int x, int y, const Color &col);

private:
    size_t lower_row_pixel_offset_;
};

#endif // LED_FLASCHEN_TASCHEN_H_
