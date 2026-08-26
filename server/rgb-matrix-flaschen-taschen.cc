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

#include "led-flaschen-taschen.h"

#include "led-matrix.h"
#include "ft-thread.h"

#include <stdio.h>
#include <stdlib.h>

#include <algorithm>

// Presenting a frame means waiting for the panel's next vertical sync. Doing
// that on the writer path serialises every writer behind the refresh rate and,
// worse, stops the UDP server from draining its socket, so the kernel discards
// whole frames. This thread owns everything that touches the FrameCanvas:
// writers only mutate fb_ and set a flag.
class RGBMatrixFlaschenTaschen::DisplayPusher : public ft::Thread {
public:
    DisplayPusher(RGBMatrixFlaschenTaschen *owner, ft::Mutex *lock)
        : owner_(owner), lock_(lock), running_(true) {}

    virtual void Run() {
        for (;;) {
            bool have_frame = false;
            {
                ft::MutexLock l(lock_);
                while (running_ && !owner_->frame_ready_) {
                    // Timeout so an exit request is noticed even when no
                    // frames are arriving at all.
                    lock_->WaitOnWithTimeout(&owner_->frame_ready_cond_, 250);
                }
                if (!running_) break;
                have_frame = owner_->TakeDirtyRegion();
            }
            // Mutex released before the vsync wait: this is the whole point.
            if (have_frame) owner_->SwapAndResync();
        }
    }

    void TriggerExit() {
        ft::MutexLock l(lock_);
        running_ = false;
        pthread_cond_signal(&owner_->frame_ready_cond_);
    }

private:
    RGBMatrixFlaschenTaschen *const owner_;
    ft::Mutex *const lock_;
    bool running_;
};

RGBMatrixFlaschenTaschen::RGBMatrixFlaschenTaschen(
    rgb_matrix::RGBMatrix *matrix, int width, int height)
    : matrix_(matrix), back_buffer_(NULL), fb_(NULL),
      dirty_x0_(0), dirty_y0_(0), dirty_x1_(0), dirty_y1_(0),
      frame_ready_(false),
      pending_brightness_(100), applied_brightness_(100),
      blanked_(false), blank_applied_(false), force_full_(false),
      pusher_(NULL) {
    if (matrix_ == NULL) {
        fprintf(stderr, "Couldn't initialize RGB matrix.\n");
        exit(1);
    }
    // Start from whatever --led-brightness asked for, so the first frame is
    // not gratuitously repainted and a `get` before any command has been sent
    // reports the truth rather than a hardcoded 100.
    pending_brightness_ = applied_brightness_ = matrix_->brightness();
    width_ = (width > 0) ? width : matrix_->width();
    height_ = (height > 0) ? height : matrix_->height();
    pthread_cond_init(&frame_ready_cond_, NULL);
    fb_ = new Color[width_ * height_];
    std::fill(fb_, fb_ + width_ * height_, Color(0, 0, 0));
    fprintf(stderr, "Running with %dx%d resolution\n", width_, height_);
}

RGBMatrixFlaschenTaschen::~RGBMatrixFlaschenTaschen() {
    StopDisplayThread();   // must not outlive the canvases it touches.
    delete matrix_;
    delete [] fb_;
    pthread_cond_destroy(&frame_ready_cond_);
}

void RGBMatrixFlaschenTaschen::PostDaemonInit() {
    // Allocate before starting the refresh thread; doing it the other way
    // round corrupted the heap (see a238529).
    back_buffer_ = matrix_->CreateFrameCanvas();
    matrix_->StartRefresh();
}

void RGBMatrixFlaschenTaschen::StartDisplayThread(ft::Mutex *lock) {
    if (pusher_ != NULL) return;   // only once
    pusher_ = new DisplayPusher(this, lock);
    // Normal priority, kept off the isolated core that drives the panel.
    pusher_->Start(0, (1 << 0) | (1 << 1) | (1 << 2));
}

void RGBMatrixFlaschenTaschen::StopDisplayThread() {
    if (pusher_ == NULL) return;
    pusher_->TriggerExit();
    pusher_->WaitStopped();
    delete pusher_;
    pusher_ = NULL;
}

void RGBMatrixFlaschenTaschen::SetPixel(int x, int y, const Color &col) {
    if (x < 0 || x >= width_ || y < 0 || y >= height_) return;
    fb_[y * width_ + x] = col;
    if (dirty_x1_ <= dirty_x0_) {          // region currently empty
        dirty_x0_ = x; dirty_x1_ = x + 1;
        dirty_y0_ = y; dirty_y1_ = y + 1;
    } else {
        if (x < dirty_x0_) dirty_x0_ = x;
        if (x + 1 > dirty_x1_) dirty_x1_ = x + 1;
        if (y < dirty_y0_) dirty_y0_ = y;
        if (y + 1 > dirty_y1_) dirty_y1_ = y + 1;
    }
}

