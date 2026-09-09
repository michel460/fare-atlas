#!/usr/bin/env python3
"""DPS trip fare tracker. Fetches one-way fares per direction/date/cabin, logs to SQLite,
sends an alert when the cheapest mix-and-match total moves, and a weekly digest on Sunday.
Modes: daily (default) | digest | test-slack | status. Flag: --full-window (fetch the +-1 day dates too)."""
import os, sys, json, sqlite3, datetime, subprocess, time, urllib.request, urllib.parse
import yaml

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
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
SERP_CLASS = {"economy": 1, "premium-economy": 2, "business": 3}

# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS obs(run_id TEXT, ts TEXT, trip TEXT, leg TEXT, date TEXT, cabin TEXT, source TEXT,
        airlines TEXT, stops INT, via TEXT, dep TEXT, arr TEXT, total_min INT, layover_min INT, price INT);
    CREATE TABLE IF NOT EXISTS insights(run_id TEXT, ts TEXT, trip TEXT, leg TEXT, date TEXT, cabin TEXT,
        lowest INT, level TEXT, typical_lo INT, typical_hi INT);
    CREATE TABLE IF NOT EXISTS metrics(run_id TEXT, ts TEXT, trip TEXT, cabin TEXT, total INT, quality_total INT,
        out_price INT, out_desc TEXT, ret_price INT, ret_desc TEXT, ref2_total INT, ref2_desc TEXT);
    CREATE TABLE IF NOT EXISTS alerts(ts TEXT, trip TEXT, cabin TEXT, total INT, reason TEXT);
    CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, ts TEXT, mode TEXT, fetches INT, errors TEXT);
    """)
    cols = {r[1] for r in c.execute("PRAGMA table_info(metrics)")}
    for col in ("q_out_desc", "q_ret_desc", "ski_total", "ski_note", "ski_ref2_total"):
        if col not in cols: c.execute(f"ALTER TABLE metrics ADD COLUMN {col} " + ("INT" if col.endswith("total") else "TEXT"))
    if "token" not in {r[1] for r in c.execute("PRAGMA table_info(obs)")}: c.execute("ALTER TABLE obs ADD COLUMN token TEXT")
    c.execute("""CREATE TABLE IF NOT EXISTS bags(run_id TEXT, ts TEXT, trip TEXT, leg TEXT, date TEXT, cabin TEXT, airlines TEXT, price INT,
        bag_included INT, price_with_bag INT, detail TEXT)""")
    return c

# ---------------------------------------------------------------- fetchers (all return list[row])
def _row(trip, leg, date, cabin, source, **k):
    k.setdefault("token", None)
    return dict(run_id=RUN_ID, ts=NOW.isoformat(), trip=trip, leg=leg, date=date, cabin=cabin, source=source, **k)

def fetch_fastflights(trip, leg, date, cabin, frm, to, max_stops):
    from flights import one_way
    r = one_way(date, frm, to, cabin, CFG["adults"], max_stops)
    if not r["ok"]: raise RuntimeError(r.get("error"))
    rows = []
    for f in r["flights"]:
        if not f.get("price"): continue
        l0, l1 = f["legs"][0], f["legs"][-1]
        rows.append(_row(trip, leg, date, cabin, "fastflights", airlines="/".join(f["airlines"]), stops=f["stops"], via=",".join(f["via"]),
                         dep="%04d-%02d-%02d %02d:%02d" % (*l0["dep_date"], *l0["dep"]), arr="%04d-%02d-%02d %02d:%02d" % (*l1["arr_date"], *l1["arr"]),
                         total_min=f["total_min"], layover_min=f["layover_min"], price=f["price"]))
    return rows, None

def fetch_serpapi(trip, leg, date, cabin, frm, to, stops_param):
    key = os.environ.get("SERPAPI_API_KEY")
    if not key: raise RuntimeError("SERPAPI_API_KEY missing")
    p = dict(engine="google_flights", departure_id=frm, arrival_id=to, outbound_date=date, type=2, travel_class=SERP_CLASS[cabin],
             stops=stops_param, adults=CFG["adults"], currency=CFG["currency"], hl="en", api_key=key)
    with urllib.request.urlopen("https://serpapi.com/search.json?" + urllib.parse.urlencode(p), timeout=90) as r: d = json.load(r)
    if d.get("error") and "hasn't returned any results" not in d["error"]: raise RuntimeError(d["error"])
    rows = []
    for f in (d.get("best_flights") or []) + (d.get("other_flights") or []):
        legs, lays = f["flights"], f.get("layovers") or []
        if not f.get("price"): continue
        rows.append(_row(trip, leg, date, cabin, "serpapi", airlines="/".join(sorted({l["airline"] for l in legs})), stops=len(legs) - 1,
                         via=",".join(l["id"] for l in lays), dep=legs[0]["departure_airport"]["time"], arr=legs[-1]["arrival_airport"]["time"],
                         total_min=f["total_duration"], layover_min=sum(l["duration"] for l in lays), price=f["price"], token=f.get("booking_token")))
    pi = d.get("price_insights") or None
    ins = None
    if pi and pi.get("lowest_price"):
        tr = pi.get("typical_price_range") or [None, None]
        ins = dict(run_id=RUN_ID, ts=NOW.isoformat(), trip=trip, leg=leg, date=date, cabin=cabin, lowest=pi.get("lowest_price"),
                   level=pi.get("price_level"), typical_lo=tr[0], typical_hi=tr[1])
    return rows, ins

def fetch_firecrawl(trip, leg, date, cabin, frm, to):
    """Rendered-page fallback for pairs Google only loads client-side."""
    from fast_flights import create_query, FlightQuery, Passengers
    from parse_gf_md import parse
    q = create_query(flights=[FlightQuery(date=date, from_airport=frm, to_airport=to)], seat=cabin, trip="one-way",
                     passengers=Passengers(adults=CFG["adults"]), currency=CFG["currency"], language="en-US")
    out = os.path.join(DIR, "logs", f"fc_{trip}_{leg}_{date}_{cabin}.md")
    subprocess.run(["firecrawl", "scrape", q.url(), "--format", "markdown", "--wait-for", "9000", "--country", "US", "-o", out],
                   check=True, timeout=180, capture_output=True)
    rows = []
    for r in parse(out):
        dur = r.get("dur") or ""; mins = 0
        for n, unit in zip(dur.split()[::2], dur.split()[1::2]): mins += int(n) * (60 if unit.startswith("hr") else 1)
        rows.append(_row(trip, leg, date, cabin, "firecrawl", airlines=(r["airlines"] or "?").replace(", ", "/"), stops=r["stops"] if r["stops"] is not None else 9,
                         via=",".join(r["via"]), dep=r["dep"], arr=r["arr"], total_min=mins or None, layover_min=None, price=r["price"]))
    return rows, None

# ---------------------------------------------------------------- collection
def dates_for(trip, leg, full):
    """Core date always; the flexible dates around it depend on what they cost.

    A window priced on SerpAPI is metered, so it stays on the Sunday cadence.
    A window with window_source: fastflights is keyless and free, so it runs
    every day: date flexibility is only worth having if it is actually looked
    at more than once a week."""
    d = trip[leg]
    win = d.get("window") or []
    if trip.get("window_source") == "fastflights":
        return [d["core"]] + win
    return [d["core"]] + (win if full else [])


def source_for(trip, leg, date):
    """The core date keeps the trip's source (SerpAPI buys price_insights);
    the flexible dates around it can use a cheaper reader."""
    if date != trip[leg]["core"] and trip.get("window_source"):
        return trip["window_source"]
    return trip["source"]

def collect(full_window):
    c = db(); fetches, errors = 0, []
    for t in CFG["trips"]:
        for leg in ("out", "ret"):
            frm, to = (t["origin"], t["dest"]) if leg == "out" else (t["dest"], t["origin"])
            for date in dates_for(t, leg, full_window):
                for cabin in t["cabins"]:
                    rows, ins = [], None
                    try:
                        if source_for(t, leg, date) == "serpapi":
                            rows, ins = fetch_serpapi(t["id"], leg, date, cabin, frm, to, t.get("serpapi_stops", 2))
                        else:
                            rows, ins = fetch_fastflights(t["id"], leg, date, cabin, frm, to, t["max_stops"])
                    except Exception as e:
                        errors.append(f"{t['id']}/{leg}/{date}/{cabin}: {e!r}"[:200])
                        fb = t.get("fallback")
                        try:
                            if fb == "firecrawl": rows, ins = fetch_firecrawl(t["id"], leg, date, cabin, frm, to)
                            elif fb == "fastflights": rows, ins = fetch_fastflights(t["id"], leg, date, cabin, frm, to, t["max_stops"])
                            if rows: errors[-1] += f" -> {fb} fallback OK ({len(rows)} rows)"
                        except Exception as e2: errors.append(f"  {fb} fallback: {e2!r}"[:200])
                    fetches += 1
                    if rows:
                        c.executemany("INSERT INTO obs(run_id,ts,trip,leg,date,cabin,source,airlines,stops,via,dep,arr,total_min,layover_min,price,token) VALUES(:run_id,:ts,:trip,:leg,:date,:cabin,:source,:airlines,:stops,:via,:dep,:arr,:total_min,:layover_min,:price,:token)", rows)
                    if ins:
                        c.execute("INSERT INTO insights VALUES(:run_id,:ts,:trip,:leg,:date,:cabin,:lowest,:level,:typical_lo,:typical_hi)", ins)
                    c.commit(); time.sleep(1.5)
    c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?)", (RUN_ID, NOW.isoformat(), "full" if full_window else "core", fetches, "\n".join(errors)))
    c.commit(); c.close()
    return fetches, errors

def check_bags(full_window):
    """For trips carrying a ski bag: does the cheapest fare per leg include a checked bag, and what does the fare with a bag cost?"""
    if not full_window: return
    key = os.environ.get("SERPAPI_API_KEY"); c = db()
    for t in CFG["trips"]:
        sb = t.get("ski_bag")
        if not (sb and sb.get("check_bag_inclusion") and key): continue
        for leg in ("out", "ret"):
            frm, to = (t["origin"], t["dest"]) if leg == "out" else (t["dest"], t["origin"])
            for cabin in t["cabins"]:
                r = c.execute("SELECT price, airlines, date, token FROM obs WHERE trip=? AND leg=? AND cabin=? AND run_id=? AND stops<=? AND token IS NOT NULL ORDER BY price LIMIT 1",
                              (t["id"], leg, cabin, RUN_ID, t["max_stops"])).fetchone()
                if not r: continue
                price, airlines, date, token = r
                try:
                    p = dict(engine="google_flights", departure_id=frm, arrival_id=to, outbound_date=date, type=2, travel_class=SERP_CLASS[cabin], adults=CFG["adults"],
                             currency=CFG["currency"], hl="en", booking_token=token, api_key=key)
                    with urllib.request.urlopen("https://serpapi.com/search.json?" + urllib.parse.urlencode(p), timeout=90) as resp: d = json.load(resp)
                    opts = []
                    for o in d.get("booking_options") or []:
                        x = o.get("together") or o.get("departing") or {}
                        bags = " ".join(x.get("baggage_prices") or [])
                        opts.append((x.get("book_with"), x.get("price"), "checked bag" in bags.lower(), bags))
                    airline_opts = [o for o in opts if o[0] and any(a.split()[0].lower() in o[0].lower() for a in airlines.split("/"))] or opts
                    cheapest = min(airline_opts, key=lambda o: o[1] or 1e9) if airline_opts else None
                    with_bag = [o for o in airline_opts if o[2] and o[1]]
                    pwb = min(with_bag, key=lambda o: o[1])[1] if with_bag else None
                    c.execute("INSERT INTO bags VALUES(?,?,?,?,?,?,?,?,?,?,?)", (RUN_ID, NOW.isoformat(), t["id"], leg, date, cabin, airlines, price,
                              int(bool(cheapest and cheapest[2])), pwb, json.dumps(opts[:6])[:800]))
                    c.commit()
                except Exception as e:
                    c.execute("INSERT INTO bags VALUES(?,?,?,?,?,?,?,?,?,?,?)", (RUN_ID, NOW.isoformat(), t["id"], leg, date, cabin, airlines, price, None, None, f"error {e!r}"[:300])); c.commit()
                time.sleep(1.5)
    c.close()

def ski_fee(t, airlines):
    """USD per direction for one ski set on this itinerary, or None if any carrier's rule is unknown."""
    fees = (t.get("ski_bag") or {}).get("fees_each_way") or {}
    vals = []
    for a in (airlines or "").split("/"):
        hit = next((v for k, v in fees.items() if k.lower() in a.lower() or a.lower() in k.lower()), None)
        if hit is None: return None
        vals.append(hit)
    return max(vals) if vals else None

