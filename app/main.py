import hashlib, io, json, os, re, secrets, shutil, threading, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from pathlib import Path
from urllib.parse import quote

import jwt
import segno
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

DATA = Path(os.environ.get("DATA_DIR", "/data"))
EVENTS_DIR = DATA / "events"          # one plain folder per event, on your drive
CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(DATA / ".cache")))
DB_FILE = DATA / "events.json"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
SITE_NAME = os.environ.get("SITE_NAME", "Photos")
OWNER_NAME = os.environ.get("OWNER_NAME", "Owner")  # label for photos you add yourself (admin upload, rsync)
UPLOADER_COLOURS = ["#ff6b6b", "#4dabf7", "#51cf66", "#fcc419", "#cc5de8", "#ff922b", "#22b8cf", "#f06595", "#94d82d", "#845ef7"]
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
HOST_PHOTOS_PATH = os.environ.get("PHOTOS_PATH") or str(DATA)
SSH_TARGET = os.environ.get("SSH_TARGET", "")
# AUTH_MODE=password (built-in login page) or cloudflare (trust Cloudflare Access in front of /admin)
AUTH_MODE = os.environ.get("AUTH_MODE", "password").strip().lower()
CF_TEAM_DOMAIN = os.environ.get("CF_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/")  # e.g. myteam.cloudflareaccess.com
CF_AUD = os.environ.get("CF_AUD", "").strip()  # Application Audience (AUD) tag of the Access app
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
_jwks = jwt.PyJWKClient(f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600) \
    if AUTH_MODE == "cloudflare" and CF_TEAM_DOMAIN else None
if AUTH_MODE == "cloudflare" and not (CF_TEAM_DOMAIN and CF_AUD):
    print("WARNING: AUTH_MODE=cloudflare needs CF_TEAM_DOMAIN and CF_AUD set, admin will be locked until then", flush=True)  # e.g. tnv@192.168.1.199, used for the rsync command
UPLOADS_TMP = DATA / ".uploads"
CHUNK_SIZE = 50 * 1024 * 1024  # Cloudflare caps each request at 100MB, so big files go in 50MB pieces

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
VID_EXT = {".mp4", ".mov", ".webm", ".m4v"}
MEDIA_EXT = IMG_EXT | VID_EXT
SIZES = {"thumb": 480, "preview": 1920}
SORTS = {"taken": "Date taken", "uploaded": "Date added", "name": "File name"}

for d in (EVENTS_DIR, CACHE_DIR, UPLOADS_TMP):
    d.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()  # re-entrant: helpers like sync_folders() also take it
_meta_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2)
_fails: dict[str, list[float]] = {}

DEFAULTS = {"public": True, "password": "", "expires": "", "cover": "", "description": "",
            "date": "", "uploaders": {}, "sort": "taken", "views": 0, "downloads": 0}


# ---------- storage ----------
def load_db():
    db = json.loads(DB_FILE.read_text()) if DB_FILE.exists() else {"events": {}}
    for ev in db["events"].values():
        for k, v in DEFAULTS.items():
            ev.setdefault(k, v)
    return db


def save_db(db):
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2))
    tmp.replace(DB_FILE)


def new_event(name):
    return {"name": name, "token": secrets.token_urlsafe(12), "created": time.time(), **DEFAULTS}


def sync_folders():
    """Folders created by hand on the drive become events automatically."""
    with _lock:
        db = load_db()
        changed = False
        for p in EVENTS_DIR.iterdir():
            if p.is_dir() and not p.name.startswith(".") and p.name not in db["events"]:
                db["events"][p.name] = new_event(p.name)
                changed = True
        if changed:
            save_db(db)
        return db


def update_event(slug, fn):
    with _lock:
        db = load_db()
        if slug in db["events"]:
            fn(db["events"][slug])
            save_db(db)


def bump(slug, field):
    update_event(slug, lambda ev: ev.__setitem__(field, ev.get(field, 0) + 1))


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or "event"


def hash_pw(pw):
    salt = secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()}"


def check_pw(pw, stored):
    salt, h = stored.split("$", 1)
    return secrets.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)


def rate_limited(key, limit=10, window=600):
    now = time.time()
    hits = [t for t in _fails.get(key, []) if now - t < window]
    _fails[key] = hits
    return len(hits) >= limit


def note_fail(key):
    _fails.setdefault(key, []).append(time.time())


