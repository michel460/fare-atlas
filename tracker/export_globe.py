#!/usr/bin/env python3
"""Export fares.db to the two JSON payloads the viewer reads.

  python3 export_globe.py <out_dir>

Writes fares.json (globe network, price series, best-per-destination) and
options.json (every itinerary from the latest run, for the detail drawer).
Routings through an airport with no coordinate here are dropped and named,
so a new hub degrades to a missing line rather than a blank page.
"""
import sqlite3, json, os, sys
import yaml

DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "fares.db")

# Airports the viewer can place. This is a starter list of common long-haul
# origins and connecting hubs; add whatever your routes actually use. A routing
# through an airport that is missing here is dropped and named, rather than
# blanking the page, so extending it is a one-line fix when you see the warning.
AP = {
 "AMS":("Amsterdam","Netherlands",52.3105,4.7683),  "ATH":("Athens","Greece",37.9364,23.9445),
 "AKL":("Auckland","New Zealand",-37.0082,174.7850),"AUH":("Abu Dhabi","UAE",24.4330,54.6511),
 "BCN":("Barcelona","Spain",41.2974,2.0833),        "BKK":("Bangkok","Thailand",13.6900,100.7501),
 "BNE":("Brisbane","Australia",-27.3842,153.1175),  "BOM":("Mumbai","India",19.0896,72.8656),
 "CAN":("Guangzhou","China",23.3924,113.2988),      "CDG":("Paris","France",49.0097,2.5479),
 "CGK":("Jakarta","Indonesia",-6.1256,106.6559),    "CMB":("Colombo","Sri Lanka",7.1808,79.8841),
 "CPT":("Cape Town","South Africa",-33.9715,18.6021),"DAD":("Da Nang","Vietnam",16.0439,108.1994),
 "DEL":("Delhi","India",28.5562,77.1000),           "DOH":("Doha","Qatar",25.2731,51.6080),
 "DPS":("Denpasar","Indonesia",-8.7482,115.1672),   "DXB":("Dubai","UAE",25.2532,55.3657),
 "FCO":("Rome","Italy",41.8003,12.2389),            "FRA":("Frankfurt","Germany",50.0379,8.5622),
 "HAN":("Hanoi","Vietnam",21.2212,105.8072),        "HKG":("Hong Kong","China",22.3080,113.9185),
 "HND":("Tokyo Haneda","Japan",35.5494,139.7798),   "IST":("Istanbul","Turkey",41.2753,28.7519),
 "ICN":("Seoul","South Korea",37.4602,126.4407),    "JED":("Jeddah","Saudi Arabia",21.6796,39.1565),
 "JFK":("New York","USA",40.6413,-73.7781),         "JNB":("Johannesburg","South Africa",-26.1367,28.2411),
 "KIX":("Osaka","Japan",34.4273,135.2440),          "KUL":("Kuala Lumpur","Malaysia",2.7456,101.7099),
 "KWI":("Kuwait City","Kuwait",29.2266,47.9689),    "LAX":("Los Angeles","USA",33.9416,-118.4085),
 "LHR":("London","UK",51.4700,-0.4543),             "MAD":("Madrid","Spain",40.4839,-3.5680),
 "MCT":("Muscat","Oman",23.5933,58.2844),           "MEL":("Melbourne","Australia",-37.6690,144.8410),
 "MNL":("Manila","Philippines",14.5086,121.0194),   "MUC":("Munich","Germany",48.3538,11.7861),
 "NRT":("Tokyo Narita","Japan",35.7720,140.3929),   "PEK":("Beijing","China",40.0799,116.6031),
 "PEN":("Penang","Malaysia",5.2971,100.2769),       "PER":("Perth","Australia",-31.9385,115.9672),
 "PRN":("Pristina","Kosovo",42.5728,21.0358),       "PVG":("Shanghai","China",31.1443,121.8083),
 "RUH":("Riyadh","Saudi Arabia",24.9576,46.6988),   "SFO":("San Francisco","USA",37.6213,-122.3790),
 "SGN":("Ho Chi Minh City","Vietnam",10.8188,106.6520),"SIN":("Singapore","Singapore",1.3644,103.9915),
 "SYD":("Sydney","Australia",-33.9399,151.1753),    "SZX":("Shenzhen","China",22.6393,113.8108),
 "TPE":("Taipei","Taiwan",25.0777,121.2328),        "VIE":("Vienna","Austria",48.1103,16.5697),
 "XMN":("Xiamen","China",24.5440,118.1276),         "YVR":("Vancouver","Canada",49.1967,-123.1815),
 "YYZ":("Toronto","Canada",43.6777,-79.6248),       "ZRH":("Zurich","Switzerland",47.4647,8.5492),
}
CB = {"economy": "e", "premium-economy": "p", "business": "b"}