void RGBMatrixFlaschenTaschen::Send() {
    // Writer side is now O(1): hand the frame to the pusher and return.
    // Updates that arrive faster than the panel can present simply coalesce.
    WakePusher();
}

void RGBMatrixFlaschenTaschen::WakePusher() {
    frame_ready_ = true;
    pthread_cond_signal(&frame_ready_cond_);
}

bool RGBMatrixFlaschenTaschen::SetGlobalBrightness(int percent) {
    if (percent < 1) percent = 1;
    if (percent > 100) percent = 100;
    pending_brightness_ = percent;
    // Must wake the pusher, and not only because there might be a frame
    // waiting: with nothing being sent -- a static image, or a blanked wall --
    // TakeDirtyRegion() is never called at all and the command would sit
    // unapplied indefinitely.
    WakePusher();
    return true;
}

bool RGBMatrixFlaschenTaschen::SetBlanked(bool blanked) {
    blanked_ = blanked;
    WakePusher();
    return true;
}

bool RGBMatrixFlaschenTaschen::TakeDirtyRegion() {
    // Brightness first, above the early returns below, so that dimming while
    // the wall is blanked is remembered rather than dropped.
    //
    // SetBrightness() is not a hardware control: it scales values as they are
    // encoded into bitplanes by MapColors(), so it "will only affect newly set
    // pixels" and does nothing at all to what is already on screen. Hence the
    // forced full-frame repaint behind it. Going through the library rather
    // than scaling fb_ ourselves is deliberate -- it applies the scale before
    // the CIE1931 curve, which makes the percentage perceptually linear, and
    // that is what a brightness slider should feel like.
    if (pending_brightness_ != applied_brightness_) {
        applied_brightness_ = pending_brightness_;
        matrix_->SetBrightness(applied_brightness_);
        force_full_ = true;
    }

    if (blanked_ != blank_applied_) {
        blank_applied_ = blanked_;
        if (blanked_) {
            // Clear() is a memset of the bitplane buffer, so it beats 20k
            // SetPixel(black) calls and handles inverse_color_ for free. fb_ is
            // deliberately untouched: it stays the source of truth, so
            // unblanking is one repaint and needs nothing from any client.
            back_buffer_->Clear();
            frame_ready_ = false;
            dirty_x0_ = dirty_y0_ = dirty_x1_ = dirty_y1_ = 0;
            return true;    // the swap, plus CopyFrom, blacks both canvases
        }
        force_full_ = true;
    }

    if (blanked_) {
        // Steady state, dark. Consume the flag or the pusher spins on a frame
        // nobody takes. Writers carry on updating fb_ and accumulating a dirty
        // region, so whatever arrived while dark shows up on unblank.
        frame_ready_ = false;
        return false;
    }

    if (!frame_ready_ && !force_full_) return false;
    frame_ready_ = false;

    if (force_full_) {
        force_full_ = false;
        dirty_x0_ = dirty_y0_ = 0;
        dirty_x1_ = width_; dirty_y1_ = height_;
    }
    if (dirty_x1_ <= dirty_x0_) return false;   // flagged, but nothing changed

    // Row-major, to stride with fb_.
    for (int y = dirty_y0_; y < dirty_y1_; ++y) {
        const Color *row = fb_ + y * width_;
        for (int x = dirty_x0_; x < dirty_x1_; ++x) {
            back_buffer_->SetPixel(x, y, row[x].r, row[x].g, row[x].b);
        }
    }
    dirty_x0_ = dirty_y0_ = dirty_x1_ = dirty_y1_ = 0;   // empty again
    return true;
}

void RGBMatrixFlaschenTaschen::SwapAndResync() {
    // SwapOnVSync() hands back the canvas that was on screen before, which is
    // two frames stale. Since updates are incremental, the spare has to be
    // brought back in sync with what is now displayed, or successive frames
    // would alternate between two different partial composites.
    //
    // CopyFrom() is a memcpy of the internal bitplane representation as
    // opposed to re-encoding every pixel through the CIE1931 lookup and the
    // per-bitplane scatter.
    //
    // It is also why one repaint is enough after a brightness change, which
    // otherwise looks like a bug waiting to happen: this leaves both canvases
    // byte-identical rather than one frame apart, so re-encoding fb_ into the
    // offscreen canvas once and swapping puts the new brightness on both.
    rgb_matrix::FrameCanvas *previous = matrix_->SwapOnVSync(back_buffer_);
    previous->CopyFrom(*back_buffer_);
    back_buffer_ = previous;
}
