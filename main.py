"""
Instagram DM Auto-Reply Bot
============================
Managed entirely from Telegram.
- Login flow on /start
- Set minimum 5 rotating auto-reply messages (with optional photo each)
- Messages rotate: user 1 gets msg1, user 2 gets msg2, etc. (loops)
- Each reply sent with a delay (2s, 3s, 4s, 5s, 6s cycling)
- Broadcast mode: minimum 10 messages, alternates per user
- Session stored in db.json (base64) for Render persistence

Requirements:
    pip install aiogram==3.7.0 instagrapi==2.1.2 aiofiles==23.2.1 python-dotenv==1.0.1
"""

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path

import aiofiles
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import BadPassword, ChallengeRequired, TwoFactorRequired

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("IGBot")

BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
ADMIN_ID      = int(os.getenv("ADMIN_ID", "0"))   # Your Telegram user ID
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
DB_FILE       = os.getenv("DB_FILE", "db.json")

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("BOT_TOKEN and ADMIN_ID are required in environment variables.")

bot     = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ─────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────
class LoginStates(StatesGroup):
    waiting_username   = State()
    waiting_password   = State()
    waiting_otp        = State()

class SetReplyStates(StatesGroup):
    collecting         = State()   # collecting messages one by one

class BroadcastStates(StatesGroup):
    collecting         = State()
    confirming         = State()

# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────
# Structure:
# {
#   "ig_session_b64": "<base64 of ig settings json>",
#   "ig_username": "...",
#   "auto_replies": [
#       {"text": "...", "photo_b64": "..." or null},
#       ...
#     ],                          ← min 5 required
#   "reply_counter": 0,          ← which message to send next
#   "broadcast_messages": [...], ← min 10 required
#   "broadcast_counter": 0,
#   "seen_threads": {},          ← {thread_id: last_seen_msg_id}
#   "all_thread_ids": [],        ← for broadcast
#   "bot_ready": false
# }

db: dict = {}


async def load_db():
    global db
    if Path(DB_FILE).exists():
        async with aiofiles.open(DB_FILE, "r") as f:
            content = await f.read()
            db = json.loads(content)
    db.setdefault("ig_session_b64", "")
    db.setdefault("ig_username", "")
    db.setdefault("auto_replies", [])
    db.setdefault("reply_counter", 0)
    db.setdefault("broadcast_messages", [])
    db.setdefault("broadcast_counter", 0)
    db.setdefault("seen_threads", {})
    db.setdefault("all_thread_ids", [])
    db.setdefault("bot_ready", False)


async def save_db():
    async with aiofiles.open(DB_FILE, "w") as f:
        await f.write(json.dumps(db, indent=2))


# ─────────────────────────────────────────────
# Instagram Client
# ─────────────────────────────────────────────
ig = Client()
ig.delay_range = [1, 3]

_otp_event: asyncio.Event = asyncio.Event()
_otp_code:  str = ""


def ig_save_session():
    settings = ig.get_settings()
    db["ig_session_b64"] = base64.b64encode(
        json.dumps(settings).encode()
    ).decode()


async def ig_login_with_credentials(username: str, password: str) -> dict:
    """
    Returns dict: {"ok": True} | {"otp": True} | {"error": "msg"}
    """
    global _otp_code
    ig.username = username
    ig.password = password

    # Try loading existing session first
    if db.get("ig_session_b64"):
        try:
            settings = json.loads(base64.b64decode(db["ig_session_b64"]).decode())
            ig.set_settings(settings)
            ig.login(username, password)
            ig_save_session()
            db["ig_username"] = username
            await save_db()
            log.info("Instagram: session login successful.")
            return {"ok": True}
        except Exception as e:
            log.warning(f"Session login failed: {e} — trying fresh.")
            ig.set_settings({})

    try:
        ig.login(username, password)
        ig_save_session()
        db["ig_username"] = username
        await save_db()
        log.info("Instagram: fresh login successful.")
        return {"ok": True}

    except TwoFactorRequired:
        log.info("Instagram: 2FA required.")
        return {"otp": True}

    except ChallengeRequired:
        return {"error": "Instagram sent a challenge (suspicious login). Open Instagram app, verify it was you, then try again."}

    except BadPassword:
        return {"error": "Wrong Instagram password. Try again."}

    except Exception as e:
        return {"error": str(e)}


