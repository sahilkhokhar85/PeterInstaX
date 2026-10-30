import asyncio
import html
import io
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    UserNotFound,
)
from instagrapi.extractors import extract_media_v1
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
IG_USERNAME = os.environ["IG_USERNAME"]
IG_PASSWORD = os.environ["IG_PASSWORD"]
IG_SESSIONID = os.getenv("IG_SESSIONID", "").strip()
IG_TOTP_SECRET = os.getenv("IG_TOTP_SECRET", "").strip()
PROXY = os.getenv("IG_PROXY", "").strip()
# fast path: home feed + stories tray (1 call each, covers ALL followed accounts)
FEED_SECONDS = int(os.getenv("FEED_SECONDS", "25"))
# slow safety net: per-account full scan (also covers /add accounts you don't follow)
FULL_SCAN_SECONDS = int(os.getenv("FULL_SCAN_SECONDS", "60"))
# priority accounts (/priority user) are re-checked this often
PRIO_SECONDS = int(os.getenv("PRIO_SECONDS", "90"))

# put the Instagram cover as the 1st frame of every video (needs ffmpeg) so Telegram shows it as preview
COVER_FRAME = os.getenv("COVER_FRAME", "1") == "1"

SESSION_FILE = "session.json"
DATA_FILE = "data.json"
MAX_SEEN = 300
TG_FILE_LIMIT = 49 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("igbot")
for noisy in ("httpx", "instagrapi", "private_request", "public_request"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

cl = Client()
cl.delay_range = [0, 1]
if PROXY:
    cl.set_proxy(PROXY)

poll_lock = asyncio.Lock()
prio_lock = asyncio.Lock()
ig_lock = threading.Lock()
cooldown_until = 0.0
session_alert_sent = False


# ---------------- state ----------------
def load_state():
    if Path(DATA_FILE).exists():
        return json.loads(Path(DATA_FILE).read_text())
    return {"paused": False, "users": {}}


def save_state():
    tmp = DATA_FILE + ".tmp"
    Path(tmp).write_text(json.dumps(state, indent=1))
    os.replace(tmp, DATA_FILE)


state = load_state()


def new_entry(uid):
    return {
        "id": str(uid),
        "init": False,
        "posts": [],
        "stories": [],
        "added": time.time(),
    }


def watched():
    """instagram user id -> username"""
    return {e["id"]: u for u, e in state["users"].items()}


# ---------------- instagram ----------------
def ig_login():
    if IG_SESSIONID:
        cl.login_by_sessionid(IG_SESSIONID)
    else:
        if Path(SESSION_FILE).exists():
            cl.load_settings(SESSION_FILE)
        code = cl.totp_generate_code(IG_TOTP_SECRET) if IG_TOTP_SECRET else ""
        cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
    cl.dump_settings(SESSION_FILE)
    log.info("Instagram login OK")


def ig_call(fn, *args, **kwargs):
    with ig_lock:
        try:
            return fn(*args, **kwargs)
        except LoginRequired:
            log.warning("Session expired, relogin")
            ig_login()
            return fn(*args, **kwargs)


def ig_fetch(uid):
    posts = ig_call(cl.user_medias_v1, int(uid), 5)
    stories = ig_call(cl.user_stories, int(uid))
    return posts, stories


def ig_resolve(username):
    return ig_call(cl.user_id_from_username, username)


def ig_following():
    return ig_call(cl.user_following_v1, cl.user_id)


def media_items(m):
    """list of dicts describing the media of a post/reel/story"""
    out = []
    if getattr(m, "media_type", 1) == 8:
        for r in m.resources:
            if r.media_type == 2 and r.video_url:
                out.append(
                    {
                        "kind": "video",
                        "url": str(r.video_url),
                        "thumb": str(r.thumbnail_url) if r.thumbnail_url else None,
                        "dur": 0,
                    }
                )
            elif r.thumbnail_url:
                out.append({"kind": "photo", "url": str(r.thumbnail_url)})
    elif m.media_type == 2 and m.video_url:
        out.append(
            {
                "kind": "video",
                "url": str(m.video_url),
                "thumb": str(m.thumbnail_url) if m.thumbnail_url else None,
                "dur": int(round(getattr(m, "video_duration", 0) or 0)),
            }
        )
    elif m.thumbnail_url:
        out.append({"kind": "photo", "url": str(m.thumbnail_url)})
    return out


# ---------------- telegram sending ----------------
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
    tmo = dict(read_timeout=180, write_timeout=180, connect_timeout=30)
    try:
        blobs = []
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as h:
            for it in items[:10]:
                r = await h.get(it["url"])
                r.raise_for_status()
                if len(r.content) > TG_FILE_LIMIT:
                    raise ValueError("file too big")
                b = {
                    "kind": it["kind"],
                    "data": r.content,
                    "dur": it.get("dur", 0),
                    "thumb": None,
                    "w": 0,
                    "h": 0,
                }
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
                        framed = await asyncio.to_thread(
                            embed_cover_bytes, b["data"], cover_raw
                        )
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
                await bot.send_video(
                    OWNER_ID, b["data"], caption=caption, parse_mode="HTML",
                    **vid_kwargs(b), **tmo,
                )
            else:
                await bot.send_photo(
                    OWNER_ID, b["data"], caption=caption, parse_mode="HTML", **tmo
                )
        else:
            group = []
            for i, b in enumerate(blobs):
                cap = caption if i == 0 else None
                if b["kind"] == "video":
                    group.append(
                        InputMediaVideo(
                            b["data"], caption=cap, parse_mode="HTML", **vid_kwargs(b)
                        )
                    )
                else:
                    group.append(
                        InputMediaPhoto(b["data"], caption=cap, parse_mode="HTML")
                    )
            await bot.send_media_group(OWNER_ID, group, **tmo)
    except Exception as e:
        log.warning("media send failed (%s), sending text only", e)
        await bot.send_message(OWNER_ID, caption, parse_mode="HTML")


async def sender_job(context: ContextTypes.DEFAULT_TYPE):
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
    cap = (
        f"{head} from @{username}{body}\n\n"
        f"🕒 {fmt_time(m.taken_at)}\n{post_link(m)}"
    )
    outbox.append((ts_of(m.taken_at), media_items(m), cap))


async def notify_story(bot, username, s):
    cap = (
        f"👀 <b>New story</b> from @{username}\n\n"
        f"🕒 {fmt_time(s.taken_at)}\n"
        f"https://www.instagram.com/stories/{username}/{s.pk}/"
    )
    outbox.append((ts_of(s.taken_at), media_items(s), cap))


def trim(entry):
    entry["posts"] = entry["posts"][-MAX_SEEN:]
    entry["stories"] = entry["stories"][-MAX_SEEN:]


# ---------------- FAST PATH: home feed + stories tray ----------------
async def fast_posts(bot):
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
            continue  # full scan will initialise this account first
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
    data = await asyncio.to_thread(
        ig_call, cl.get_reels_tray_feed, "pull_to_refresh"
    )
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

        stories = await asyncio.to_thread(ig_call, cl.user_stories, int(uid))
        for s in sorted(stories, key=lambda x: x.taken_at):
            sid = str(s.pk)
            if sid in entry["stories"]:
                continue
            entry["stories"].append(sid)
            if s.taken_at.timestamp() <= prev:
                continue  # older story, just mark as seen
            await notify_story(bot, username, s)
        trim(entry)

    state["tray_init"] = True
    save_state()


async def feed_job(context: ContextTypes.DEFAULT_TYPE):
    if state["paused"] or not state["users"]:
        return
    for name, fn in (("posts", fast_posts), ("stories", fast_stories)):
        try:
            await fn(context.bot)
        except (PleaseWaitFewMinutes, RateLimitError):
            log.warning("Rate limited (%s), sleeping 5 min", name)
            await asyncio.sleep(300)
            return
        except Exception as e:
            log.exception("fast %s error: %s", name, e)


# ---------------- SLOW PATH: per-account full scan (safety net) ----------------
async def poll_user(bot, username, entry):
    posts, stories = await asyncio.to_thread(ig_fetch, entry["id"])
    first_run = not entry["init"]

    for m in sorted(posts, key=lambda x: x.taken_at):
        pid = str(m.pk)
        if pid in entry["posts"]:
            continue
        entry["posts"].append(pid)
        if first_run:
            continue
        await notify_post(bot, username, m)

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


async def scan_users(bot, names, lock):
    global cooldown_until, session_alert_sent
    if state["paused"] or lock.locked() or time.time() < cooldown_until:
        return
    async with lock:
        for username in names:
            if time.time() < cooldown_until:
                break
            entry = state["users"].get(username)
            if not entry:
                continue
            try:
                await poll_user(bot, username, entry)
                save_state()
                session_alert_sent = False
            except (PleaseWaitFewMinutes, RateLimitError):
                log.warning("Rate limited by Instagram, pausing 5 min")
                cooldown_until = time.time() + 300
                break
            except LoginRequired:
                log.error("Instagram session dead (login_required)")
                cooldown_until = time.time() + 600
                if not session_alert_sent:
                    session_alert_sent = True
                    await bot.send_message(
                        OWNER_ID,
                        "⚠️ Instagram session expire ho gaya (login_required).\n"
                        "Browser se naya sessionid lo, VPS par .env mein IG_SESSIONID badlo, "
                        "phir: sudo systemctl restart igbot",
                    )
                break
            except UserNotFound:
                log.warning("User not found: %s", username)
            except Exception as e:
                log.error("poll error for %s: %s", username, e)
            await asyncio.sleep(random.uniform(0.4, 1.0))


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    names = [u for u, e in state["users"].items() if not e.get("prio")]
    await scan_users(context.bot, names, poll_lock)


async def poll_prio_job(context: ContextTypes.DEFAULT_TYPE):
    names = [u for u, e in state["users"].items() if e.get("prio")]
    if names:
        await scan_users(context.bot, names, prio_lock)


# ---------------- commands (owner only) ----------------
owner = filters.User(OWNER_ID)


def clean(name):
    name = name.strip().rstrip("/").split("/")[-1].split("?")[0]
    return name.lstrip("@").lower()


async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "Instagram → Telegram bot ready 🔥\n\n"
        "/add user1 user2 – pages add karo\n"
        "/remove user – hatao\n"
        "/list – watch list\n"
        "/import – jinhe tumhara IG account follow karta hai sabko add karo\n"
        "/pause /resume – notifications band/chalu\n"
        "/status – bot status"
    )


