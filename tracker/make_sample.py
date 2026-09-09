#!/usr/bin/env python3
"""Generate the sample dataset the bundled viewer opens with.

The real tracker writes these two files from its SQLite database. This
produces the same shapes for an invented traveller, so the repository can
demonstrate itself without shipping anybody's actual travel plans. The
numbers are made up and deliberately not passed off as real fares.

  python3 make_sample.py ../viewer/sample
"""
import json, os, random, sys, datetime

random.seed(20260909)          # deterministic: the demo looks the same for everyone

AP = {
    "SIN": ("Singapore", "Singapore", 1.3644, 103.9915),
    "LHR": ("London", "UK", 51.4700, -0.4543),
    "NRT": ("Tokyo", "Japan", 35.7720, 140.3929),
    "SYD": ("Sydney", "Australia", -33.9399, 151.1753),
    "CPT": ("Cape Town", "South Africa", -33.9715, 18.6021),
    "DXB": ("Dubai", "UAE", 25.2532, 55.3657),
    "DOH": ("Doha", "Qatar", 25.2731, 51.6080),
    "HKG": ("Hong Kong", "China", 22.3080, 113.9185),
    "BKK": ("Bangkok", "Thailand", 13.6900, 100.7501),
    "JNB": ("Johannesburg", "South Africa", -26.1367, 28.2411),
}
ORIGIN = "SIN"

TRIPS = [
    dict(id="LHR", city="London", kind="tracked", css="#6FD9CF", why="", nonstop=True,
         when="4 to 18 Mar 2027", cabins=["economy", "premium-economy", "business"],
         hubs=[[], ["DXB"], ["DOH"], ["BKK"]], base=780, out="2027-03-04", ret="2027-03-18"),
    dict(id="NRT", city="Tokyo", kind="tracked", css="#F0A44A", why="cherry blossom",
         when="1 to 9 Apr 2027", cabins=["economy", "premium-economy"],
         hubs=[[], ["HKG"], ["BKK"]], base=520, out="2027-04-01", ret="2027-04-09"),
    dict(id="SYD", city="Sydney", kind="watch", css="#B9DE55", why="friends", target=450,
         hubs=[[], ["BKK"]], base=470, out="2027-02-14", ret="2027-02-24"),
    dict(id="CPT", city="Cape Town", kind="watch", css="#A78BFA", why="a long way off", target=1100,
         hubs=[["DOH"], ["DXB", "JNB"], ["JNB"]], base=1180, out="2027-06-06", ret="2027-06-20"),
]

CARRIERS = ["Singapore Airlines", "Qatar Airways", "Emirates", "Cathay Pacific",
            "THAI", "Qantas", "British Airways", "ANA"]
CAB = {"economy": "e", "premium-economy": "p", "business": "b"}
MULT = {"e": 1.0, "p": 1.9, "b": 4.1}


def stamp(date, minutes_from_midnight):
    """MM-DD HH:MM, derived from the leg date so departure and arrival stay
    consistent with the duration between them."""
    base = datetime.datetime.combine(datetime.date.fromisoformat(date), datetime.time())
    t = base + datetime.timedelta(minutes=minutes_from_midnight)
    return "%02d-%02d %02d:%02d" % (t.month, t.day, t.hour, t.minute)


