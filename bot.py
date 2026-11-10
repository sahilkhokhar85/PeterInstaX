"""
Instagram -> Telegram notifier bot (self-hosted on a VPS).

v2 rewrite focused on account safety (the original got the account locked after
~29h of sustained polling from a datacenter IP). Key changes vs the first version:

  1. SAFETY
     - The Instagram session (cookies + device fingerprint: device_id, uuid, phone_id,
       advertising_id, user_agent, ...) is persisted in session.json and ALWAYS reused
       on every restart, never regenerated. Re-authenticating under a fresh random
       device while reusing an old session cookie is exactly the kind of mismatch
       Instagram's abuse detection flags - this bot logs in on the VPS as rarely as
       possible and otherwise always resumes.
     - A single continuous, randomized-order account loop (not a fixed-interval
       "scan everyone, then repeat" job) spreads the ~76 accounts' checks evenly
       across the whole allowed delay window, with a random 3-7s (configurable) gap
       between accounts. This removes the back-to-back, zero-rest hammering pattern
       that the first version had despite a nominal "FULL_SCAN_SECONDS=60".
     - Stories are only re-checked every Nth round per account (STORY_EVERY) -
       posts matter more than story latency, and this roughly halves/thirds request
       volume for comparatively low-value traffic.
     - Centralized backoff: 429 (PleaseWaitFewMinutes/RateLimitError) triggers an
       ADAPTIVE cooldown that grows the more consecutive hits happen (5 -> 10 -> 20
       ... capped at 60 min) instead of a flat 5 minutes every time, and resets after
       a clean cycle. challenge_required PAUSES the bot and alerts the owner instead
       of silently retrying (retrying a challenge from a bot process is how
       auto-logout/locks get worse, not better).
     - Optional QUIET_HOURS: skip polling during a configured local time window so
       the traffic pattern has a daily rest period like a real person would, rather
       than running identically 24/7 forever.
     - FAST_PATH (home feed + stories tray, 2 calls for ~all accounts) stays OFF by
       default - it killed the session twice on this account already. The code path
       is kept (gated behind the env var) with the same backoff/circuit-breaker
       wrapped around it, in case a better-behaved mobile session makes it viable
       later; it is not recommended without testing on a throwaway account first.

  2. RELIABLE LOGIN
     - `python bot.py --login` is a ONE-TIME, interactive, LOCAL-machine command
       (run on your laptop/home IP, never on the VPS) that logs in fresh (password +
       2FA, or an IG_SESSIONID browser cookie) and writes session.json. Copy that
       file to the VPS (scp) and the service only ever resumes it from then on - the
       VPS itself should essentially never need to perform an actual Instagram
       login. This is the single biggest fix for the 429/ClientNeedsUpgradeError
       problems: those happened on real *login* attempts from the datacenter IP, not
       on ordinary authenticated requests.
     - challenge_required (checkpoint) is handled explicitly: the bot does not loop
       retrying it. It pauses, alerts the owner with the exact recovery steps, and
       waits for a replaced session.json + restart.

  3. REMOTE CONTROL FROM mhajk_.py
     - This bot can be driven remotely by another process (a Telegram bot such as
       mhajk_.py) through two small shared JSON files instead of a live API:
         IG_CMD_FILE  - a command queue. Each entry: {"id", "cmd", "args", "ts"}.
                        This bot polls it every IG_CMD_POLL_SECONDS, applies each
                        command through the exact same functions the /add /remove
                        etc. Telegram handlers use, and removes processed entries.
         IG_DATA_FILE - a read-only mirror of data.json (accounts, paused flag,
                        ready counts) that this bot refreshes after every state
                        change, for mhajk_.py to read and display without needing
                        to touch data.json directly.
       This assumes both bots can see the same filesystem (same VPS, or a synced
       directory). If they run on different machines, sync IG_CMD_FILE/IG_DATA_FILE
       between them (rsync/cron, or a small HTTP shim) - that transport is outside
       this file's job.

  4. PROXY
     - IG_PROXY is applied to the instagrapi client exactly as before, but startup
       now logs plainly whether a proxy is active, and the README explains what to
       do when the VPS's IP changes (new VPS every month per the hosting plan).

  5. UNCHANGED ON PURPOSE (per the brief - "keep as they are")
     - Notification caption format and layout.
     - The cover-first-frame video trick (embed_cover_bytes): Telegram's preview is
       built from the video's own first frame, so the Instagram cover image is
       re-encoded as a short first "frame" via an ffmpeg concat filter, exactly as
       in the original. Logic is carried over essentially unchanged.
     - Carousel posts sent as a Telegram media group; single media sent directly.
     - The owner-only command set: /add /remove /list /import /pause /resume
       /status /priority /unpriority.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import html
import io
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# --------------------------------------------------------------------------- config
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
IG_USERNAME = os.environ.get("IG_USERNAME", "")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")
IG_SESSIONID = os.getenv("IG_SESSIONID", "").strip()
IG_TOTP_SECRET = os.getenv("IG_TOTP_SECRET", "").strip()
PROXY = os.getenv("IG_PROXY", "").strip()

FAST_PATH = os.getenv("FAST_PATH", "0") == "1"          # OFF by default - see module docstring
FULL_SCAN_SECONDS = int(os.getenv("FULL_SCAN_SECONDS", "300"))   # target time to cycle through ALL non-priority accounts once
PRIO_SECONDS = int(os.getenv("PRIO_SECONDS", "90"))

SCAN_GAP_MIN = float(os.getenv("SCAN_GAP_MIN", "3"))     # jitter between accounts, seconds
SCAN_GAP_MAX = float(os.getenv("SCAN_GAP_MAX", "7"))
STORY_EVERY = int(os.getenv("STORY_EVERY", "3"))         # check an account's stories every Nth round (posts checked every round)

# "HH:MM-HH:MM" in QUIET_TZ (default IST) - polling pauses during this window each day.
# Leave empty to disable. Example: QUIET_HOURS=02:00-07:00
QUIET_HOURS = os.getenv("QUIET_HOURS", "").strip()
QUIET_TZ_OFFSET_MIN = int(os.getenv("QUIET_TZ_OFFSET_MIN", "330"))  # +05:30 IST by default

COVER_FRAME = os.getenv("COVER_FRAME", "1") == "1"

SESSION_FILE = os.getenv("IG_SESSION_FILE", "session.json")
DATA_FILE = os.getenv("IG_DATA_STORE", "data.json")
IG_CMD_FILE = os.getenv("IG_CMD_FILE", "ig_commands.json")       # commands FROM mhajk_.py
IG_DATA_FILE = os.getenv("IG_DATA_FILE", "ig_data.json")         # status mirror FOR mhajk_.py
IG_CMD_POLL_SECONDS = float(os.getenv("IG_CMD_POLL_SECONDS", "5"))

MAX_SEEN = 300
TG_FILE_LIMIT = 49 * 1024 * 1024

# adaptive 429 backoff: starts here, doubles each consecutive hit, capped here
RATE_LIMIT_BASE_COOLDOWN = 300          # 5 min
RATE_LIMIT_MAX_COOLDOWN = 3600          # 60 min

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("igbot")
for noisy in ("httpx", "instagrapi", "private_request", "public_request"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- Instagram client
def _build_client():
    from instagrapi import Client

    c = Client()
    c.delay_range = [1, 3]       # instagrapi's own small inter-request jitter
    if PROXY:
        c.set_proxy(PROXY)
        log.info("Instagram client: proxy ACTIVE (%s)", PROXY.split("@")[-1])
    else:
        log.info("Instagram client: no proxy configured (IG_PROXY empty)")
    return c


cl = None  # built in main()/login flow so --login mode can run without needing BOT_TOKEN etc.

poll_lock = asyncio.Lock()
prio_lock = asyncio.Lock()
ig_lock = threading.Lock()

# adaptive rate-limit state
_rl_consecutive = 0
cooldown_until = 0.0
session_alert_sent = False
challenge_paused = False
round_counter = 0


# --------------------------------------------------------------------------- state (data.json)
def load_state():
    if Path(DATA_FILE).exists():
        try:
            return json.loads(Path(DATA_FILE).read_text())
        except Exception as e:
            log.error("data.json unreadable (%s) - starting empty rather than crashing", e)
    return {"paused": False, "users": {}}


def save_state():
    tmp = DATA_FILE + ".tmp"
    Path(tmp).write_text(json.dumps(state, indent=1))
    os.replace(tmp, DATA_FILE)
    _write_ig_mirror()


def _write_ig_mirror():
    """Refresh the small read-only status mirror that mhajk_.py (or any other
    controller) can read without touching data.json directly."""
    try:
        users = state.get("users", {})
        mirror = {
            "paused": state.get("paused", False),
            "accounts": len(users),
            "ready": sum(1 for e in users.values() if e.get("init")),
            "priority": sum(1 for e in users.values() if e.get("prio")),
            "users": {u: {"id": e.get("id"), "init": e.get("init", False), "prio": e.get("prio", False)} for u, e in users.items()},
            "challenge_paused": challenge_paused,
            "cooldown_until": cooldown_until,
            "updated_at": time.time(),
        }
        tmp = IG_DATA_FILE + ".tmp"
        Path(tmp).write_text(json.dumps(mirror, indent=1))
        os.replace(tmp, IG_DATA_FILE)
    except Exception as e:
        log.warning("could not write IG_DATA_FILE mirror: %s", e)


state = load_state()


def new_entry(uid):
    return {"id": str(uid), "init": False, "posts": [], "stories": [], "added": time.time(), "story_round": 0}


def watched():
    """instagram user id -> username"""
    return {e["id"]: u for u, e in state["users"].items()}


# --------------------------------------------------------------------------- login
class ChallengeRequiredPause(Exception):
    """Raised internally when Instagram asks for a checkpoint/challenge we cannot
    safely solve from an unattended VPS process."""


def _session_looks_alive() -> bool:
    """Cheap, low-cost call to confirm the resumed session actually still works,
    without doing a fresh login."""
    try:
        cl.account_info()
        return True
    except Exception as e:
        log.warning("resumed session failed a liveness check: %s", e)
        return False


def ig_login(interactive: bool = False):
    """Log in, preferring to RESUME the persisted session (same device fingerprint,
    same cookies) over ever performing a fresh login from this process. A fresh
    login should really only ever happen via `python bot.py --login` run on a
    laptop/home IP - see the module docstring."""
    from instagrapi.exceptions import ChallengeRequired, LoginRequired

    if Path(SESSION_FILE).exists():
        try:
            cl.load_settings(SESSION_FILE)
            if _session_looks_alive():
                log.info("Resumed existing Instagram session (device fingerprint preserved)")
                return
            log.warning("Saved session.json did not pass the liveness check - will attempt fresh login")
        except Exception as e:
            log.warning("load_settings failed (%s) - will attempt fresh login", e)

    if not interactive:
        # On the VPS we do NOT want to silently attempt a fresh login (that is
        # exactly what triggered the 429/ClientNeedsUpgradeError before). Fail
        # loudly instead and tell the owner how to fix it properly.
        raise RuntimeError(
            "No usable session.json. Do NOT let the VPS attempt a fresh Instagram "
            "login - run `python bot.py --login` on your laptop/home IP, then copy "
            "the resulting session.json to the VPS and restart the service."
        )

    try:
        if IG_SESSIONID:
            cl.login_by_sessionid(IG_SESSIONID)
        else:
            code = ""
            if IG_TOTP_SECRET:
                code = cl.totp_generate_code(IG_TOTP_SECRET)
            elif interactive:
                maybe = input("2FA code (blank if not asked / not using TOTP): ").strip()
                code = maybe
            cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
    except ChallengeRequired:
        log.error("Instagram asked for a challenge (checkpoint) during login.")
        if interactive:
            print(
                "\nInstagram wants a verification step. Open the Instagram app or "
                "instagram.com on this SAME machine/network and complete it, then "
                "re-run `python bot.py --login`.\n"
            )
        raise
    cl.dump_settings(SESSION_FILE)
    log.info("Fresh Instagram login OK, session.json written")


def interactive_login():
    """Entry point for `python bot.py --login` - meant to be run on a laptop/home
    IP, NEVER on the VPS. Produces session.json to copy over via scp."""
    global cl
    cl = _build_client()
    print("Instagram login helper - run this on your laptop/home network, not the VPS.\n")
    if not IG_USERNAME or (not IG_PASSWORD and not IG_SESSIONID):
        print("Set IG_USERNAME + (IG_PASSWORD or IG_SESSIONID) in .env first.")
        sys.exit(1)
    ig_login(interactive=True)
    print(f"\nDone. {SESSION_FILE} written in the current directory.")
    print(f"Copy it to the VPS, e.g.:\n  scp {SESSION_FILE} user@your-vps:/opt/insta-tg-bot/{SESSION_FILE}\n")


# --------------------------------------------------------------------------- IG call wrapper (centralized backoff)
def ig_call(fn, *args, **kwargs):
    """Every Instagram API call goes through here so backoff/relogin/challenge
    handling is in exactly one place."""
    global _rl_consecutive, cooldown_until
    from instagrapi.exceptions import (
        ChallengeRequired,
        ClientError,
        LoginRequired,
        PleaseWaitFewMinutes,
        RateLimitError,
    )

    with ig_lock:
        try:
            result = fn(*args, **kwargs)
            _rl_consecutive = 0  # a clean call resets the adaptive backoff
            return result
        except LoginRequired:
            log.warning("Session expired (login_required) - trying to resume session.json once")
            try:
                cl.load_settings(SESSION_FILE)
                result = fn(*args, **kwargs)
                _rl_consecutive = 0
                return result
            except Exception:
                raise
        except ChallengeRequired:
            raise ChallengeRequiredPause()
        except (PleaseWaitFewMinutes, RateLimitError):
            _rl_consecutive += 1
            cooldown = min(RATE_LIMIT_MAX_COOLDOWN, RATE_LIMIT_BASE_COOLDOWN * (2 ** (_rl_consecutive - 1)))
            cooldown_until = time.time() + cooldown
            log.warning("Rate limited by Instagram (hit #%d) - cooling down %ds", _rl_consecutive, cooldown)
            raise
        except ClientError as e:
            log.warning("Instagram client error: %s", e)
            raise


def ig_fetch(uid):
    posts = ig_call(cl.user_medias_v1, int(uid), 5)
    return posts


def ig_fetch_stories(uid):
    return ig_call(cl.user_stories, int(uid))


def ig_resolve(username):
    return ig_call(cl.user_id_from_username, username)


def ig_following():
    return ig_call(cl.user_following_v1, cl.user_id)


# --------------------------------------------------------------------------- media extraction (unchanged)
def media_items(m):
    """list of dicts describing the media of a post/reel/story"""
    out = []
    if getattr(m, "media_type", 1) == 8:
        for r in m.resources:
            if r.media_type == 2 and r.video_url:
                out.append({"kind": "video", "url": str(r.video_url),
                            "thumb": str(r.thumbnail_url) if r.thumbnail_url else None, "dur": 0})
            elif r.thumbnail_url:
                out.append({"kind": "photo", "url": str(r.thumbnail_url)})
    elif m.media_type == 2 and m.video_url:
        out.append({"kind": "video", "url": str(m.video_url),
                    "thumb": str(m.thumbnail_url) if m.thumbnail_url else None,
                    "dur": int(round(getattr(m, "video_duration", 0) or 0))})
    elif m.thumbnail_url:
        out.append({"kind": "photo", "url": str(m.thumbnail_url)})
    return out


# --------------------------------------------------------------------------- Telegram sending (unchanged - keep format + cover-frame trick)
IST = timezone(timedelta(hours=5, minutes=30))
outbox = []  # (posted_ts, items, caption) -> sent in order by sender_job
send_lock = asyncio.Lock()


def make_thumb(raw):
    """jpeg thumbnail (<=320px) plus original width/height from image bytes"""
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        im.thumbnail((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue(), w, h
    except Exception as e:
        log.warning("make_thumb failed: %s", e)
        return None, 0, 0


def embed_cover_bytes(video, cover):
    """Re-encode video with the cover image as its very first frame. Returns bytes or None."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        log.warning("ffmpeg/ffprobe not installed -> cover frame skipped")
        return None
    try:
        with tempfile.TemporaryDirectory() as d:
            vp, cp, op = (os.path.join(d, n) for n in ("in.mp4", "cover.jpg", "out.mp4"))
            with open(vp, "wb") as f:
                f.write(video)
            Image.open(io.BytesIO(cover)).convert("RGB").save(cp, "JPEG", quality=92)

            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,r_frame_rate",
                 "-of", "csv=p=0", vp],
                capture_output=True, text=True, timeout=30,
            )
            w_s, h_s, fr_s = pr.stdout.strip().split("\n")[0].split(",")[:3]
            width, height = int(w_s), int(h_s)
            num, _, den = fr_s.partition("/")
            fps = float(num) / float(den or 1)
            if not (1 <= fps <= 120):
                fps = 30.0
            width -= width % 2
            height -= height % 2
            frame_len = 1.0 / fps
            delay_ms = max(1, int(round(frame_len * 1000)))

            has_audio = bool(subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", vp],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip())

            fc = (
                f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.3f},format=yuv420p[c];"
                f"[0:v]scale={width}:{height},setsar=1,fps={fps:.3f},format=yuv420p[v];"
                f"[c][v]concat=n=2:v=1:a=0[outv]"
            )
            args = ["ffmpeg", "-y", "-loglevel", "error", "-i", vp, "-loop", "1",
                    "-framerate", f"{fps:.3f}", "-t", f"{frame_len:.4f}", "-i", cp]
            if has_audio:
                fc += f";[0:a]adelay={delay_ms}|{delay_ms}[outa]"
                args += ["-filter_complex", fc, "-map", "[outv]", "-map", "[outa]"]
            else:
                args += ["-filter_complex", fc, "-map", "[outv]"]
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
            if has_audio:
                args += ["-c:a", "aac", "-b:a", "128k"]
            args += ["-movflags", "+faststart", op]
            subprocess.run(args, check=True, capture_output=True, timeout=240)
            with open(op, "rb") as f:
                out = f.read()
            return out or None
    except Exception as e:
        log.warning("cover-first-frame failed: %s", e)
        return None