# ---------------------------------------------------------------- metrics
def _best(c, trip, leg, cabin, run_id, max_stops, max_lay=None, min_stops=None):
    sql = "SELECT price, airlines, via, stops, total_min, layover_min, date, dep FROM obs WHERE trip=? AND leg=? AND cabin=? AND run_id=? AND stops<=?"
    args = [trip, leg, cabin, run_id, max_stops]
    if min_stops is not None: sql += " AND stops>=?"; args.append(min_stops)
    if max_lay is not None: sql += " AND layover_min IS NOT NULL AND layover_min<=?"; args.append(max_lay)
    return c.execute(sql + " ORDER BY price LIMIT 1", args).fetchone()

def _desc(r):
    if not r: return None
    h, m = divmod(r[4] or 0, 60); day = datetime.date.fromisoformat(r[6]).strftime("%-d %b")
    lay = f", {r[5] // 60}h{r[5] % 60:02d} stop" if r[5] else ""
    return f"${r[0]:,} {r[1]} via {r[2] or 'nonstop'} {h}h{m:02d}{lay} on {day}"

def compute_metrics(run_id=RUN_ID):
    c = db(); out = []
    for t in CFG["trips"]:
        for cabin in t["cabins"]:
            o, r = _best(c, t["id"], "out", cabin, run_id, t["max_stops"]), _best(c, t["id"], "ret", cabin, run_id, t["max_stops"])
            qo, qr = _best(c, t["id"], "out", cabin, run_id, t["max_stops"], CFG["quality_max_layover_min"]), _best(c, t["id"], "ret", cabin, run_id, t["max_stops"], CFG["quality_max_layover_min"])
            r2o, r2r = _best(c, t["id"], "out", cabin, run_id, 2), _best(c, t["id"], "ret", cabin, run_id, 2)
            m = dict(run_id=run_id, ts=NOW.isoformat(), trip=t["id"], cabin=cabin,
                     total=(o[0] + r[0]) if o and r else None, quality_total=(qo[0] + qr[0]) if qo and qr else None,
                     out_price=o[0] if o else None, out_desc=_desc(o), ret_price=r[0] if r else None, ret_desc=_desc(r),
                     ref2_total=(r2o[0] + r2r[0]) if r2o and r2r else None, ref2_desc=(f"{_desc(r2o)} + {_desc(r2r)}" if r2o and r2r else None),
                     q_out_desc=_desc(qo), q_ret_desc=_desc(qr), ski_total=None, ski_note=None, ski_ref2_total=None)
            if t.get("ski_bag"):
                fo, fr = (ski_fee(t, o[1]) if o else None), (ski_fee(t, r[1]) if r else None)
                bags = {b[0]: b for b in c.execute("SELECT leg, bag_included, price_with_bag, price FROM bags WHERE trip=? AND cabin=? AND run_id=(SELECT run_id FROM bags WHERE trip=? AND cabin=? ORDER BY ts DESC LIMIT 1)", (t["id"], cabin, t["id"], cabin))}
                notes = []
                if m["total"] is not None and fo is not None and fr is not None:
                    base_o = o[0]; base_r = r[0]
                    for leg, base in (("out", base_o), ("ret", base_r)):
                        b = bags.get(leg)
                        if b and b[1] == 0 and b[2]: notes.append(f"{'out' if leg == 'out' else 'back'} fare has no checked bag, with bag ${b[2]:,}"); 
                    bo = bags.get("out"); br = bags.get("ret")
                    eff_o = bo[2] if (bo and bo[1] == 0 and bo[2] and bo[3] == base_o) else base_o
                    eff_r = br[2] if (br and br[1] == 0 and br[2] and br[3] == base_r) else base_r
                    m["ski_total"] = eff_o + eff_r + fo + fr
                    if fo or fr: notes.append(f"ski fees ${fo:,} out + ${fr:,} back")
                elif m["total"] is not None: notes.append("ski fee unknown for " + "/".join(sorted({a for x in (o, r) if x for a in x[1].split('/') if ski_fee(t, a) is None})))
                if m["ref2_total"] is not None:
                    f2o, f2r = ski_fee(t, r2o[1]), ski_fee(t, r2r[1])
                    if f2o is not None and f2r is not None: m["ski_ref2_total"] = m["ref2_total"] + f2o + f2r
                m["ski_note"] = "; ".join(notes) or None
            c.execute("DELETE FROM metrics WHERE run_id=? AND trip=? AND cabin=?", (run_id, t["id"], cabin))
            c.execute("INSERT INTO metrics(run_id,ts,trip,cabin,total,quality_total,out_price,out_desc,ret_price,ret_desc,ref2_total,ref2_desc,q_out_desc,q_ret_desc,ski_total,ski_note,ski_ref2_total) "
                      "VALUES(:run_id,:ts,:trip,:cabin,:total,:quality_total,:out_price,:out_desc,:ret_price,:ret_desc,:ref2_total,:ref2_desc,:q_out_desc,:q_ret_desc,:ski_total,:ski_note,:ski_ref2_total)", m)
            out.append(m)
    c.commit(); c.close(); return out

