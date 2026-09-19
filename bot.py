import asyncio
import logging
import os
import secrets
from contextlib import suppress

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Add Heroku Postgres and set DATABASE_URL.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

pool: asyncpg.Pool | None = None


async def db():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return pool


async def init_db():
    p = await db()
    async with p.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS required_chats (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            username TEXT,
            chat_type TEXT NOT NULL,
            join_url TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS contents (
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            source_chat_id BIGINT NOT NULL,
            source_message_id BIGINT NOT NULL,
            content_type TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS unlocks (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
            unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(user_id, content_id)
        );
        """)


def admin_only(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def save_user(message: Message):
    if not message.from_user:
        return
    p = await db()
    await p.execute(
        """
        INSERT INTO users(user_id, username, first_name)
        VALUES($1,$2,$3)
        ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name,
            last_seen=NOW()
        """,
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )


async def get_required_chats():
    p = await db()
    return await p.fetch(
        "SELECT * FROM required_chats WHERE active=TRUE ORDER BY sort_order, id"
    )


async def membership_ok(user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except Exception as e:
        logger.warning("Membership check failed for %s / %s: %s", user_id, chat_id, e)
        return False


async def missing_chats(user_id: int):
    chats = await get_required_chats()
    missing = []
    for chat in chats:
        if not await membership_ok(user_id, chat["chat_id"]):
            missing.append(chat)
    return missing


def join_keyboard(chats, code: str) -> InlineKeyboardMarkup:
    rows = []
    for i, chat in enumerate(chats, 1):
        rows.append([
            InlineKeyboardButton(
                text=f"➕ Join {i}. {chat['title'][:35]}",
                url=chat["join_url"],
            )
        ])
    rows.append([
        InlineKeyboardButton(text="🔄 CHECK & UNLOCK", callback_data=f"unlock:{code}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_lock_screen(target_chat_id: int, user_id: int, code: str):
    chats = await get_required_chats()
    if not chats:
        await bot.send_message(
            target_chat_id,
            "⚠️ The content owner has not configured any required channels yet."
        )
        return

    missing = await missing_chats(user_id)
    if not missing:
        await deliver_content(target_chat_id, code)
        return

    text = (
        "🔒 <b>CONTENT LOCKED</b>\n\n"
        "To unlock this content, join <b>all</b> of the required channels/groups below.\n\n"
        f"📢 Required: <b>{len(chats)}</b>\n"
        f"✅ Already joined: <b>{len(chats)-len(missing)}</b>\n"
        f"❌ Remaining: <b>{len(missing)}</b>\n\n"
        "After joining, tap <b>CHECK & UNLOCK</b>."
    )
    await bot.send_message(
        target_chat_id,
        text,
        reply_markup=join_keyboard(chats, code),
        disable_web_page_preview=True,
    )


async def deliver_content(target_chat_id: int, code: str):
    p = await db()
    row = await p.fetchrow(
        "SELECT * FROM contents WHERE code=$1 AND active=TRUE", code
    )
    if not row:
        await bot.send_message(target_chat_id, "❌ Content not found or has been disabled.")
        return

    await bot.copy_message(
        chat_id=target_chat_id,
        from_chat_id=row["source_chat_id"],
        message_id=row["source_message_id"],
    )


@router.message(CommandStart())
async def start(message: Message):
    await save_user(message)
    args = (message.text or "").split(maxsplit=1)
    code = args[1].strip() if len(args) > 1 else None

    if not code:
        await message.answer(
            "👋 <b>Content Lock Bot</b>\n\n"
            "Send me a valid content link to unlock protected content."
        )
        return

    p = await db()
    exists = await p.fetchrow(
        "SELECT id FROM contents WHERE code=$1 AND active=TRUE", code
    )
    if not exists:
        await message.answer("❌ Invalid or expired content link.")
        return

    await send_lock_screen(message.chat.id, message.from_user.id, code)


@router.callback_query(F.data.startswith("unlock:"))
async def unlock(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    missing = await missing_chats(user_id)
    if missing:
        names = "\n".join(f"• {x['title']}" for x in missing[:20])
        await callback.answer(
            f"You still need to join {len(missing)} chat(s).",
            show_alert=True,
        )
        with suppress(Exception):
            await callback.message.edit_text(
                "🔒 <b>Still locked</b>\n\n"
                "Please join the following:\n\n"
                f"{names}\n\n"
                "Then tap <b>CHECK & UNLOCK</b> again.",
                reply_markup=join_keyboard(missing, code),
            )
        return

    p = await db()
    row = await p.fetchrow(
        "SELECT id FROM contents WHERE code=$1 AND active=TRUE", code
    )
    if not row:
        await callback.answer("Content no longer exists.", show_alert=True)
        return

    await p.execute(
        """
        INSERT INTO unlocks(user_id, content_id)
        VALUES($1,$2)
        ON CONFLICT(user_id, content_id) DO NOTHING
        """,
        user_id,
        row["id"],
    )
    await callback.answer("✅ Verified! Unlocking content...")
    with suppress(Exception):
        await callback.message.delete()
    await deliver_content(callback.message.chat.id, code)


@router.message(Command("admin"))
async def admin(message: Message):
    if not admin_only(message):
        return
    await message.answer(
        "👑 <b>ADMIN PANEL</b>\n\n"
        "/addchat — add required channel/group\n"
        "/listchats — list required chats\n"
        "/removechat ID — remove a required chat\n"
        "/content — create content from the next message\n"
        "/listcontent — list locked contents\n"
        "/deletecontent CODE — disable content\n"
        "/stats — statistics\n\n"
        "<b>Add chat format:</b>\n"
        "<code>/addchat @username | https://t.me/username</code>\n\n"
        "For a private group/channel, use its numeric chat ID and invite link:\n"
        "<code>/addchat -1001234567890 | https://t.me/+invite</code>"
    )


@router.message(Command("addchat"))
async def addchat(message: Message):
    if not admin_only(message):
        return

    raw = (message.text or "").partition(" ")[2].strip()
    if "|" not in raw:
        await message.answer(
            "❌ Format:\n"
            "<code>/addchat @username | https://t.me/username</code>\n\n"
            "Private:\n"
            "<code>/addchat -1001234567890 | https://t.me/+invite</code>"
        )
        return

    chat_ref, join_url = [x.strip() for x in raw.split("|", 1)]
    try:
        chat = await bot.get_chat(chat_ref)
        if chat.type not in {ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP}:
            await message.answer("❌ Only channels/groups are supported.")
            return

        # Test that the bot can perform the membership check.
        await bot.get_chat_member(chat.id, ADMIN_ID)

        username = getattr(chat, "username", None)
        if not join_url:
            if username:
                join_url = f"https://t.me/{username}"
            else:
                await message.answer(
                    "❌ A private chat needs an invite URL. "
                    "Add it after the | character."
                )
                return

        p = await db()
        await p.execute(
            """
            INSERT INTO required_chats(chat_id,title,username,chat_type,join_url)
            VALUES($1,$2,$3,$4,$5)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=EXCLUDED.title,
                username=EXCLUDED.username,
                chat_type=EXCLUDED.chat_type,
                join_url=EXCLUDED.join_url,
                active=TRUE
            """,
            chat.id,
            chat.title or "Untitled",
            username,
            chat.type.value,
            join_url,
        )
        await message.answer(
            f"✅ Added:\n<b>{chat.title}</b>\n"
            f"ID: <code>{chat.id}</code>\n"
            f"Type: <code>{chat.type.value}</code>"
        )
    except Exception as e:
        logger.exception("addchat failed")
        await message.answer(
            "❌ Could not add this chat.\n\n"
            "Make sure the bot is an administrator in the channel/group "
            "and that the chat ID/username and invite URL are correct.\n\n"
            f"<code>{str(e)[:500]}</code>"
        )


@router.message(Command("listchats"))
async def listchats(message: Message):
    if not admin_only(message):
        return
    chats = await get_required_chats()
    if not chats:
        await message.answer("No required chats configured.")
        return

    lines = ["📢 <b>REQUIRED CHATS</b>\n"]
    for i, c in enumerate(chats, 1):
        lines.append(
            f"<b>{i}. {c['title']}</b>\n"
            f"DB ID: <code>{c['id']}</code>\n"
            f"Chat ID: <code>{c['chat_id']}</code>\n"
            f"Type: {c['chat_type']}\n"
        )
    await message.answer("\n".join(lines))


@router.message(Command("removechat"))
async def removechat(message: Message):
    if not admin_only(message):
        return
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg.isdigit():
        await message.answer("Usage: <code>/removechat 3</code>")
        return

    p = await db()
    result = await p.execute(
        "UPDATE required_chats SET active=FALSE WHERE id=$1", int(arg)
    )
    await message.answer("✅ Chat removed from the required list." if result.endswith("1")
                         else "❌ Chat ID not found.")


@router.message(Command("content"))
async def content_command_v2(message: Message):
    if not admin_only(message):
        return
    p = await db()
    await p.execute(
        "UPDATE users SET username='__CONTENT_PENDING__' WHERE user_id=$1",
        ADMIN_ID,
    )
    await message.answer(
        "📦 <b>CONTENT CREATION MODE ON</b>\n\n"
        "Send or forward ONE message now.\n"
        "Supported: text, photo, video, document, audio, voice, etc.\n\n"
        "After saving, the bot will return your unlock link."
    )


async def main():
    await init_db()
    await prepare_admin_state()
    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
