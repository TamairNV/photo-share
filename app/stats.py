"""Lightweight usage stats: who looked, what got viewed/downloaded/uploaded, bytes moved, errors.

Everything lives in one SQLite file next to the photos (DATA/.stats.db). Writes are queued in memory
and flushed by one background thread every couple of seconds, so a request never waits on the disk.
Live numbers (current transfer rate, transfers in flight) are kept in memory only.

Two tables:
  hits     one row per human-meaningful action (page view, photo opened, file downloaded, upload ...)
  traffic  per-minute totals of requests, bytes out/in and errors, measured by the ASGI middleware
"""
import hashlib, os, queue, sqlite3, threading, time
from collections import deque

_q: "queue.Queue[tuple]" = queue.Queue()
_db_path = None
_salt = b""
KEEP_DAYS = 120

# ---- live (in memory) ----
_live_lock = threading.Lock()
_sec_out: deque = deque(maxlen=600)   # (second, bytes) for the last 10 minutes
_sec_in: deque = deque(maxlen=600)
_active: dict[int, dict] = {}          # in-flight requests that move real data
_minute: dict = {}                     # current minute's traffic totals, flushed to sqlite
_next_id = 0
STARTED = time.time()


def init(path, secret):
    global _db_path, _salt
    _db_path = str(path)
    _salt = hashlib.sha256(("stats:" + secret).encode()).digest()
    con = connect()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS hits (
            ts REAL NOT NULL, event TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
            visitor TEXT NOT NULL DEFAULT '', item TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 1,
            bytes INTEGER NOT NULL DEFAULT 0, who TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '',
            device TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS hits_ts ON hits(ts);
        CREATE INDEX IF NOT EXISTS hits_ev ON hits(event, ts);
        CREATE TABLE IF NOT EXISTS traffic (
            minute INTEGER PRIMARY KEY, req INTEGER NOT NULL DEFAULT 0, bytes_out INTEGER NOT NULL DEFAULT 0,
            bytes_in INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0);
    """)
    con.execute("DELETE FROM hits WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
    con.execute("DELETE FROM traffic WHERE minute < ?", (int(time.time() // 60) - KEEP_DAYS * 1440,))
    con.commit(); con.close()
    threading.Thread(target=_writer, daemon=True, name="stats-writer").start()


def connect():
    con = sqlite3.connect(_db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def visitor_id(ip):
    """Short salted hash: enough to count unique people, without keeping raw IPs on disk."""
    return hashlib.blake2b(ip.encode(), key=_salt[:32], digest_size=6).hexdigest()


def device_of(ua):
    ua = (ua or "").lower()
    if any(w in ua for w in ("bot", "crawl", "spider", "whatsapp", "facebookexternalhit", "preview", "curl", "python-")):
        return "bot"
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "iphone" in ua or "mobile" in ua or "android" in ua:
        return "phone"
    return "computer" if ua else "unknown"


def log(kind, request=None, event="", item="", n=1, nbytes=0, who="", detail=""):
    vis = country = dev = ""
    if request is not None:
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
        vis = visitor_id(ip)
        country = (request.headers.get("cf-ipcountry") or "")[:2]
        dev = device_of(request.headers.get("user-agent"))
    _q.put(("hit", (time.time(), event or "", kind, vis, (item or "")[:300], int(n), int(nbytes), who or "",
                    country, dev, (detail or "")[:300])))


def _writer():
    con = connect()
    while True:
        time.sleep(2)
        rows = []
        try:
            while True:
                rows.append(_q.get_nowait())
        except queue.Empty:
            pass
        with _live_lock:  # take this flush's totals; the middleware starts fresh counters
            mins = dict(_minute)
            _minute.clear()
        try:
            hits = [r[1] for r in rows if r[0] == "hit"]
            if hits:
                con.executemany("INSERT INTO hits VALUES (?,?,?,?,?,?,?,?,?,?,?)", hits)
            for m, t in mins.items():
                con.execute("INSERT INTO traffic(minute, req, bytes_out, bytes_in, errors) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(minute) DO UPDATE SET req=req+excluded.req, bytes_out=bytes_out+excluded.bytes_out, "
                            "bytes_in=bytes_in+excluded.bytes_in, errors=errors+excluded.errors",
                            (m, t["req"], t["out"], t["in"], t["err"]))
            con.commit()
        except Exception as e:  # stats must never take the site down
            print("stats write failed:", e, flush=True)


# ---- traffic metering middleware ----
def _classify(path, method):
    p = path.split("/")
    if path.startswith("/e/") and len(p) > 3:
        sub = p[3]
        if sub == "file":
            return "file"
        if sub in ("download.zip",):
            return "zip"
        return sub  # thumb / preview / unlock / beacon
    if path.startswith("/e/"):
        return "page"
    if path.startswith("/u/"):
        return "upload" if path.endswith("/upload") else "page"
    if path.startswith("/admin"):
        return "upload" if path.endswith("/upload") else "admin"
    return "other"


def _add_bytes(dq, sec, n):
    if dq and dq[-1][0] == sec:
        dq[-1][1] += n
    else:
        dq.append([sec, n])


def _minute_rec(m):
    rec = _minute.get(m)
    if rec is None:
        rec = _minute[m] = {"req": 0, "out": 0, "in": 0, "err": 0}
    return rec


class StatsMiddleware:
    """Counts bytes as they actually go out / come in, so a long ZIP or upload shows up live."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        global _next_id
        hdrs = {k: v for k, v in scope.get("headers", [])}
        ip = (hdrs.get(b"cf-connecting-ip") or b"").decode() or (scope.get("client") or ("?",))[0]
        kind = _classify(scope["path"], scope["method"])
        with _live_lock:
            _next_id += 1
            rid = _next_id
            _active[rid] = {"kind": kind, "path": scope["path"], "start": time.time(), "out": 0, "in": 0,
                            "total": 0, "visitor": visitor_id(ip), "status": 0}
        status = 0

        async def rcv():
            msg = await receive()
            if msg["type"] == "http.request":
                n = len(msg.get("body", b""))
                if n:
                    now = int(time.time())
                    with _live_lock:
                        _add_bytes(_sec_in, now, n)
                        _minute_rec(now // 60)["in"] += n
                        a = _active.get(rid)
                        if a: a["in"] += n
            return msg

        async def snd(msg):
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
                with _live_lock:
                    a = _active.get(rid)
                    if a:
                        a["status"] = status
                        for k, v in msg.get("headers", []):
                            if k.lower() == b"content-length":
                                try: a["total"] = int(v)
                                except ValueError: pass
            elif msg["type"] == "http.response.body":
                n = len(msg.get("body", b""))
                if n:
                    now = int(time.time())
                    with _live_lock:
                        _add_bytes(_sec_out, now, n)
                        _minute_rec(now // 60)["out"] += n
                        a = _active.get(rid)
                        if a: a["out"] += n
            await send(msg)

        try:
            await self.app(scope, rcv, snd)
        except Exception:
            status = 500
            raise
        finally:
            m = int(time.time() // 60)
            with _live_lock:
                _active.pop(rid, None)
                rec = _minute_rec(m)
                rec["req"] += 1
                if status >= 500 or (status >= 400 and status not in (401, 403, 404, 410)):
                    rec["err"] += 1
                elif status == 0:
                    rec["err"] += 1  # client hung up / crashed before a response
            if status >= 400 and kind != "admin" and not (status == 404 and scope["path"] in ("/favicon.ico", "/robots.txt")):
                _q.put(("hit", (time.time(), "", "error", visitor_id(ip), scope["path"][:300], 1, 0, "",
                                (hdrs.get(b"cf-ipcountry") or b"").decode()[:2], device_of((hdrs.get(b"user-agent") or b"").decode()),
                                f"{scope['method']} {status}")))


def live():
    """Current transfer rate and what is moving right now."""
    now = time.time()
    sec = int(now)
    with _live_lock:
        out = {s: b for s, b in _sec_out if s > sec - 300}
        inn = {s: b for s, b in _sec_in if s > sec - 300}
        active = [dict(a) for a in _active.values()]
    series = [[s, out.get(s, 0), inn.get(s, 0)] for s in range(sec - 299, sec + 1)]
    # "now" rate = average of the last 5 complete seconds, so it doesn't flicker
    recent = series[-6:-1]
    moving = []
    for a in active:
        age = now - a["start"]
        if a["kind"] in ("file", "zip", "upload") and (age > 1 or a["out"] + a["in"] > 2 << 20):
            moving.append({"kind": a["kind"], "path": a["path"], "secs": round(age), "out": a["out"], "in": a["in"],
                           "total": a["total"], "visitor": a["visitor"],
                           "rate": (a["out"] + a["in"]) / age if age > 0 else 0})
    moving.sort(key=lambda x: -x["secs"])
    return {"now_out": sum(r[1] for r in recent) / 5, "now_in": sum(r[2] for r in recent) / 5,
            "series": series, "moving": moving, "open_requests": len(active)}