async def ig_submit_otp(username: str, password: str, otp: str) -> dict:
    try:
        ig.login(username, password, verification_code=otp)
        ig_save_session()
        db["ig_username"] = username
        await save_db()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def ig_safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.error(f"IG error [{fn.__name__}]: {e}")
        return None


# ─────────────────────────────────────────────
# Delay logic
# Cycle: 2s, 3s, 4s, 5s, 6s → repeat
# ─────────────────────────────────────────────
REPLY_DELAYS = [2, 3, 4, 5, 6]

def get_reply_delay(index: int) -> int:
    return REPLY_DELAYS[index % len(REPLY_DELAYS)]


# ─────────────────────────────────────────────
# Helper: send photo+text or just text to IG DM
# ─────────────────────────────────────────────
def ig_send_reply(thread_id: str, user_pk: int, reply: dict):
    text      = reply.get("text", "")
    photo_b64 = reply.get("photo_b64", "")

    if photo_b64:
        photo_bytes = base64.b64decode(photo_b64)
        tmp_path = "/tmp/ig_reply_photo.jpg"
        with open(tmp_path, "wb") as f:
            f.write(photo_bytes)
        ig_safe(ig.direct_send_photo, tmp_path, [user_pk])

    if text:
        ig_safe(ig.direct_answer, thread_id, text)


