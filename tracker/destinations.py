#!/usr/bin/env python3
"""Add, remove and inspect the places being tracked.

  destinations.py list
  destinations.py watch SYD --city Sydney --why friends --nights 10 --target 450
  destinations.py watch CPT --city "Cape Town" --window 2027-06-01 2027-06-30 --nights 14 --target 1100
  destinations.py trip  LHR --city London --out 2027-03-04 --ret 2027-03-18 --flex 3
  destinations.py disable SYD
  destinations.py enable  SYD
  destinations.py remove  SYD

Two kinds of destination:

  trip   you know the dates. Priced every morning, alerts when the total moves.
         --flex N adds N days either side of each date; those extra dates are
         priced with the keyless reader, so flexibility costs no API quota.
  watch  you know the place, not the dates. Swept across a horizon (or pinned
         to a --window) for a fare under --target.

config.yaml is edited as TEXT rather than reparsed and rewritten, because the
comments in it explain the choices and a YAML round-trip would silently throw
them away. Every write is validated by reparsing, and rolled back if the result
is not loadable.
"""
import argparse, datetime, os, shutil, sys
import yaml

DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(DIR, "config.yaml")

PALETTE = ["#6FD9CF", "#F0A44A", "#5EA8F0", "#B9DE55",
           "#A78BFA", "#F2789B", "#F26B5E", "#7FD1B9", "#E8C468"]


# ---------------------------------------------------------------- helpers
def load():
    with open(CFG_PATH) as fh:
        return yaml.safe_load(fh)


def lines():
    with open(CFG_PATH) as fh:
        return fh.read().split("\n")


def write(new_lines):
    """Write, then prove the result still parses. If it does not, put the old
    file back: a tracker that cannot read its config does not run at all."""
    backup = CFG_PATH + ".prev"
    shutil.copy2(CFG_PATH, backup)
    with open(CFG_PATH, "w") as fh:
        fh.write("\n".join(new_lines))
    try:
        cfg = yaml.safe_load(open(CFG_PATH))
        assert isinstance(cfg, dict) and ("trips" in cfg or "watchlist" in cfg)
    except Exception as e:
        shutil.copy2(backup, CFG_PATH)
        sys.exit("config would not parse after the edit, so nothing changed: %r" % (e,))
    return cfg


def block_start(ls, key):
    return next((i for i, l in enumerate(ls) if l.startswith(key + ":")), None)


def block_end(ls, key):
    """Index just past the last CONTENT line of a top-level `key:` block.

    The next section is usually introduced by a comment block explaining it,
    and those comments belong to that section, not this one. So find the next
    top-level key, then walk back over the blank and comment lines that
    introduce it. Getting this wrong inserts new entries under the wrong
    heading, and a later removal then scans across the boundary.
    """
    start = block_start(ls, key)
    if start is None:
        return None
    nxt = len(ls)
    for i in range(start + 1, len(ls)):
        l = ls[i]
        if l.strip() and not l.startswith((" ", "\t")) and not l.lstrip().startswith("#"):
            nxt = i
            break
    end = nxt
    while end > start + 1 and (not ls[end - 1].strip() or ls[end - 1].lstrip().startswith("#")):
        end -= 1
    return end


def item_span(ls, code):
    """(start, end) of the list item for `code`, never crossing out of its own
    section."""
    for key in ("trips", "watchlist"):
        s0, e0 = block_start(ls, key), block_end(ls, key)
        if s0 is None or e0 is None:
            continue
        start = None
        for i in range(s0 + 1, e0):
            if ls[i].startswith("  - "):
                if start is not None:
                    return start, i
                head = ls[i].strip()
                if head in ("- id: %s" % code, "- dest: %s" % code):
                    start = i
            elif start is None and ls[i].strip() in ("id: %s" % code, "dest: %s" % code):
                j = i
                while j > s0 and not ls[j].startswith("  - "):
                    j -= 1
                if ls[j].startswith("  - "):
                    start = j
        if start is not None:
            return start, e0
    return None