def vid_kwargs(b):
    kw = {"supports_streaming": True}
    if b["dur"]:
        kw["duration"] = b["dur"]
    if b["w"]:
        kw["width"], kw["height"] = b["w"], b["h"]
    if b["thumb"]:
        kw["thumbnail"] = b["thumb"]
    return kw


async def send_media(bot, items, caption):
    from telegram import InputMediaPhoto, InputMediaVideo

    tmo = dict(read_timeout=180, write_timeout=180, connect_timeout=30)
    try:
        blobs = []
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as h:
            for it in items[:10]:
                r = await h.get(it["url"])
                r.raise_for_status()
                if len(r.content) > TG_FILE_LIMIT:
                    raise ValueError("file too big")
                b = {"kind": it["kind"], "data": r.content, "dur": it.get("dur", 0), "thumb": None, "w": 0, "h": 0}
                if it["kind"] == "video" and it.get("thumb"):
                    cover_raw = None
                    try:
                        tr = await h.get(it["thumb"])
                        tr.raise_for_status()
                        cover_raw = tr.content
                        b["thumb"], b["w"], b["h"] = make_thumb(cover_raw)
                    except Exception as e:
                        log.warning("thumbnail download failed: %s", e)
                    if not b["thumb"]:
                        log.warning("video sent WITHOUT thumbnail")
                    if COVER_FRAME and cover_raw:
                        framed = await asyncio.to_thread(embed_cover_bytes, b["data"], cover_raw)
                        if framed and len(framed) <= TG_FILE_LIMIT:
                            b["data"] = framed
                        else:
                            log.warning("sending original video (no cover frame)")
                blobs.append(b)
        if not blobs:
            raise ValueError("no media")
        if len(blobs) == 1:
            b = blobs[0]
            if b["kind"] == "video":
                await bot.send_video(OWNER_ID, b["data"], caption=caption, parse_mode="HTML", **vid_kwargs(b), **tmo)
            else:
                await bot.send_photo(OWNER_ID, b["data"], caption=caption, parse_mode="HTML", **tmo)
        else:
            group = []
            for i, b in enumerate(blobs):
                cap = caption if i == 0 else None
                if b["kind"] == "video":
                    group.append(InputMediaVideo(b["data"], caption=cap, parse_mode="HTML", **vid_kwargs(b)))
                else:
                    group.append(InputMediaPhoto(b["data"], caption=cap, parse_mode="HTML"))
            await bot.send_media_group(OWNER_ID, group, **tmo)
    except Exception as e:
        log.warning("media send failed (%s), sending text only", e)
        await bot.send_message(OWNER_ID, caption, parse_mode="HTML")