# ─────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────
async def poll_instagram():
    log.info(f"Polling Instagram every {POLL_INTERVAL}s...")
    while True:
        try:
            if not db.get("bot_ready") or not db.get("auto_replies"):
                await asyncio.sleep(POLL_INTERVAL)
                continue

            threads = ig_safe(ig.direct_threads, amount=20) or []

            for thread in threads:
                thread_id = str(thread.id)
                messages  = ig_safe(ig.direct_messages, thread.id, amount=5) or []
                if not messages:
                    continue

                # Find the other user
                other_user = next(
                    (u for u in thread.users if str(u.pk) != str(ig.user_id)),
                    None
                )
                if not other_user:
                    continue

                # Track this thread for broadcast
                if thread_id not in db["all_thread_ids"]:
                    db["all_thread_ids"].append(thread_id)

                last_seen = db["seen_threads"].get(thread_id, "")

                for msg in reversed(messages):
                    msg_id = str(msg.id)

                    # Already processed
                    if msg_id == last_seen:
                        break

                    # Skip our own messages
                    if str(msg.user_id) == str(ig.user_id):
                        continue

                    # New message from other user — send auto reply
                    replies     = db["auto_replies"]
                    idx         = db["reply_counter"] % len(replies)
                    chosen      = replies[idx]
                    delay_secs  = get_reply_delay(idx)

                    log.info(
                        f"New DM from @{other_user.username} | "
                        f"reply #{idx+1} | delay {delay_secs}s"
                    )

                    await asyncio.sleep(delay_secs)
                    ig_send_reply(thread_id, other_user.pk, chosen)

                    db["reply_counter"] = (idx + 1) % len(replies)
                    db["seen_threads"][thread_id] = msg_id

                    # Notify admin on Telegram
                    preview = chosen.get("text", "")[:60] or "[photo only]"
                    await bot.send_message(
                        ADMIN_ID,
                        f"📨 <b>New DM</b> from @{other_user.username}\n"
                        f"↩️ Replied with message #{idx+1}:\n"
                        f"<i>{preview}</i>"
                    )
                    break  # one reply per thread per poll cycle

            await save_db()

        except Exception as e:
            log.error(f"Polling error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────
# /start — login flow
# ─────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    await state.clear()

    # Already logged in?
    if db.get("ig_session_b64") and db.get("ig_username"):
        if db.get("bot_ready") and db.get("auto_replies"):
            await message.answer(
                f"✅ Bot is already running!\n\n"
                f"👤 Instagram: @{db['ig_username']}\n"
                f"💬 Auto-replies set: {len(db['auto_replies'])}\n\n"
                f"Use /menu for options."
            )
        else:
            await message.answer(
                f"✅ Logged in as @{db['ig_username']}\n\n"
                f"⚠️ Auto-replies not configured yet.\n"
                f"Let's set them up now.\n\n"
                f"You need to add <b>minimum 5 messages</b>.\n"
                f"Send /setreply to begin."
            )
        return

    await message.answer(
        "👋 <b>Welcome to Instagram DM Auto-Reply Bot</b>\n\n"
        "Let's connect your Instagram account first.\n\n"
        "📧 Please send your <b>Instagram username</b>:"
    )
    await state.set_state(LoginStates.waiting_username)


# ─────────────────────────────────────────────
# Login — username
# ─────────────────────────────────────────────
@dp.message(LoginStates.waiting_username)
async def login_username(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    username = message.text.strip().lstrip("@")
    await state.update_data(username=username)
    await message.answer(
        f"✅ Username: <b>@{username}</b>\n\n"
        f"🔒 Now send your <b>Instagram password</b>:\n"
        f"<i>(This is stored securely in your database only)</i>"
    )
    await state.set_state(LoginStates.waiting_password)


# ─────────────────────────────────────────────
# Login — password
# ─────────────────────────────────────────────
@dp.message(LoginStates.waiting_password)
async def login_password(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    password = message.text.strip()
    data     = await state.get_data()
    username = data["username"]

    await message.answer("⏳ Logging in to Instagram...")

    # Delete the password message for security
    try:
        await message.delete()
    except Exception:
        pass

    result = await ig_login_with_credentials(username, password)

    if result.get("ok"):
        await state.clear()
        await message.answer(
            f"✅ <b>Logged in as @{username}!</b>\n\n"
            f"Now let's set up your auto-reply messages.\n"
            f"You need <b>minimum 5 messages</b>.\n\n"
            f"Send /setreply to begin."
        )

    elif result.get("otp"):
        await state.update_data(password=password)
        await message.answer(
            "📱 <b>Two-Factor Authentication required.</b>\n\n"
            "Please enter the OTP code from your authenticator app or SMS:"
        )
        await state.set_state(LoginStates.waiting_otp)

    else:
        await state.clear()
        await message.answer(
            f"❌ Login failed:\n<code>{result['error']}</code>\n\n"
            f"Send /start to try again."
        )


# ─────────────────────────────────────────────
# Login — OTP
# ─────────────────────────────────────────────
@dp.message(LoginStates.waiting_otp)
async def login_otp(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    otp  = message.text.strip()
    data = await state.get_data()

    await message.answer("⏳ Verifying OTP...")
    result = await ig_submit_otp(data["username"], data["password"], otp)

    if result.get("ok"):
        await state.clear()
        await message.answer(
            f"✅ <b>Logged in as @{data['username']}!</b>\n\n"
            f"Now set up your auto-reply messages.\n"
            f"Send /setreply to begin."
        )
    else:
        await message.answer(
            f"❌ OTP failed:\n<code>{result['error']}</code>\n\n"
            f"Send /start to try again."
        )
        await state.clear()


# ─────────────────────────────────────────────
# /setreply — collect minimum 5 messages
# ─────────────────────────────────────────────
@dp.message(Command("setreply"))
async def cmd_setreply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    await state.clear()
    await state.update_data(collecting=[], mode="setreply")

    await message.answer(
        "💬 <b>Set Auto-Reply Messages</b>\n\n"
        "Send your messages one by one.\n"
        "Each message can be <b>text only</b> or <b>photo with caption</b>.\n\n"
        "📌 <b>Minimum 5 messages required.</b>\n\n"
        "Send <b>message #1</b> now:\n"
        "<i>(Photo + caption = photo will be sent with the text)</i>"
    )
    await state.set_state(SetReplyStates.collecting)


@dp.message(SetReplyStates.collecting)
async def collect_reply_message(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data       = await state.get_data()
    collecting = data.get("collecting", [])
    mode       = data.get("mode", "setreply")
    min_count  = 5 if mode == "setreply" else 10

    # Parse the message
    text      = ""
    photo_b64 = ""

    if message.text and message.text.lower() == "done":
        if len(collecting) < min_count:
            await message.answer(
                f"⚠️ You've only added <b>{len(collecting)}</b> messages.\n"
                f"Minimum required: <b>{min_count}</b>\n\n"
                f"Please send more messages."
            )
            return
        await finalize_messages(message, state, collecting, mode)
        return

    if message.photo:
        photo_b64 = await download_photo_b64(message.photo[-1].file_id)
        text = message.caption or ""
    elif message.text:
        text = message.text.strip()
    else:
        await message.answer("⚠️ Please send text or a photo (with optional caption).")
        return

    if not text and not photo_b64:
        await message.answer("⚠️ Empty message. Please send text or a photo.")
        return

    collecting.append({"text": text, "photo_b64": photo_b64})
    await state.update_data(collecting=collecting)

    count     = len(collecting)
    remaining = max(0, min_count - count)

    preview = f"📝 {text[:40]}" if text else "🖼 Photo"
    if text and photo_b64:
        preview = f"🖼+📝 Photo + text"

    if remaining > 0:
        await message.answer(
            f"✅ <b>Message #{count} saved:</b> {preview}\n\n"
            f"📌 {remaining} more required. Send <b>message #{count+1}</b>:"
        )
    else:
        await message.answer(
            f"✅ <b>Message #{count} saved:</b> {preview}\n\n"
            f"You've reached the minimum {min_count}!\n"
            f"You can keep adding more, or send <b>done</b> to finish.\n\n"
            f"Send <b>message #{count+1}</b> or type <b>done</b>:"
        )


async def finalize_messages(message: Message, state: FSMContext, collecting: list, mode: str):
    await state.clear()

    if mode == "setreply":
        db["auto_replies"]   = collecting
        db["reply_counter"]  = 0
        db["bot_ready"]      = True
        await save_db()

        await message.answer(
            f"🎉 <b>Auto-replies configured!</b>\n\n"
            f"📨 Total messages: <b>{len(collecting)}</b>\n"
            f"🔄 They will rotate: msg1 → msg2 → ... → msg{len(collecting)} → msg1\n"
            f"⏱ Delays: 2s → 3s → 4s → 5s → 6s → repeat\n\n"
            f"✅ <b>Bot is now active and polling Instagram!</b>\n\n"
            f"Use /menu for all options."
        )
        log.info(f"Auto-replies set: {len(collecting)} messages. Bot is ready.")

    elif mode == "broadcast":
        db["broadcast_messages"] = collecting
        db["broadcast_counter"]  = 0
        await save_db()

        await message.answer(
            f"✅ <b>Broadcast messages saved!</b>\n\n"
            f"📢 Total: <b>{len(collecting)}</b> messages\n"
            f"🔄 They alternate per user (user1→msg1, user2→msg2...)\n\n"
            f"Ready to send? Use /broadcast to start.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📢 Send Broadcast Now", callback_data="confirm_broadcast")
            ]])
        )


# ─────────────────────────────────────────────
# /broadcast — set 10 messages then send
# ─────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if not db.get("bot_ready"):
        await message.answer("⚠️ Bot not ready. Complete /setreply first.")
        return

    if db.get("broadcast_messages"):
        await message.answer(
            f"📢 You already have <b>{len(db['broadcast_messages'])}</b> broadcast messages saved.\n\n"
            f"What do you want to do?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Send Existing Broadcast", callback_data="confirm_broadcast")],
                [InlineKeyboardButton(text="🔄 Set New Broadcast Messages", callback_data="new_broadcast")],
            ])
        )
        return

    await start_broadcast_collection(message, state)