def itinerary(trip, leg, cab, date):
    via = random.choice(trip["hubs"])
    stops = len(via)
    price = int(trip["base"] * MULT[cab] * random.uniform(0.86, 1.55) - stops * 40)
    mins = (7 + stops * 4) * 60 + random.randint(0, 200)
    if trip["id"] == "CPT":
        mins += 300
    dep = random.randint(0, 23) * 60 + random.choice([0, 15, 30, 45])
    return dict(lg=leg, cb=cab, p=price, al="/".join(random.sample(CARRIERS, 1 + (stops > 1))),
                st=stops, v=via, m=mins, ly=stops * random.randint(70, 400), d=date,
                dp=stamp(date, dep), ar=stamp(date, dep + mins))


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    routes, options, watch, latest, pairs, maxstops, series = [], {}, {}, {}, {}, {}, []
    runs = [(datetime.date(2027, 1, 2) + datetime.timedelta(days=k)).isoformat() for k in range(5)]

    for t in TRIPS:
        rows = []
        for leg, date in (("out", t["out"]), ("ret", t["ret"])):
            for cab in [CAB[c] for c in t.get("cabins", ["economy"])]:
                for _ in range(6):
                    rows.append(itinerary(t, leg, cab, date))
        seen, ded = set(), []
        for o in sorted(rows, key=lambda x: (x["lg"], x["cb"], x["p"])):
            sig = (o["lg"], o["cb"], o["p"], tuple(o["v"]))
            if sig not in seen:
                seen.add(sig)
                ded.append(o)
        options[t["id"]] = ded
        maxstops[t["id"]] = 2
        pairs[t["id"]] = [[t["out"], t["ret"]]]

        for o in ded:
            routes.append(dict(trip=t["id"], leg=o["lg"], via=o["v"], airlines=o["al"],
                               stops=o["st"], price=o["p"], mins=o["m"]))

        eco = [o for o in ded if o["cb"] == "e"]
        co = min([o for o in eco if o["lg"] == "out"], key=lambda x: x["p"])
        cr = min([o for o in eco if o["lg"] == "ret"], key=lambda x: x["p"])
        desc = lambda o: "$%s %s via %s %dh%02d" % (
            format(o["p"], ","), o["al"], ",".join(o["v"]) or "nonstop", o["m"] // 60, o["m"] % 60)

        if t["kind"] == "tracked":
            for c in t["cabins"]:
                k = CAB[c]
                cc = [o for o in ded if o["cb"] == k]
                a = min([o for o in cc if o["lg"] == "out"], key=lambda x: x["p"])
                b = min([o for o in cc if o["lg"] == "ret"], key=lambda x: x["p"])
                latest["%s|%s" % (t["id"], c)] = dict(total=a["p"] + b["p"], out=desc(a), ret=desc(b))
                for i, d in enumerate(runs):
                    drift = 1 + (i - 2) * random.uniform(0.004, 0.02)
                    series.append(dict(d=d, trip=t["id"], cabin=c,
                                       total=int((a["p"] + b["p"]) * drift)))
        else:
            watch[t["id"]] = dict(out_date=t["out"], ret_date=t["ret"], total=co["p"] + cr["p"],
                                  out=desc(co), ret=desc(cr),
                                  nonstop=int(co["st"] == 0 and cr["st"] == 0))

    used = {ORIGIN} | {t["id"] for t in TRIPS} | {h for r in routes for h in r["via"]}
    fares = dict(
        airports={k: dict(city=AP[k][0], country=AP[k][1], lat=AP[k][2], lon=AP[k][3])
                  for k in used if k in AP},
        routes=routes, series=series, latest=latest, watch=watch,
        alerts=[dict(ts=runs[2], who="SYD", what="under your $450 jump-on price"),
                dict(ts=runs[3], who="LHR", what="new low (was $1,704, down $61)")],
        runs=runs, nobs=sum(len(v) for v in options.values()) * 4,
        hubs=sorted({h for r in routes for h in r["via"]}),
        maxStops=maxstops, pairs=pairs, origin=ORIGIN,
        trips=[dict(id=t["id"], key=t["id"].lower(), kind=t["kind"], city=t["city"],
                    css=t["css"], why=t.get("why", ""), when=t.get("when", ""),
                    target=t.get("target"), cabins=t.get("cabins", ["economy"]))
               for t in TRIPS],
        page=dict(title="Fare Atlas", headline="Nine hours to anywhere worth going.",
                  headline_em="Sample data.", origin_label="SIN Singapore"))

    open(os.path.join(out_dir, "fares.json"), "w").write(json.dumps(fares, separators=(",", ":")))
    open(os.path.join(out_dir, "options.json"), "w").write(json.dumps(options, separators=(",", ":")))
    print("sample written: %d routes, %d itineraries, %d destinations"
          % (len(routes), sum(len(v) for v in options.values()), len(TRIPS)))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample")
