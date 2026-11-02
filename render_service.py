#!/usr/bin/env python3
"""Card / video render service for the LinkedIn agent. Localhost-only.
 - POST /render  {draft_id, html, draft_json} -> renders PNG, persists <id>.png/.json, returns PNG bytes
 - GET  /draft?id=   -> stored post JSON
 - GET  /card?id=    -> stored PNG
 - GET  /latest      -> newest draft JSON (+ id)
 - POST /claim   {token, ttl}   -> single-listener lock (atomic). {"ok": true/false}
 - POST /posted  {draft_id}     -> idempotency gate. {"first": true} first time, else {"first": false}
 - GET  /health
"""
import glob, math, sys, html as _html, json, os, re, subprocess, time, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

def _load_config():
    """Optional: the same config.env build_wf.py reads, if it sits next to this file.
    Values already in the environment (e.g. from the systemd unit) win."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_config()

def _default_handle():
    h = os.environ.get("LINKEDIN_HANDLE", "").strip().strip("/")
    if h.startswith("in/"):
        h = h[3:]
    return "in/" + (h or "your-handle")
# Watermark badge when a caller sends no handle. n8n always sends the configured one
# (LINKEDIN_HANDLE in config.env), so this is only the fallback.
DEFAULT_HANDLE = _default_handle()

REAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
TMP = "/tmp"
PREFIX = "liagent_draft_"
PRUNE_AGE = 172800   # delete draft media (html/png/json) older than 48h
CHROME = ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
          "--force-device-scale-factor=1", "--window-size=1080,1080",
          "--virtual-time-budget=8000", "--user-data-dir=/tmp/cr-svc"]
def _probe_height(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=height", "-of", "csv=p=0", path],
                           capture_output=True, timeout=30, text=True)
        return int(r.stdout.strip().split("\n")[0])
    except Exception:
        return 720

_lock = threading.Lock()
_claim_until = 0.0
_claim_token = ""
_posted = {}   # draft_id -> timestamp
_used = []     # list of {"url","title","ts"} of recently used stories (variety control)
# ...persisted, because this process is restarted after every workflow deploy and an
# in-memory list meant the "do not pick these" list was usually empty, so /new repeated.
_USED_FILE = os.path.join(TMP, "liagent_used.json")

def prune():
    now = time.time()
    for fn in os.listdir(TMP):
        if fn.startswith("liagent_"):
            p = os.path.join(TMP, fn)
            try:
                if now - os.path.getmtime(p) > PRUNE_AGE:
                    os.remove(p)
            except OSError:
                pass
    with _lock:
        for k in [k for k, t in _posted.items() if now - t > PRUNE_AGE]:
            _posted.pop(k, None)

def _prune_loop():
    while True:
        try:
            prune()
        except Exception:
            pass
        time.sleep(3600)   # sweep every hour, regardless of draft activity

def render(draft_id, html, size=None):
    hp = os.path.join(TMP, PREFIX + draft_id + ".html")
    pp = os.path.join(TMP, PREFIX + draft_id + ".png")
    with open(hp, "w") as f:
        f.write(html)
    cmd = [("--window-size=%d,%d" % tuple(size)) if size and a.startswith("--window-size=") else a
           for a in CHROME]
    subprocess.run(cmd + ["--screenshot=" + pp, "file://" + hp],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    with open(pp, "rb") as f:
        return f.read()

def watermark(draft_id, file_url, handle=DEFAULT_HANDLE):
    raw = os.path.join(TMP, "liagent_vidraw_" + draft_id + ".mp4")
    out = os.path.join(TMP, "liagent_vid_" + draft_id + ".mp4")
    req = urllib.request.Request(file_url, headers={"User-Agent": "linkedin-agent/1.0"})
    with open(raw, "wb") as f:
        f.write(urllib.request.urlopen(req, timeout=90).read())
    h = _probe_height(raw)
    fs = max(20, h // 22); mg = max(16, h // 26); bw = max(8, h // 55)
    # sanitize handle for ffmpeg drawtext (drop the delimiter chars it would choke on)
    safe = "".join(c for c in str(handle) if c.isalnum() or c in "/-_.")[:48] or DEFAULT_HANDLE
    vf = ("drawtext=fontfile=/usr/share/fonts/truetype/sora/Sora.ttf:text=%s:"
          "x=w-tw-%d:y=h-th-%d:fontsize=%d:fontcolor=white:"
          "box=1:boxcolor=0x0a66c2@0.9:boxborderw=%d" % (safe, mg, mg, fs, bw))
    subprocess.run(["ffmpeg", "-y", "-i", raw, "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
    try:
        os.remove(raw)
    except OSError:
        pass
    return os.path.getsize(out)

# --- clips & stills from a public video (announcements, keynotes) -------------
# Only the needed seconds are fetched (yt-dlp --download-sections), so a 90-minute
# keynote never lands on this box and what we keep is a short excerpt, not a copy.
# A credit line is burned in; the handle badge matches watermark() above.
YTDLP = os.environ.get("YTDLP", "/opt/render/venv/bin/yt-dlp")
DENO = os.environ.get("DENO", "/usr/local/bin/deno")
DENO_CACHE = os.environ.get("DENO_DIR", "/opt/render/denocache")
CLIP_MAX = 60                      # hard cap on excerpt length, seconds

def _tsec(v):
    """'1:02:03' / '12:30' / '45' / 45 -> seconds."""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    parts = str(v).strip().split(":")
    try:
        parts = [int(x) for x in parts]
    except ValueError:
        return 0
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec

def _hms(sec):
    sec = max(0, int(sec))
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)

def _drawtext(text, x, y, fontsize, box_color, border):
    safe = "".join(c for c in str(text) if c.isalnum() or c in " /-_.,()&+@:")[:64]
    safe = safe.replace("%", "")                       # ffmpeg expands % in drawtext
    safe = safe.replace(":", "\\\\:")                   # drawtext needs the colon double-escaped
    return ("drawtext=fontfile=/usr/share/fonts/truetype/sora/Sora.ttf:text=%s:"
            "x=%s:y=%s:fontsize=%d:fontcolor=white:"
            "box=1:boxcolor=%s:boxborderw=%d" % (safe, x, y, fontsize, box_color, border))

def _fetch_section(url, dest_noext, start, dur, max_h=1080):
    """yt-dlp just the wanted window. Returns the produced file path, or ''."""
    for stale in glob.glob(dest_noext + ".*"):
        try:
            os.remove(stale)
        except OSError:
            pass
    a = _tsec(start)
    b = a + max(1, min(int(dur or 15), CLIP_MAX))
    cmd = [YTDLP, "--no-playlist", "--quiet", "--no-warnings", "--no-progress",
           "--js-runtimes", "deno:" + DENO,
           "--download-sections", "*%s-%s" % (_hms(a), _hms(b)),
           "--force-keyframes-at-cuts",
           "-f", ("bv*[vcodec^=avc1][height<=%d]+ba[acodec^=mp4a]/b[ext=mp4]/b" % max_h),
           "--merge-output-format", "mp4",
           "-o", dest_noext + ".%(ext)s", "--", url]
    env = dict(os.environ, DENO_DIR=DENO_CACHE)
    subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=300)
    hits = glob.glob(dest_noext + ".*")
    return hits[0] if hits else ""

def ytsearch(query, n=5):
    """Candidate videos for a story. Flat search: titles/channels only, no download."""
    env = dict(os.environ, DENO_DIR=DENO_CACHE)
    try:
        out = subprocess.run([YTDLP, "--flat-playlist", "--quiet", "--no-warnings",
                              "--js-runtimes", "deno:" + DENO,
                              "--print", "%(id)s\t%(title)s\t%(channel)s\t%(duration)s",
                              "--", "ytsearch%d:%s" % (max(1, min(int(n), 10)), query)],
                             env=env, capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    items = []
    for line in (out.stdout or "").strip().split("\n"):
        f = line.split("\t")
        if len(f) >= 3 and f[0]:
            items.append({"id": f[0], "title": f[1], "channel": f[2],
                          "duration": _tsec(f[3]) if len(f) > 3 and f[3].isdigit() else 0,
                          "url": "https://www.youtube.com/watch?v=" + f[0]})
    return items

def _load_used():
    try:
        with open(_USED_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            _used[:] = [x for x in data if isinstance(x, dict) and x.get("ts")]
    except Exception:
        pass

def _save_used():
    try:
        tmp = _USED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_used, f)
        os.replace(tmp, _USED_FILE)
    except Exception:
        pass

def _vtt_cues(path):
    """VTT -> [{t, text}]. YouTube auto-captions roll the same words across cues, so
    only newly-added text is kept, otherwise the transcript triples in size."""
    cues, last = [], ""
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return cues
    blocks = re.split(r"\n\s*\n", raw)
    for b in blocks:
        m = re.search(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->", b)
        if not m:
            continue
        t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        body = []
        for line in b.split("\n"):
            if "-->" in line or re.match(r"^\s*(WEBVTT|Kind:|Language:|NOTE|\d+)\s*$", line):
                continue
            body.append(re.sub(r"<[^>]+>", "", line).strip())
        txt = " ".join(x for x in body if x).strip()
        if not txt or txt == last:
            continue
        if last and txt.startswith(last):          # rolling caption: keep the new tail
            txt_new = txt[len(last):].strip()
            if txt_new:
                cues.append({"t": t, "text": txt_new})
            last = txt
            continue
        cues.append({"t": t, "text": txt})
        last = txt
    return cues

def transcript(url, lang="en", max_chars=60000):
    """Captions + canonical id/url, so a later /clip targets the same video."""
    base = os.path.join(TMP, "liagent_subs_" + re.sub(r"[^A-Za-z0-9]", "", str(abs(hash(url))))[:12])
    for stale in glob.glob(base + "*"):
        try:
            os.remove(stale)
        except OSError:
            pass
    env = dict(os.environ, DENO_DIR=DENO_CACHE)
    meta = {}
    try:
        out = subprocess.run([YTDLP, "--no-playlist", "--quiet", "--no-warnings",
                              "--js-runtimes", "deno:" + DENO, "--skip-download",
                              "--print", "%(id)s\t%(title)s\t%(webpage_url)s\t%(duration)s",
                              "--", url], env=env, capture_output=True, text=True, timeout=120)
        parts = (out.stdout or "").strip().split("\n")[0].split("\t")
        if len(parts) >= 3:
            meta = {"id": parts[0], "title": parts[1], "url": parts[2],
                    "duration": _tsec(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0}
    except Exception:
        pass
    target = meta.get("url") or url
    subprocess.run([YTDLP, "--no-playlist", "--quiet", "--no-warnings", "--skip-download",
                    "--js-runtimes", "deno:" + DENO,
                    "--write-subs", "--write-auto-subs",
                    "--sub-langs", "%s.*,%s" % (lang, lang), "--sub-format", "vtt",
                    "-o", base + ".%(ext)s", "--", target],
                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    cues = []
    for f in sorted(glob.glob(base + "*.vtt")):
        cues = _vtt_cues(f)
        if cues:
            break
    for f in glob.glob(base + "*"):
        try:
            os.remove(f)
        except OSError:
            pass
    total, kept, truncated = 0, [], False
    for c in cues:
        total += len(c["text"]) + 8
        if total > max_chars:
            truncated = True
            break
        kept.append(c)
    out = {"ok": bool(kept), "cues": kept, "truncated": truncated}
    out.update(meta)
    return out

_STOP = set(("the a an and or of to in on for with at by from as is are was were be been it its "
             "this that these those new now how why what when who will would can could may might "
             "says say said just more most very than then into over after before about his her their "
             "our your my you we they he she i s t re ve ll d m o").split())

def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", str(text).lower())
            if len(w) > 2 and w not in _STOP]

def _pick_video(items, query):
    """Prefer a video whose channel is named in the story (the official account), then length."""
    qw = set(_words(query))
    best, best_score = None, -1
    for it in items:
        ch = set(_words(it.get("channel", "")))
        score = 3 * len(qw & ch)                       # official-channel match dominates
        score += min(2, len(qw & set(_words(it.get("title", "")))))
        if (it.get("duration") or 0) < 45:
            score -= 2                                 # shorts/teasers rarely hold the moment
        if score > best_score:
            best, best_score = it, score
    return best

def _best_moment(cues, keywords, lead=3):
    """Cue with the most story keywords; back up a few seconds so the sentence starts."""
    kw = set(_words(keywords))
    if not kw or not cues:
        return None, "", 0
    best, best_hits = None, 0
    for c in cues:
        hits = len(kw & set(_words(c["text"])))
        if hits > best_hits:
            best, best_hits = c, hits
    if not best:
        return None, "", 0
    return max(0, int(best["t"]) - lead), best["text"], best_hits

def shot(draft_id, url, w=1280, h=860, timeout=20):
    """Capture the top of the story page. Refuses anything that is not a real link: Chromium
    with an empty url happily renders its own start page, which is how a Google search once
    went out as if it were the article."""
    u = str(url or "").strip()
    if not re.match(r"^https?://[^/\s]+\.[^/\s]{2,}", u):
        _log("shot refused, unusable link:", repr(u)[:60])
        return {"ok": False, "reason": "this draft has no source link, so there is no page to capture"}
    t0 = time.time()
    tmp_png = os.path.join(TMP, "liagent_shotraw_" + draft_id + ".png")
    out = os.path.join(TMP, PREFIX + draft_id + ".png")
    # --virtual-time-budget waits for the network to go quiet, which ad-heavy news pages never
    # do (fool.com hung past 30s). --timeout stops loading after 7s and shoots what is there.
    cmd = [("--window-size=%d,%d" % (w, h)) if a.startswith("--window-size=") else
           "--timeout=7000" if a.startswith("--virtual-time-budget=") else a for a in CHROME]
    try:
        subprocess.run(cmd + ["--screenshot=" + tmp_png, u],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception as e:
        _log("shot chromium failed:", e)
        return {"ok": False, "reason": "the page did not load in time"}
    if not os.path.exists(tmp_png) or os.path.getsize(tmp_png) < 8000:
        return {"ok": False, "reason": "the page did not render"}
    subprocess.run(["ffmpeg", "-y", "-i", tmp_png, "-vf", "scale=1080:-2", out],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
    try:
        os.remove(tmp_png)
    except OSError:
        pass
    ok = os.path.exists(out) and os.path.getsize(out) > 8000
    _log("shot %s in %.1fs ok=%s" % (u[:60], time.time() - t0, ok))
    if ok:
        _clear_video(draft_id)
    return {"ok": bool(ok), "bytes": os.path.getsize(out) if ok else 0,
            "reason": "" if ok else "the capture came out empty"}
def _video_from_page(url):
    """The video the story itself embeds. yt-dlp handles far more than YouTube, so a company
    newsroom, a blog post or a news page with a player all resolve here, and whatever comes
    back is by definition the primary source for that story."""
    if not url:
        return None
    env = dict(os.environ, DENO_DIR=DENO_CACHE)
    try:
        out = subprocess.run([YTDLP, "--no-playlist", "--quiet", "--no-warnings", "--skip-download",
                              "--js-runtimes", "deno:" + DENO,
                              "--print", "%(id)s\t%(title)s\t%(channel,uploader,extractor)s\t%(webpage_url)s\t%(duration)s",
                              "--", url], env=env, capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    line = (out.stdout or "").strip().split("\n")[0]
    f = line.split("\t")
    if len(f) < 4 or not f[0] or f[3].startswith("NA"):
        return None
    return {"id": f[0], "title": f[1], "channel": f[2], "url": f[3],
            "duration": _tsec(f[4]) if len(f) > 4 and f[4].isdigit() else 0, "fromPage": True}

def _moment_window(cues, idx, lead=3, min_dur=10, max_dur=75):
    """Run to the end of the thought, not to a stopwatch: keep following cues while they
    arrive without a long silence, and stop on a sentence end once it is long enough."""
    start = max(0, int(cues[idx]["t"]) - lead)
    end = int(cues[idx]["t"]) + 6
    j = idx
    while j + 1 < len(cues):
        gap = cues[j + 1]["t"] - cues[j]["t"]
        if gap > 8:                                  # a long silence reads as a topic break
            break
        if cues[j + 1]["t"] - start > max_dur:
            break
        j += 1
        end = int(cues[j]["t"]) + 5
        if cues[j]["text"].rstrip().endswith((".", "!", "?")) and (end - start) >= min_dur:
            break
    return start, max(min_dur, min(max_dur, end - start))

def mediaprobe(query, keywords="", lang="en", page_url=""):
    """What material exists for this story, and which format it argues for. No download."""
    page_vid = _video_from_page(page_url)
    items = [] if page_vid else ytsearch(query, 5)
    out = {"candidates": ([{"title": page_vid.get("title"), "channel": page_vid.get("channel"),
                            "duration": page_vid.get("duration")}] if page_vid else
                          [{"title": i.get("title"), "channel": i.get("channel"),
                            "duration": i.get("duration")} for i in items])}
    if not page_vid and not items:
        out.update(ok=True, recommend="card", why="no video found for this story")
        return out
    vid = page_vid or _pick_video(items, query)
    qw = set(_words(query))
    # a video embedded in the story page is the story source, so it counts as official
    official = bool(page_vid) or bool(qw & set(_words(vid.get("channel", ""))))
    tr = transcript(vid["url"], lang=lang)
    cues = tr.get("cues") or []
    kw = set(_words(keywords or query))
    best_i, best_hits = -1, 0
    for i, c in enumerate(cues):
        hits = len(kw & set(_words(c["text"])))
        if hits > best_hits:
            best_i, best_hits = i, hits
    start = dur = None
    matched = ""
    if best_i >= 0:
        start, dur = _moment_window(cues, best_i)
        matched = cues[best_i]["text"]
    short = (vid.get("duration") or 0) < 45
    # Relevance gate. Keyword overlap alone picks nonsense: a funding story matched a music
    # channel, and an AI-policy story matched a tutorial channel strongly enough to clip it.
    # Nothing is used unless the channel is the subject of the story, or the TITLE is clearly
    # about it; otherwise the card stays, which is always a safe answer.
    title_hits = len(qw & set(_words(vid.get("title", ""))))
    relevant = official or title_hits >= 2
    if not relevant:
        rec, why = "card", ("found only loosely related videos (closest: %s), keeping the card"
                            % (vid.get("channel") or "unknown"))
    elif not official:
        # On topic but second hand: a channel re-reading the news, or an explainer. Using their
        # footage borrows someone elses work without the authenticity of the primary source, so
        # the card wins. Variety then comes from the other formats, not from third party video.
        rec, why = "card", ("closest video is %s re-reporting it, not the source, so keeping the card"
                            % (vid.get("channel") or "a third party"))
    elif best_hits >= 2 and not short:
        rec, why = "clip", "a clear moment in the %s video" % (vid.get("channel") or "source")
    elif not short:
        rec, why = "frame", "the %s video fits but no single moment stands out, so a still" % (vid.get("channel") or "source")
    else:
        rec, why = "card", "only short teaser videos found, keeping the card"
    out.update(ok=True, recommend=rec, why=why, official=official, titleHits=title_hits,
               hadTranscript=bool(cues), keywordHits=best_hits, matched=matched,
               start=start, dur=dur, startLabel=(_hms(start) if start is not None else ""),
               video={"id": vid.get("id"), "title": vid.get("title"),
                      "channel": vid.get("channel"), "duration": vid.get("duration"),
                      "url": tr.get("url") or vid["url"]})
    return out

def automedia(draft_id, query, keywords="", credit="", lang="en", force="", page_url=""):
    """Decide the format from what actually exists, then produce only that."""
    p = mediaprobe(query, keywords, lang, page_url)
    rec = force or p.get("recommend") or "card"
    p["chosen"] = rec
    if rec == "card" or not p.get("video"):
        p["chosen"] = "card"
        return p
    v = p["video"]
    cred = credit or ("Source: " + (v.get("channel") or "YouTube"))
    p["credit"] = cred
    start = p.get("start")
    if rec == "clip":
        dur = p.get("dur") or 15
        # a longer excerpt at 1080p gets heavy for a chat upload; 720p above ~30s
        p["bytes"] = clip(draft_id, v["url"], start if start is not None else 30,
                          dur, cred, max_h=(1080 if dur <= 30 else 720))
        p["durUsed"] = dur
    else:
        at = start if start is not None else max(30, int((v.get("duration") or 300) * 0.1))
        p["bytes"] = grab_frame(draft_id, v["url"], at, cred)
    p["ok"] = bool(p.get("bytes"))
    return p

EXPIRED = ("that draft is more than 2 days old and its files were cleaned up, so I cannot change it. "
           "Send /new or paste the link again")

def _draft_gone(draft_id):
    """Draft files are pruned after 48h; buttons on older drafts must say so, not 'no subject'."""
    return not os.path.exists(os.path.join(TMP, PREFIX + str(draft_id) + ".json"))

def _mark_video(draft_id, size):
    """Publishing posts a video only when the draft says isVideo, so every path that attaches
    one must set it (the auto clip did not, and its clip could never be published)."""
    jp = os.path.join(TMP, PREFIX + str(draft_id) + ".json")
    try:
        obj = json.loads(open(jp).read())
    except Exception:
        obj = {"postText": ""}
    obj["isVideo"] = True
    obj["videoBytes"] = size
    with open(jp, "w") as f:
        f.write(json.dumps(obj))

def _clear_video(draft_id):
    """A photo or page shot replaces an attached clip as the post image."""
    jp = os.path.join(TMP, PREFIX + str(draft_id) + ".json")
    try:
        obj = json.loads(open(jp).read())
        if obj.pop("isVideo", None):
            obj.pop("videoBytes", None)
            with open(jp, "w") as f:
                f.write(json.dumps(obj))
    except Exception:
        pass

_STOP = set("a an and are as at but by for from how in into is it its just new now of on or so "
            "than that the their this to up was what when why with".split())

def _search_queries(query):
    """YouTube returns nothing for a long headline full of figures ('Starlink hit 13.6M
    subscribers up 89%, but ARPU fell 17% to $69' -> 0 results), so fall back to the first
    few distinct words, then fewer."""
    words, seen = [], set()
    for w in re.sub(r"[^\w\s'.-]", " ", str(query)).split():
        k = w.lower().strip(".'-")
        if not k or k in seen or k in _STOP or re.match(r"^[\d.,]+[a-z]?$", k):
            continue
        seen.add(k)
        words.append(w.strip(".'-"))
    out = []
    for q in (str(query).strip(), " ".join(words[:6]), " ".join(words[:3])):
        if q and q not in out:
            out.append(q)
    return out

def autoclip(draft_id, query, keywords="", credit="", dur=15, lang="en", handle=DEFAULT_HANDLE):
    """Story -> the right video -> the right seconds of it. One call, so n8n stays thin."""
    items = []
    for q in _search_queries(query):
        items = ytsearch(q, 5)
        if items:
            break
    if not items:
        _log("autoclip no video for %r" % str(query)[:80])
        return {"ok": False, "reason": "no video found"}
    vid = _pick_video(items, query)
    tr = transcript(vid["url"], lang=lang)
    start, matched, hits = _best_moment(tr.get("cues") or [], keywords or query)
    fallback = False
    if start is None:
        d = tr.get("duration") or vid.get("duration") or 0
        start = max(30, int(d * 0.1)) if d else 30     # nothing matched: a little way in
        fallback = True
    cred = credit or ("Source: " + (vid.get("channel") or "YouTube"))
    size = clip(draft_id, tr.get("url") or vid["url"], start, dur, cred, handle)
    if size > 0:
        _mark_video(draft_id, size)
    _log("autoclip %r -> %s in %ds ok=%s" % (str(query)[:60], vid.get("id"), start, size > 0))
    return {"ok": size > 0, "bytes": size, "start": start, "startLabel": _hms(start),
            "matched": matched, "keywordHits": hits, "fallbackMoment": fallback,
            "hadTranscript": bool(tr.get("cues")), "credit": cred,
            "video": {"id": vid.get("id"), "title": vid.get("title"),
                      "channel": vid.get("channel"), "url": tr.get("url") or vid["url"]},
            "candidates": [{"title": i.get("title"), "channel": i.get("channel")} for i in items]}

def clip(draft_id, url, start=None, dur=15, credit="", handle=DEFAULT_HANDLE, max_h=1080):
    """Short excerpt of a public video, credit burned bottom-left, handle bottom-right."""
    raw_base = os.path.join(TMP, "liagent_clipraw_" + draft_id)
    out = os.path.join(TMP, "liagent_vid_" + draft_id + ".mp4")
    raw = _fetch_section(url, raw_base, start, dur, max_h)
    if not raw:
        return 0
    h = _probe_height(raw) or 720
    fs = max(20, h // 24); mg = max(16, h // 26); bw = max(8, h // 55)
    vf = [_drawtext(handle, "w-tw-%d" % mg, "h-th-%d" % mg, fs, "0x0a66c2@0.9", bw)]
    if credit:
        vf.append(_drawtext(credit, "%d" % mg, "h-th-%d" % mg, max(16, int(fs * 0.8)),
                            "black@0.65", bw))
    subprocess.run(["ffmpeg", "-y", "-i", raw, "-vf", ",".join(vf),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    try:
        os.remove(raw)
    except OSError:
        pass
    return os.path.getsize(out) if os.path.exists(out) else 0

def grab_frame(draft_id, url, at=None, credit=""):
    """A single still from a public video -> the draft's photo slot (card background)."""
    raw_base = os.path.join(TMP, "liagent_frameraw_" + draft_id)
    out = os.path.join(TMP, "liagent_photo_" + draft_id + ".img")
    raw = _fetch_section(url, raw_base, at, 2)
    if not raw:
        return 0
    vf = "scale='min(1920,iw)':-2"
    if credit:
        vf += "," + _drawtext(credit, "40", "h-th-40", 34, "black@0.65", 10)
    # the .img extension makes ffmpeg guess a (disabled) codec, so name mjpeg explicitly
    subprocess.run(["ffmpeg", "-y", "-i", raw, "-frames:v", "1", "-vf", vf,
                    "-c:v", "mjpeg", "-q:v", "3", "-f", "image2", out],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    try:
        os.remove(raw)
    except OSError:
        pass
    return os.path.getsize(out) if os.path.exists(out) else 0

# --- find the SAME story on a readable outlet when the pasted link is blocked ---
_CHAL = ["just a moment", "enable javascript and cookies", "verify you are human", "are you a robot", "checking your browser"]

def _http(url, timeout=13):
    req = urllib.request.Request(url, headers={"User-Agent": REAL_UA,
          "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml"})
    r = urllib.request.urlopen(req, timeout=timeout)
    return r.geturl(), r.read().decode("utf-8", "replace")

def _strip(html):
    t = re.sub(r"<script[\s\S]*?</script>", " ", html)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def _og(html, prop):
    m = (re.search(r'<meta[^>]+property=["\']og:' + prop + r'["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:' + prop + r'["\']', html, re.I))
    return m.group(1).strip() if m else ""

def find_news(title):
    """Search Bing News for the headline, follow the top results, return the first
    readable publisher article as {ok,url,title,article}."""
    try:
        _, rss = _http("https://www.bing.com/news/search?q=" + quote(title[:90]) + "&format=rss", 18)
    except Exception:
        return {"ok": False}
    rss = rss.replace("&amp;", "&")
    clicks = re.findall(r'(https?://www\.bing\.com/news/apiclick\.aspx[^<\s"]+)', rss)[:6]
    for link in clicks:
        try:
            final, html = _http(link, 13)
        except Exception:
            continue
        body = _strip(html)
        low = html.lower()
        if len(body) < 900:
            continue
        if any(c in low for c in _CHAL) and len(body) < 1600:
            continue
        t = _og(html, "title")
        d = _og(html, "description")
        article = (("TITLE: " + t + "\n") if t else "") + (("SUMMARY: " + d + "\n\n") if d else "") + body
        return {"ok": True, "url": final, "title": t or title, "article": article[:7000]}
    return {"ok": False}

# --- resolve a background photo for the card: use the article image only if it actually loads,
#     else find a relevant one (Wikipedia, then Wikimedia Commons) ---
def _http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": REAL_UA, "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))