async def cmd_add(update: Update, context):
    if not context.args:
        return await update.message.reply_text("Use: /add username1 username2")
    msgs = []
    for raw in context.args:
        u = clean(raw)
        if u in state["users"]:
            msgs.append(f"• @{u} already hai")
            continue
        try:
            uid = await asyncio.to_thread(ig_resolve, u)
            state["users"][u] = new_entry(uid)
            msgs.append(f"✅ @{u} add ho gaya")
        except Exception as e:
            msgs.append(f"❌ @{u}: {e}")
    save_state()
    await update.message.reply_text("\n".join(msgs))


async def cmd_remove(update: Update, context):
    msgs = []
    for raw in context.args:
        u = clean(raw)
        if state["users"].pop(u, None):
            msgs.append(f"🗑 @{u} hata diya")
        else:
            msgs.append(f"• @{u} list me nahi tha")
    save_state()
    await update.message.reply_text("\n".join(msgs) or "Use: /remove username")


async def cmd_priority(update: Update, context):
    if not context.args:
        return await update.message.reply_text("Use: /priority user1 user2  (har ~90s check)")
    msgs = []
    for raw in context.args:
        u = clean(raw)
        e = state["users"].get(u)
        if not e:
            msgs.append(f"• @{u} list me nahi hai (pehle /add karo)")
            continue
        e["prio"] = True
        msgs.append(f"⭐ @{u} priority me")
    save_state()
    await update.message.reply_text("\n".join(msgs))


