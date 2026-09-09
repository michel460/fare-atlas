"""Parse a Firecrawl markdown render of a Google Flights one-way results page into itineraries."""
import re, sys, json
WS = re.compile("[    ]")
def parse(path):
    lines = [l.strip() for l in WS.sub(" ", open(path, encoding="utf-8").read()).splitlines()]
    starts = [i for i, l in enumerate(lines) if re.match(r"^- \d{1,2}:\d{2} [AP]M$", l)]
    out, seen = [], set()
    for si, i in enumerate(starts):
        j_end = starts[si + 1] if si + 1 < len(starts) else min(len(lines), i + 80)
        blk = [l for l in lines[i:j_end] if l]
        on = [l for l in blk if re.match(r"^\d{1,2}:\d{2} [AP]M on ", l)]
        if len(on) < 2: continue
        dep, arr = on[0], on[1]
        k = blk.index(arr) + 1
        airlines = next((l for l in blk[k:k+4] if not re.match(r"^(\d+ hr|–|\d{1,2}:\d{2})", l) and not l.startswith("$")), None)
        dur = next((l for l in blk if re.match(r"^\d+ hr( \d+ min)?$", l)), None)
        stops_l = next((l for l in blk if re.match(r"^(Nonstop|\d stops?)$", l)), None)
        stops = None if stops_l is None else (0 if stops_l == "Nonstop" else int(stops_l[0]))
        via = []
        if stops:
            s_idx = blk.index(stops_l)
            for l in blk[s_idx+1:s_idx+1+2*stops+2]:
                if re.match(r"^,? ?[A-Z]{3}$", l): via.append(l.strip(", "))
                if len(via) == stops: break
        price_l = next((l for l in blk if re.match(r"^\$[\d,]+$", l)), None)
        price = int(price_l[1:].replace(",", "")) if price_l else None
        lay = next((m.group(1) for l in blk for m in [re.match(r"^\d stops? in .*?\d stops?(\d+ hr(?: \d+ min)?)$", l)] if m), None)
        rec = dict(dep=dep, arr=arr, airlines=airlines, dur=dur, stops=stops, via=via, price=price, layover=lay)
        key = (dep, arr, airlines, price)
        if price and key not in seen: seen.add(key); out.append(rec)
    return out
if __name__ == "__main__":
    for path in sys.argv[1:]:
        recs = sorted(parse(path), key=lambda r: (r["price"] or 1e9))
        print(f"\n== {path} · {len(recs)} itineraries")
        for r in recs[:12]:
            print(f"  ${r['price']:>6,} | {r['stops']} stop via {','.join(r['via']) or '-':<8} | {(r['airlines'] or '?')[:26]:<26} | {r['dur'] or '?':<13} | {r['dep']} -> {r['arr']}")
        json.dump(recs, open(path.replace(".md", ".json"), "w"), indent=1)
