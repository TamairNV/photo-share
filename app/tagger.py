"""Auto-tags photos by running Claude Code headless (`claude -p`) on your own subscription.

A background thread looks for photos with no tags yet (new uploads, rsync'd cards, and everything
already on the drive the first time it runs), copies their small thumbnails into a temp folder and
asks Claude to tag a batch at a time. Tags live in <DATA>/.tags/<event>.json, keyed by file name,
so they are just another cache next to the photos.

If Claude says the usage limit is hit, the worker stops and tries again later (when the limit
resets if Claude says when, otherwise after TAG_LIMIT_WAIT_HOURS). Nothing is lost, the photos
just stay untagged until then.
"""
import json, os, re, shutil, subprocess, tempfile, threading, time
from pathlib import Path

ENABLED = os.environ.get("AUTO_TAG", "1").strip().lower() not in ("0", "false", "no", "off", "")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
MODEL = os.environ.get("TAG_MODEL", "haiku")
BATCH = max(1, int(os.environ.get("TAG_BATCH", "20")))
SCAN_EVERY = 10 * 60          # also picks up files copied in with rsync
SETTLE = 60                   # after an upload, wait this long so a burst of uploads goes in one batch
LIMIT_WAIT = float(os.environ.get("TAG_LIMIT_WAIT_HOURS", "3")) * 3600
ERROR_WAIT = 15 * 60
MAX_FAILS = 3                 # give up on a photo Claude keeps choking on
MAX_TAGS = 8

VOCAB = ("people, group, couple, portrait, selfie, kids, baby, crowd, dancing, costume, makeup, food, drinks, "
         "cake, table, pet, dog, cat, indoors, outdoors, night, sunset, sunrise, sky, landscape, aerial, drone, "
         "beach, sea, lake, river, forest, mountain, snow, city, street, building, house, garden, car, "
         "bike, boat, stage, music, sport, party, decorations, lights, fireworks, fire, pumpkin, candid, "
         "funny, blurry, screenshot, document")

SCHEMA = json.dumps({
    "type": "object",
    "properties": {"photos": {"type": "array", "items": {
        "type": "object",
        "properties": {"file": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
        "required": ["file", "tags"]}}},
    "required": ["photos"]})

SYSTEM = ("You tag photos for a private photo gallery so guests can filter them. "
          "You only read the image files you are given and answer with the requested JSON.")

LIMIT_RE = re.compile(r"usage limit|rate.?limit|limit reached|hit your limit|out of (extra )?usage|"
                      r"too many requests|\b429\b|overloaded|resets? (at|in)\b", re.I)

status = {"state": "off" if not ENABLED else "starting", "msg": "", "until": 0, "last": 0, "tagged": 0}
_wake = threading.Event()
_kick_at = 0.0
_lock = threading.Lock()
_cfg = {}


def init(data_dir, list_events, list_images, thumb_for):
    """list_events() -> [slug]; list_images(slug) -> {name: mtime} (photos only);
    thumb_for(slug, name) -> Path of a small JPEG."""
    _cfg.update(dir=Path(data_dir) / ".tags", events=list_events, images=list_images, thumb=thumb_for)
    _cfg["dir"].mkdir(parents=True, exist_ok=True)
    if ENABLED:
        threading.Thread(target=_run, name="tagger", daemon=True).start()


# ---------- storage ----------
def _file(slug):
    return _cfg["dir"] / f"{slug}.json"


def load(slug):
    """{name: {"t": [tags], "m": mtime, "fail": n}}"""
    try:
        return json.loads(_file(slug).read_text())
    except Exception:
        return {}


def tags_for(slug):
    """{name: [tags]} for the gallery."""
    return {n: e.get("t", []) for n, e in load(slug).items()}


def _save(slug, data):
    tmp = _cfg["dir"] / f".{slug}.tmp"
    tmp.write_text(json.dumps(data))
    tmp.replace(_file(slug))


def forget_event(slug):
    _file(slug).unlink(missing_ok=True)


def pending(slug, data=None, images=None):
    data = load(slug) if data is None else data
    images = _cfg["images"](slug) if images is None else images
    out = []
    for name, mtime in images.items():
        e = data.get(name)
        if not e or e.get("m") != mtime or (not e.get("t") and e.get("fail", 0) and e["fail"] < MAX_FAILS):
            out.append(name)
    return out


def progress(slug):
    """(tagged, total) photos in one event."""
    images = _cfg["images"](slug)
    return len(images) - len(pending(slug, images=images)), len(images)


def kick(now=False):
    """Called after uploads/rescans. Waits SETTLE seconds so a burst ends up in one batch."""
    global _kick_at
    if not ENABLED:
        return
    with _lock:
        _kick_at = time.time() + (0 if now else SETTLE)
        if now:
            status["until"] = 0  # "Tag now" in the admin skips any limit/error wait
    _wake.set()


# ---------- tag text clean-up ----------
def clean(tags):
    out = []
    for t in tags or []:
        t = re.sub(r"[^a-z0-9 &'-]+", "", str(t).lower()).strip(" -'")
        t = re.sub(r"\s+", " ", t)[:24]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


# ---------- calling Claude ----------
class LimitHit(Exception):
    def __init__(self, until):
        self.until = until


def _limit_until(text):
    m = re.search(r"\|(\d{10})\b", text)  # "Claude AI usage limit reached|1730000000"
    if m and int(m.group(1)) > time.time():
        return min(int(m.group(1)) + 120, time.time() + 24 * 3600)
    return time.time() + LIMIT_WAIT


def _ask_claude(files, known_tags):
    """files: [(label, path)]. Returns {label: [tags]}."""
    with tempfile.TemporaryDirectory(prefix="tag-") as tmp:
        for label, p in files:
            shutil.copyfile(p, Path(tmp) / label)
        listing = "\n".join(label for label, _ in files)
        known = ", ".join(known_tags[:80]) or "(none yet)"
        prompt = (
            f"Tag each of these {len(files)} photos. They are JPEG files in the current folder; "
            "read every one with the Read tool (you can read several at once).\n\n"
            f"{listing}\n\n"
            f"Give each photo 3 to {MAX_TAGS - 2} short lowercase tags (single words or short phrases, singular "
            "nouns where possible) describing what a guest might search for: who/what is in it, the setting, "
            "the activity, time of day. Prefer tags already used in this album so filters stay consistent: "
            f"{known}.\nOther good tags: {VOCAB}.\n"
            "Use 'people' whenever there is a person, plus 'group' (3+ people), 'selfie', 'kids' etc. as fitting. "
            "Don't name or guess who people are. Return every file name exactly as given.")
        cmd = [CLAUDE_BIN, "-p", prompt, "--model", MODEL, "--output-format", "json", "--json-schema", SCHEMA,
               "--tools", "Read", "--allowedTools", "Read", "--system-prompt", SYSTEM,
               "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config", "--max-turns", "12"]
        env = {**os.environ, "DISABLE_AUTOUPDATER": "1"}
        if not os.access(env.get("HOME") or "/", os.W_OK):
            env["HOME"] = "/tmp"  # Claude Code keeps its settings in $HOME; the container user has none
        try:
            r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=900, env=env)
        except FileNotFoundError:
            raise RuntimeError("Claude Code isn't installed in the container")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Claude took too long (15 min)")
    out, err = r.stdout.strip(), r.stderr.strip()
    try:
        res = json.loads(out)
    except Exception:
        res = None
    text = (res or {}).get("result") if isinstance(res, dict) else None
    blob = f"{out}\n{err}"
    if r.returncode != 0 or not isinstance(res, dict) or res.get("is_error"):
        if LIMIT_RE.search(blob):
            raise LimitHit(_limit_until(blob))
        if re.search(r"log ?in|logged in|auth|credential|oauth|api key|401", blob, re.I):
            raise RuntimeError("Claude Code isn't logged in. Set CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token`)")
        raise RuntimeError((text or err or out or f"claude exited {r.returncode}")[:300])
    data = res.get("structured_output")
    if not isinstance(data, dict) and text:
        m = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except Exception:
            data = None
    if not isinstance(data, dict) or not isinstance(data.get("photos"), list):
        raise RuntimeError("Claude's answer wasn't the expected JSON")
    got = {}
    for ph in data["photos"]:
        if isinstance(ph, dict) and isinstance(ph.get("file"), str):
            got[ph["file"].strip().split("/")[-1]] = clean(ph.get("tags"))
    return got


