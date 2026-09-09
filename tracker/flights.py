"""Google Flights one-way reader (keyless) for the DPS<->YVR tracker."""
import json, time, datetime
from selectolax.lexbor import LexborHTMLParser
from fast_flights import create_query, FlightQuery, Passengers, fetch_flights_html

def _t(v):
    v = [*(v or []), None, None]; return (v[0] or 0, v[1] or 0)

def _entry(k, bucket):
    fl = k[0]; price = k[1][0][1] if k[1] and k[1][0] else None
    legs = []
    for s in fl[2]:
        legs.append(dict(frm=s[3], to=s[6], dep_date=tuple(s[20]), dep=_t(s[8]), arr_date=tuple(s[21]), arr=_t(s[10]),
                         dur=s[11], plane=s[17], flight_no=(s[22][0] if isinstance(s[22], list) and s[22] else None)))
    lay = 0
    for a, b in zip(legs, legs[1:]):
        da = datetime.datetime(*a["arr_date"], *a["arr"]); db = datetime.datetime(*b["dep_date"], *b["dep"])
        lay += int((db - da).total_seconds() // 60)
    return dict(bucket=bucket, price=price, airlines=fl[1], stops=len(legs) - 1, via=[l["to"] for l in legs[:-1]],
                dep=f"{legs[0]['dep_date'][1]:02d}-{legs[0]['dep_date'][2]:02d} {legs[0]['dep'][0]:02d}:{legs[0]['dep'][1]:02d}",
                arr=f"{legs[-1]['arr_date'][1]:02d}-{legs[-1]['arr_date'][2]:02d} {legs[-1]['arr'][0]:02d}:{legs[-1]['arr'][1]:02d}",
                total_min=sum(l["dur"] for l in legs) + lay, layover_min=lay, legs=legs)

def one_way(date, frm, to, seat, adults=1, max_stops=1, currency="USD", retries=3):
    q = create_query(flights=[FlightQuery(date=date, from_airport=frm, to_airport=to, max_stops=max_stops)],
                     seat=seat, trip="one-way", passengers=Passengers(adults=adults), currency=currency, language="en-US")
    err = None
    for _ in range(retries):
        try:
            html = fetch_flights_html(q)
            s = LexborHTMLParser(html).css_first(r"script.ds\:1")
            if s is None: raise RuntimeError("no ds:1 block (blocked/consent page?)")
            payload = json.loads(s.text().split("data:", 1)[1].rsplit(",", 1)[0])
            out = []
            for idx, bucket in ((2, "best"), (3, "other")):
                if payload[idx] and payload[idx][0]:
                    for k in payload[idx][0]:
                        try: out.append(_entry(k, bucket))
                        except Exception as e: out.append(dict(bucket=bucket, parse_error=repr(e)[:120]))
            return dict(ok=True, url=q.url(), flights=out)
        except Exception as e:
            err = repr(e)[:300]; time.sleep(4)
    return dict(ok=False, url=q.url(), error=err, flights=[])

if __name__ == "__main__":
    import sys
    OUT, RET = sys.argv[1], sys.argv[2]; adults = int(sys.argv[3]) if len(sys.argv) > 3 else 1; DEST = sys.argv[4] if len(sys.argv) > 4 else "YVR"
    snap = dict(queried_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), out=OUT, ret=RET, adults=adults, dest=DEST, currency="USD", legs={})
    for seat in ("economy", "premium-economy", "business"):
        for tag, d, f, t in (("out", OUT, "DPS", DEST), ("ret", RET, DEST, "DPS")):
            snap["legs"][f"{seat}/{tag}"] = one_way(d, f, t, seat, adults); time.sleep(2)
    json.dump(snap, open(f"snap_DPS-{DEST}_{OUT}_{RET}_{adults}pax.json", "w"), indent=1)
    for key, r in snap["legs"].items():
        fl = sorted([x for x in r["flights"] if x.get("price")], key=lambda x: x["price"])
        print(f"\n== {key} · {len(fl)} itineraries · ok={r['ok']} {r.get('error','')}")
        for x in fl[:8]:
            h, m = divmod(x["total_min"], 60)
            print(f"  ${x['price']:>6,} | {'+'.join(x['airlines']):<6} | via {','.join(x['via']) or 'nonstop':<4} | {x['dep']} -> {x['arr']} | {h:2d}h{m:02d} (layover {x['layover_min']//60}h{x['layover_min']%60:02d}) | {x['bucket']}")
