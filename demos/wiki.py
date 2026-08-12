#!/usr/bin/env python3
"""The encyclopedia being written, live: forty seconds of Wikipedia's edit stream.

Every change to every Wikimedia wiki -- nine hundred of them, three hundred
languages -- goes out on one public event stream in real time, tens of edits a
second, forever. This panel is forty seconds of that firehose played back at its
own pace: **one coloured stroke per edit**, arriving right to left, rising above
the line for bytes added and falling below it for bytes taken away, with the
titles of the articles crawling underneath.

The titles are the point. They are absurd and human and never the same twice --
PHILADELPHIA, NEWTOWN AND NEW YORK RAILROAD; 2026 PACIFIC TYPHOON SEASON;
KATEDRALA SVATE REPARATY (NICE); MARGHERITA ZOEBELI -- and the whole design is
arranged so that a person walking past reads two or three of them and
understands, without being told, that this is the encyclopedia being written
right now by somebody they have never met.

**Every other data panel on this wall is a snapshot of a state.** Tides, grid
mix, air quality, aircraft: each is a picture of how things are. This is the
only one that is a stream of *events*, and it is drawn as one -- there is no
chart of the rate, there are the edits themselves, in the order and at the
spacing they actually happened. The burstiness is real. Where four edits land
inside a tenth of a second the strokes pile into a picket; where the stream
draws breath there is a gap. Aggregating that into a line chart would throw away
the only thing a firehose has that a snapshot does not.

**Three encodings, no legend needed for two of them.**

  * **Up or down** is add or remove. Nothing else uses the vertical axis, so
    there is no ambiguity to resolve at three metres: a panel leaning upwards is
    an encyclopedia growing, which most minutes it is (a typical window adds
    680 kB and removes 55 kB).
  * **Height** is the size of the edit, on a square-root axis against a 900-byte
    full scale. A typo fix is two pixels, a paragraph is six, somebody pasting a
    filmography is the full height. Square root rather than linear because the
    median edit is thirty bytes and the largest in a window is twenty thousand,
    and a linear axis draws the median as nothing at all.
  * **Colour** is the project, hue by language and saturation by family, so the
    English, Spanish and Basque Wikipedias are three different colours and
    fr.wikipedia and fr.wiktionary are two shades of one. This is the part that
    needs the key, and the key is the row of chips under the header.

**Brightness is the bot share**, and it is the fact the panel most wants to
communicate. Somewhere between half and two thirds of all edits are made by
software -- category maintenance, interwiki links, Commons file housekeeping,
Wikidata bots grinding through a database import -- and they are drawn at 40% of
a human edit's brightness. The result is a dim churning mass with bright human
strokes standing out of it, which is exactly the true shape of the thing, and
the number is printed anyway.

**Why the titles are Latin-script only, and how the panel stays honest about
it.** The font is 3x5 pixels: A-Z, digits, a dozen punctuation marks. Roughly
44% of main-space titles in any window are Cyrillic, CJK, Devanagari, Arabic or
Hebrew, and a wall of tofu boxes is a failure and not a compromise. So the panel
splits the two channels: **colour carries every project, including the ones
whose script cannot be drawn; type carries only the ones that can.** The strokes
from ru.wikipedia and zh.wikisource are up there in their own colours doing
their share of the work; their titles simply do not appear in the crawl, and the
legend says LATIN TITLES so nobody concludes the encyclopedia is written in
English. Accented Latin is folded rather than dropped (see ftdata.py), which is
what keeps es, fr, cs, pt and eu in the crawl instead of only en.

**It is a recording and it says so.** The wall cannot hold a socket open, so
ftdata.py opens the stream, listens for forty seconds, aggregates, and hangs up
every fifteen minutes. What is on the panel is therefore the last window
Wikipedia published, and the age of it is in the top right corner like every
other data panel here. Nothing in this module touches the network.

**Privacy.** The stream carries `user` -- a username, or a bare IP address for
an anonymous editor. It never reaches this file, because it never reaches the
record: ftdata.py drops it, drops the edit summaries, drops the revision ids
that could be looked up to recover it, and keeps only namespace-0 titles so that
somebody's user page cannot arrive as an article name. See the comment block
above `_wiki_stream()`.

**Frame budget.** The entire window -- every stroke, every title -- is
rasterised once in `build()` into one strip 780 pixels wide, and `render()` is
two slice copies: the static header onto the top eleven rows, and a moving
window into the strip onto the other fifty-two. At the default twenty pixels a
second and twenty frames a second the strip advances exactly one pixel a frame,
which is the smoothest a crawl can be.

Run:  python3 ftdata.py --once --only wiki-stream
      python3 wiki.py --host 127.0.0.1
      FT_DATA_CACHE=/tmp/empty python3 wiki.py      # the no-data card
      python3 scripts/test-wiki.py
"""