def main(out_dir):
    c = sqlite3.connect(DB)
    cur = c.cursor()
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
    best = {}
    for t, l, v, al, st, pr, tm in cur.execute(
            "SELECT trip,leg,via,airlines,stops,price,total_min FROM obs "
            "WHERE via IS NOT NULL AND price IS NOT NULL"):
        k = (t, l, v)
        if k not in best or pr < best[k][2]:
            best[k] = (al, st, pr, tm)
    for (t, l, v), (al, st, pr, tm) in best.items():
        add(t, l, v.split(","), al, st, pr, tm)

    wb = {}
    for d, l, v, al, st, pr, tm in cur.execute(
            "SELECT dest,leg,via,airlines,stops,price,total_min FROM wobs WHERE price IS NOT NULL"):
        k = (d, l, v)
        if k not in wb or pr < wb[k][2]:
            wb[k] = (al, st, pr, tm)
    for (d, l, v), (al, st, pr, tm) in wb.items():
        add(d, l, v.split(",") if v else [], al, st, pr, tm)

    series = [dict(d=d, trip=t, cabin=cb, total=x) for d, t, cb, x in cur.execute(
        "SELECT substr(ts,1,10),trip,cabin,total FROM metrics ORDER BY ts")]
    latest = {t + "|" + cb: dict(total=x, out=od, ret=rd) for t, cb, x, od, rd in cur.execute(
        "SELECT trip,cabin,total,out_desc,ret_desc FROM metrics "
        "WHERE run_id=(SELECT MAX(run_id) FROM metrics)")}
    watch = {d: dict(out_date=od, ret_date=rd, total=x, out=o, ret=r, nonstop=ns)
             for d, od, rd, x, o, r, ns in cur.execute(
        "SELECT dest,out_date,ret_date,total,out_desc,ret_desc,nonstop FROM wbest w "
        "WHERE total=(SELECT MIN(total) FROM wbest WHERE dest=w.dest) GROUP BY dest")}

    alerts = [dict(ts=t[:10], who=a, what=w) for t, a, w in cur.execute(
        "SELECT ts,trip,reason FROM alerts ORDER BY ts")]
    alerts += [dict(ts=t[:10], who=d, what=w) for t, d, w in cur.execute(
        "SELECT ts,dest,reason FROM walerts WHERE reason NOT LIKE 'first%' ORDER BY ts")]

    used = {"DPS"}
    for r in routes:
        used.update(r["via"])
        used.add(r["trip"])

    fares = dict(
        airports={k: dict(city=AP[k][0], country=AP[k][1], lat=AP[k][2], lon=AP[k][3]) for k in used},
        routes=routes, series=series, latest=latest, watch=watch, alerts=alerts,
        runs=[r[0] for r in cur.execute("SELECT substr(ts,1,10) FROM runs ORDER BY ts")],
        nobs=cur.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
             + cur.execute("SELECT COUNT(*) FROM wobs").fetchone()[0],
        hubs=sorted({h for r in routes for h in r["via"]}))

    # every itinerary from the latest run, for the drawer
    opts = {}
    last = cur.execute("SELECT MAX(run_id) FROM obs").fetchone()[0]
    for t, l, d, cb, al, st, v, dp, ar, tm, ly, pr in cur.execute(
            "SELECT trip,leg,date,cabin,airlines,stops,via,dep,arr,total_min,layover_min,price "
            "FROM obs WHERE run_id=? AND price IS NOT NULL", (last,)):
        opts.setdefault(t, []).append(dict(lg=l, cb=CB.get(cb, "e"), p=pr, al=al, st=st,
            v=[x for x in (v or "").split(",") if x], m=tm, ly=ly or 0, d=d, dp=dp, ar=ar))
    wlast = cur.execute("SELECT MAX(run_id) FROM wobs").fetchone()[0]
    for d0, l, d, al, st, v, dp, ar, tm, pr in cur.execute(
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
    cfg = yaml.safe_load(open(os.path.join(DIR, "config.yaml")))
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
                          city=t.get("city", t["id"]), css=t.get("colour") or PALETTE[i % len(PALETTE)],
                          why=t.get("why", ""), when=t.get("when", ""),
                          cabins=t.get("cabins", ["economy"])))
        i += 1
    for w in (cfg.get("watchlist") or []):
        if not w.get("enabled", True):
            continue
        trips.append(dict(id=w["dest"], key=w["dest"].lower(), kind="watch",
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
