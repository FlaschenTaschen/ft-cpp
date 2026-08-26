"""Writing a preview clip, shared by the two things that make them.

Kept apart from both callers because the palette handling is the fiddly part
and is worth having in exactly one place: quantising each frame on its own
makes the colours crawl between frames, which on a gradient -- and most of
these are gradients -- is more distracting than the banding it avoids.

The format is animated WebP, losslessly encoded. These were GIFs, which cost
about a third more for pixels that were also worse: GIF's compression is weak
enough that the palette had to be cut to 128 colours to keep the files
reasonable, which is real damage on a wall whose whole business is gradients.
WebP's lossless mode compresses a paletted image so much better that 256
colours in WebP still come out smaller than 128 in GIF -- measured across this
rotation, 26% smaller *and* strictly closer to the frames the demo rendered,
with several of them landing exact.

Lossy WebP was measured too and is a trap here. These clips are 320x64 of
dithered noise and hard pixel edges, which is the worst case for a DCT: at a
quality that matched GIF's error it was no smaller, and at any size below GIF
it looked worse than the GIF it would have replaced.

Animated WebP has been supported by every current browser for years (Chrome
32, Firefox 65, Safari 14). What it is not is a file you can drop into an
ancient image viewer, which is not what these are for.
"""

import os

COLOURS = 256                                # what the shared palette holds
SUFFIX = ".webp"


def quantise(shots):
    """-> paletted frames sharing one palette chosen over the whole clip."""
    from PIL import Image

    if len(shots) == 1:
        return [shots[0].quantize(colors=COLOURS, method=Image.MEDIANCUT)]
    # Stack the clip into one tall image, quantise once, cut it back up: one
    # palette for every frame, chosen over the whole clip.
    w, h = shots[0].size
    tall = Image.new("RGB", (w, h * len(shots)))
    for n, im in enumerate(shots):
        tall.paste(im, (0, n * h))
    pal = tall.quantize(colors=COLOURS, method=Image.MEDIANCUT)
    return [pal.crop((0, n * h, w, (n + 1) * h)) for n in range(len(shots))]


def save(shots, path, fps):
    """Write RGB PIL frames as a looping animated WebP, on one palette."""
    shots = [s for s in shots if s is not None]
    if not shots:
        raise ValueError("nothing to save")

    # Quantise first, then hand WebP RGB. The quantiser is what chooses the
    # 256 colours -- WebP rediscovers the palette by itself and stores it as
    # one -- so the colour selection stays the deliberate, whole-clip one
    # rather than whatever the encoder would have picked per frame.
    frames = [f.convert("RGB") for f in quantise(shots)]

    # Written to a temp name and renamed, so a killed run cannot leave a
    # truncated file that the UI would then serve forever. The temp name has
    # no useful extension, hence the explicit format.
    tmp = path + ".tmp"
    frames[0].save(tmp, format="WEBP", save_all=True,
                   append_images=frames[1:],
                   duration=int(round(1000.0 / fps)), loop=0,
                   lossless=True, quality=100, method=6)
    os.replace(tmp, path)
    return os.path.getsize(path)