import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

PRODUCT = "wiki-stream"


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font -- the one caiso, bgp, propagation, sort and tide
# all draw with -- five rows a glyph, each row an octal digit whose three bits
# are the three columns.
#
# It ships with A-Z, the digits and `- . / :`, which is everything a prefix or a
# megawatt figure needs and nowhere near enough for an article title. Titles are
# full of apostrophes, commas and parentheses -- ST. MICHAEL'S ABBEY (ORANGE
# COUNTY, CALIFORNIA) uses four of them in one line -- and a title rendered with
# those silently dropped is a different title. So nine glyphs are added here, in
# a copy of the dict; defcon.py itself is not touched.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({
    "'": ("2", "2", "0", "0", "0"),
    ",": ("0", "0", "0", "2", "4"),
    ";": ("0", "2", "0", "2", "4"),
    "(": ("1", "2", "2", "2", "1"),
    ")": ("4", "2", "2", "2", "4"),
    "!": ("2", "2", "2", "0", "2"),
    "?": ("6", "1", "2", "0", "2"),
    "&": ("6", "6", "3", "5", "3"),
    "+": ("0", "2", "7", "2", "0"),
    "*": ("0", "5", "2", "5", "0"),
    "=": ("0", "7", "0", "7", "0"),
    "%": ("5", "1", "2", "4", "5"),
    "_": ("0", "0", "0", "0", "7"),
    '"': ("5", "5", "0", "0", "0"),
})

