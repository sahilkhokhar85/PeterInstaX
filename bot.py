import asyncio
import json
import logging
import os
import random
from pathlib import Path

import httpx
from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    UserNotFound,
)
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
IG_USERNAME = os.environ["IG_USERNAME"]
IG_PASSWORD = os.environ["IG_PASSWORD"]
IG_TOTP_SECRET = os.getenv("IG_TOTP_SECRET", "").strip()
PROXY = os.getenv("IG_PROXY", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "45"))

SESSION_FILE = "session.json"
DATA_FILE = "data.json"
MAX_SEEN = 300
TG_FILE_LIMIT = 49 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("igbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

cl = Client()
cl.delay_range = [1, 3]
if PROXY:
    cl.set_proxy(PROXY)

poll_lock = asyncio.Lock()


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
    return {"id": str(uid), "init": False, "posts": [], "stories": []}


# ---------------- instagram ----------------
def ig_login():
    if Path(SESSION_FILE).exists():
        cl.load_settings(SESSION_FILE)
    code = cl.totp_generate_code(IG_TOTP_SECRET) if IG_TOTP_SECRET else ""
    cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
    cl.dump_settings(SESSION_FILE)
    log.info("Instagram login OK")


def ig_call(fn, *args, **kwargs):
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
    """list of (kind, url) for a post/reel/story"""
    out = []
    if getattr(m, "media_type", 1) == 8:
        for r in m.resources:
            if r.media_type == 2 and r.video_url:
                out.append(("video", str(r.video_url)))
            elif r.thumbnail_url:
                out.append(("photo", str(r.thumbnail_url)))
    elif m.media_type == 2 and m.video_url:
        out.append(("video", str(m.video_url)))
    elif m.thumbnail_url:
        out.append(("photo", str(m.thumbnail_url)))
    return out


# ---------------- telegram sending ----------------
async def send_media(bot, items, caption):
    caption = caption[:1000]
    try:
        blobs = []
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as h:
            for kind, url in items[:10]:
                r = await h.get(url)
                r.raise_for_status()
                if len(r.content) > TG_FILE_LIMIT:
                    raise ValueError("file too big")
                blobs.append((kind, r.content))
        if not blobs:
            raise ValueError("no media")
        if len(blobs) == 1:
            kind, data = blobs[0]
            if kind == "video":
                await bot.send_video(
                    OWNER_ID, data, caption=caption, supports_streaming=True
                )
            else:
                await bot.send_photo(OWNER_ID, data, caption=caption)
        else:
            group = []
            for i, (kind, data) in enumerate(blobs):
                cap = caption if i == 0 else None
                cls = InputMediaVideo if kind == "video" else InputMediaPhoto
                group.append(cls(data, caption=cap))
            await bot.send_media_group(OWNER_ID, group)
    except Exception as e:
        log.warning("media send failed (%s), sending link only", e)
        await bot.send_message(OWNER_ID, caption)


def post_link(m):
    kind = "reel" if getattr(m, "product_type", "") == "clips" else "p"
    return f"https://www.instagram.com/{kind}/{m.code}/"


# ---------------- polling ----------------
async def poll_user(bot, username, entry):
    posts, stories = await asyncio.to_thread(ig_fetch, entry["id"])
    first_run = not entry["init"]

    # oldest first so notifications arrive in order
    for m in sorted(posts, key=lambda x: x.taken_at):
        pid = str(m.pk)
        if pid in entry["posts"]:
            continue
        entry["posts"].append(pid)
        if first_run:
            continue
        is_reel = getattr(m, "product_type", "") == "clips"
        head = "🎬 New reel" if is_reel else "📸 New post"
        cap = f"{head} from @{username}\n\n{(m.caption_text or '')[:600]}\n\n{post_link(m)}"
        await send_media(bot, media_items(m), cap)

    for s in sorted(stories, key=lambda x: x.taken_at):
        sid = str(s.pk)
        if sid in entry["stories"]:
            continue
        entry["stories"].append(sid)
        if first_run:
            continue
        cap = f"👀 New story from @{username}\nhttps://www.instagram.com/stories/{username}/{sid}/"
        await send_media(bot, media_items(s), cap)

    entry["posts"] = entry["posts"][-MAX_SEEN:]
    entry["stories"] = entry["stories"][-MAX_SEEN:]
    entry["init"] = True


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    if state["paused"] or poll_lock.locked():
        return
    async with poll_lock:
        for username in list(state["users"]):
            entry = state["users"].get(username)
            if not entry:
                continue
            try:
                await poll_user(context.bot, username, entry)
                save_state()
            except (PleaseWaitFewMinutes, RateLimitError):
                log.warning("Rate limited by Instagram, sleeping 5 min")
                await asyncio.sleep(300)
                break
            except UserNotFound:
                log.warning("User not found: %s", username)
            except Exception as e:
                log.exception("poll error for %s: %s", username, e)
            await asyncio.sleep(random.uniform(1.5, 3.5))


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


async def cmd_list(update: Update, context):
    names = sorted(state["users"])
    if not names:
        return await update.message.reply_text("List khaali hai. /add ya /import use karo.")
    text = f"Watching {len(names)} accounts:\n" + "\n".join(f"@{n}" for n in names)
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
    await update.message.reply_text(
        f"Accounts: {len(state['users'])}\n"
        f"Paused: {state['paused']}\n"
        f"Poll gap: {POLL_SECONDS}s"
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
    ]:
        app.add_handler(CommandHandler(name, fn, filters=owner))
    app.job_queue.run_repeating(poll_job, interval=POLL_SECONDS, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