def history(trip, cabin):
    c = db()
    rows = c.execute("SELECT ts, total, quality_total FROM metrics WHERE trip=? AND cabin=? AND total IS NOT NULL ORDER BY ts", (trip, cabin)).fetchall()
    last_alert = c.execute("SELECT total FROM alerts WHERE trip=? AND cabin=? ORDER BY ts DESC LIMIT 1", (trip, cabin)).fetchone()
    raw = c.execute("SELECT leg, level, typical_lo, typical_hi, lowest, date FROM insights WHERE trip=? AND cabin=? AND run_id=(SELECT run_id FROM insights WHERE trip=? AND cabin=? ORDER BY ts DESC LIMIT 1)", (trip, cabin, trip, cabin)).fetchall()
    core = {leg: trip_cfg(trip)[leg]["core"] for leg in ("out", "ret")}
    ins = [next((r for r in raw if r[0] == leg and r[5] == core[leg]), next((r for r in raw if r[0] == leg), None)) for leg in ("out", "ret")]
    ins = [r[:5] for r in ins if r]
    c.close(); return rows, (last_alert[0] if last_alert else None), ins

# ---------------------------------------------------------------- reporting
def slack(text, header):
    if not NOTIFY or not os.path.exists(NOTIFY):
        print("[%s] %s\n%s" % (header, "-" * 40, text))
        return
    subprocess.run(["/usr/bin/python3", NOTIFY, "--header", header],
                   input=text.encode(), env=dict(os.environ), check=True, timeout=60)

