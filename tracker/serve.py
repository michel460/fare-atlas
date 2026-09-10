#!/usr/bin/env python3
"""Serve the viewer, and a small API for managing what is tracked.

  python3 serve.py                 # http://127.0.0.1:8712
  python3 serve.py --port 9000
  python3 serve.py --host 0.0.0.0 --token "$FARE_API_TOKEN"

Without this, changing destinations means editing config.yaml or running
destinations.py on the machine the tracker happens to live on. That makes the
tool awkward to hand to anyone else, so the same operations are exposed over
HTTP and the viewer grows a Manage panel when it finds them.

Security posture, because this writes to a config file and triggers pricing:

  * Binds to 127.0.0.1 by default. Nothing is reachable off the machine.
  * Binding anywhere else REQUIRES a credential and refuses to start without
    one, rather than quietly listening on a public interface with no auth.
    Either a shared --token, compared in constant time, or FARE_SUPABASE_URL
    to accept the session of a site that has already signed the user in.
  * Every field is validated and bounded before it reaches the config: IATA
    codes must be three letters, dates must be real ISO dates, numbers are
    range-checked. Nothing is interpolated into a shell; destinations.py is
    called in-process with parsed values.
  * Writes go through destinations.py, which reparses the config afterwards
    and rolls back if the result would not load.
"""
import argparse, datetime, hmac, json, os, re, sys, threading, time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
import destinations as D                                    # noqa: E402

VIEWER = os.path.abspath(os.path.join(DIR, "..", "viewer"))
CODE_RE = re.compile(r"^[A-Z]{3}$")
TEXT_RE = re.compile(r"^[\w \-'&.,()/]{0,60}$")
COLOUR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
CABINS = {"economy", "premium-economy", "business"}
LOCK = threading.Lock()                                     # one writer at a time


class Bad(Exception):
    pass


# ------------------------------------------------------- session auth
# Optional, and off by default. When this server sits behind a site that
# already signs the user in with Supabase, it accepts that session rather
# than inventing a second credential.
#
# Verification asks Supabase whether the token is good, instead of checking
# the signature locally. That costs a round trip, but it means the project's
# JWT secret never has to exist on this machine, and it keeps working if the
# project moves to asymmetric signing keys. For a personal deployment that is
# the better trade: nothing here is worth stealing.
#
#   FARE_SUPABASE_URL   https://<ref>.supabase.co
#   FARE_SUPABASE_ANON  the public anon key (Supabase wants it as `apikey`)
#   FARE_ALLOWED_SUBS   comma-separated user ids allowed to write
SB_URL = os.environ.get("FARE_SUPABASE_URL", "").rstrip("/")
SB_ANON = os.environ.get("FARE_SUPABASE_ANON", "")
ALLOWED = [x.strip() for x in os.environ.get("FARE_ALLOWED_SUBS", "").split(",") if x.strip()]
SESSION_AUTH = bool(SB_URL and SB_ANON)

_seen = {}                       # token -> (checked_at, user_id or None)
_SEEN_TTL = 60
_SEEN_MAX = 64                   # bounded: this is keyed by attacker-supplied input


def verify_session(header):
    """The user id behind a live Supabase session, or None.

    Never raises: anything malformed is simply not authorised. Results are
    cached briefly so a burst of writes is not a burst of round trips, and the
    cache is bounded because its keys come from whoever is calling."""
    if not header.startswith("Bearer "):
        return None
    tok = header[7:].strip()
    if not tok or len(tok) > 4096:
        return None

    now = time.time()
    hit = _seen.get(tok)
    if hit and now - hit[0] < _SEEN_TTL:
        return hit[1]

    uid = None
    try:
        req = urllib.request.Request(
            SB_URL + "/auth/v1/user",
            headers={"Authorization": "Bearer " + tok, "apikey": SB_ANON})
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status == 200:
                uid = (json.loads(r.read() or b"{}") or {}).get("id")
    except Exception:
        uid = None                                  # unreachable or rejected

    if uid and ALLOWED and uid not in ALLOWED:
        uid = None

    if len(_seen) >= _SEEN_MAX:
        _seen.clear()
    _seen[tok] = (now, uid)
    return uid


# ---------------------------------------------------------------- validation
def code(v):
    v = str(v or "").strip().upper()
    if not CODE_RE.match(v):
        raise Bad("airport code must be three letters, got %r" % v)
    return v


def text(v, field):
    v = str(v or "").strip()
    if not TEXT_RE.match(v):
        raise Bad("%s has characters that are not allowed" % field)
    return v


def date(v, field):
    try:
        return datetime.date.fromisoformat(str(v)).isoformat()
    except Exception:
        raise Bad("%s must be a date like 2027-03-04" % field)


def num(v, field, lo, hi):
    try:
        n = int(v)
    except Exception:
        raise Bad("%s must be a whole number" % field)
    if not lo <= n <= hi:
        raise Bad("%s must be between %d and %d" % (field, lo, hi))
    return n


def colour(v):
    if not v:
        return None
    v = str(v).strip()
    if not COLOUR_RE.match(v):
        raise Bad("colour must look like #6FD9CF")
    return v


class Args(dict):
    """destinations.py takes argparse namespaces; this is the same thing
    built from validated JSON instead of a command line."""
    __getattr__ = dict.get