def client_ip(request):
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def safe_name(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip().replace("\x00", "")
    if not name or name.startswith("."):
        raise HTTPException(400, "bad filename")
    return name


def get_file(slug, name):
    """name is a path relative to the event folder (subfolders allowed, e.g. 100CANON/IMG_1.JPG)."""
    root = (EVENTS_DIR / slug).resolve()
    rel = Path((name or "").replace("\\", "/"))
    if rel.is_absolute() or any(part.startswith(".") for part in rel.parts) or not rel.parts:
        raise HTTPException(404)
    p = (root / rel).resolve()
    if root not in p.parents or not p.is_file() or p.suffix.lower() not in MEDIA_EXT:
        raise HTTPException(404)
    return p


def rel_name(slug, p):
    return p.relative_to((EVENTS_DIR / slug).resolve()).as_posix()


def cache_path(slug, size, name):
    return CACHE_DIR / slug / size / (quote(name, safe="") + ".jpg")


# ---------- photo metadata (date taken etc.), cached per event ----------
def _video_taken(p):
    """Read creation time from the mvhd atom of an mp4/mov (seconds since 1904, UTC)."""
    import struct
    try:
        with open(p, "rb") as f:
            end = p.stat().st_size

            def find(start, stop, want):
                pos = start
                while pos + 8 <= stop:
                    f.seek(pos)
                    size, kind = struct.unpack(">I4s", f.read(8))
                    hdr = 8
                    if size == 1:
                        size = struct.unpack(">Q", f.read(8))[0]; hdr = 16
                    elif size == 0:
                        size = stop - pos
                    if size < hdr:
                        return None
                    if kind == want:
                        return pos + hdr, pos + size
                    pos += size
                return None

            moov = find(0, end, b"moov")
            if not moov:
                return None
            mvhd = find(moov[0], moov[1], b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            version = f.read(1)[0]
            f.read(3)
            secs = struct.unpack(">Q", f.read(8))[0] if version == 1 else struct.unpack(">I", f.read(4))[0]
            ts = secs - 2082844800
            return ts if 946684800 < ts < time.time() + 86400 else None  # ignore unset/garbage dates
    except Exception:
        return None


def _exif_taken(p):
    if p.suffix.lower() in VID_EXT:
        return _video_taken(p)
    try:
        with Image.open(p) as im:
            ex = im.getexif()
            raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)  # DateTimeOriginal, DateTime
            if raw:
                return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return None


def media_files(slug, sort="taken"):
    folder = EVENTS_DIR / slug
    if not folder.is_dir():
        return []
    root = folder.resolve()
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.startswith(".") and os.path.splitext(fn)[1].lower() in MEDIA_EXT:
                p = Path(dirpath) / fn
                files[p.relative_to(root).as_posix()] = p
    mfile = CACHE_DIR / slug / "meta.json"
    with _meta_lock:
        try:
            meta = json.loads(mfile.read_text())
        except Exception:
            meta = {}
        fresh, dirty = {}, False
        for name, p in files.items():
            st = p.stat()
            m = meta.get(name)
            if not m or m["mtime"] != st.st_mtime or m["size"] != st.st_size or m.get("v") != 2:
                m = {"mtime": st.st_mtime, "size": st.st_size, "taken": _exif_taken(p), "v": 2}
                dirty = True
            fresh[name] = m
        if dirty or len(fresh) != len(meta):
            mfile.parent.mkdir(parents=True, exist_ok=True)
            mfile.write_text(json.dumps(fresh))
    sources = load_sources(slug)
    items = []
    for name, p in files.items():
        m = fresh[name]
        items.append({"name": name, "path": p, "video": p.suffix.lower() in VID_EXT, "size": m["size"],
                      "mtime": m["mtime"], "taken": m["taken"] or m["mtime"], "exact": bool(m["taken"]),
                      "by": sources.get(name, "")})
    key = {"taken": lambda i: (i["taken"], i["name"]), "uploaded": lambda i: (i["mtime"], i["name"]),
           "name": lambda i: i["name"].lower()}.get(sort, lambda i: i["taken"])
    return sorted(items, key=key)


def resized(slug, name, size):
    src = get_file(slug, name)
    if src.suffix.lower() in VID_EXT:
        raise HTTPException(404)
    out = cache_path(slug, size, name)
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((SIZES[size], SIZES[size]))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            tmp = out.with_name(out.name + f".{secrets.token_hex(4)}.tmp")
            im.save(tmp, "JPEG", quality=82, optimize=True)
            tmp.replace(out)
    return out


def pregenerate(slug, name):
    for size in SIZES:
        try:
            resized(slug, name, size)
        except Exception:
            pass


def drop_cache(slug, name):
    for size in SIZES:
        cache_path(slug, size, name).unlink(missing_ok=True)


# ---------- who uploaded what ----------
SOURCES_DIR = DATA / ".sources"
SOURCES_DIR.mkdir(parents=True, exist_ok=True)


def load_sources(slug):
    try:
        return json.loads((SOURCES_DIR / f"{slug}.json").read_text())
    except Exception:
        return {}


def set_source(slug, name, uploader_id):
    with _lock:
        src = load_sources(slug)
        if uploader_id:
            src[name] = uploader_id
        else:
            src.pop(name, None)
        tmp = SOURCES_DIR / f".{slug}.tmp"
        tmp.write_text(json.dumps(src))
        tmp.replace(SOURCES_DIR / f"{slug}.json")


def uploader_legend(ev, items):
    """[{id, name, colour, count}] for everyone who has photos here (plus active uploaders)."""
    counts = {}
    for i in items:
        counts[i["by"]] = counts.get(i["by"], 0) + 1
    out = []
    if counts.get(""):
        out.append({"id": "", "name": OWNER_NAME, "colour": "#9a9aa0", "count": counts[""]})
    for uid, u in ev.get("uploaders", {}).items():
        if counts.get(uid) or not u.get("revoked"):
            out.append({"id": uid, "name": u["name"], "colour": u["colour"], "count": counts.get(uid, 0)})
    return out


def find_uploader(utoken):
    db = sync_folders()
    for slug, ev in db["events"].items():
        for uid, u in ev.get("uploaders", {}).items():
            if not u.get("revoked") and secrets.compare_digest(u["token"], utoken):
                return slug, ev, uid, u
    raise HTTPException(404, "Upload link not found or turned off")


def receive_chunk(slug, upload_id, filename, offset, total, chunk, uploader_id=""):
    """Append one piece of a file. Every upload goes through here; small files are just one piece."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", upload_id):
        raise HTTPException(400, "bad upload id")
    name = safe_name(filename)
    if Path(name).suffix.lower() not in MEDIA_EXT:
        raise HTTPException(415, "not a photo or video")
    tmp = UPLOADS_TMP / f"{slug}--{upload_id}"
    have = tmp.stat().st_size if tmp.exists() else 0
    if offset == 0:
        have = 0
    elif offset != have:
        return {"status": "resume", "offset": have}  # client resends from here
    with open(tmp, "wb" if offset == 0 else "ab") as out:
        shutil.copyfileobj(chunk, out, 1 << 20)
    have = tmp.stat().st_size
    if have < total:
        return {"status": "partial", "offset": have}
    dest = EVENTS_DIR / slug / name
    stem, ext, i = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = EVENTS_DIR / slug / f"{stem}_{i}{ext}"; i += 1
    shutil.move(str(tmp), dest)
    if uploader_id:
        set_source(slug, dest.name, uploader_id)
    _pool.submit(pregenerate, slug, dest.name)
    return {"status": "done", "saved": dest.name}


def clean_stale_uploads():
    for p in UPLOADS_TMP.iterdir():
        if time.time() - p.stat().st_mtime > 86400:
            p.unlink(missing_ok=True)


# ---------- zip streaming ----------
class _ZipStream(io.RawIOBase):
    def __init__(self):
        self.buf = bytearray()
    def writable(self):
        return True
    def write(self, b):
        self.buf += b
        return len(b)
    def pop(self):
        b = bytes(self.buf); self.buf.clear(); return b


def zip_stream(files):
    """files: list of (arcname, path). Streamed, so no temp file and no size limit."""
    s = _ZipStream()
    with zipfile.ZipFile(s, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for arc, f in files:
            with z.open(zipfile.ZipInfo.from_file(f, arc), "w", force_zip64=True) as dst, open(f, "rb") as src:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    yield s.pop()
            yield s.pop()
    yield s.pop()


def zip_response(files, name):
    return StreamingResponse(zip_stream(files), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------- app ----------
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 24 * 30,
                   https_only=BASE_URL.startswith("https"), same_site="lax")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["site_name"] = SITE_NAME
templates.env.filters["q"] = lambda s: quote(s, safe="")


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_date(s):
    try:
        return date.fromisoformat(s).strftime("%-d %B %Y")
    except Exception:
        return s


templates.env.filters["size"] = fmt_size
templates.env.filters["nicedate"] = fmt_date


def share_url(request, token):
    return f"{BASE_URL or str(request.base_url).rstrip('/')}/e/{token}"


def verify_cf_access(request):
    """Check the signed JWT Cloudflare Access adds to every request it lets through. Returns the email or None."""
    token = request.headers.get("cf-access-jwt-assertion")
    if not token or not _jwks or not CF_AUD:
        return None
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=CF_AUD,
                            issuer=f"https://{CF_TEAM_DOMAIN}", leeway=30)
    except Exception:
        return None
    email = (claims.get("email") or "").lower()
    if ADMIN_EMAILS and email not in ADMIN_EMAILS:
        return None
    return email or claims.get("sub")


def is_admin(request):
    if AUTH_MODE == "cloudflare":
        who = verify_cf_access(request)
        if who:
            request.session["admin"] = who  # lets the admin preview locked/expired guest pages
            return True
        return False
    return bool(request.session.get("admin"))


def require_admin(request):
    if is_admin(request):
        return
    if AUTH_MODE == "cloudflare":
        raise HTTPException(403, "Admin is protected by Cloudflare Access. Open it via https://" +
                            (BASE_URL.split("://")[-1] or "your domain") + "/admin")
    raise HTTPException(303, headers={"Location": "/admin/login"})


def check_csrf(request, token):
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "bad csrf token")


def ctx(request, **kw):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    return {"request": request, "csrf": request.session["csrf"], **kw}


def expired(ev):
    if not ev.get("expires"):
        return False
    try:
        return date.today() > date.fromisoformat(ev["expires"])
    except ValueError:
        return False


def find_event(token):
    db = sync_folders()
    for slug, ev in db["events"].items():
        if secrets.compare_digest(ev["token"], token):
            return slug, ev
    raise HTTPException(404, "Link not found")


def unlocked(request, ev):
    return not ev.get("password") or request.session.get("unlock:" + ev["token"]) == ev["password"][:16]


def has_uploader_access(request, ev):
    uid = request.session.get("up:" + ev["token"])
    u = ev.get("uploaders", {}).get(uid) if uid else None
    return bool(u and not u.get("revoked"))


def gallery_items(slug, ev):
    items = []
    for i in media_files(slug, ev["sort"]):
        t = datetime.fromtimestamp(i["taken"])
        items.append({"name": i["name"], "video": i["video"], "by": i["by"], "day": t.strftime("%Y-%m-%d"),
                      "dayLabel": t.strftime("%A %-d %B %Y"), "time": t.strftime("%H:%M"),
                      "when": t.strftime("%-d %b %Y, %H:%M")})
    cover = ev["cover"] if any(i["name"] == ev["cover"] and not i["video"] for i in items) else \
        next((i["name"] for i in items if not i["video"]), "")
    return items, cover


def guest_event(request, token):
    """For file/zip endpoints: event must be live and unlocked (admins always get in)."""
    slug, ev = find_event(token)
    if request.session.get("admin") or has_uploader_access(request, ev):
        return slug, ev
    if not ev["public"] or expired(ev):
        raise HTTPException(404)
    if not unlocked(request, ev):
        raise HTTPException(403)
    return slug, ev


# ---------- public ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", ctx(request))


@app.get("/e/{token}", response_class=HTMLResponse)
def event_page(request: Request, token: str):
    slug, ev = find_event(token)
    is_admin = bool(request.session.get("admin"))
    if not is_admin and (not ev["public"] or expired(ev)):
        return templates.TemplateResponse(request, "gone.html", ctx(request, ev=ev, expired=expired(ev)), status_code=410)
    if not is_admin and not unlocked(request, ev):
        return templates.TemplateResponse(request, "unlock.html", ctx(request, ev=ev, error=None))
    seen = request.session.setdefault("seen", [])
    if not is_admin and token not in seen:
        request.session["seen"] = (seen + [token])[-50:]
        bump(slug, "views")
    items, cover = gallery_items(slug, ev)
    return templates.TemplateResponse(request, "event.html", ctx(
        request, ev=ev, token=token, items=items, cover=cover, legend=uploader_legend(ev, items), uploader=None))


@app.post("/e/{token}/unlock", response_class=HTMLResponse)
def unlock(request: Request, token: str, password: str = Form(...)):
    slug, ev = find_event(token)
    key = "ev:" + client_ip(request)
    if rate_limited(key):
        return templates.TemplateResponse(request, "unlock.html", ctx(request, ev=ev, error="Too many tries, wait a few minutes"), status_code=429)
    if ev.get("password") and check_pw(password, ev["password"]):
        request.session["unlock:" + token] = ev["password"][:16]
        return RedirectResponse(f"/e/{token}", 303)
    note_fail(key)
    return templates.TemplateResponse(request, "unlock.html", ctx(request, ev=ev, error="Wrong password"), status_code=401)


@app.get("/e/{token}/thumb/{name:path}")
def thumb(request: Request, token: str, name: str):
    slug, _ = guest_event(request, token)
    return FileResponse(resized(slug, name, "thumb"), headers={"Cache-Control": "private, max-age=86400"})


@app.get("/e/{token}/preview/{name:path}")
def preview(request: Request, token: str, name: str):
    slug, _ = guest_event(request, token)
    return FileResponse(resized(slug, name, "preview"), headers={"Cache-Control": "private, max-age=86400"})


@app.get("/e/{token}/file/{name:path}")
def original(request: Request, token: str, name: str, dl: int = 0):
    slug, _ = guest_event(request, token)
    p = get_file(slug, name)
    if dl and not request.session.get("admin"):
        bump(slug, "downloads")
    return FileResponse(p, filename=p.name if dl else None,
                        content_disposition_type="attachment" if dl else "inline")


@app.get("/e/{token}/download.zip")
def download_all(request: Request, token: str):
    slug, ev = guest_event(request, token)
    if not request.session.get("admin"):
        bump(slug, "downloads")
    return zip_response([(i["name"], i["path"]) for i in media_files(slug, ev["sort"])], slugify(ev["name"]) + ".zip")


@app.post("/e/{token}/download.zip")
def download_selected(request: Request, token: str, names: list[str] = Form(...)):
    slug, ev = guest_event(request, token)
    files = [(n, get_file(slug, n)) for n in dict.fromkeys(names)]
    if not request.session.get("admin"):
        bump(slug, "downloads")
    return zip_response(files, slugify(ev["name"]) + f"-{len(files)}-photos.zip")


@app.get("/u/{utoken}", response_class=HTMLResponse)
def uploader_page(request: Request, utoken: str):
    """Secret upload link for one named person. Works even if the public link is off or password protected."""
    slug, ev, uid, u = find_uploader(utoken)
    request.session["up:" + ev["token"]] = uid
    items, cover = gallery_items(slug, ev)
    return templates.TemplateResponse(request, "event.html", ctx(
        request, ev=ev, token=ev["token"], items=items, cover=cover, legend=uploader_legend(ev, items),
        uploader={**u, "id": uid, "utoken": utoken}, chunk_size=CHUNK_SIZE))


@app.post("/u/{utoken}/upload")
def uploader_upload(request: Request, utoken: str, chunk: UploadFile = File(...), upload_id: str = Form(...),
                    filename: str = Form(...), offset: int = Form(...), total: int = Form(...), csrf: str = Form("")):
    slug, ev, uid, u = find_uploader(utoken)
    check_csrf(request, csrf)
    return receive_chunk(slug, upload_id, filename, offset, total, chunk.file, uploader_id=uid)


# ---------- admin ----------
@app.get("/admin/events/{slug}/qr.svg")
def qr(request: Request, slug: str):
    require_admin(request)
    _, ev = get_event(slug)
    buf = io.BytesIO()
    segno.make(share_url(request, ev["token"]), error="m").save(buf, kind="svg", scale=8, border=2)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.get("/admin/login", response_class=HTMLResponse)
def login_form(request: Request):
    if AUTH_MODE == "cloudflare":
        return RedirectResponse("/admin", 303)
    return templates.TemplateResponse(request, "login.html", ctx(request, error=None, configured=bool(ADMIN_PASSWORD)))


@app.post("/admin/login")
def login(request: Request, password: str = Form(...)):
    if AUTH_MODE == "cloudflare":
        raise HTTPException(404)
    key = "admin:" + client_ip(request)
    if rate_limited(key, limit=8):
        return templates.TemplateResponse(request, "login.html", ctx(request, error="Too many tries, wait a few minutes", configured=True), status_code=429)
    if ADMIN_PASSWORD and secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        request.session["admin"] = True
        return RedirectResponse("/admin", 303)
    note_fail(key)
    time.sleep(1)
    return templates.TemplateResponse(request, "login.html", ctx(request, error="Wrong password", configured=bool(ADMIN_PASSWORD)), status_code=401)


@app.post("/admin/logout")
def logout(request: Request):
    request.session.clear()
    if AUTH_MODE == "cloudflare":
        return RedirectResponse("/cdn-cgi/access/logout", 303)
    return RedirectResponse("/admin/login", 303)


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    require_admin(request)
    db = sync_folders()
    events = []
    for slug, ev in db["events"].items():
        items = media_files(slug, ev["sort"])
        imgs = [i["name"] for i in items if not i["video"]]
        cover = ev["cover"] if ev["cover"] in imgs else (imgs[0] if imgs else "")
        events.append({"slug": slug, **ev, "count": len(items), "bytes": sum(i["size"] for i in items),
                       "cover_name": cover, "expired": expired(ev), "url": share_url(request, ev["token"])})
    events.sort(key=lambda e: (e["date"] or datetime.fromtimestamp(e["created"]).date().isoformat()), reverse=True)
    return templates.TemplateResponse(request, "admin.html", ctx(request, events=events))


@app.post("/admin/events")
def create_event(request: Request, name: str = Form(...), csrf: str = Form("")):
    require_admin(request); check_csrf(request, csrf)
    with _lock:
        db = load_db()
        base = slugify(name); slug = base; i = 2
        while slug in db["events"] or (EVENTS_DIR / slug).exists():
            slug = f"{base}-{i}"; i += 1
        (EVENTS_DIR / slug).mkdir(parents=True)
        db["events"][slug] = new_event(name.strip() or slug)
        db["events"][slug]["date"] = date.today().isoformat()
        save_db(db)
    return RedirectResponse(f"/admin/events/{slug}", 303)


def get_event(slug):
    db = sync_folders()
    if slug not in db["events"]:
        raise HTTPException(404)
    return db, db["events"][slug]


@app.get("/admin/events/{slug}", response_class=HTMLResponse)
def admin_event(request: Request, slug: str):
    require_admin(request)
    _, ev = get_event(slug)
    items = media_files(slug, ev["sort"])
    base = BASE_URL or str(request.base_url).rstrip("/")
    uploaders = [{"id": uid, **u, "url": f"{base}/u/{u['token']}", "count": sum(1 for i in items if i["by"] == uid)}
                 for uid, u in ev.get("uploaders", {}).items() if not u.get("revoked")]
    return templates.TemplateResponse(request, "admin_event.html", ctx(
        request, slug=slug, ev=ev, items=items, url=share_url(request, ev["token"]), sorts=SORTS,
        legend=uploader_legend(ev, items), uploaders=uploaders,
        colours={uid: u["colour"] for uid, u in ev.get("uploaders", {}).items()},
        total=sum(i["size"] for i in items), expired=expired(ev),
        folder=f"{HOST_PHOTOS_PATH.rstrip('/')}/events/{slug}", ssh_target=SSH_TARGET,
        chunk_size=CHUNK_SIZE, missing_thumbs=sum(1 for i in items if not i["video"] and not cache_path(slug, "thumb", i["name"]).exists()),
        rescanned=request.query_params.get("rescanned"), saved=request.query_params.get("saved")))


@app.post("/admin/events/{slug}/upload")
def upload(request: Request, slug: str, chunk: UploadFile = File(...), upload_id: str = Form(...),
           filename: str = Form(...), offset: int = Form(...), total: int = Form(...), csrf: str = Form("")):
    require_admin(request); check_csrf(request, csrf)
    get_event(slug)
    return receive_chunk(slug, upload_id, filename, offset, total, chunk.file)


@app.post("/admin/events/{slug}/rescan")
def rescan(request: Request, slug: str, csrf: str = Form("")):
    """After an rsync/manual copy: re-read the folder and build thumbnails in the background."""
    require_admin(request); check_csrf(request, csrf)
    _, ev = get_event(slug)
    items = media_files(slug, ev["sort"])
    todo = [i["name"] for i in items if not i["video"] and not cache_path(slug, "thumb", i["name"]).exists()]
    for n in todo:
        _pool.submit(pregenerate, slug, n)
    clean_stale_uploads()
    return RedirectResponse(f"/admin/events/{slug}?rescanned={len(todo)}", 303)


@app.post("/admin/events/{slug}/uploaders")
def add_uploader(request: Request, slug: str, name: str = Form(...), csrf: str = Form("")):
    require_admin(request); check_csrf(request, csrf)
    get_event(slug)

    def apply(ev):
        ups = ev.setdefault("uploaders", {})
        used = {u["colour"] for u in ups.values()}
        colour = next((c for c in UPLOADER_COLOURS if c not in used), UPLOADER_COLOURS[len(ups) % len(UPLOADER_COLOURS)])
        ups[secrets.token_hex(4)] = {"name": name.strip()[:40] or "Uploader", "colour": colour,
                                     "token": secrets.token_urlsafe(12), "created": time.time(), "revoked": False}
    update_event(slug, apply)
    return RedirectResponse(f"/admin/events/{slug}#uploaders", 303)


@app.post("/admin/events/{slug}/uploaders/{uid}/revoke")
def revoke_uploader(request: Request, slug: str, uid: str, csrf: str = Form("")):
    """Turns the link off. Their photos stay, still tagged with their name and colour."""
    require_admin(request); check_csrf(request, csrf)
    get_event(slug)
    update_event(slug, lambda ev: ev.get("uploaders", {}).get(uid, {}).__setitem__("revoked", True)
                 if uid in ev.get("uploaders", {}) else None)
    return RedirectResponse(f"/admin/events/{slug}#uploaders", 303)


@app.post("/admin/events/{slug}/delete-photos")
def delete_photos(request: Request, slug: str, names: list[str] = Form(...), csrf: str = Form("")):
    require_admin(request); check_csrf(request, csrf)
    for n in names:
        try:
            p = get_file(slug, n)
        except HTTPException:
            continue
        p.unlink()
        drop_cache(slug, n)
        set_source(slug, n, "")
    return RedirectResponse(f"/admin/events/{slug}", 303)


@app.post("/admin/events/{slug}/cover")
def set_cover(request: Request, slug: str, name: str = Form(...), csrf: str = Form("")):
    require_admin(request); check_csrf(request, csrf)
    get_file(slug, name)
    update_event(slug, lambda ev: ev.__setitem__("cover", name))
    return RedirectResponse(f"/admin/events/{slug}", 303)


@app.post("/admin/events/{slug}/settings")
def event_settings(request: Request, slug: str, name: str = Form(...), description: str = Form(""),
                   event_date: str = Form(""), expires: str = Form(""), sort: str = Form("taken"),
                   public: str = Form(""), password: str = Form(""),
                   clear_password: str = Form(""), regen: str = Form(""), csrf: str = Form("")):
    require_admin(request); check_csrf(request, csrf)

    def apply(ev):
        ev["name"] = name.strip() or ev["name"]
        ev["description"] = description.strip()[:2000]
        ev["date"] = event_date
        ev["expires"] = expires
        ev["sort"] = sort if sort in SORTS else "taken"
        ev["public"] = bool(public)
        if clear_password:
            ev["password"] = ""
        elif password:
            ev["password"] = hash_pw(password)
        if regen:
            ev["token"] = secrets.token_urlsafe(12)
    get_event(slug)
    update_event(slug, apply)
    return RedirectResponse(f"/admin/events/{slug}?saved=1", 303)


@app.post("/admin/events/{slug}/delete")
def delete_event(request: Request, slug: str, confirm: str = Form(""), csrf: str = Form("")):
    require_admin(request); check_csrf(request, csrf)
    if confirm != slug:
        raise HTTPException(400, "type the event id to confirm")
    with _lock:
        db = load_db()
        if slug not in db["events"]:
            raise HTTPException(404)
        shutil.rmtree(EVENTS_DIR / slug, ignore_errors=True)
        shutil.rmtree(CACHE_DIR / slug, ignore_errors=True)
        (SOURCES_DIR / f"{slug}.json").unlink(missing_ok=True)
        del db["events"][slug]
        save_db(db)
    return RedirectResponse("/admin", 303)


@app.get("/admin/events/{slug}/thumb/{name:path}")
def admin_thumb(request: Request, slug: str, name: str):
    require_admin(request)
    return FileResponse(resized(slug, name, "thumb"))


@app.get("/healthz")
def health():
    return {"ok": True}