# ---------- worker ----------
def _tag_batch(slug, names, images):
    data = load(slug)
    counts = {}
    for e in data.values():
        for t in e.get("t", []):
            counts[t] = counts.get(t, 0) + 1
    known = sorted(counts, key=lambda t: -counts[t])
    files, labels = [], {}
    for k, name in enumerate(names):
        try:
            p = _cfg["thumb"](slug, name)
        except Exception:
            p = None
        if not p:
            e = data.setdefault(name, {})
            e.update(t=[], m=images[name], fail=MAX_FAILS)  # unreadable image, don't keep trying
            continue
        label = f"{k + 1:03d}.jpg"
        labels[label] = name
        files.append((label, p))
    if files:
        got = _ask_claude(files, known)  # errors bubble up; the photos stay pending for next time
        for label, name in labels.items():
            tags = got.get(label)
            if tags:
                data[name] = {"t": tags, "m": images[name]}
                status["tagged"] += 1
            else:
                e = data.setdefault(name, {"t": []})
                e.update(m=images[name], fail=e.get("fail", 0) + 1)
    _save(slug, data)


def _sweep():
    """Tag everything that needs it. Returns when done; raises LimitHit / other errors."""
    work = []
    for slug in _cfg["events"]():
        images = _cfg["images"](slug)
        data = load(slug)
        gone = [n for n in data if n not in images]
        if gone:
            for n in gone:
                del data[n]
            _save(slug, data)
        todo = pending(slug, data, images)
        if todo:
            work.append((slug, images, todo))
    left = sum(len(t) for _, _, t in work)
    for slug, images, todo in work:
        for i in range(0, len(todo), BATCH):
            status.update(state="working", msg=f"Tagging, {left} photo{'s' if left != 1 else ''} to go")
            _tag_batch(slug, todo[i:i + BATCH], images)
            left -= len(todo[i:i + BATCH])


def _run():
    global _kick_at
    if not shutil.which(CLAUDE_BIN):
        status.update(state="error", msg="Claude Code isn't installed in the container")
        return
    next_scan = 0.0  # backfill everything already on the drive right away
    while True:
        now = time.time()
        due = min(next_scan, _kick_at) if _kick_at else next_scan
        if status["until"] > now:
            due = max(due, status["until"])  # waiting out a usage limit or an error
        if due > now:
            _wake.wait(timeout=min(due - now, 300))
            _wake.clear()
            continue
        with _lock:
            _kick_at = 0
            status["until"] = 0
        try:
            _sweep()
            status.update(state="idle", msg="All photos tagged", last=time.time())
            next_scan = time.time() + SCAN_EVERY
        except LimitHit as e:
            status.update(state="waiting", until=e.until,
                          msg="Hit the Claude usage limit, carrying on at " + time.strftime("%H:%M", time.localtime(e.until)))
            next_scan = e.until
        except Exception as e:
            status.update(state="error", msg=str(e)[:300], until=time.time() + ERROR_WAIT)
            next_scan = time.time() + ERROR_WAIT
            print(f"tagger: {e}", flush=True)