def _img_ok(url):
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": REAL_UA})
        r = urllib.request.urlopen(req, timeout=10)
        ct = (r.headers.get("Content-Type") or "").lower()
        chunk = r.read(50000)
        return ("image" in ct) and len(chunk) > 3000
    except Exception:
        return False

def _wiki_photo(q):
    try:
        j = _http_json("https://en.wikipedia.org/w/api.php?action=query&format=json&generator=search&gsrsearch="
                       + quote(q) + "&gsrlimit=3&prop=pageimages&piprop=thumbnail&pithumbsize=1200")
        pages = list((j.get("query") or {}).get("pages", {}).values())
        pages.sort(key=lambda r: r.get("index", 99))
        for r in pages:
            src = (r.get("thumbnail") or {}).get("source")
            # photos only: a lead image that is an SVG diagram or a PNG chart comes back as
            # ".svg.png"/".png" (the Starlink article's orbit diagram ended up in a gallery)
            if src and re.search(r"\.jpe?g$", src.split("?")[0], re.I):
                return src
    except Exception:
        pass
    return ""

def _x_token(tweet_id):
    """The token X's own embed computes for its public syndication endpoint:
    (id / 1e15 * pi) written in base 36, with zeros and the dot removed."""
    x = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip, fp, s = int(x), x - int(x), ""
    while ip:
        s, ip = digits[ip % 36] + s, ip // 36
    f = ""
    for _ in range(12):
        fp *= 36
        f += digits[int(fp)]
        fp -= int(fp)
    return ((s or "0") + "." + f).replace("0", "").replace(".", "")

