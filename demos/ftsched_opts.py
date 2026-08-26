#!/usr/bin/env python3
"""What each demo can be configured with, read off the demo's own parser.

Every demo module already declares its options in add_arguments(), with a
type, a default, its choices and a line of help. That is a complete
description of what the effect can be told to do, it sits next to the code it
configures, and it cannot drift from it -- so the panel's editor is generated
from it rather than from a table here that somebody would have to keep in step
with twenty-nine demos and would not.

Reading it back means walking ArgumentParser._actions, which is private. There
is no public introspection API and there has not been one in the fifteen years
argparse has been in the standard library; the alternative is parsing --help
output, which is worse in every way. The shape used here -- option_strings,
dest, type, default, choices, nargs -- has been stable since 2.7 and is what
every other tool that has wanted this has used.

Two things the walk has to fix up:

  * A flag pair -- scroller's --plasma / --no-plasma, fireflies' --no-grass --
    is two actions writing one dest. They collapse to a single checkbox, whose
    value is the parser's default for the *dest* rather than either action's
    const, which is the only place the real default lives.
  * An option taking more than one value has no sensible widget, so it is left
    out rather than shown as a control that cannot round-trip it. No demo has
    one today; this is here so that adding one produces a missing field rather
    than a corrupted one.

check() is the other half. The editor is the only thing that writes options at
runtime, so this is where a bad value is turned into a sentence -- an unknown
option, a value outside the declared choices, a number that will not parse.
Catching it here rather than at build time is the difference between a 400
with an explanation in it and an effect that quietly switches itself off in
forty-five seconds' time when the builder gets to it.
"""

import threading

import demoscene as ds

# A message for the board, not a novel. splitflap's --messages is the longest
# default in the show at about 200 characters, and MAX_BODY in the API is 4 kB
# for the whole command.
MAX_TEXT = 512

# Nothing a demo takes is meaningfully larger than this, and it keeps a stray
# 1e308 out of a numpy allocation.
MAX_NUMBER = 1e9

_lock = threading.Lock()
_cache = {}                                  # module name -> [option, ...]
_base = None                                 # dests every demo gets for free


def _base_dests():
    """The options ds.parser() hands out: geometry and the frame loop.

    Taken from the parser rather than listed, so an option added there does
    not turn up in the editor as if it were the effect's.
    """
    global _base
    if _base is None:
        _base = frozenset(a.dest for a in ds.parser("demo")._actions)
    return _base


def _is_flag(action):
    """A store_true/store_false: no value, and a boolean const."""
    return action.nargs == 0 and isinstance(action.const, bool)


def _describe(module):
    ap = ds.parser(getattr(module, "__name__", "demo"))
    if not hasattr(module, "add_arguments"):
        return []
    module.add_arguments(ap)

    base = _base_dests()
    order, groups = [], {}
    for action in ap._actions:
        if action.dest in base or not action.option_strings:
            continue
        if action.dest not in groups:
            order.append(action.dest)
            groups[action.dest] = []
        groups[action.dest].append(action)

    out = []
    for dest in order:
        actions = groups[dest]
        if any(_is_flag(a) for a in actions):
            kind = "bool"
        elif any(a.nargs not in (None, 0) for a in actions):
            continue                         # multi-valued: no widget for it
        else:
            declared = next((a.type for a in actions if a.type), None)
            kind = {int: "int", float: "float"}.get(declared, "str")
        default = ap.get_default(dest)
        if not isinstance(default, (bool, int, float, str, type(None))):
            continue                         # not something JSON can carry
        choices = next((sorted(map(str, a.choices))
                        for a in actions if a.choices), None)
        out.append({
            # `name` is the dest, which is what ds.options() sets and what the
            # rotation file's "options" is keyed by. `label` is that made
            # readable; the flags are shown too, because the person editing
            # --min-cols from a phone is often the person who has run it from
            # a terminal.
            "name": dest,
            "label": dest.replace("_", "-"),
            "flags": [s for a in actions for s in a.option_strings],
            "type": kind,
            "default": default,
            "choices": choices,
            "help": "; ".join(a.help for a in actions if a.help),
        })
    return out


def schema(name, warn=None):
    """The options the demo module `name` declares. Cached; never raises.

    A module that will not import is not a reason to fail the request: it will
    fail its own build soon enough and be switched off there, with a better
    message. Here it is simply an effect with nothing to configure.
    """
    with _lock:
        if name in _cache:
            return _cache[name]
    try:
        found = _describe(__import__(name))
    except Exception as exc:
        if warn:
            warn("no options for %s (%s: %s)" % (name, type(exc).__name__, exc))
        found = []
    with _lock:
        _cache.setdefault(name, found)
        return _cache[name]


def prime(names, warn=None):
    """Import and describe a list of modules up front.

    Called once at startup, off the frame loop. Every one of these gets
    imported within a cycle anyway -- the builder does it the first time it
    builds each effect -- so this costs startup time rather than memory, and
    it buys a control panel that answers /api/schema out of a dict instead of
    importing twenty-nine modules on the HTTP thread while the wall is
    waiting on the GIL.
    """
    for name in names:
        schema(name, warn)


def check(name, options):
    """Validate {dest: value} against a module's schema.

    Returns a new dict holding each value in the type the demo declared, so
    what reaches the entry is already what build() will see. Raises ValueError
    with something worth showing a person.
    """
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    known = {o["name"]: o for o in schema(name)}
    clean = {}
    for key, value in options.items():
        spec = known.get(key)
        if spec is None:
            raise ValueError("%s has no option --%s"
                             % (name, str(key).replace("_", "-")))
        clean[key] = _one(name, spec, value)
    return clean


def _one(name, spec, value):
    label, kind = spec["label"], spec["type"]
    if kind == "bool":
        if isinstance(value, bool):
            out = value
        elif isinstance(value, str):
            out = value.strip().lower() in ("1", "true", "yes", "on")
        else:
            raise ValueError("%s --%s wants true or false" % (name, label))
    elif kind in ("int", "float"):
        if isinstance(value, bool):          # bool is an int; not here it isn't
            raise ValueError("%s --%s wants a number" % (name, label))
        try:
            out = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("%s --%s wants a number, got %r"
                             % (name, label, value))
        # Catches inf and NaN as well as the merely absurd: neither comparison
        # holds for a NaN, so it falls out here rather than reaching numpy.
        if not -MAX_NUMBER <= out <= MAX_NUMBER:
            raise ValueError("%s --%s is out of range" % (name, label))
    else:
        if not isinstance(value, str):
            raise ValueError("%s --%s wants text" % (name, label))
        if len(value) > MAX_TEXT:
            raise ValueError("%s --%s is longer than %d characters"
                             % (name, label, MAX_TEXT))
        out = value
    if spec["choices"] is not None and str(out) not in spec["choices"]:
        raise ValueError("%s --%s must be one of %s"
                         % (name, label, ", ".join(spec["choices"])))
    return out