async def start_broadcast_collection(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(collecting=[], mode="broadcast")

    await message.answer(
        "📢 <b>Set Broadcast Messages</b>\n\n"
        "These messages will be sent to <b>all existing DM contacts</b>.\n"
        "Each user gets a different message (alternating).\n\n"
        "📌 <b>Minimum 10 messages required.</b>\n\n"
        "Send <b>message #1</b> now:"
    )
    await state.set_state(BroadcastStates.collecting)


@dp.message(BroadcastStates.collecting)
async def collect_broadcast_message(message: Message, state: FSMContext):
    # Reuse same logic as setreply collector
    await collect_reply_message(message, state)


@dp.callback_query(F.data == "new_broadcast")
async def cb_new_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_reply_markup()
    await start_broadcast_collection(call.message, state)


@dp.callback_query(F.data == "confirm_broadcast")
async def cb_confirm_broadcast(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    broadcast_msgs = db.get("broadcast_messages", [])
    thread_ids     = db.get("all_thread_ids", [])

    if not broadcast_msgs:
        await call.answer("No broadcast messages set.", show_alert=True)
        return
    if not thread_ids:
        await call.answer("No DM threads found yet. Wait for first DMs to arrive.", show_alert=True)
        return

    await call.message.edit_reply_markup()
    await call.message.answer(
        f"📢 <b>Starting Broadcast</b>\n\n"
        f"📬 Sending to <b>{len(thread_ids)}</b> conversations\n"
        f"💬 Using <b>{len(broadcast_msgs)}</b> rotating messages\n\n"
        f"⏳ This may take a while..."
    )

    asyncio.create_task(run_broadcast(call.message, broadcast_msgs, thread_ids))


async def run_broadcast(message: Message, broadcast_msgs: list, thread_ids: list):
    success = 0
    failed  = 0
    counter = db.get("broadcast_counter", 0)

    for i, thread_id in enumerate(thread_ids):
        try:
            # Get thread info
            thread = ig_safe(ig.direct_thread, thread_id)
            if not thread:
                failed += 1
                continue

            other_user = next(
                (u for u in thread.users if str(u.pk) != str(ig.user_id)),
                None
            )
            if not other_user:
                failed += 1
                continue

            msg_idx = (counter + i) % len(broadcast_msgs)
            chosen  = broadcast_msgs[msg_idx]
            delay   = get_reply_delay(i)

            await asyncio.sleep(delay)
            ig_send_reply(thread_id, other_user.pk, chosen)
            success += 1

            log.info(f"Broadcast → @{other_user.username} (msg #{msg_idx+1})")

        except Exception as e:
            log.error(f"Broadcast error for thread {thread_id}: {e}")
            failed += 1

        # Small extra gap between each send
        await asyncio.sleep(2)

    db["broadcast_counter"] = (counter + len(thread_ids)) % len(broadcast_msgs)
    await save_db()

    await message.answer(
        f"✅ <b>Broadcast Complete!</b>\n\n"
        f"✔️ Sent: {success}\n"
        f"❌ Failed: {failed}"
    )


# ─────────────────────────────────────────────
# /menu
# ─────────────────────────────────────────────
@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    replies_count   = len(db.get("auto_replies", []))
    broadcast_count = len(db.get("broadcast_messages", []))
    thread_count    = len(db.get("all_thread_ids", []))
    ready           = db.get("bot_ready", False)
    ig_user         = db.get("ig_username", "not logged in")

    status_icon = "🟢" if ready else "🔴"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Set Auto-Reply Messages", callback_data="menu_setreply")],
        [InlineKeyboardButton(text="👁 View Current Replies",    callback_data="menu_viewreply")],
        [InlineKeyboardButton(text="📢 Broadcast",               callback_data="menu_broadcast")],
        [InlineKeyboardButton(text="📊 Status",                  callback_data="menu_status")],
        [InlineKeyboardButton(text="🔄 Re-Login Instagram",      callback_data="menu_relogin")],
    ])

    await message.answer(
        f"🤖 <b>Instagram DM Bot — Menu</b>\n\n"
        f"{status_icon} Status: {'Active' if ready else 'Not ready'}\n"
        f"👤 Instagram: @{ig_user}\n"
        f"💬 Auto-replies: {replies_count}\n"
        f"📢 Broadcast messages: {broadcast_count}\n"
        f"📬 Known DM threads: {thread_count}",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "menu_setreply")