# ---------------------------------------------------------------- operations
def op_list():
    cfg = D.load()
    trips = [dict(code=t["id"], city=t.get("city", ""), kind="trip",
                  why=t.get("why", ""), colour=t.get("colour"),
                  out=t["out"]["core"], ret=t["ret"]["core"],
                  flex=len(t["out"].get("window") or []) // 2,
                  cabins=t.get("cabins", []), enabled=True)
             for t in (cfg.get("trips") or [])]
    watch = [dict(code=w["dest"], city=w.get("city", ""), kind="watch",
                  why=w.get("why", ""), colour=w.get("colour"),
                  nights=w.get("nights"), target=w.get("target_rt"),
                  window=w.get("window"), horizon=w.get("horizon_days"),
                  enabled=bool(w.get("enabled", True)))
             for w in (cfg.get("watchlist") or [])]
    return dict(origin=cfg.get("origin"), destinations=trips + watch)


def op_add(body):
    kind = body.get("kind")
    if kind not in ("watch", "trip"):
        raise Bad("kind must be 'watch' or 'trip'")
    a = Args(code=code(body.get("code")),
             city=text(body.get("city"), "city") or None,
             why=text(body.get("why"), "why"),
             colour=colour(body.get("colour")))

    if kind == "watch":
        a["nights"] = num(body.get("nights", 14), "nights", 1, 90)
        a["target"] = num(body.get("target"), "target", 1, 100000)
        a["max_stops"] = num(body.get("max_stops", 2), "stops", 0, 3)
        if body.get("window"):
            w = body["window"]
            if not isinstance(w, (list, tuple)) or len(w) != 2:
                raise Bad("window must be a start and an end date")
            a["window"] = [date(w[0], "window start"), date(w[1], "window end")]
            if a["window"][0] > a["window"][1]:
                raise Bad("the window ends before it starts")
            a["horizon"] = [30, 300]
        else:
            h = body.get("horizon") or [30, 300]
            a["horizon"] = [num(h[0], "horizon start", 1, 360), num(h[1], "horizon end", 2, 360)]
            if a["horizon"][0] >= a["horizon"][1]:
                raise Bad("the horizon ends before it starts")
            a["window"] = None
        D.cmd_watch(a)
    else:
        a["out"] = date(body.get("out"), "outbound date")
        a["ret"] = date(body.get("ret"), "return date")
        if a["ret"] < a["out"]:
            raise Bad("the return is before the outbound")
        if a["out"] < datetime.date.today().isoformat():
            raise Bad("the outbound date is in the past")
        a["flex"] = num(body.get("flex", 0), "flex", 0, 7)
        cabins = body.get("cabins") or ["economy"]
        if not set(cabins) <= CABINS:
            raise Bad("cabins must be from: %s" % ", ".join(sorted(CABINS)))
        a["cabins"] = ",".join(cabins)
        a["max_stops"] = num(body.get("max_stops", 1), "stops", 0, 3)
        a["source"] = "fastflights" if body.get("source") != "serpapi" else "serpapi"
        D.cmd_trip(a)
    return op_list()


def op_set_enabled(c, on):
    D._set_enabled(code(c), on)
    return op_list()


def op_remove(c):
    D.cmd_remove(Args(code=code(c)))
    return op_list()


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "fare-atlas"
    token = None
    remote = False

    def _send(self, status, payload=None, ctype="application/json"):
        body = b"" if payload is None else (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authed(self):
        auth = self.headers.get("Authorization", "")
        if SESSION_AUTH and verify_session(auth):
            return True
        if Handler.token:
            return hmac.compare_digest(auth, "Bearer " + Handler.token)
        return not (SESSION_AUTH or Handler.remote)         # loopback, no auth needed

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 16384:
            raise Bad("expected a small JSON body")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._send(200, dict(ok=True, writable=True,
                                        auth="session" if SESSION_AUTH else
                                             ("token" if Handler.token else "none")))
        if path == "/api/destinations":
            if not self._authed():
                return self._send(401, dict(error="unauthorised"))
            return self._send(200, op_list())
        return self._static(path)

    def do_POST(self):
        self._write(urlparse(self.path).path, "POST")

    def do_DELETE(self):
        self._write(urlparse(self.path).path, "DELETE")

    def _write(self, path, method):
        if not self._authed():
            return self._send(401, dict(error="unauthorised"))
        try:
            with LOCK:
                if method == "POST" and path == "/api/destinations":
                    return self._send(200, op_add(self._json_body()))
                m = re.match(r"^/api/destinations/([A-Za-z]{3})(/enable|/disable)?$", path)
                if m:
                    c, verb = m.group(1), m.group(2)
                    if method == "DELETE" and not verb:
                        return self._send(200, op_remove(c))
                    if method == "POST" and verb:
                        return self._send(200, op_set_enabled(c, verb == "/enable"))
                return self._send(404, dict(error="no such endpoint"))
        except Bad as e:
            return self._send(400, dict(error=str(e)))
        except SystemExit as e:                             # destinations.py rejects it
            return self._send(400, dict(error=str(e)))
        except Exception as e:
            return self._send(500, dict(error="%s: %s" % (type(e).__name__, e)))

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(VIEWER, rel))
        if not full.startswith(VIEWER + os.sep) and full != VIEWER:
            return self._send(403, dict(error="forbidden"))  # no path traversal
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self._send(404, dict(error="not found"))
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".json": "application/json", ".css": "text/css",
                 ".png": "image/png"}.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s %s\n" % (self.command, self.path))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8712)
    ap.add_argument("--token", default=os.environ.get("FARE_API_TOKEN", ""))
    args = ap.parse_args()

    Handler.remote = args.host not in ("127.0.0.1", "localhost", "::1")
    if Handler.remote and not (args.token or SESSION_AUTH):
        sys.exit("refusing to listen on %s without --token or FARE_SUPABASE_URL: this API "
                 "writes to your config and starts pricing runs." % args.host)
    Handler.token = args.token or None

    print("viewer  http://%s:%d/" % (args.host, args.port))
    print("api     %s" % ("session (Supabase)" if SESSION_AUTH else
                          ("shared token" if Handler.token else "loopback only, no auth")))
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
