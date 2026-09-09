#!/usr/bin/env python3
"""Opportunistic destination watcher for the DPS fare tracker.

The trips in tracker.py have fixed dates. These do not: they are places you
go when a fare is cheap enough to jump on, so instead of pricing one date we
sample a rotating set of departure dates across a horizon and remember the
cheapest round trip we have ever seen.

Runs on the KEYLESS Google Flights reader (flights.one_way), so it costs
no SerpAPI quota at all. Invoked from run.sh after tracker.py, and never
allowed to break it.

Modes: daily (default) | status | test
"""
import os, sys, json, sqlite3, datetime, subprocess, time
import yaml

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
from flights import one_way

CFG = yaml.safe_load(open(os.path.join(DIR, "config.yaml")))
DB = os.path.join(DIR, "fares.db")
SGT = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(SGT)
RUN_ID = NOW.strftime("%Y%m%dT%H%M")
# Where alerts go. Any executable that accepts --header and reads the body on
# stdin will do; unset it and the tracker simply runs without notifications.
# Where alerts go: any executable taking --header and reading the body on
# stdin. Unset, everything prints to the console instead.
NOTIFY = os.environ.get("FARE_NOTIFY_CMD", "")
SWEEP_SAMPLES  = int(CFG.get("watchlist_sweep_samples", 5))
REFINE_SAMPLES = int(CFG.get("watchlist_refine_samples", 1))
NEW_LOW_PCT    = float(CFG.get("watchlist_new_low_pct", 3.0))
NEW_LOW_ABS    = int(CFG.get("watchlist_new_low_abs", 25))


# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS wobs(run_id TEXT, ts TEXT, dest TEXT, leg TEXT, date TEXT,
        airlines TEXT, stops INT, via TEXT, dep TEXT, arr TEXT, total_min INT, price INT);
    CREATE TABLE IF NOT EXISTS wbest(run_id TEXT, ts TEXT, dest TEXT, out_date TEXT, ret_date TEXT,
        total INT, out_price INT, out_desc TEXT, ret_price INT, ret_desc TEXT, nonstop INT);
    CREATE TABLE IF NOT EXISTS walerts(ts TEXT, dest TEXT, total INT, out_date TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS wruns(run_id TEXT PRIMARY KEY, ts TEXT, fetches INT, errors TEXT);
    """)
    if "note" not in {r[1] for r in c.execute("PRAGMA table_info(wruns)")}:
        c.execute("ALTER TABLE wruns ADD COLUMN note TEXT")
    return c


def watchlist():
    return CFG.get("watchlist") or []


# ---------------------------------------------------------------- dates
def horizon(w):
    """Departure offsets from today as (lo, hi). A destination pinned to a
    month or an exact date arrives as an absolute window, with the tail
    trimmed by `nights` so the return still lands inside it."""
    today = datetime.date.today()
    if w.get("window"):
        a = datetime.date.fromisoformat(w["window"][0])
        b = datetime.date.fromisoformat(w["window"][1])
        lo = max(1, (a - today).days)
        hi = max(lo, (b - today).days - int(w.get("nights", 14)))
    else:
        lo, hi = w["horizon_days"]
    return int(lo), int(hi)


def plan_dates(w, seed, sweeping, inc_date):
    """Coarse to fine.

    A 270-day horizon sampled two dates at a time takes months to see, so
    each run one destination takes its turn at a SWEEP: dates spread evenly
    across the whole horizon, which finds the cheap month. The others
    REFINE, re-pricing their best known departure plus a nearby date, which
    finds the cheap week inside it.
    """
    today = datetime.date.today()
    lo, hi = horizon(w)
    lo_d = today + datetime.timedelta(days=lo)
    hi_d = today + datetime.timedelta(days=hi)
    if hi <= lo:                                   # pinned to a single date
        return [lo_d]

    inc = None
    if inc_date:
        try:
            d = datetime.date.fromisoformat(inc_date)
            if lo_d <= d <= hi_d:
                inc = d
        except ValueError:
            inc = None

    if sweeping:
        n = max(2, SWEEP_SAMPLES)
        out = [lo_d + datetime.timedelta(days=int((hi - lo) * i / (n - 1))) for i in range(n)]
    else:
        base = inc or (lo_d + datetime.timedelta(days=(hi - lo) // 2))
        out = []
        for i in range(max(1, REFINE_SAMPLES)):
            step = [-10, -6, -3, 3, 6, 10][(seed + i) % 6]
            out.append(min(max(base + datetime.timedelta(days=step), lo_d), hi_d))
    if inc:
        out.append(inc)                            # never lose a known-good date
    return sorted(set(out))


def incumbent(c, dest):
    r = c.execute("SELECT out_date, total FROM wbest WHERE dest=? ORDER BY total ASC LIMIT 1",
                  (dest,)).fetchone()
    return r if r else None


# ---------------------------------------------------------------- fetching
def cheapest(rows):
    ok = [x for x in rows if x.get("price")]
    return min(ok, key=lambda x: x["price"]) if ok else None


def desc(x):
    if not x:
        return None
    h, m = divmod(x["total_min"], 60)
    via = ",".join(x["via"]) if x["via"] else "nonstop"
    return "${:,} {} via {} {}h{:02d}".format(
        x["price"], "/".join(x["airlines"])[:38], via, h, m)


def price_pair(w, out_date, nights, errors):
    """Price one candidate: out on out_date, back nights later."""
    dest, stops = w["dest"], int(w.get("max_stops", 2))
    ret_date = (datetime.date.fromisoformat(out_date) +
                datetime.timedelta(days=int(nights))).isoformat()
    legs = {}
    for leg, d, frm, to in (("out", out_date, "DPS", dest), ("ret", ret_date, dest, "DPS")):
        r = one_way(d, frm, to, "economy", 1, max_stops=stops)
        if not r["ok"]:
            errors.append("{} {} {}: {}".format(dest, leg, d, str(r.get("error"))[:90]))
        legs[leg] = (d, r.get("flights") or [])
        time.sleep(2)
    return ret_date, legs


# ---------------------------------------------------------------- run
def run():
    c = db()
    seed = int(NOW.strftime("%j"))          # day of year: rotates coverage
    errors, fetches, summaries = [], 0, []

    active = [w for w in watchlist() if w.get("enabled", True)]
    sweep_dest = active[seed % len(active)]["dest"] if active else None

    for w in active:
        dest, nights = w["dest"], w.get("nights", 14)
        inc = incumbent(c, dest)
        dates = [d.isoformat() for d in
                 plan_dates(w, seed, dest == sweep_dest, inc[0] if inc else None)]

        best = None
        for out_date in dates:
            ret_date, legs = price_pair(w, out_date, nights, errors)
            fetches += 2
            for leg, (d, rows) in legs.items():
                for x in rows:
                    if not x.get("price"):
                        continue
                    c.execute("INSERT INTO wobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (RUN_ID, NOW.isoformat(), dest, leg, d, "/".join(x["airlines"]),
                               x["stops"], ",".join(x["via"]), x["dep"], x["arr"],
                               x["total_min"], x["price"]))
            co, cr = cheapest(legs["out"][1]), cheapest(legs["ret"][1])
            if not (co and cr):
                continue
            total = co["price"] + cr["price"]
            if best is None or total < best["total"]:
                best = dict(total=total, out_date=out_date, ret_date=ret_date,
                            out=co, ret=cr,
                            nonstop=int(co["stops"] == 0 and cr["stops"] == 0))

        if not best:
            continue
        c.execute("INSERT INTO wbest VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (RUN_ID, NOW.isoformat(), dest, best["out_date"], best["ret_date"],
                   best["total"], best["out"]["price"], desc(best["out"]),
                   best["ret"]["price"], desc(best["ret"]), best["nonstop"]))

        # alert: an all-time low, or cheap enough to act on
        prev = c.execute("SELECT MIN(total) FROM wbest WHERE dest=? AND run_id<>?",
                         (dest, RUN_ID)).fetchone()[0]
        target = w.get("target_rt")
        reason = None
        if prev is None:
            reason = "first reading"
        elif best["total"] < prev:
            # a fare drifting down a few dollars a day must not DM every morning
            drop = prev - best["total"]
            if drop >= NEW_LOW_ABS or drop >= prev * NEW_LOW_PCT / 100.0:
                reason = "new low (was ${:,}, down ${:,})".format(prev, drop)
        if target and best["total"] <= target:
            recent = c.execute(
                "SELECT ts FROM walerts WHERE dest=? AND total<=? ORDER BY ts DESC LIMIT 1",
                (dest, target)).fetchone()
            fresh = (not recent) or (NOW - datetime.datetime.fromisoformat(recent[0])).days >= 7
            if fresh:
                reason = "under your ${:,} jump-on price".format(target) + \
                         ("" if not reason else " and a new low")
        if reason:
            c.execute("INSERT INTO walerts VALUES(?,?,?,?,?)",
                      (NOW.isoformat(), dest, best["total"], best["out_date"], reason))
            summaries.append(
                "*{}* ({})  *${:,}* round trip  _{}_\n"
                "{} to {}\nOut  {}\nBack {}".format(
                    w.get("name", dest), w.get("why", ""), best["total"], reason,
                    best["out_date"], best["ret_date"], desc(best["out"]), desc(best["ret"])))

    c.execute("INSERT OR REPLACE INTO wruns VALUES(?,?,?,?,?)",
              (RUN_ID, NOW.isoformat(), fetches, " | ".join(errors)[:2000],
               "sweep=" + (sweep_dest or "-")))
    c.commit()

    if summaries:
        slack("\n\n".join(summaries), "✈️ Watchlist fare")

    # run.sh swallows this module's exit code so it can never take the
    # fixed-date alerts down with it. That isolation also makes a broken
    # reader silent, so say so rather than going quiet with stale numbers.
    if fetches and len(errors) * 2 >= fetches:
        slack("{} of {} fetches failed, so watchlist prices may be stale.\n\n{}".format(
                  len(errors), fetches, "\n".join(errors[:6])[:1200]),
              "⚠️ Watchlist degraded")
    elif active and not c.execute("SELECT 1 FROM wbest WHERE run_id=?", (RUN_ID,)).fetchone():
        slack("No destination produced a price this run.", "⚠️ Watchlist found nothing")
    return fetches, errors


def slack(text, header):
    if not NOTIFY or not os.path.exists(NOTIFY):
        print("[%s] %s\n%s" % (header, "-" * 40, text))
        return
    subprocess.run(["/usr/bin/python3", NOTIFY, "--header", header],
                   input=text.encode(), env=dict(os.environ), check=True, timeout=60)


def status():
    c = db()
    print("watchlist destinations:", ", ".join(w["dest"] for w in watchlist()) or "none")
    for dest, out_date, ret_date, total, od, rd, ns in c.execute(
            "SELECT dest,out_date,ret_date,total,out_desc,ret_desc,nonstop FROM wbest w "
            "WHERE total=(SELECT MIN(total) FROM wbest WHERE dest=w.dest) GROUP BY dest"):
        print("\n{}  best ${:,}{}  {} to {}".format(
            dest, total, "  NONSTOP" if ns else "", out_date, ret_date))
        print("   out ", od)
        print("   back", rd)
    n = c.execute("SELECT COUNT(*) FROM wobs").fetchone()[0]
    print("\n{} watchlist observations, {} runs".format(
        n, c.execute("SELECT COUNT(*) FROM wruns").fetchone()[0]))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "status":
        status()
    elif mode == "test":
        slack("Watchlist wired up: " + ", ".join(w["dest"] for w in watchlist()),
              "✈️ Watchlist test")
    else:
        f, e = run()
        print("{} fetches, {} errors".format(f, len(e)))
        for x in e:
            print("  !", x)