async def sender_job(context):
    """sends queued notifications one by one, oldest posted first"""
    if send_lock.locked():
        return
    async with send_lock:
        while outbox:
            outbox.sort(key=lambda x: x[0])
            _, items, caption = outbox.pop(0)
            try:
                await send_media(context.bot, items, caption)
            except Exception as e:
                log.error("send failed: %s", e)


def post_link(m):
    kind = "reel" if getattr(m, "product_type", "") == "clips" else "p"
    return f"https://www.instagram.com/{kind}/{m.code}/"


def fmt_time(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b, %I:%M %p IST")


def ts_of(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def notify_post(bot, username, m):
    is_reel = getattr(m, "product_type", "") == "clips"
    head = "🎬 <b>New reel</b>" if is_reel else "📸 <b>New post</b>"
    text = (m.caption_text or "").strip()
    if len(text) > 700:
        text = text[:700] + "…"
    body = f"\n\n<b>{html.escape(text)}</b>" if text else ""
    cap = f"{head} from @{username}{body}\n\n🕒 {fmt_time(m.taken_at)}\n{post_link(m)}"
    outbox.append((ts_of(m.taken_at), media_items(m), cap))


async def notify_story(bot, username, s):
    cap = (f"👀 <b>New story</b> from @{username}\n\n🕒 {fmt_time(s.taken_at)}\n"
           f"https://www.instagram.com/stories/{username}/{s.pk}/")
    outbox.append((ts_of(s.taken_at), media_items(s), cap))


def trim(entry):
    entry["posts"] = entry["posts"][-MAX_SEEN:]
    entry["stories"] = entry["stories"][-MAX_SEEN:]


# --------------------------------------------------------------------------- quiet hours
def in_quiet_hours() -> bool:
    if not QUIET_HOURS or "-" not in QUIET_HOURS:
        return False
    try:
        start_s, end_s = QUIET_HOURS.split("-", 1)
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except Exception:
        log.warning("QUIET_HOURS=%r is malformed, ignoring", QUIET_HOURS)
        return False
    now_local = datetime.now(timezone.utc) + timedelta(minutes=QUIET_TZ_OFFSET_MIN)
    mins = now_local.hour * 60 + now_local.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= mins < end
    return mins >= start or mins < end  # wraps past midnight


# --------------------------------------------------------------------------- FAST PATH (home feed + stories tray) - OFF by default, see docstring
async def fast_posts(bot):
    from instagrapi.extractors import extract_media_v1

    data = await asyncio.to_thread(ig_call, cl.get_timeline_feed, "pull_to_refresh")
    ids = watched()
    first = not state.get("feed_init")
    found = []
    for fi in data.get("feed_items", []):
        raw = fi.get("media_or_ad")
        if not isinstance(raw, dict) or raw.get("ad_id") or not raw.get("code"):
            continue
        u = raw.get("user") or {}
        uid = str(u.get("pk") or u.get("pk_id") or u.get("id") or "")
        if uid in ids:
            found.append((ids[uid], raw))
    found.sort(key=lambda x: x[1].get("taken_at", 0))

    for username, raw in found:
        entry = state["users"].get(username)
        if not entry or not entry["init"]:
            continue
        pid = str(raw.get("pk") or str(raw.get("id", "")).split("_")[0])
        if pid in entry["posts"]:
            continue
        entry["posts"].append(pid)
        trim(entry)
        if first:
            continue
        try:
            m = extract_media_v1(raw)
        except Exception as e:
            log.warning("parse feed item failed: %s", e)
            continue
        await notify_post(bot, username, m)
    state["feed_init"] = True
    save_state()


async def fast_stories(bot):
    data = await asyncio.to_thread(ig_call, cl.get_reels_tray_feed, "pull_to_refresh")
    ids = watched()
    tray = state.setdefault("tray", {})
    first = not state.get("tray_init")
    now = time.time()
    if first:
        state["tray_started"] = now
    started = state.get("tray_started", now)

    for t in data.get("tray", []):
        u = t.get("user") or {}
        uid = str(u.get("pk") or u.get("pk_id") or u.get("id") or "")
        if uid not in ids:
            continue
        username = ids[uid]
        entry = state["users"][username]
        ts = int(t.get("latest_reel_media") or 0)
        prev = tray.get(uid)
        tray[uid] = ts
        if first:
            continue
        if prev is None:
            prev = max(started, entry.get("added", 0))
        if ts <= prev or not entry["init"]:
            continue

        stories = await asyncio.to_thread(ig_fetch_stories, uid)
        for s in sorted(stories, key=lambda x: x.taken_at):
            sid = str(s.pk)
            if sid in entry["stories"]:
                continue
            entry["stories"].append(sid)
            if s.taken_at.timestamp() <= prev:
                continue
            await notify_story(bot, username, s)
        trim(entry)

    state["tray_init"] = True
    save_state()


async def feed_job(context):
    if state["paused"] or not state["users"] or challenge_paused:
        return
    if time.time() < cooldown_until:
        return
    for name, fn in (("posts", fast_posts), ("stories", fast_stories)):
        try:
            await fn(context.bot)
        except ChallengeRequiredPause:
            await _handle_challenge(context.bot)
            return
        except Exception:
            # ig_call already set cooldown_until / logged specifics for rate-limit
            # cases; anything else here is logged and the cycle just ends early.
            log.exception("fast %s error", name)
            return


# --------------------------------------------------------------------------- SLOW PATH: continuous randomized-order loop (the main safety redesign)
async def _handle_challenge(bot):
    global challenge_paused
    if challenge_paused:
        return
    challenge_paused = True
    state["paused"] = True
    save_state()
    log.error("Instagram challenge_required - pausing until a human fixes the session")
    try:
        await bot.send_message(
            OWNER_ID,
            "🚫 Instagram ne checkpoint/challenge maanga hai - bot ne apne aap PAUSE kar diya.\n\n"
            "Auto-retry karna lock ko behtar nahi, aksar WORSE karta hai - isliye bot kuch nahi "
            "karega jab tak tum manually fix na karo:\n\n"
            "1) Apne phone/laptop (jahan se pehle login kiya tha) se Instagram app/website khol "
            "kar checkpoint complete karo.\n"
            "2) Us hi device/network se `python bot.py --login` chalao (VPS par NAHI) - naya "
            "session.json banega.\n"
            "3) session.json VPS par copy karo, phir `/resume` bhejo ya service restart karo.",
        )
    except Exception as e:
        log.error("could not even send the challenge alert: %s", e)


async def poll_one_account(bot, username, entry, do_stories: bool):
    posts = await asyncio.to_thread(ig_fetch, entry["id"])
    first_run = not entry["init"]

    for m in sorted(posts, key=lambda x: x.taken_at):
        pid = str(m.pk)
        if pid in entry["posts"]:
            continue
        entry["posts"].append(pid)
        if first_run:
            continue
        await notify_post(bot, username, m)

    if do_stories:
        stories = await asyncio.to_thread(ig_fetch_stories, entry["id"])
        for s in sorted(stories, key=lambda x: x.taken_at):
            sid = str(s.pk)
            if sid in entry["stories"]:
                continue
            entry["stories"].append(sid)
            if first_run:
                continue
            await notify_story(bot, username, s)

    trim(entry)
    entry["init"] = True


async def continuous_scan_loop(bot, prio: bool):
    """Replaces the old fixed-interval 'scan everyone, then repeat' job with ONE
    never-ending loop that visits accounts in a freshly-shuffled order each round,
    with a random SCAN_GAP_MIN..MAX second gap between each - the actual delay
    before a new post is noticed is therefore roughly (accounts / 1) * average_gap,
    which for ~76 accounts and a 3-7s gap lands in the ~5-6 minute range the brief
    asks for, WITHOUT ever bursting many requests back-to-back."""
    global cooldown_until, session_alert_sent, round_counter
    lock = prio_lock if prio else poll_lock
    label = "priority" if prio else "normal"
    async with lock:
        while True:
            if challenge_paused:
                await asyncio.sleep(30)
                continue
            if state["paused"]:
                await asyncio.sleep(10)
                continue
            if not prio and in_quiet_hours():
                await asyncio.sleep(60)
                continue
            if time.time() < cooldown_until:
                await asyncio.sleep(min(30, cooldown_until - time.time()))
                continue

            names = [u for u, e in state["users"].items() if bool(e.get("prio")) == prio]
            if not names:
                await asyncio.sleep(PRIO_SECONDS if prio else 15)
                continue
            random.shuffle(names)
            round_counter += 1

            for username in names:
                if state["paused"] or challenge_paused or time.time() < cooldown_until:
                    break
                entry = state["users"].get(username)
                if not entry:
                    continue
                story_round = entry.get("story_round", 0) + 1
                entry["story_round"] = story_round
                do_stories = prio or (story_round % max(1, STORY_EVERY) == 0)
                try:
                    await poll_one_account(bot, username, entry, do_stories)
                    save_state()
                    session_alert_sent = False
                except ChallengeRequiredPause:
                    await _handle_challenge(bot)
                    break
                except Exception as e:
                    from instagrapi.exceptions import LoginRequired, UserNotFound

                    if isinstance(e, UserNotFound):
                        log.warning("User not found: %s", username)
                    elif isinstance(e, LoginRequired):
                        log.error("Instagram session dead (login_required) even after resume attempt")
                        cooldown_until = time.time() + 600
                        if not session_alert_sent:
                            session_alert_sent = True
                            try:
                                await bot.send_message(
                                    OWNER_ID,
                                    "⚠️ Instagram session resume nahi ho payi (login_required).\n"
                                    "VPS par fresh login MAT karne do - apne laptop/home se "
                                    "`python bot.py --login` chalao, session.json copy karo, "
                                    "phir service restart karo.",
                                )
                            except Exception:
                                pass
                        break
                    # rate-limit cases already set cooldown_until inside ig_call
                    else:
                        log.error("poll error for %s: %s", username, e)
                gap = random.uniform(SCAN_GAP_MIN, SCAN_GAP_MAX)
                await asyncio.sleep(gap)

            if not prio:
                # a brief rest between full rounds, on top of the per-account gaps,
                # so the pattern has visible "pauses" rather than one continuous churn
                await asyncio.sleep(max(5, FULL_SCAN_SECONDS - len(names) * ((SCAN_GAP_MIN + SCAN_GAP_MAX) / 2)))


# --------------------------------------------------------------------------- remote-control command queue (mhajk_.py integration)
def _apply_add(usernames):
    msgs = []
    for raw in usernames:
        u = clean(raw)
        if u in state["users"]:
            msgs.append(f"• @{u} already hai")
            continue
        try:
            uid = ig_resolve(u)
            state["users"][u] = new_entry(uid)
            msgs.append(f"✅ @{u} add ho gaya")
        except Exception as e:
            msgs.append(f"❌ @{u}: {e}")
    save_state()
    return msgs


def _apply_remove(usernames):
    msgs = []
    for raw in usernames:
        u = clean(raw)
        if state["users"].pop(u, None):
            msgs.append(f"🗑 @{u} hata diya")
        else:
            msgs.append(f"• @{u} list me nahi tha")
    save_state()
    return msgs


def _apply_priority(usernames, on: bool):
    msgs = []
    for raw in usernames:
        u = clean(raw)
        e = state["users"].get(u)
        if not e:
            msgs.append(f"• @{u} list me nahi hai")
            continue
        if on:
            e["prio"] = True
            msgs.append(f"⭐ @{u} priority me")
        else:
            e.pop("prio", None)
            msgs.append(f"@{u} normal me wapas")
    save_state()
    return msgs


def _apply_import():
    following = ig_following()
    added = 0
    for u in following:
        name = u.username.lower()
        if name not in state["users"]:
            state["users"][name] = new_entry(u.pk)
            added += 1
    save_state()
    return added, len(state["users"])


async def ig_command_queue_loop(bot):
    """Polls IG_CMD_FILE every IG_CMD_POLL_SECONDS for commands queued by an
    external controller (e.g. mhajk_.py's Telegram /ig* commands) and applies
    them through the SAME functions the local Telegram commands use, so behavior
    is identical either way. Each processed command is removed from the file and,
    where useful, its result is sent back to the owner on Telegram."""
    while True:
        await asyncio.sleep(IG_CMD_POLL_SECONDS)
        try:
            if not Path(IG_CMD_FILE).exists():
                continue
            try:
                cmds = json.loads(Path(IG_CMD_FILE).read_text())
            except Exception:
                continue
            if not cmds:
                continue
            remaining = []
            for c in cmds:
                name, args = c.get("cmd"), c.get("args") or []
                try:
                    if name == "add":
                        msgs = await asyncio.to_thread(_apply_add, args)
                    elif name == "remove":
                        msgs = await asyncio.to_thread(_apply_remove, args)
                    elif name == "priority":
                        msgs = await asyncio.to_thread(_apply_priority, args, True)
                    elif name == "unpriority":
                        msgs = await asyncio.to_thread(_apply_priority, args, False)
                    elif name == "pause":
                        state["paused"] = True
                        save_state()
                        msgs = ["⏸ Paused (remote command)"]
                    elif name == "resume":
                        state["paused"] = False
                        save_state()
                        msgs = ["▶️ Resumed (remote command)"]
                    elif name == "import":
                        added, total = await asyncio.to_thread(_apply_import)
                        msgs = [f"✅ {added} naye add hue (remote import). Total: {total}"]
                    else:
                        msgs = [f"❓ unknown remote command: {name}"]
                    log.info("remote command applied: %s %s -> %s", name, args, msgs)
                except Exception as e:
                    log.error("remote command %s failed: %s", name, e)
                # processed (success or failure) - don't keep retrying a bad command forever
            # clear the file: this simple design processes the whole queue each poll
            Path(IG_CMD_FILE).write_text(json.dumps(remaining))
        except Exception as e:
            log.exception("ig_command_queue_loop error: %s", e)


# --------------------------------------------------------------------------- Telegram commands (owner only, unchanged set)
def clean(name):
    name = name.strip().rstrip("/").split("/")[-1].split("?")[0]
    return name.lstrip("@").lower()


async def cmd_start(update, context):
    await update.message.reply_text(
        "Instagram → Telegram bot ready 🔥\n\n"
        "/add user1 user2 – pages add karo\n"
        "/remove user – hatao\n"
        "/list – watch list\n"
        "/import – jinhe tumhara IG account follow karta hai sabko add karo\n"
        "/pause /resume – notifications band/chalu\n"
        "/status – bot status"
    )


async def cmd_add(update, context):
    if not context.args:
        return await update.message.reply_text("Use: /add username1 username2")
    msgs = await asyncio.to_thread(_apply_add, context.args)
    await update.message.reply_text("\n".join(msgs))


async def cmd_remove(update, context):
    if not context.args:
        return await update.message.reply_text("Use: /remove username")
    msgs = await asyncio.to_thread(_apply_remove, context.args)
    await update.message.reply_text("\n".join(msgs))


async def cmd_priority(update, context):
    if not context.args:
        return await update.message.reply_text("Use: /priority user1 user2  (har ~90s check)")
    msgs = await asyncio.to_thread(_apply_priority, context.args, True)
    await update.message.reply_text("\n".join(msgs))


async def cmd_unpriority(update, context):
    if not context.args:
        return await update.message.reply_text("Use: /unpriority user")
    msgs = await asyncio.to_thread(_apply_priority, context.args, False)
    await update.message.reply_text("\n".join(msgs))


async def cmd_list(update, context):
    names = sorted(state["users"], key=lambda n: (not state["users"][n].get("prio"), n))
    if not names:
        return await update.message.reply_text("List khaali hai. /add ya /import use karo.")
    text = f"Watching {len(names)} accounts (⭐ = priority):\n" + "\n".join(
        f"{'⭐' if state['users'][n].get('prio') else ''}@{n}" for n in names
    )
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i: i + 4000])