async def cb_menu_setreply(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_reply_markup()
    await cmd_setreply(call.message, state)


@dp.callback_query(F.data == "menu_viewreply")
async def cb_menu_viewreply(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    replies = db.get("auto_replies", [])
    if not replies:
        await call.answer("No auto-replies set yet.", show_alert=True)
        return

    await call.message.edit_reply_markup()
    await call.message.answer(f"📋 <b>Current Auto-Reply Messages ({len(replies)} total):</b>")

    for i, r in enumerate(replies):
        text      = r.get("text", "") or "<i>No text</i>"
        photo_b64 = r.get("photo_b64", "")
        delay     = get_reply_delay(i)

        if photo_b64:
            photo_bytes = base64.b64decode(photo_b64)
            with open("/tmp/preview.jpg", "wb") as f:
                f.write(photo_bytes)
            await call.message.answer_photo(
                photo=open("/tmp/preview.jpg", "rb"),
                caption=f"<b>Message #{i+1}</b> (delay: {delay}s)\n{text}"
            )
        else:
            await call.message.answer(
                f"<b>Message #{i+1}</b> (delay: {delay}s)\n{text}"
            )


@dp.callback_query(F.data == "menu_broadcast")
async def cb_menu_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_reply_markup()
    await cmd_broadcast(call.message, state)


@dp.callback_query(F.data == "menu_status")
async def cb_menu_status(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    replies_count   = len(db.get("auto_replies", []))
    thread_count    = len(db.get("all_thread_ids", []))
    seen_count      = len(db.get("seen_threads", {}))
    reply_idx       = db.get("reply_counter", 0)
    ready           = db.get("bot_ready", False)

    await call.answer()
    await call.message.answer(
        f"📊 <b>Bot Status</b>\n\n"
        f"{'🟢 Active' if ready else '🔴 Not ready'}\n"
        f"👤 Instagram: @{db.get('ig_username', 'N/A')}\n"
        f"💬 Auto-reply messages: {replies_count}\n"
        f"🔢 Next reply index: #{reply_idx + 1}\n"
        f"📬 Known DM threads: {thread_count}\n"
        f"👁 Tracked threads: {seen_count}\n"
        f"⏱ Poll interval: {POLL_INTERVAL}s"
    )


@dp.callback_query(F.data == "menu_relogin")
async def cb_menu_relogin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_reply_markup()
    db["ig_session_b64"] = ""
    await save_db()
    await call.message.answer(
        "🔄 Session cleared.\n\n"
        "Send your <b>Instagram username</b>:"
    )
    await state.set_state(LoginStates.waiting_username)


# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────
async def download_photo_b64(file_id: str) -> str:
    """Download a Telegram photo and return as base64 string."""
    file = await bot.get_file(file_id)
    tmp  = "/tmp/tg_photo.jpg"
    await bot.download_file(file.file_path, destination=tmp)
    with open(tmp, "rb") as f:
        return base64.b64encode(f.read()).decode()


# ─────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────
@dp.message(Command("help"))
async def cmd_help(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🤖 <b>Instagram DM Auto-Reply Bot — Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start     — Login to Instagram\n"
        "/setreply  — Set auto-reply messages (min 5)\n"
        "/broadcast — Send broadcast to all DM contacts\n"
        "/menu      — Main menu with all options\n"
        "/help      — This message\n\n"
        "<b>How auto-reply works:</b>\n"
        "• Someone DMs you on Instagram\n"
        "• Bot waits 2-6s (cycling per message)\n"
        "• Sends the next message in your rotation\n"
        "• Rotates: msg1→msg2→...→msg5→msg1\n\n"
        "<b>How broadcast works:</b>\n"
        "• Set 10 messages\n"
        "• Each existing DM contact gets a different message\n"
        "• Alternates: contact1→msg1, contact2→msg2..."
    )


# ─────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────
async def on_startup():
    await load_db()
    log.info("Database loaded.")

    # Restore Instagram session if available
    if db.get("ig_session_b64") and db.get("ig_username"):
        try:
            settings = json.loads(base64.b64decode(db["ig_session_b64"]).decode())
            ig.set_settings(settings)
            ig.login(db["ig_username"], "")
            log.info(f"Instagram session restored for @{db['ig_username']}")
        except Exception as e:
            log.warning(f"Could not restore Instagram session: {e}")
            log.info("Admin will need to /start and re-login.")

    asyncio.create_task(poll_instagram())

    # Notify admin
    try:
        status = "✅ Running" if db.get("bot_ready") else "⚠️ Setup needed"
        await bot.send_message(
            ADMIN_ID,
            f"🤖 <b>Bot started!</b>\n\n"
            f"Status: {status}\n"
            f"Instagram: @{db.get('ig_username', 'not logged in')}\n\n"
            f"{'Use /menu to manage.' if db.get('bot_ready') else 'Use /start to login to Instagram.'}"
        )
    except Exception as e:
        log.warning(f"Could not notify admin: {e}")


async def main():
    dp.startup.register(on_startup)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