def taken_colours(cfg):
    out = []
    for t in (cfg.get("trips") or []) + (cfg.get("watchlist") or []):
        if t.get("colour"):
            out.append(t["colour"])
    return out


def next_colour(cfg):
    used = taken_colours(cfg)
    for c in PALETTE:
        if c not in used:
            return c
    return PALETTE[len(used) % len(PALETTE)]


def known(cfg):
    ids = [t["id"] for t in (cfg.get("trips") or [])]
    ids += [w["dest"] for w in (cfg.get("watchlist") or [])]
    return ids


def flex_dates(core, n):
    d = datetime.date.fromisoformat(core)
    return [(d + datetime.timedelta(days=k)).isoformat()
            for k in range(-n, n + 1) if k != 0]


# ---------------------------------------------------------------- commands
def cmd_list(args):
    cfg = load()
    origin = cfg.get("origin", "?")
    print("origin %s\n" % origin)
    print("fixed dates:")
    for t in cfg.get("trips") or []:
        print("  %-4s %-14s %s to %s  %s  +/-%d days  %s"
              % (t["id"], t.get("city", ""), t["out"]["core"], t["ret"]["core"],
                 ",".join(c[:3] for c in t.get("cabins", [])),
                 len(t["out"].get("window") or []) // 2,
                 t.get("why", "")))
    print("\nwatching for a fare:")
    for w in cfg.get("watchlist") or []:
        when = ("%s to %s" % tuple(w["window"])) if w.get("window") \
            else "%d-%d days out" % tuple(w.get("horizon_days", [0, 0]))
        print("  %-4s %-14s %-26s under $%-6s %d nights  %s%s"
              % (w["dest"], w.get("city", ""), when, w.get("target_rt", "?"),
                 w.get("nights", 0), w.get("why", ""),
                 "" if w.get("enabled", True) else "   [disabled]"))


def cmd_watch(args):
    cfg = load()
    if args.code in known(cfg):
        sys.exit("%s is already tracked. Remove it first, or edit config.yaml." % args.code)
    ls = lines()
    end = block_end(ls, "watchlist")
    if end is None:
        sys.exit("no `watchlist:` section in config.yaml")

    home = (args.origin or cfg.get("origin") or "").upper()
    blk = ["  - id: %s" % args.code,
           "    name: %s -> %s" % (home or "???", args.city or args.code),
           "    dest: %s" % args.code]
    if home and home != (cfg.get("origin") or ""):
        blk.append("    origin: %s" % home)
    if args.city:
        blk.append("    city: %s" % args.city)
    blk.append('    colour: "%s"' % (args.colour or next_colour(cfg)))
    blk.append('    why: "%s"' % (args.why or ""))
    blk.append("    nights: %d" % args.nights)
    if args.window:
        blk.append('    window: ["%s", "%s"]   # a fixed window, not a rolling horizon'
                   % (args.window[0], args.window[1]))
    else:
        blk.append("    horizon_days: [%d, %d]" % (args.horizon[0], args.horizon[1]))
    blk.append("    target_rt: %d               # alert when a round trip drops under this"
               % args.target)
    blk.append("    max_stops: %d" % args.max_stops)
    blk.append("    enabled: true")

    write(ls[:end] + blk + ls[end:])
    print("watching %s%s, alerting under $%s" %
          (args.code, " (%s)" % args.city if args.city else "", args.target))
    print("run `python3 watchlist.py daily` to price it now, or wait for the next run.")