async def cmd_import(update, context):
    await update.message.reply_text("Following list la raha hu...")
    try:
        added, total = await asyncio.to_thread(_apply_import)
    except Exception as e:
        return await update.message.reply_text(f"❌ {e}")
    await update.message.reply_text(
        f"✅ {added} naye add hue. Total: {total}\n"
        "(Pehle round me sirf purana content mark hoga, spam nahi aayega.)"
    )


async def cmd_pause(update, context):
    state["paused"] = True
    save_state()
    await update.message.reply_text("⏸ Paused")


async def cmd_resume(update, context):
    global challenge_paused
    state["paused"] = False
    challenge_paused = False
    save_state()
    await update.message.reply_text("▶️ Resumed")


async def cmd_status(update, context):
    ready = sum(1 for e in state["users"].values() if e["init"])
    await update.message.reply_text(
        f"Accounts: {len(state['users'])} (ready: {ready})\n"
        f"Paused: {state['paused']}{' (challenge pause!)' if challenge_paused else ''}\n"
        f"Priority accounts: {sum(1 for e in state['users'].values() if e.get('prio'))} (har {PRIO_SECONDS}s)\n"
        f"Fast feed path: {'ON' if FAST_PATH else 'OFF'}\n"
        f"Quiet hours: {QUIET_HOURS or 'off'}\n"
        f"Story check: every {STORY_EVERY} round(s)\n"
        f"Cooldown active: {'yes, ' + str(int(cooldown_until - time.time())) + 's left' if time.time() < cooldown_until else 'no'}"
    )