_GLYPHS = {}
for _ch, _rows in _FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((5 * scale, 1), bool)
    out = np.zeros((5, len(s) * 4 - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def text_width(s, scale=1):
    return max(1, (len(str(s)) * 4 - 1) * scale)


def blit_text(dst, y, x, s, rgb, scale=1):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn."""
    m = text_mask(s, scale)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = rgb
    return gw


def blit_wrapped(dst, y, x, s, rgb, scale=1):
    """Draw a string into a strip that wraps horizontally.

    The strip is a loop -- the end of the window butts against its beginning --
    so a title starting near the right-hand end has to run off the edge and
    reappear at column zero, or it would be cut in half once every pass. Two
    blits, one at x and one at x - width, and the clipping in blit_text() takes
    care of whichever is off the end.
    """
    w = dst.shape[1]
    blit_text(dst, y, x, s, rgb, scale)
    if x + text_width(s, scale) > w:
        blit_text(dst, y, x - w, s, rgb, scale)


# --------------------------------------------------------------------------
# Colour: hue by language, saturation and value by project family.
#
# Two schemes were tried before this one. A hue straight off a hash of the wiki
# name gives 40 unrelated colours, which is confetti: pretty for a second and
# unreadable after that, and it puts en.wikipedia and en.wiktionary in
# unrelated places. A hue by *family* alone -- all Wikipedias blue, all
# Wiktionaries green -- reads beautifully and throws away the language, which is
# the more interesting axis, because "these forty seconds are Basque, Spanish,
# Japanese and Czech at the same time" is the fact worth carrying.
#
# So: language picks the hue, family picks how saturated and bright it is.
# fr.wikipedia and fr.wiktionary are two shades of the same violet; es and eu
# are different colours. The ten or so languages that actually turn up in
# quantity are pinned to hues far apart from each other, because those are the
# ones somebody might learn; everything else takes a stable hash, which cannot
# be learned but is at least consistent from one fetch to the next.
#
# Commons and Wikidata have no language at all -- they are the shared file
# repository and the shared fact database -- and between them they are usually
# half the stream. They get colours nothing else uses: Commons a dusty gold,
# Wikidata a pale steel blue. On a busy panel those two are most of the mass,
# and having them be the two least saturated things on it is deliberate: the
# language projects, which are the interesting minority, stay the brightest.
# --------------------------------------------------------------------------

_LANG_HUE = {
    "en": 0.58, "es": 0.08, "fr": 0.74, "de": 0.14, "ja": 0.94,
    "zh": 0.99, "ru": 0.80, "it": 0.34, "pt": 0.45, "ar": 0.39,
    "nl": 0.19, "pl": 0.87, "sv": 0.54, "eu": 0.66, "ceb": 0.29,
    "vi": 0.49, "uk": 0.83, "fa": 0.42, "simple": 0.61, "cs": 0.90,
    "hu": 0.11, "ko": 0.92, "tr": 0.05, "id": 0.25, "he": 0.70,
    "fi": 0.47, "no": 0.56, "da": 0.52, "ca": 0.16, "sr": 0.77,
    "fa": 0.42, "hi": 0.03, "th": 0.36, "el": 0.63, "ro": 0.22,
    "bn": 0.31, "mg": 0.27, "war": 0.68, "sh": 0.85, "ur": 0.37,
}

# Family -> (saturation, value). A Wikipedia article is the thing most people
# mean by "an edit", so it is the loudest; the sister projects step down.
_FAMILY_SV = {
    "wikipedia": (0.95, 1.00),
    "wiktionary": (0.80, 0.78),
    "wikisource": (0.62, 0.70),
    "wikiquote": (0.62, 0.66),
    "wikibooks": (0.62, 0.66),
    "wikinews": (0.62, 0.66),
    "wikivoyage": (0.62, 0.66),
    "wikiversity": (0.62, 0.66),
    "other": (0.30, 0.62),
}

# The multilingual projects, which have no language code to take a hue from.
_SPECIAL = {
    "commonswiki": (0.12, 0.50, 0.92),      # dusty gold: files
    "wikidatawiki": (0.545, 0.30, 0.86),    # pale steel: facts
    "metawiki": (0.0, 0.0, 0.62),
    "specieswiki": (0.30, 0.35, 0.70),
    "mediawikiwiki": (0.0, 0.0, 0.55),
    "incubatorwiki": (0.0, 0.0, 0.55),
    "sourceswiki": (0.62, 0.35, 0.66),
    "foundationwiki": (0.0, 0.0, 0.55),
    "outreachwiki": (0.0, 0.0, 0.55),
    "wikifunctionswiki": (0.545, 0.30, 0.70),
}

_FAMILIES = ("wiktionary", "wikisource", "wikiquote", "wikibooks", "wikinews",
             "wikivoyage", "wikiversity", "wikipedia")


def split_wiki(name):
    """('en', 'wikipedia') out of 'enwiki'; ('fr', 'wiktionary') out of
    'frwiktionary'. Returns (None, 'other') for the multilingual projects.

    The database names are the language code with the family glued on, and
    'wiki' on its own means Wikipedia -- enwiki, not enwikipedia -- which is the
    one irregular case and the commonest one.
    """
    name = str(name or "")
    if name in _SPECIAL:
        return None, "other"
    for fam in _FAMILIES:
        if name.endswith(fam):
            return name[:-len(fam)], fam
    if name.endswith("wiki"):
        return name[:-4], "wikipedia"
    return None, "other"


def stable_hue(s):
    """A hue in 0..1 from a string, the same on every machine and every run.

    Deliberately not `hash()`, which is salted per process in Python 3 and would
    give the Faroese Wikipedia a different colour every time ftsched restarted.
    """
    h = 2166136261
    for ch in str(s):
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return (h % 997) / 997.0


def project_rgb(name):
    """(r, g, b) for a wiki's database name, as floats 0..255."""
    if name in _SPECIAL:
        hue, sat, val = _SPECIAL[name]
    else:
        lang, fam = split_wiki(name)
        hue = _LANG_HUE.get(lang, stable_hue(lang or name))
        sat, val = _FAMILY_SV.get(fam, _FAMILY_SV["other"])
    rgb = ds.hsv_to_rgb(np.array(hue, f32), np.array(sat, f32),
                        np.array(val, f32))
    return np.clip(np.asarray(rgb, f32) * 255.0, 0, 255)


def project_label(name):
    """A short human tag for the legend: EN, FR-WKT, COMMONS, WIKIDATA."""
    short = {"commonswiki": "COMMONS", "wikidatawiki": "WIKIDATA",
             "metawiki": "META", "specieswiki": "SPECIES",
             "mediawikiwiki": "MEDIAWIKI", "incubatorwiki": "INCUBATOR",
             "sourceswiki": "SOURCES", "wikifunctionswiki": "FUNCTIONS"}
    if name in short:
        return short[name]
    lang, fam = split_wiki(name)
    if not lang:
        return str(name).upper()[:9]
    suffix = {"wikipedia": "", "wiktionary": "-WKT", "wikisource": "-SRC",
              "wikiquote": "-QUO", "wikibooks": "-BKS", "wikinews": "-NWS",
              "wikivoyage": "-VOY", "wikiversity": "-UNI"}.get(fam, "-?")
    return (lang.upper() + suffix)[:10]


# The furniture, in the same cool near-monochrome the other data panels use, so
# that every saturated pixel on the panel is an edit and nothing else is.
C_TEXT = (198, 214, 232)
C_DIM = (86, 104, 126)
C_DIMMER = (52, 64, 80)
C_LINE = (26, 34, 46)
C_SEP = (16, 22, 30)
C_ADD = (120, 230, 150)
C_DEL = (240, 120, 110)
C_NEW = (255, 255, 235)
C_WARN = (255, 110, 90)

# How much of a human edit's brightness a bot edit gets. Low enough that the
# human strokes stand out of the mass at three metres, high enough that the
# bots are plainly the majority of what is there -- which is the fact.
BOT_DIM = 0.40

# How many projects the key names before it gives up and says "+34 more". Five,
# because the row has two other things to say -- how many projects were left out,
# and that the crawl below is Latin script only -- and both of those are more
# informative than a sixth chip for a wiki that contributed nine edits.
MAX_CHIPS = 5


# --------------------------------------------------------------------------
# Reading what ftdata left behind. `load()` never raises, so everything still
# capable of being wrong is wrong about content, and is caught here.
# --------------------------------------------------------------------------

def read_stream(cache_dir):
    """(record, age, problem). `record` is None if there is nothing drawable."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached wiki record"
    payload, age = got
    try:
        ms = [int(v) for v in payload["ms"]]
        d = [int(v) for v in payload["d"]]
        pi = [int(v) for v in payload["pi"]]
        fl = [int(v) for v in payload["f"]]
        names = [str(v) for v in payload["pnames"]]
        secs = float(payload["secs"])
    except Exception:                                        # noqa: BLE001
        return None, age, "wiki record is malformed"
    n = len(ms)
    # Two is the floor rather than a plausible-looking fifty: the fetcher
    # already refuses to store a window with fewer than eight edits in it, so
    # anything this catches is a corrupt or half-written record and not a quiet
    # night on Wikipedia. A threshold with an opinion in it would eventually
    # throw away a real record for having the wrong shape.
    if n < 2 or not (len(d) == len(pi) == len(fl) == n) or secs <= 0 or not names:
        return None, age, "wiki record has no usable window"

    titles = []
    for row in (payload.get("titles") or []):
        try:
            t_ms, p, delta, text = int(row[0]), int(row[1]), int(row[2]), str(row[3])
        except Exception:                                    # noqa: BLE001
            continue
        if text and 0 <= p < len(names):
            titles.append((t_ms, p, delta, text))

    rec = {
        "ms": np.asarray(ms, np.int32), "d": np.asarray(d, np.int32),
        "pi": np.asarray(pi, np.int32), "f": np.asarray(fl, np.int32),
        "names": names, "secs": secs, "n": n,
        "per_s": float(payload.get("per_s", n / secs)),
        "all_per_s": float(payload.get("all_per_s", 0.0)),
        "bot_pct": float(payload.get("bot_pct", 0.0)),
        "n_new": int(payload.get("n_new", 0)),
        "n_projects": int(payload.get("n_projects", len(names))),
        "add_bytes": int(payload.get("add_bytes", 0)),
        "del_bytes": int(payload.get("del_bytes", 0)),
        "projects": [(str(a), int(b)) for a, b in
                     (payload.get("projects") or []) if b],
        "titles": titles, "age": age,
    }
    return rec, age, None


def kilo(n):
    """682000 -> '682K'; 1240000 -> '1.2M'. The font has a full stop in it."""
    n = abs(int(n))
    if n < 1000:
        return "%d" % n
    if n < 1000000:
        return "%dK" % int(round(n / 1000.0))
    return "%.1fM" % (n / 1000000.0)


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--speed", type=float, default=20.0,
                    help="pixels per second of stream time (20 = 1 px/frame)")
    ap.add_argument("--full", type=float, default=900.0,
                    help="stroke full-scale in bytes changed")
    ap.add_argument("--lanes", type=int, default=3,
                    help="rows of scrolling article titles")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")


# --------------------------------------------------------------------------
# Layout. Two blocks: a fixed head (identity, rate, key, age) and one scrolling
# band that holds everything else. The split is what makes render() two slice
# copies, and it is also the right answer visually -- the numbers must not move,
# and the stream must never stop.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h, max_lanes=3):
        self.w, self.h = w, h
        self.head_h = 5 if h >= 20 else 0
        self.legend_h = 5 if (h >= 44 and w >= 160) else 0
        self.band_y = self.head_h + (1 if self.head_h else 0) \
            + self.legend_h + (1 if self.legend_h else 0)
        band = h - self.band_y
        # Titles want three lanes of six rows plus a row of air. Below about
        # forty rows of band there is no room for both a stream worth looking at
        # and any type at all, and the stream is the demo.
        self.lanes = 0
        if band >= 44 and w >= 160:
            self.lanes = 3
        elif band >= 34 and w >= 160:
            self.lanes = 2
        elif band >= 26 and w >= 120:
            self.lanes = 1
        self.lanes = max(0, min(self.lanes, int(max_lanes)))
        self.title_h = self.lanes * 7 + (1 if self.lanes else 0)
        self.stream_h = max(4, band - self.title_h)
        self.stream_y = self.band_y
        # The zero line sits at the middle of the stream region. Additions and
        # removals get the same room even though additions are twenty times the
        # bytes: an asymmetric axis would make a removal look bigger than it is,
        # and the one number this panel must not exaggerate is a deletion.
        self.mid = self.stream_y + self.stream_h // 2
        self.up_rows = self.mid - self.stream_y
        self.dn_rows = self.stream_y + self.stream_h - self.mid - 1
        self.title_y = self.stream_y + self.stream_h + 1

    def lane_y(self, i):
        return self.title_y + i * 7


# --------------------------------------------------------------------------
# Baking the window into one strip.
# --------------------------------------------------------------------------

def bake_strip(rec, lay, args):
    """Every edit and every title, rasterised once, as a (band_h, S, 3) tile.

    The strip is the window laid out along its own time axis at `--speed`
    pixels a second, so the horizontal position of a stroke *is* when the edit
    happened. Scrolling it at the same speed replays the window at 1:1, and the
    burstiness of the real stream survives intact rather than being averaged
    into a rate.

    Everything wraps modulo the strip width, because the panel loops: the end of
    the forty seconds butts straight against the beginning and a stroke or a
    title crossing that seam has to come back round rather than be cut off.
    """
    band_h = lay.h - lay.band_y
    strip_w = max(lay.w + 1, int(round(rec["secs"] * args.speed)))

    ms = rec["ms"]
    cols = (ms.astype(np.float64) * (args.speed / 1000.0)).astype(np.int32) \
        % strip_w
    d = rec["d"].astype(np.float64)
    full = max(1.0, float(args.full))
    # Square root against a 900-byte full scale. See the module docstring: the
    # median edit is thirty bytes and the biggest in a window is twenty
    # thousand, so a linear axis would draw the median -- which is most of the
    # panel -- as nothing.
    frac = np.sqrt(np.minimum(np.abs(d), full) / full)
    up = d >= 0
    heights = np.maximum(1, np.round(
        frac * np.where(up, lay.up_rows, lay.dn_rows))).astype(np.int32)

    # One colour per event: the project's, dimmed if a bot made it.
    palette = np.stack([project_rgb(n) for n in rec["names"]])   # (P, 3) float
    ev_rgb = palette[rec["pi"]]
    ev_rgb = ev_rgb * np.where((rec["f"] & 1) != 0, BOT_DIM, 1.0)[:, None]

    # Collapse onto columns. At twenty pixels a second and fifteen edits a
    # second two edits regularly land in the same column; the taller one wins
    # rather than the later one, so a burst is never hidden behind a typo fix.
    # A Python loop over ~600 events, once, in build(): the vectorised
    # alternative is np.maximum.at, which is slower than this on the wall.
    up_h = np.zeros(strip_w, np.int32)
    dn_h = np.zeros(strip_w, np.int32)
    up_c = np.zeros((strip_w, 3), f32)
    dn_c = np.zeros((strip_w, 3), f32)
    new_marks = []
    for i in range(len(cols)):
        c, hh = int(cols[i]), int(heights[i])
        if up[i]:
            if hh >= up_h[c]:
                up_h[c] = hh
                up_c[c] = ev_rgb[i]
        else:
            if hh >= dn_h[c]:
                dn_h[c] = hh
                dn_c[c] = ev_rgb[i]
        if rec["f"][i] & 2:
            new_marks.append((c, hh, bool(up[i])))

    strip = np.zeros((band_h, strip_w, 3), np.uint8)
    y0 = lay.stream_y - lay.band_y
    mid = lay.mid - lay.band_y

    # The two bands, each in one vectorised pass. "Distance from the zero line"
    # rather than "row index" is what makes both directions the same expression
    # and leaves no off-by-one at the line itself.
    if lay.up_rows > 0:
        dist = (mid - np.arange(y0, mid))[:, None]              # up_rows..1
        lit = dist <= up_h[None, :]
        strip[y0:mid] = np.where(lit[:, :, None], up_c[None, :, :], 0.0
                                 ).astype(np.uint8)
    if lay.dn_rows > 0:
        bot = mid + 1 + lay.dn_rows
        dist = (np.arange(mid + 1, bot) - mid)[:, None]         # 1..dn_rows
        lit = dist <= dn_h[None, :]
        strip[mid + 1:bot] = np.where(lit[:, :, None], dn_c[None, :, :], 0.0
                                      ).astype(np.uint8)

    # The zero line, under nothing and over nothing: a dim rule the whole way
    # across, brightened where an edit actually sits so the line reads as a
    # ground the strokes stand on rather than a wire drawn behind them.
    strip[mid] = C_LINE
    touched = (up_h > 0) | (dn_h > 0)
    strip[mid, touched] = (58, 72, 92)

    # New pages -- `type: new`, an article that did not exist a second ago --
    # get a white pip at the tip of their stroke. A few dozen a window, and they
    # are the most interesting single event in the stream.
    for c, hh, is_up in new_marks:
        y = mid - hh - 1 if is_up else mid + hh + 1
        if 0 <= y < band_h:
            strip[y, c] = C_NEW

    # Titles, packed greedily into lanes so that none of them overlaps another.
    # Each keeps its own arrival time as its start column, so a title crawls in
    # underneath the stroke it belongs to -- which is the whole reason the crawl
    # is horizontal and at the stream's own speed rather than a ticker of its
    # own. What that costs is that only the fifteen or so that fit are shown,
    # and which ones is decided by the packer rather than by importance; there
    # is no importance in this data to decide by.
    drawn = 0
    if lay.lanes:
        ends = [-10] * lay.lanes       # first free column in each lane
        first = [None] * lay.lanes     # where that lane's earliest title starts
        gap = 12
        for t_ms, p, delta, text in rec["titles"]:
            x = int(t_ms * args.speed / 1000.0)
            if x >= strip_w:
                continue
            width = 4 + text_width(text)
            if width + gap >= strip_w:
                continue
            lane = None
            for i in range(lay.lanes):
                if x < ends[i]:
                    continue
                # A title near the end of the strip runs off it and comes back
                # at column zero -- where this lane's first title already is.
                # Without this check the two overlap once a pass, and the
                # collision is exactly at the seam where it looks like a bug
                # rather than like two titles. Cost: the last title or two in
                # a lane is dropped.
                tail = x + width + gap - strip_w
                if tail > 0 and first[i] is not None and tail > first[i]:
                    continue
                lane = i
                break
            if lane is None:
                continue
            y = lay.lane_y(lane) - lay.band_y
            rgb = project_rgb(rec["names"][p])
            if rgb.max() < 120:                # keep the dim projects legible
                rgb = rgb * (150.0 / max(1.0, rgb.max()))
            # A two-pixel chip in the project's colour, then the title in a
            # cooler version of it: the chip ties the line to the strokes above
            # without the type itself being a saturated colour, which at 3x5 is
            # the difference between readable and not.
            strip[y:y + 5, x % strip_w:x % strip_w + 2] = np.clip(rgb, 0, 255)
            blit_wrapped(strip, y, (x + 4) % strip_w, text,
                         tuple(int(v) for v in np.clip(
                             rgb * 0.45 + 130.0, 0, 255)))
            ends[lane] = x + width + gap
            if first[lane] is None:
                first[lane] = x
            drawn += 1

    # One screen of the beginning tacked onto the end, so that reading a
    # window of `w` columns at any offset is one contiguous slice with no
    # wrap-around arithmetic in the hot path.
    tile = np.concatenate([strip, strip[:, :lay.w]], axis=1)
    return tile, strip_w, drawn


# --------------------------------------------------------------------------
# The head: identity, the rate, the byte balance, the age -- and under it the
# colour key and the bot share. The ladder-of-shorter-forms shape caiso, tide
# and bgp use, because clipping this line loses the end of it, and the end of it
# is how old the data is.
# --------------------------------------------------------------------------

def header_text(rec, stale, w):
    if rec is None:
        return "NO WIKI DATA", "", ""
    lefts = ["WIKIMEDIA %d EDITS/S" % int(round(rec["per_s"])),
             "WIKIMEDIA %d/S" % int(round(rec["per_s"])),
             "%d EDITS/S" % int(round(rec["per_s"]))]
    mids = ["+%s -%s BYTES" % (kilo(rec["add_bytes"]), kilo(rec["del_bytes"])),
            "+%s -%s" % (kilo(rec["add_bytes"]), kilo(rec["del_bytes"])),
            "+%s" % kilo(rec["add_bytes"]), ""]
    age = ftdata.describe_age(rec["age"])
    rights = ["%dS WINDOW  %s AGO" % (int(round(rec["secs"])), age),
              "%s AGO" % age, age, ""]
    if stale:
        rights = [("STALE " + r if r else "STALE") for r in rights]
    for left in lefts:
        for right in rights:
            for mid in mids:
                need = text_width(left) + text_width(right) + 2
                if mid:
                    need += text_width(mid) + 10
                if need <= w:
                    return left, mid, right
    return lefts[-1], "", ""


def draw_head(dst, lay, rec, stale, drawn):
    """The header line, the separator, and the key row under it."""
    if lay.head_h:
        left, mid, right = header_text(rec, stale, lay.w)
        blit_text(dst, 0, 1, left, C_TEXT)
        rw = text_width(right) if right else 0
        if right:
            blit_text(dst, 0, lay.w - rw - 1, right,
                      C_WARN if stale else C_DIM)
        if mid:
            mw = text_width(mid)
            mx = min(lay.w - rw - 5 - mw,
                     max(text_width(left) + 6, (lay.w - mw) // 2))
            blit_text(dst, 0, mx, mid, C_DIM)
        dst[lay.head_h] = C_SEP

    if not lay.legend_h or rec is None:
        return
    y = lay.head_h + 1

    # Right-hand end first, because it is the part with a number in it and the
    # chips are what gives way when the panel is narrow. A two-segment bar --
    # bots on the left in grey, humans on the right in white -- with the
    # percentage printed on the end of it. "More than half of the edits to
    # Wikipedia are made by software" is a fact most people do not know and the
    # bar is how they find it out in the second before they read the number.
    bot = max(0.0, min(100.0, rec["bot_pct"]))
    bar_w = 40 if lay.w >= 240 else 24
    tag = "BOT %d%%" % int(round(bot))
    right_w = bar_w + 3 + text_width(tag)
    bx = lay.w - right_w - 1
    nbot = int(round(bar_w * bot / 100.0))
    dst[y + 1:y + 4, bx:bx + nbot] = C_DIMMER
    dst[y + 1:y + 4, bx + nbot:bx + bar_w] = C_TEXT
    blit_text(dst, y, bx + bar_w + 3, tag, C_DIM)

    # The colour key: the projects that actually contributed, biggest first,
    # until the row runs out. A key for forty wikis is impossible and would be
    # the wrong thing anyway -- what a person needs is to recognise the four
    # colours covering most of the panel, and to see that there are many more.
    # ...and, if there is room for it, the one caveat that matters: the crawl
    # below is only the titles this font can spell. Without it a viewer would
    # reasonably conclude that the encyclopedia is written in Latin script,
    # which the colours directly beside it are busy disproving. It is claimed
    # before the chips are laid out rather than squeezed in afterwards, because
    # a sixth chip is worth less than this sentence.
    note = "LATIN TITLES" if (lay.lanes and drawn) else ""
    if note and text_width(note) + 40 > bx:
        note = ""
    limit = bx - 6 - (text_width(note) + 8 if note else 0)
    if note:
        blit_text(dst, y, bx - 6 - text_width(note), note, C_DIMMER)

    x, shown = 1, 0
    for name, _count in rec["projects"][:MAX_CHIPS]:
        label = project_label(name)
        need = 3 + 2 + text_width(label) + 6
        if x + need > limit:
            break
        dst[y:y + 5, x:x + 3] = np.clip(project_rgb(name), 0, 255)
        blit_text(dst, y, x + 5, label, C_DIM)
        x += need
        shown += 1
    rest = max(0, rec["n_projects"] - shown)
    for extra in ("+%d MORE PROJECTS" % rest, "+%d MORE" % rest, "+%d" % rest):
        if rest and x + text_width(extra) <= limit:
            blit_text(dst, y, x, extra, C_DIMMER)
            break
    dst[y + lay.legend_h] = C_SEP


def draw_nodata(dst, lay, lines):
    """The honest panel: no strokes, no titles, no implied encyclopedia."""
    dst[:] = (4, 6, 10)
    scale = 2 if (lay.h >= 32 and lay.w >= 200) else 1
    y = max(0, lay.h // 2 - (len(lines) * (6 * scale + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += 5 * sc + 3
    return dst


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h, args.lanes)
    cache = args.cache_dir

    frame = np.zeros((h, w, 3), np.uint8)
    head = np.zeros((h, w, 3), np.uint8)      # the fixed top, baked once

    cell = {"rec": None, "problem": None, "loaded": -1e18, "stale": False,
            "tile": None, "strip_w": 1, "drawn": 0}

    def reload_data(now):
        rec, age, problem = read_stream(cache)
        cell["rec"], cell["problem"], cell["loaded"] = rec, problem, now
        head[:] = 0
        if rec is None:
            cell["stale"], cell["tile"] = False, None
            return
        cell["stale"] = not ftdata.is_fresh(PRODUCT, age)
        tile, strip_w, drawn = bake_strip(rec, lay, args)
        cell["tile"], cell["strip_w"], cell["drawn"] = tile, strip_w, drawn
        draw_head(head, lay, rec, cell["stale"], drawn)

    def render(t, i):
        now = time.time()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        if cell["rec"] is None or cell["tile"] is None:
            lines = [("NO WIKI DATA", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --ONCE --ONLY WIKI-STREAM",
                      C_TEXT)]
            if cell["problem"]:
                lines.append((str(cell["problem"]).upper()[:56], C_DIM))
            return draw_nodata(frame, lay, lines)

        # Two copies and nothing else. The head never changes; the band is one
        # contiguous slice of the tile, which is why the tile carries a screen
        # of its own beginning stitched onto its end.
        frame[:lay.band_y] = head[:lay.band_y]
        o = int(t * args.speed) % cell["strip_w"]
        frame[lay.band_y:] = cell["tile"][:, o:o + w]
        return frame

    reload_data(time.time())
    render.state = cell               # tests reach in here; nothing else does
    render.layout = lay
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "The Wikipedia edit firehose: forty seconds, played back",
                  fps=20)


if __name__ == "__main__":
    main()