def trip_cfg(tid): return next(t for t in CFG["trips"] if t["id"] == tid)

def days_to(tid): return (datetime.date.fromisoformat(trip_cfg(tid)["out"]["core"]) - NOW.date()).days

def fmt_metric_block(m, rows, last_alert, ins, verbose):
    t = trip_cfg(m["trip"]); bench = rows[0][1] if rows else None
    lo = min(r[1] for r in rows) if rows else None; hi = max(r[1] for r in rows) if rows else None
    lines = [f"*{t['name']} · {m['cabin'].replace('-', ' ')}*  ({days_to(m['trip'])} days to go)"]
    if m["total"] is None:
        lines.append(f"No itinerary with <= {t['max_stops']} stop in either direction." + (f" Best 2-stop reference: ${m['ref2_total']:,} ({m['ref2_desc']})" if m["ref2_total"] else ""))
        return "\n".join(lines)
    delta = f" ({(m['total'] - bench) / bench * 100:+.1f}% vs benchmark ${bench:,})" if bench and bench != m["total"] else " (= benchmark)" if bench else ""
    lines.append(f"Cheapest 1-stop, per person: *${m['total']:,}*{delta}")
    lines.append(f"  out: {m['out_desc']}"); lines.append(f"  back: {m['ret_desc']}")
    if m["quality_total"] and (m["quality_total"] != m["total"] or m.get("q_out_desc") != m["out_desc"] or m.get("q_ret_desc") != m["ret_desc"]):
        lines.append(f"  with layovers <= 6h: ${m['quality_total']:,}")
        if m.get("q_out_desc") != m["out_desc"]: lines.append(f"    out: {m['q_out_desc']}")
        if m.get("q_ret_desc") != m["ret_desc"]: lines.append(f"    back: {m['q_ret_desc']}")
    if m["ref2_total"] and m["ref2_total"] < m["total"]: lines.append(f"  cheaper with 2 stops: ${m['ref2_total']:,}" + (f" ({m['ref2_desc']})" if verbose else ""))
    if t.get("ski_bag"):
        if m.get("ski_total"): lines.append(f"  *with ski bag: ${m['ski_total']:,}*" + (f" · 2-stop with ski bag: ${m['ski_ref2_total']:,}" if m.get("ski_ref2_total") else ""))
        if m.get("ski_note"): lines.append(f"    {m['ski_note']}")
    if verbose and rows and len(rows) > 1: lines.append(f"  since tracking: low ${lo:,} · high ${hi:,} · {len(rows)} readings")
    for leg, level, tlo, thi, lowest in ins:
        if level: lines.append(f"  Google says {'out' if leg == 'out' else 'back'} is *{level}* (typical ${tlo:,}-${thi:,})")
    return "\n".join(lines)