def _ago(created):
    """How long ago, in words the writer can use as is ('almost four years ago'): left to the model,
    a 2022 post became 'four years ago today'."""
    try:
        t = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return ""
    days = max(0, int((time.time() - t) // 86400))
    words = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
    w = lambda n: words[n] if n < len(words) else str(n)
    if days == 0: return "today"
    if days == 1: return "yesterday"
    if days < 14: return "%s days ago" % w(days)
    if days < 60: return "%s weeks ago" % w(days // 7)
    if days < 365: return "%s months ago" % w(days // 30)
    y = days / 365.25
    if y - int(y) >= 0.75: return "almost %s years ago" % w(int(y) + 1)
    if y - int(y) < 0.25: return ("a year ago" if int(y) == 1 else "%s years ago" % w(int(y)))
    return "over %s ago" % ("a year" if int(y) == 1 else w(int(y)) + " years")

def x_post(url):
    """A REAL X post, read from the public embed data (the same source X's embed widget uses), for
    the card that shows that actual post: author, handle, verified type, text, original time,
    real like/reply counts and its photo. Nothing is made up; if X does not return it, ok=false."""
    m = re.search(r"(?:x|twitter)\.com/[^/\s]+/status(?:es)?/(\d+)", str(url or ""))
    if not m:
        return {"ok": False, "reason": "that is not a link to an X post"}
    tid = m.group(1)
    try:
        d = _http_json("https://cdn.syndication.twimg.com/tweet-result?id=%s&token=%s&lang=en" % (tid, _x_token(tid)), timeout=15)
    except Exception:
        d = {}
    if not d or d.get("__typename") not in ("Tweet", None) or not d.get("text"):
        return {"ok": False, "reason": "X did not return that post (deleted, private, or age-restricted)"}
    u = d.get("user") or {}
    a, b = (d.get("display_text_range") or [0, len(d.get("text", ""))])[:2]
    text = d.get("text", "")[a:b]
    for e in (d.get("entities") or {}).get("urls", []) or []:       # t.co links -> what X displays
        if e.get("url") and e.get("display_url"):
            text = text.replace(e["url"], e["display_url"])
    text = _html.unescape(re.sub(r"\s*https?://t\.co/\S+$", "", text)).strip()
    vt = (u.get("verified_type") or "").lower()
    badge = "gold" if vt == "business" else "grey" if vt == "government" else "blue" if (u.get("is_blue_verified") or u.get("verified")) else ""
    photo = ""
    for md in d.get("mediaDetails") or []:
        photo = md.get("media_url_https") or ""
        if photo:
            break
    return {"ok": True, "id": tid, "url": "https://x.com/%s/status/%s" % (u.get("screen_name", "i"), tid),
            "ago": _ago(d.get("created_at", "")),
            "name": u.get("name", ""), "handle": u.get("screen_name", ""), "badge": badge,
            "avatar": (u.get("profile_image_url_https") or "").replace("_normal.", "_400x400."),
            # X's affiliation label (an employer's logo next to the name): real, X shows it on the post
            "affiliate": (((u.get("highlighted_label") or {}).get("badge") or {}).get("url") or ""),
            "affiliateName": ((u.get("highlighted_label") or {}).get("description") or ""),
            "text": text, "created": d.get("created_at", ""), "likes": int(d.get("favorite_count") or 0),
            "replies": int(d.get("conversation_count") or 0), "photo": photo}

def person_photo(name):
    """Photo of a quoted person for the quote card — only when the Wikipedia page is clearly about
    them (every part of the name in the page title) and the image is a real photo. A search
    result that is merely close (a namesake, their company) must never put a wrong face on a quote."""
    toks = [t for t in re.findall(r"[a-z]+", str(name or "").lower()) if len(t) > 1]
    if len(toks) < 2:
        return ""
    try:
        j = _http_json("https://en.wikipedia.org/w/api.php?action=query&format=json&generator=search&gsrsearch="
                       + quote(str(name)) + "&gsrlimit=3&prop=pageimages&piprop=thumbnail&pithumbsize=1000")
        pages = sorted((j.get("query") or {}).get("pages", {}).values(), key=lambda r: r.get("index", 99))
        for r in pages:
            title = r.get("title", "").lower()
            src = (r.get("thumbnail") or {}).get("source") or ""
            if all(t in title for t in toks) and re.search(r"\.jpe?g$", src.split("?")[0], re.I) and _img_ok(src):
                return src
    except Exception:
        pass
    return ""

def _commons_photo(q):
    try:
        j = _http_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search&gsrsearch="
                       + quote(q + " filemime:image/jpeg") + "&gsrnamespace=6&gsrlimit=8&prop=imageinfo&iiprop=url&iiurlwidth=1200")
        for r in (j.get("query") or {}).get("pages", {}).values():
            info = (r.get("imageinfo") or [{}])[0]
            src = info.get("thumburl") or info.get("url") or ""
            if re.search(r"\.jpe?g$", src.split("?")[0], re.I):
                return src
    except Exception:
        pass
    return ""

def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)

def _probe_avg(path):
    """Average colour of an image, by scaling it to a single pixel. Cheap way to spot a
    tile that came out black because the source had transparency."""
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=1:1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, timeout=30)
        b = r.stdout[:3]
        return sum(b) / 3.0 if len(b) == 3 else 0
    except Exception:
        return 0

def _ahash(path):
    """8x8 average hash, so two near-identical photos do not both end up in a gallery."""
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=8:8,format=gray",
                            "-f", "rawvideo", "-"], capture_output=True, timeout=30)
        d = r.stdout[:64]
        if len(d) < 64:
            return None
        avg = sum(d) / 64.0
        return [1 if x > avg else 0 for x in d]
    except Exception:
        return None