async def cmd_unpriority(update: Update, context):
    msgs = []
    for raw in context.args:
        u = clean(raw)
        e = state["users"].get(u)
        if e and e.pop("prio", None):
            msgs.append(f"@{u} normal me wapas")
    save_state()
    await update.message.reply_text("\n".join(msgs) or "Use: /unpriority user")


async def cmd_list(update: Update, context):
    names = sorted(state["users"], key=lambda n: (not state["users"][n].get("prio"), n))
    if not names:
        return await update.message.reply_text("List khaali hai. /add ya /import use karo.")
    text = f"Watching {len(names)} accounts (⭐ = priority):\n" + "\n".join(
        f"{'⭐' if state['users'][n].get('prio') else ''}@{n}" for n in names
    )
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i : i + 4000])


async def cmd_import(update: Update, context):
    await update.message.reply_text("Following list la raha hu...")
    try:
        following = await asyncio.to_thread(ig_following)
    except Exception as e:
        return await update.message.reply_text(f"❌ {e}")
    added = 0
    for u in following:
        name = u.username.lower()
        if name not in state["users"]:
            state["users"][name] = new_entry(u.pk)
            added += 1
    save_state()
    await update.message.reply_text(
        f"✅ {added} naye add hue. Total: {len(state['users'])}\n"
        "(Pehle round me sirf purana content mark hoga, spam nahi aayega.)"
    )


async def cmd_pause(update: Update, context):
    state["paused"] = True
    save_state()
    await update.message.reply_text("⏸ Paused")


async def cmd_resume(update: Update, context):
    state["paused"] = False
    save_state()
    await update.message.reply_text("▶️ Resumed")


async def cmd_status(update: Update, context):
    ready = sum(1 for e in state["users"].values() if e["init"])
    await update.message.reply_text(
        f"Accounts: {len(state['users'])} (ready: {ready})\n"
        f"Paused: {state['paused']}\n"
        f"Priority accounts: {sum(1 for e in state['users'].values() if e.get('prio'))} (har {PRIO_SECONDS}s)\n"
        f"Fast feed path: {'ON' if os.getenv('FAST_PATH', '0') == '1' else 'OFF'}"
    )


def main():
    ig_login()
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in [
        ("start", cmd_start),
        ("add", cmd_add),
        ("remove", cmd_remove),
        ("list", cmd_list),
        ("import", cmd_import),
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("status", cmd_status),
        ("priority", cmd_priority),
        ("unpriority", cmd_unpriority),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=owner))
    # NOTE: fast path (home feed/tray) kills the session on this setup -> off by default
    if os.getenv("FAST_PATH", "0") == "1":
        app.job_queue.run_repeating(feed_job, interval=FEED_SECONDS, first=15)
    app.job_queue.run_repeating(poll_job, interval=FULL_SCAN_SECONDS, first=5)
    app.job_queue.run_repeating(poll_prio_job, interval=PRIO_SECONDS, first=10)
    app.job_queue.run_repeating(sender_job, interval=3, first=3)
    app.run_polling()


if __name__ == "__main__":
    main()