def cmd_trip(args):
    cfg = load()
    if args.code in known(cfg):
        sys.exit("%s is already tracked. Remove it first, or edit config.yaml." % args.code)
    ls = lines()
    end = block_end(ls, "trips")
    if end is None:
        sys.exit("no `trips:` section in config.yaml")

    origin = (args.origin or cfg.get("origin") or "???").upper()
    cabins = [c.strip() for c in args.cabins.split(",") if c.strip()]
    ow = flex_dates(args.out, args.flex) if args.flex else []
    rw = flex_dates(args.ret, args.flex) if args.flex else []
    fmt = lambda xs: "[" + ", ".join('"%s"' % x for x in xs) + "]"

    blk = ["  - id: %s" % args.code,
           "    name: %s -> %s" % (origin, args.city or args.code),
           "    origin: %s" % origin,
           "    dest: %s" % args.code]
    if args.city:
        blk.append("    city: %s" % args.city)
    blk.append('    colour: "%s"' % (args.colour or next_colour(cfg)))
    blk.append('    why: "%s"' % (args.why or ""))
    blk.append('    when: "%s to %s"' % (args.out, args.ret))
    blk.append("    out: {core: \"%s\", window: %s}" % (args.out, fmt(ow)))
    blk.append("    ret: {core: \"%s\", window: %s}" % (args.ret, fmt(rw)))
    blk.append("    cabins: [%s]" % ", ".join(cabins))
    blk.append("    max_stops: %d" % args.max_stops)
    blk.append("    source: %s" % args.source)
    if args.source == "serpapi":
        blk.append("    serpapi_stops: %d" % (args.max_stops + 1))
        blk.append("    fallback: fastflights")
    if ow:
        blk.append("    window_source: fastflights   # flexible dates are free, so they run daily")

    write(ls[:end] + blk + ls[end:])
    print("tracking %s %s to %s%s" % (args.code, args.out, args.ret,
                                      ", +/-%d days" % args.flex if args.flex else ""))


def cmd_remove(args):
    ls = lines()
    span = item_span(ls, args.code)
    if not span:
        sys.exit("%s is not in config.yaml" % args.code)
    start, end = span
    write(ls[:start] + ls[end:])
    print("removed %s. Its history stays in fares.db." % args.code)


def _set_enabled(code, value):
    ls = lines()
    span = item_span(ls, code)
    if not span:
        sys.exit("%s is not in config.yaml" % code)
    start, end = span
    for i in range(start, end):
        if ls[i].strip().startswith("enabled:"):
            ls[i] = "    enabled: %s" % ("true" if value else "false")
            break
    else:
        ls.insert(end, "    enabled: %s" % ("true" if value else "false"))
    write(ls)
    print("%s %s" % (code, "enabled" if value else "disabled (kept, not priced)"))


def cmd_enable(args):
    _set_enabled(args.code, True)


def cmd_disable(args):
    _set_enabled(args.code, False)


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show everything being tracked").set_defaults(fn=cmd_list)

    w = sub.add_parser("watch", help="watch a place with no fixed dates")
    w.add_argument("code", help="IATA code, e.g. SYD")
    w.add_argument("--city")
    w.add_argument("--why", default="")
    w.add_argument("--nights", type=int, default=14)
    w.add_argument("--target", type=int, required=True, help="round-trip price worth taking")
    w.add_argument("--horizon", type=int, nargs=2, default=[30, 300], metavar=("MIN", "MAX"))
    w.add_argument("--window", nargs=2, metavar=("START", "END"),
                   help="pin to a fixed window instead of a rolling horizon")
    w.add_argument("--max-stops", type=int, default=2)
    w.add_argument("--origin", help="fly from here instead of the configured origin")
    w.add_argument("--colour")
    w.set_defaults(fn=cmd_watch)

    t = sub.add_parser("trip", help="track a trip you have dates for")
    t.add_argument("code")
    t.add_argument("--city")
    t.add_argument("--why", default="")
    t.add_argument("--out", required=True, help="outbound date, YYYY-MM-DD")
    t.add_argument("--ret", required=True, help="return date, YYYY-MM-DD")
    t.add_argument("--flex", type=int, default=0, help="days either side of each date")
    t.add_argument("--cabins", default="economy")
    t.add_argument("--max-stops", type=int, default=1)
    t.add_argument("--source", default="fastflights", choices=["fastflights", "serpapi"])
    t.add_argument("--origin", help="fly from here instead of the configured origin")
    t.add_argument("--colour")
    t.set_defaults(fn=cmd_trip)

    for name, fn, helptext in (("remove", cmd_remove, "stop tracking a destination"),
                               ("enable", cmd_enable, "start pricing it again"),
                               ("disable", cmd_disable, "keep it but stop pricing it")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("code")
        p.set_defaults(fn=fn)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