def _hamming(a, b):
    return sum(1 for x, y in zip(a, b) if x != y) if a and b else 99

def _commons_photos(q, n=8):
    """Several freely licensed photos for a subject, not just the first one."""
    out = []
    try:
        # JPEG originals only: diagrams, charts and maps are SVG/PNG on Commons, and a gallery
        # for "Starlink satellite" came back half orbit diagrams and line charts.
        j = _http_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search&gsrsearch="
                       + quote(q + " filemime:image/jpeg") + "&gsrnamespace=6&gsrlimit=" + str(n)
                       + "&prop=imageinfo&iiprop=url|mime&iiurlwidth=1200")
        for r in (j.get("query") or {}).get("pages", {}).values():
            info = (r.get("imageinfo") or [{}])[0]
            if info.get("mime") and info.get("mime") != "image/jpeg":
                continue
            src = info.get("thumburl") or info.get("url") or ""
            # Commons appends utm_* tracking params, so the extension is no longer at the
            # end of the URL. Test the path, not the whole string, or everything is rejected.
            if re.search(r"\.(jpe?g|png)$", src.split("?")[0], re.I) and src not in out:
                out.append(src)
    except Exception:
        pass
    return out

def _tile(src, dst, w, h, timeout=25):
    """Scale+crop to fill. Transparent sources are composited onto WHITE: ffmpeg defaults to
    black, which is what turned several gallery tiles into black squares."""
    fc = ("color=white:s=%dx%d[bg];[0:v]scale=%d:%d:force_original_aspect_ratio=increase,"
          "crop=%d:%d,format=rgba[fg];[bg][fg]overlay=format=auto,format=yuv420p"
          % (w, h, w, h, w, h))
    try:
        subprocess.run(["ffmpeg", "-y", "-i", src, "-filter_complex", fc, "-frames:v", "1", dst],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        return False
    if not os.path.exists(dst) or os.path.getsize(dst) < 2000:
        return False
    if _probe_avg(dst) < 16:                 # effectively a black square, not a photograph
        _log("tile rejected (near black):", os.path.basename(src))
        try:
            os.remove(dst)
        except OSError:
            pass
        return False
    return True

def photos(draft_id, subject, article="", mode="single", want=4, deadline=18):
    """Real photos instead of a card. Hard-bounded: this runs inside the approvals listener,
    which holds a 65s claim, so it must finish fast or every other button goes deaf."""
    subj = str(subject or "").strip()
    if len(subj) < 3:
        return {"ok": False, "reason": "this draft has no subject to search photos for"}
    t0 = time.time()
    cand = []
    if article and _img_ok(article):
        cand.append(article)
    w = _wiki_photo(subj)
    if w and w not in cand:
        cand.append(w)
    for u in _commons_photos(subj):
        if u not in cand:
            cand.append(u)
    if not cand:
        return {"ok": False, "reason": "no photo found for " + subj}

    def grab(idx_url):
        i, u = idx_url
        fp = os.path.join(TMP, "liagent_pic_%s_%d.jpg" % (draft_id, i))
        try:
            req = urllib.request.Request(u, headers={"User-Agent": REAL_UA})
            data = urllib.request.urlopen(req, timeout=6).read(9000000)
            if len(data) < 3000:
                return None
            with open(fp, "wb") as f:
                f.write(data)
            return fp
        except Exception:
            return None

    need = 1 if mode != "gallery" else max(2, min(int(want), 4))
    files = []
    pool = ThreadPoolExecutor(max_workers=6)
    futs = [pool.submit(grab, (i, u)) for i, u in enumerate(cand[:8])]
    done, _ = futures_wait(futs, timeout=max(4, deadline - (time.time() - t0)))
    pool.shutdown(wait=False)
    for f in futs:
        if f in done:
            try:
                r = f.result(timeout=0)
            except Exception:
                r = None
            if r:
                files.append(r)

    out = os.path.join(TMP, PREFIX + draft_id + ".png")
    tiles, hashes = [], []
    side = 1080 if need == 1 else (540 if need >= 4 else 540)
    th = 1080 if need == 2 else side
    for fp in files:
        if len(tiles) >= need:
            break
        dst = os.path.join(TMP, "liagent_tile_%s_%d.jpg" % (draft_id, len(tiles)))
        if not _tile(fp, dst, 1080 if need == 1 else side, 1080 if need <= 2 else side):
            continue
        h = _ahash(dst)
        if any(_hamming(h, prev) < 8 for prev in hashes):   # a near-duplicate of one we have
            _log("tile rejected (duplicate)")
            try: os.remove(dst)
            except OSError: pass
            continue
        hashes.append(h)
        tiles.append(dst)
    for fp in files:
        try: os.remove(fp)
        except OSError: pass

    if not tiles:
        return {"ok": False, "reason": "found images for " + subj + " but none were usable (blank or unreadable)"}

    if len(tiles) == 1:
        subprocess.run(["ffmpeg", "-y", "-i", tiles[0], "-vf", "scale=1080:1080", out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
    elif len(tiles) >= 4:
        subprocess.run(["ffmpeg", "-y", "-i", tiles[0], "-i", tiles[1], "-i", tiles[2], "-i", tiles[3],
                        "-filter_complex", "[0][1]hstack[a];[2][3]hstack[b];[a][b]vstack", out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    else:
        a = tiles[0]
        b = tiles[1] if len(tiles) > 1 else tiles[0]
        for x in (a, b):
            _tile(x, x, 540, 1080)
        subprocess.run(["ffmpeg", "-y", "-i", a, "-i", b, "-filter_complex", "[0][1]hstack", out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    for x in tiles:
        try: os.remove(x)
        except OSError: pass
    ok = os.path.exists(out) and os.path.getsize(out) > 8000
    _log("photos %s subject=%r used=%d in %.1fs" % (mode, subj, len(tiles), time.time() - t0))
    if ok:
        _clear_video(draft_id)
    return {"ok": bool(ok), "used": len(tiles), "bytes": os.path.getsize(out) if ok else 0,
            "seconds": round(time.time() - t0, 1),
            "reason": "" if ok else "could not compose the image"}
def find_image(article, subject):
    if _img_ok(article):
        return {"url": article}
    if subject:
        w = _wiki_photo(subject)
        if _img_ok(w):
            return {"url": w}
        c = _commons_photo(subject)
        if _img_ok(c):
            return {"url": c}
    return {"url": ""}

class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/render":
                data = self._body()
                did = str(data.get("draft_id", "d0"))
                dj = data.get("draft_json", "")
                if dj:
                    with open(os.path.join(TMP, PREFIX + did + ".json"), "w") as f:
                        f.write(dj)
                prune()
                return self._send(200, "image/png", render(did, data.get("html", "")))
            if path == "/claim":
                global _claim_until, _claim_token
                data = self._body()
                token = str(data.get("token", ""))
                ttl = float(data.get("ttl", 65))
                with _lock:
                    now = time.time()
                    if data.get("release"):
                        # the listener ending at its 15 min cap hands over at once instead of
                        # leaving every tap unanswered until the 65s lock runs out
                        if _claim_token == token:
                            _claim_until = 0
                        return self._json({"ok": True, "released": _claim_token == token})
                    if _claim_until > now and _claim_token != token:
                        return self._json({"ok": False})
                    _claim_until = now + ttl
                    _claim_token = token
                    return self._json({"ok": True})
            if path == "/posted":
                data = self._body()
                did = str(data.get("draft_id", ""))
                with _lock:
                    first = did not in _posted
                    if first:
                        _posted[did] = time.time()
                    return self._json({"first": first})
            if path == "/debug":
                n = int(self.headers.get("Content-Length", 0))
                with open(os.path.join(TMP, "liagent_debug.json"), "wb") as f:
                    f.write(self.rfile.read(n))
                return self._json({"ok": True})
            if path == "/used":
                data = self._body()
                u = str(data.get("url", "")).strip()
                t = str(data.get("title", "")).strip()
                with _lock:
                    if u or t:
                        _used.append({"url": u, "title": t, "ts": time.time()})
                    cutoff = time.time() - 3 * 86400
                    while _used and _used[0]["ts"] < cutoff:
                        _used.pop(0)
                    del _used[:-40]
                    _save_used()
                return self._json({"ok": True, "count": len(_used)})
            if path == "/setphoto":
                # store a user photo as a card BACKGROUND asset (served back via GET /photo?id=)
                # so Chromium renders from localhost and restyles keep working after the
                # Telegram file URL expires (~1h). Pruned with everything else after 48h.
                data = self._body()
                did = str(data.get("draft_id", ""))
                fu = data.get("file_url", "")
                req = urllib.request.Request(fu, headers={"User-Agent": "linkedin-agent/1.0"})
                img = urllib.request.urlopen(req, timeout=25).read()
                with open(os.path.join(TMP, "liagent_photo_" + did + ".img"), "wb") as f:
                    f.write(img)
                return self._json({"ok": True, "id": did,
                                   "url": "http://127.0.0.1:8099/photo?id=" + did,
                                   "bytes": len(img)})
            if path == "/setcard":
                data = self._body()
                did = str(data.get("draft_id", ""))
                fu = data.get("file_url", "")
                req = urllib.request.Request(fu, headers={"User-Agent": "linkedin-agent/1.0"})
                img = urllib.request.urlopen(req, timeout=25).read()
                with open(os.path.join(TMP, PREFIX + did + ".png"), "wb") as f:
                    f.write(img)
                return self._json({"ok": True, "bytes": len(img)})
            if path == "/photos":
                data = self._body()
                if _draft_gone(str(data.get("draft_id", "x"))):
                    return self._json({"ok": False, "reason": EXPIRED})
                return self._json(photos(str(data.get("draft_id", "x")), data.get("subject", ""),
                                         data.get("article", ""), data.get("mode", "single"),
                                         data.get("want", 4)))
            if path == "/shot":
                data = self._body()
                if _draft_gone(str(data.get("draft_id", "x"))):
                    return self._json({"ok": False, "reason": EXPIRED})
                return self._json(shot(str(data.get("draft_id", "x")), data.get("url", "")))
            if path == "/mediaprobe":
                data = self._body()
                return self._json(mediaprobe(data.get("query", ""), data.get("keywords", ""),
                                             data.get("lang", "en"), data.get("page_url", "")))
            if path == "/automedia":
                data = self._body()
                return self._json(automedia(str(data.get("draft_id", "x")), data.get("query", ""),
                                            data.get("keywords", ""), data.get("credit", ""),
                                            data.get("lang", "en"), data.get("force", ""),
                                            data.get("page_url", "")))
            if path == "/autoclip":
                data = self._body()
                if _draft_gone(str(data.get("draft_id", "x"))):
                    return self._json({"ok": False, "reason": EXPIRED})
                return self._json(autoclip(str(data.get("draft_id", "x")), data.get("query", ""),
                                           data.get("keywords", ""), data.get("credit", ""),
                                           int(data.get("dur", 15)), data.get("lang", "en"),
                                           data.get("handle", DEFAULT_HANDLE)))
            if path == "/ytsearch":
                data = self._body()
                return self._json({"items": ytsearch(data.get("q", ""), data.get("n", 5))})
            if path == "/transcript":
                data = self._body()
                return self._json(transcript(data.get("url", ""), data.get("lang", "en"),
                                             int(data.get("max_chars", 60000))))
            if path == "/clip":
                data = self._body()
                did = str(data.get("draft_id", "x"))
                size = clip(did, data.get("url", ""), data.get("start"),
                            data.get("dur", 15), data.get("credit", ""),
                            data.get("handle", DEFAULT_HANDLE))
                return self._json({"ok": size > 0, "bytes": size})
            if path == "/grabframe":
                data = self._body()
                did = str(data.get("draft_id", "x"))
                size = grab_frame(did, data.get("url", ""), data.get("at"),
                                  data.get("credit", ""))
                return self._json({"ok": size > 0, "bytes": size})
            if path == "/video":
                data = self._body()
                did = str(data.get("draft_id", ""))
                size = watermark(did, data.get("file_url", ""), data.get("handle", DEFAULT_HANDLE))
                _mark_video(did, size)
                return self._json({"ok": True, "bytes": size})
            if path == "/uploadvideo":
                data = self._body()
                did = str(data.get("id", ""))
                with open(os.path.join(TMP, "liagent_vid_" + did + ".mp4"), "rb") as f:
                    content = f.read()
                etags = []
                for ins in data.get("instructions", []):
                    fb = int(ins["firstByte"]); lb = int(ins["lastByte"])
                    rq = urllib.request.Request(ins["uploadUrl"], data=content[fb:lb + 1], method="PUT")
                    r = urllib.request.urlopen(rq, timeout=240)
                    etags.append((r.headers.get("ETag", "") or "").strip('"'))
                return self._json({"etags": etags})
            self._send(404, "text/plain", b"not found")
        except Exception as e:
            self._send(500, "text/plain", str(e).encode())

    def do_GET(self):
        u = urlparse(self.path)
        did = parse_qs(u.query).get("id", [""])[0]
        try:
            if u.path == "/draft":
                with open(os.path.join(TMP, PREFIX + did + ".json"), "rb") as f:
                    return self._send(200, "application/json", f.read())
            if u.path == "/card":
                with open(os.path.join(TMP, PREFIX + did + ".png"), "rb") as f:
                    b = f.read()
                if b[:8] == b"\x89PNG\r\n\x1a\n":
                    ct = "image/png"
                elif b[:2] == b"\xff\xd8":
                    ct = "image/jpeg"
                else:
                    ct = "application/octet-stream"
                return self._send(200, ct, b)
            if u.path == "/photo":
                with open(os.path.join(TMP, "liagent_photo_" + did + ".img"), "rb") as f:
                    b = f.read()
                if b[:8] == b"\x89PNG\r\n\x1a\n":
                    ct = "image/png"
                elif b[:2] == b"\xff\xd8":
                    ct = "image/jpeg"
                else:
                    ct = "application/octet-stream"
                return self._send(200, ct, b)
            if u.path == "/vidfile":
                with open(os.path.join(TMP, "liagent_vid_" + did + ".mp4"), "rb") as f:
                    return self._send(200, "video/mp4", f.read())
            if u.path == "/latest":
                # skip custom title-cards (ids starting with 'card'): they are standalone images,
                # not drafts — feedback/like/publish must keep targeting the real latest draft
                files = [f for f in os.listdir(TMP) if f.startswith(PREFIX) and f.endswith(".json")
                         and not f[len(PREFIX):].startswith("card")]
                if not files:
                    return self._send(404, "text/plain", b"no drafts")
                latest = max(files, key=lambda f: os.path.getmtime(os.path.join(TMP, f)))
                obj = json.loads(open(os.path.join(TMP, latest)).read())
                obj["id"] = latest[len(PREFIX):-5]
                return self._json(obj)
            if u.path == "/debug":
                with open(os.path.join(TMP, "liagent_debug.json"), "rb") as f:
                    return self._send(200, "application/json", f.read())
            if u.path == "/avatar":
                # Served locally so the card can show a real face: AVATAR_URL pointed at a
                # LinkedIn CDN link does not work (media.licdn.com answers 999 to servers).
                here = os.path.dirname(os.path.abspath(__file__))
                for ext, ct in (("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("png", "image/png")):
                    fp = os.path.join(here, "avatar." + ext)
                    if os.path.exists(fp):
                        with open(fp, "rb") as f:
                            return self._send(200, ct, f.read())
                return self._send(404, "text/plain", b"no avatar file in the render service dir")
            if u.path == "/used":
                with _lock:
                    recent = [{"url": x["url"], "title": x["title"]} for x in _used[-25:]]
                return self._json({"recent": recent})
            if u.path == "/findnews":
                title = parse_qs(u.query).get("title", [""])[0]
                if not title:
                    return self._json({"ok": False})
                return self._json(find_news(title))
            if u.path == "/xpost":
                return self._json(x_post(parse_qs(u.query).get("url", [""])[0]))
            if u.path == "/personphoto":
                return self._json({"url": person_photo(parse_qs(u.query).get("name", [""])[0])})
            if u.path == "/findimage":
                qs = parse_qs(u.query)
                return self._json(find_image(qs.get("article", [""])[0], qs.get("subject", [""])[0]))
            if u.path == "/health":
                return self._send(200, "text/plain", b"ok")
            self._send(404, "text/plain", b"not found")
        except FileNotFoundError:
            self._send(404, "text/plain", b"not found")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    _load_used()
    prune()  # sweep once on startup
    threading.Thread(target=_prune_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8099), H).serve_forever()