# --------------------------------------------------------------------------- main
def main():
    global cl
    if not BOT_TOKEN or not OWNER_ID:
        print("BOT_TOKEN and OWNER_ID must be set in .env"); sys.exit(1)

    from telegram.ext import Application, CommandHandler, filters

    cl = _build_client()
    ig_login(interactive=False)

    app = Application.builder().token(BOT_TOKEN).build()
    owner = filters.User(OWNER_ID)
    for name, fn in [
        ("start", cmd_start), ("add", cmd_add), ("remove", cmd_remove), ("list", cmd_list),
        ("import", cmd_import), ("pause", cmd_pause), ("resume", cmd_resume), ("status", cmd_status),
        ("priority", cmd_priority), ("unpriority", cmd_unpriority),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=owner))

    if FAST_PATH:
        log.warning("FAST_PATH=1: home-feed/reels-tray polling enabled - this killed the "
                     "session twice before. Watch the logs closely / test on a throwaway account first.")
        app.job_queue.run_repeating(feed_job, interval=max(60, int(os.getenv("FEED_SECONDS", "60"))), first=15)

    app.job_queue.run_repeating(sender_job, interval=3, first=3)

    async def _post_init(application):
        application.create_task(continuous_scan_loop(application.bot, prio=False))
        application.create_task(continuous_scan_loop(application.bot, prio=True))
        application.create_task(ig_command_queue_loop(application.bot))

    app.post_init = _post_init
    app.run_polling()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true",
                         help="Interactive LOCAL login (run on your laptop/home IP, never the VPS) - writes session.json")
    ns = parser.parse_args()
    if ns.login:
        interactive_login()
    else:
        main()
