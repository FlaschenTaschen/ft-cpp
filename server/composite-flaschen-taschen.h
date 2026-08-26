// -*- mode: c++; c-basic-offset: 4; indent-tabs-mode: nil; -*-
// Copyright (C) 2016 Henner Zeller <h.zeller@acm.org>
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

#ifndef COMPOSITE_FLASCHEN_TASCHEN_H_
#define COMPOSITE_FLASCHEN_TASCHEN_H_

#include "flaschen-taschen.h"

#include <stddef.h>

#include <vector>

namespace ft {
class Mutex;
}

// A composite screen allows a layered screen: The lowest layer is the
// background, but layers can be stacked, higher number is on front.
//
// In layers above the background (> 0), 'black' is seen as transparent.
// That way, it is easy to do independent overlays (announcement texts above
// background), or sprites.
//
// Layers above the background can automatically be garbage collected so that
// leftover content does not permanently obstruct the view.
class CompositeFlaschenTaschen : public FlaschenTaschen {
public:
    // Does _not_ take over ownership of delegatee.
    CompositeFlaschenTaschen(FlaschenTaschen *delegatee, int layers);
    ~CompositeFlaschenTaschen();

    virtual int width() const { return width_; }
    virtual int height() const { return height_; }

    // Set pixel to the currently configured layer (see SetLayer()).
    virtual void SetPixel(int x, int y, const Color &col);
    virtual void Send();

    // -- Layering features

    // Set layer for subsequent SetPixel() operations.
    void SetLayer(int layer);

    // Start a garbage collection thread that cleans
    // overlay layers if they haven't been touched in more than
    // "timeout_seconds". Uses mutex for exclusive access to display.
    void StartLayerGarbageCollection(ft::Mutex *lock,
                                     int timeout_seconds);

    // Black out every layer, background included, and send the result.
    // Unlike the garbage collector this does not spare layer 0, so it wipes
    // what is on the display rather than only what is stale. A client that is
    // still sending will of course paint over it on its next frame.
    // Must be called with the writer mutex held.
    void Clear();

private:
    typedef int Ticks;
    class ScreenBuffer;
    class ZBuffer;
    class LayerGarbageCollector;
    friend class LayerGarbageCollector;

    void SetPixelAtLayer(int x, int y, int layer, const Color &col);
    void SetTimeTicks(Ticks t) { current_time_ = t; }
    void ClearLayersOlderThan(Ticks t);

    // Black out one allocated layer, recompositing where it was visible.
    // Does not Send(); callers batch that.
    void ClearLayer(size_t layer);

    FlaschenTaschen *const delegatee_;
    const int width_;
    const int height_;
    int current_layer_;
    bool any_visible_pixel_drawn_;
    Ticks current_time_;

    std::vector<ScreenBuffer*> screens_;
    ZBuffer *z_buffer_;
    std::vector<Ticks> last_layer_update_time_;

    LayerGarbageCollector *garbage_collect_;
};

#endif // COMPOSITE_FLASCHEN_TASCHEN_H_
