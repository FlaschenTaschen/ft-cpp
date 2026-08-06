"""Writing a preview GIF, shared by the two things that make them.

Kept apart from both callers because the palette handling is the fiddly part
and is worth having in exactly one place: quantising each frame on its own
makes the colours crawl between frames, which on a gradient -- and most of
these are gradients -- is more distracting than the banding it avoids.
"""

import os


def save(shots, path, fps):
    """Write RGB PIL frames as a looping GIF, on one shared palette."""
    from PIL import Image

    shots = [s for s in shots if s is not None]
    if not shots:
        raise ValueError("nothing to save")

    if len(shots) == 1:
        quantised = [shots[0].quantize(colors=128, method=Image.MEDIANCUT)]
    else:
        # Stack the clip into one tall image, quantise once, cut it back up:
        # one palette for every frame, chosen over the whole clip.
        w, h = shots[0].size
        tall = Image.new("RGB", (w, h * len(shots)))
        for n, im in enumerate(shots):
            tall.paste(im, (0, n * h))
        pal = tall.quantize(colors=128, method=Image.MEDIANCUT)
        quantised = [pal.crop((0, n * h, w, (n + 1) * h))
                     for n in range(len(shots))]

    # Written to a temp name and renamed, so a killed run cannot leave a
    # truncated GIF that the UI would then serve forever. The temp name has no
    # useful extension, hence the explicit format.
    tmp = path + ".tmp"
    quantised[0].save(tmp, format="GIF", save_all=True,
                      append_images=quantised[1:],
                      duration=int(round(1000.0 / fps)), loop=0, optimize=True)
    os.replace(tmp, path)
    return os.path.getsize(path)