def check_alerts(metrics):
    c = db(); msgs = []
    for m in metrics:
        if m["total"] is None: continue
        rows, last_alert, ins = history(m["trip"], m["cabin"])
        prior = [r[1] for r in rows if r[0] < m["ts"]]
        base = last_alert if last_alert is not None else (prior[0] if prior else None)
        reason = None
        if base is not None and abs(m["total"] - base) / base * 100 >= CFG["alert_threshold_pct"]:
            reason = f"moved {(m['total'] - base) / base * 100:+.1f}% vs last reference ${base:,}"
        elif prior and m["total"] < min(prior): reason = f"new low (was ${min(prior):,})"
        if reason:
            c.execute("INSERT INTO alerts VALUES(?,?,?,?,?)", (NOW.isoformat(), m["trip"], m["cabin"], m["total"], reason)); c.commit()
            msgs.append(fmt_metric_block(m, rows, last_alert, ins, verbose=False) + f"\n  why: {reason}")
    c.close()
    if msgs: slack("\n\n".join(msgs), "✈️ Fare move")
    return len(msgs)

def digest(metrics):
    blocks = []
    for m in metrics:
        rows, last_alert, ins = history(m["trip"], m["cabin"])
        blocks.append(fmt_metric_block(m, rows, last_alert, ins, verbose=True))
    notes = [f"_{t['id']}: {t['notes']}_" for t in CFG["trips"] if t.get("notes")]
    c = db(); left = None
    try:
        key = os.environ.get("SERPAPI_API_KEY")
        if key:
            with urllib.request.urlopen(f"https://serpapi.com/account.json?api_key={key}", timeout=30) as r: left = json.load(r).get("total_searches_left")
    except Exception: pass
    errs = c.execute("SELECT errors FROM runs WHERE run_id=?", (RUN_ID,)).fetchone(); c.close()
    tail = [f"Prices per person, USD, mix-and-match one-ways. SerpAPI searches left: {left}."]
    if errs and errs[0]: tail.append("Fetch errors this run:\n" + errs[0][:600])
    slack("\n\n".join(blocks + notes + tail), "✈️ Weekly fare digest")

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    mode = next((a for a in sys.argv[1:] if not a.startswith("--")), "daily")
    full = "--full-window" in sys.argv or NOW.weekday() == CFG["digest_weekday"]
    if mode == "test-slack":
        slack("Fare tracker wired up. Daily 07:00 SGT, digest on Sundays.", "✈️ Fare tracker test"); sys.exit(0)
    if mode == "status":
        ms = compute_metrics(db().execute("SELECT run_id FROM obs ORDER BY ts DESC LIMIT 1").fetchone()[0])
        parts = [f"{m['trip']} {m['cabin'].replace('premium-economy', 'PE')} ${m['total']:,}" for m in ms if m["total"]]
        print(f"{NOW:%Y-%m-%d %H:%M} SGT · cheapest 1-stop per person: " + " · ".join(parts)); sys.exit(0)
    fetches, errors = collect(full)
    print(f"{RUN_ID} fetched {fetches} pages, {len(errors)} errors"); [print("  ERR", e) for e in errors]
    check_bags(full)
    metrics = compute_metrics()
    for m in metrics: print(f"  {m['trip']:<4} {m['cabin']:<16} total={m['total']} quality={m['quality_total']} ref2={m['ref2_total']}")
    n = check_alerts(metrics); print(f"  alerts sent: {n}")
    if mode == "digest" or NOW.weekday() == CFG["digest_weekday"] or "--digest" in sys.argv: digest(metrics); print("  digest sent")
    if errors and not (mode == "digest" or NOW.weekday() == CFG["digest_weekday"]):
        slack("Fare tracker fetch errors:\n" + "\n".join(errors)[:3000], "⚠️ Fare tracker")
