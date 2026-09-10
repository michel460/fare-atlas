#!/usr/bin/env python3
"""Export fares.db to the two JSON payloads the viewer reads.

  python3 export_globe.py <out_dir>

Writes fares.json (globe network, price series, best-per-destination) and
options.json (every itinerary from the latest run, for the detail drawer).
Routings through an airport with no coordinate here are dropped and named,
so a new hub degrades to a missing line rather than a blank page.
"""
import sqlite3, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faconfig

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "fares.db")

from airports import AP                                     # noqa: E402
CB = {"economy": "e", "premium-economy": "p", "business": "b"}


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isfile(DB):
        sys.exit("no database yet at %s. Price something first:\n"
                 "    python3 tracker.py daily" % DB)
    c = sqlite3.connect(DB)
    cur = c.cursor()
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"obs", "wobs"} & tables:
        sys.exit("the database has no fares in it yet. Price something first:\n"
                 "    python3 tracker.py daily")
    routes, missing = [], set()

    def add(trip, leg, via, al, stops, price, mins):
        via = [v for v in via if v]
        for code in via + [trip, "DPS"]:
            if code not in AP:
                missing.add(code)
                return
        routes.append(dict(trip=trip, leg=leg, via=via, airlines=al,
                           stops=stops, price=price, mins=mins))

    # cheapest itinerary per hub chain: the network drawn on the globe
    def rows(sql, args=()):
        """A table that does not exist yet is simply no rows: the watchlist may
        not have run, or the fixed-date trips may not have."""
        try:
            return list(cur.execute(sql, args))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise

    best = {}
    for t, l, v, al, st, pr, tm in rows(
            "SELECT trip,leg,via,airlines,stops,price,total_min FROM obs "
            "WHERE via IS NOT NULL AND price IS NOT NULL"):
        k = (t, l, v)
        if k not in best or pr < best[k][2]:
            best[k] = (al, st, pr, tm)
    for (t, l, v), (al, st, pr, tm) in best.items():
        add(t, l, v.split(","), al, st, pr, tm)

    wb = {}
    for d, l, v, al, st, pr, tm in rows(
            "SELECT dest,leg,via,airlines,stops,price,total_min FROM wobs WHERE price IS NOT NULL"):
        k = (d, l, v)
        if k not in wb or pr < wb[k][2]:
            wb[k] = (al, st, pr, tm)
    for (d, l, v), (al, st, pr, tm) in wb.items():
        add(d, l, v.split(",") if v else [], al, st, pr, tm)

    series = [dict(d=d, trip=t, cabin=cb, total=x) for d, t, cb, x in rows(
        "SELECT substr(ts,1,10),trip,cabin,total FROM metrics ORDER BY ts")]
    latest = {t + "|" + cb: dict(total=x, out=od, ret=rd) for t, cb, x, od, rd in rows(
        "SELECT trip,cabin,total,out_desc,ret_desc FROM metrics "
        "WHERE run_id=(SELECT MAX(run_id) FROM metrics)")}
    watch = {d: dict(out_date=od, ret_date=rd, total=x, out=o, ret=r, nonstop=ns)
             for d, od, rd, x, o, r, ns in rows(
        "SELECT dest,out_date,ret_date,total,out_desc,ret_desc,nonstop FROM wbest w "
        "WHERE total=(SELECT MIN(total) FROM wbest WHERE dest=w.dest) GROUP BY dest")}

    alerts = [dict(ts=t[:10], who=a, what=w) for t, a, w in rows(
        "SELECT ts,trip,reason FROM alerts ORDER BY ts")]
    alerts += [dict(ts=t[:10], who=d, what=w) for t, d, w in rows(
        "SELECT ts,dest,reason FROM walerts WHERE reason NOT LIKE 'first%' ORDER BY ts")]

    used = {"DPS"}
    for r in routes:
        used.update(r["via"])
        used.add(r["trip"])

    fares = dict(
        airports={k: dict(city=AP[k][0], country=AP[k][1], lat=AP[k][2], lon=AP[k][3]) for k in used},
        routes=routes, series=series, latest=latest, watch=watch, alerts=alerts,
        runs=[r[0] for r in rows("SELECT substr(ts,1,10) FROM runs ORDER BY ts")],
        nobs=sum((rows("SELECT COUNT(*) FROM %s" % t) or [(0,)])[0][0] for t in ("obs", "wobs")),
        hubs=sorted({h for r in routes for h in r["via"]}))

    # every itinerary from the latest run, for the drawer
    opts = {}
    last = (rows("SELECT MAX(run_id) FROM obs") or [(None,)])[0][0]
    for t, l, d, cb, al, st, v, dp, ar, tm, ly, pr in rows(
            "SELECT trip,leg,date,cabin,airlines,stops,via,dep,arr,total_min,layover_min,price "
            "FROM obs WHERE run_id=? AND price IS NOT NULL", (last,)):
        opts.setdefault(t, []).append(dict(lg=l, cb=CB.get(cb, "e"), p=pr, al=al, st=st,
            v=[x for x in (v or "").split(",") if x], m=tm, ly=ly or 0, d=d, dp=dp, ar=ar))
    wlast = (rows("SELECT MAX(run_id) FROM wobs") or [(None,)])[0][0]
    for d0, l, d, al, st, v, dp, ar, tm, pr in rows(
            "SELECT dest,leg,date,airlines,stops,via,dep,arr,total_min,price "
            "FROM wobs WHERE run_id=? AND price IS NOT NULL", (wlast,)):
        opts.setdefault(d0, []).append(dict(lg=l, cb="e", p=pr, al=al, st=st,
            v=[x for x in (v or "").split(",") if x], m=tm, ly=0, d=d, dp=dp, ar=ar))

    for k in opts:
        seen, ded = set(), []
        for o in sorted(opts[k], key=lambda x: (x["lg"], x["cb"], x["p"])):
            sig = (o["lg"], o["cb"], o["p"], o["al"], tuple(o["v"]), o["m"])
            if sig in seen or any(x not in AP for x in o["v"]):
                continue
            seen.add(sig)
            ded.append(o)
        opts[k] = ded

    # Stop limits, and which (out, ret) date combinations are actually
    # bookable. The watchlist samples several candidate date pairs per run,
    # so pairing the cheapest outbound with a return from a DIFFERENT
    # candidate invents a trip nobody can buy. Fixed-date trips are genuinely
    # mix-and-match one-ways, so every combination of their dates is valid.
    cfg = faconfig.load()
    maxstops, pairs = {}, {}
    for t in cfg.get("trips") or []:
        maxstops[t["id"]] = int(t.get("max_stops", 1))
    for w in cfg.get("watchlist") or []:
        maxstops[w["dest"]] = int(w.get("max_stops", 2))

    import datetime as _dt
    for dest in set(list(opts.keys())):
        outs = sorted({o["d"] for o in opts[dest] if o["lg"] == "out"})
        rets = sorted({o["d"] for o in opts[dest] if o["lg"] == "ret"})
        wl = next((w for w in (cfg.get("watchlist") or []) if w["dest"] == dest), None)
        if wl:
            n = int(wl.get("nights", 14))
            pairs[dest] = [[d, (_dt.date.fromisoformat(d) + _dt.timedelta(days=n)).isoformat()]
                           for d in outs
                           if (_dt.date.fromisoformat(d) + _dt.timedelta(days=n)).isoformat() in rets]
        else:
            pairs[dest] = [[a, b] for a in outs for b in rets]
    fares["maxStops"] = maxstops
    fares["pairs"] = pairs

    # Trip metadata travels WITH the data, so the viewer holds no personal
    # labels and can be published as-is. Colours cycle through a fixed palette
    # chosen to stay legible on the dark ground.
    PALETTE = ["#6FD9CF", "#F0A44A", "#5EA8F0", "#B9DE55",
               "#A78BFA", "#F2789B", "#F26B5E", "#7FD1B9", "#E8C468"]
    trips, i = [], 0
    for t in (cfg.get("trips") or []):
        trips.append(dict(id=t["id"], key=t["id"].lower(), kind="tracked",
                          origin=t.get("origin") or cfg.get("origin"),
                          city=t.get("city", t["id"]), css=t.get("colour") or PALETTE[i % len(PALETTE)],
                          why=t.get("why", ""), when=t.get("when", ""),
                          cabins=t.get("cabins", ["economy"])))
        i += 1
    for w in (cfg.get("watchlist") or []):
        if not w.get("enabled", True):
            continue
        trips.append(dict(id=w["dest"], key=w["dest"].lower(), kind="watch",
                          origin=w.get("origin") or cfg.get("origin"),
                          city=w.get("city", w["dest"]), css=w.get("colour") or PALETTE[i % len(PALETTE)],
                          why=w.get("why", ""), when=w.get("when", ""),
                          target=w.get("target_rt"), cabins=["economy"]))
        i += 1
    fares["trips"] = [t for t in trips if t["id"] in used]
    fares["page"] = cfg.get("page") or {}
    fares["origin"] = cfg.get("origin", "DPS")

    open(os.path.join(out_dir, "fares.json"), "w").write(json.dumps(fares, separators=(",", ":")))
    open(os.path.join(out_dir, "options.json"), "w").write(json.dumps(opts, separators=(",", ":")))
    print("routes {}  hubs {}  observations {}  options {}".format(
        len(routes), len(fares["hubs"]), fares["nobs"], sum(len(v) for v in opts.values())))
    print("nonstop destinations:", sorted({r["trip"] for r in routes if r["stops"] == 0}))
    if missing:
        print("NO COORDINATE, routings dropped:", sorted(missing))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
