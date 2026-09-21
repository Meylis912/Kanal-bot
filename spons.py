"""
Kanal Medya Temizleyici Bot  (aiogram 3.x + Motor/MongoDB)

Ne yapar:
  * Bot kanala ADMIN yapılınca kanal otomatik kaydolur, kurucuya bildirim gider.
  * Ya da botta "Kanal ekle" butonuyla kanal elle eklenir.
  * Kayıtlı kanallarda foto / video / GIF / yuvarlak video atılınca ANINDA silinir.
  * Bir kanalın ayarlarını SADECE botu o kanala ekleyen kişi ve kanalın kurucusu (creator) yönetir.
  * Bot adminleri (kurucu + eklediği adminler) tüm kanallara ve tüm kullanıcılara REKLAM dağıtabilir.

ENV değişkenleri:
  BOT_TOKEN  - BotFather token
  OWNER_ID   - Kurucunun Telegram ID'si
  MONGO_URI  - MongoDB bağlantı adresi
  DB_NAME    - (opsiyonel) varsayılan: media_cleaner
  PORT       - (Render otomatik verir) Flask health-check sunucusu için

requirements.txt:
  aiogram>=3.13
  motor>=3.4
  flask
"""
import asyncio
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from html import escape
from collections import deque

import aiohttp
from flask import Flask
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageOriginUser,
)
from motor.motor_asyncio import AsyncIOMotorClient

# --------------------------------------------------------------------------
# Ayarlar
# --------------------------------------------------------------------------
# Değerler doğrudan yazıldı. Render'da BOT_TOKEN / OWNER_ID / MONGO_URI
# environment değişkeni tanımlarsan onlar öncelikli olur.
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8897093028:AAEFuBMy5D02PCL53CUnTuKQ5FZeBcxwOcU"
OWNER_ID = int(os.getenv("OWNER_ID") or 7523674506)
MONGO_URI = os.getenv("MONGO_URI") or "mongodb+srv://mergenowlyagulyyew41_db_user:ZvZhOKOAF6ZMRbHX@cluster1.l8z8gll.mongodb.net/?appName=Cluster1"
DB_NAME = os.getenv("DB_NAME", "media_cleaner")

# Render URL'in: buraya yaz (Render zaten RENDER_EXTERNAL_URL verir, o varsa otomatik kullanılır)
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL") or "https://tgakanalxns.onrender.com"
PING_INTERVAL = 300  # saniye (5 dakika)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cleaner")

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
admins_col = db["admins"]
channels_col = db["channels"]
users_col = db["users"]  # /start yapan herkes (reklam dağıtımı için)
ads_col = db["ads"]  # reklam gönderim geçmişi

# Hızlı erişim için bellek önbelleği (her kanal postunda DB'ye gitmemek için)
ADMINS: set[int] = set()
CHANNELS: dict[int, dict] = {}
AD_IDS: set = set()  # botun kendi gönderdiği reklam postları (silinmesin)
AD_ORDER: deque = deque()
BROADCAST = {"running": False}
BG_TASKS: set = set()
WARN_TS: dict[int, float] = {}  # kanal -> son uyarı zamanı (spam engeli)

# Silinecek medya türleri (kanal başına aç/kapa yapılır)
FIELDS = {
    "del_photo": "Foto",
    "del_video": "Video",
    "del_gif": "GIF",
    "del_round": "Yuvarlak video",
}

# Yönetici bazlı kurallar (her yönetici için ayrı aç/kapa)
ADMIN_FIELDS = {
    "del_photo": "Foto",
    "del_video": "Video",
    "del_gif": "GIF",
    "del_round": "Yuvarlak video",
    "del_text": "Yazı (SMS)",
    "del_all": "Her şey",
}
RULE_ICONS = {
    "del_photo": "🖼",
    "del_video": "🎬",
    "del_gif": "🎞",
    "del_round": "⭕",
    "del_text": "💬",
    "del_all": "🚫",
}


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------
def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def is_admin(uid: int) -> bool:
    return uid == OWNER_ID or uid in ADMINS


def can_advertise(uid: int) -> bool:
    """Reklam dağıtabilir: kurucu + kurucunun eklediği adminler."""
    return is_admin(uid)


def can_manage(uid: int, cfg: dict) -> bool:
    """Kanalı yönetebilir: SADECE botu ekleyen kişi ya da kanalın kurucusu (creator)."""
    return uid == cfg.get("added_by") or (cfg.get("creator_id") is not None and uid == cfg.get("creator_id"))


def has_access(uid: int) -> bool:
    """Botu kullanabilir: bot adminleri, botu kanala ekleyen ya da kanalın kurucusu."""
    return is_admin(uid) or any(can_manage(uid, c) for c in CHANNELS.values())


def visible_channels(uid: int) -> dict:
    """Kullanıcının yönetebildiği kanallar (bot adminleri bile başkasının kanalını yönetemez)."""
    return {cid: c for cid, c in CHANNELS.items() if can_manage(uid, c)}


def remember_ad(chat_id: int, message_id: int):
    key = (chat_id, message_id)
    AD_IDS.add(key)
    AD_ORDER.append(key)
    while len(AD_ORDER) > 5000:
        AD_IDS.discard(AD_ORDER.popleft())


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name)}</a>'


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


async def safe_edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def notify(bot: Bot, text: str, extra_ids=()):
    """Kurucuya (ve varsa ekleyen adminlere) bildirim gönderir."""
    for uid in {OWNER_ID, *extra_ids}:
        try:
            await bot.send_message(uid, text)
        except Exception as e:  # kullanıcı botu başlatmamış olabilir
            log.warning("Bildirim gönderilemedi (%s): %s", uid, e)


async def load_cache():
    ADMINS.clear()
    CHANNELS.clear()
    async for a in admins_col.find():
        ADMINS.add(a["_id"])
    async for c in channels_col.find():
        CHANNELS[c["_id"]] = c
    log.info("Yüklendi: %d admin, %d kanal", len(ADMINS), len(CHANNELS))


async def get_creator_id(bot: Bot, chat_id: int):
    """Kanalın kurucusunun (creator) Telegram ID'sini bulur."""
    try:
        for m in await bot.get_chat_administrators(chat_id):
            if m.status == ChatMemberStatus.CREATOR:
                return m.user.id
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    return None


async def register_channel(chat: Chat, added_by: int, creator_id=None) -> bool:
    """Kanalı kaydeder. Yeni eklendiyse True, zaten varsa False döner.
    Kanalı ilk ekleyen kişi (added_by) sonradan değiştirilemez."""
    existing = CHANNELS.get(chat.id)
    if existing:
        upd = {"title": chat.title, "username": chat.username}
        if creator_id:
            upd["creator_id"] = creator_id
        await channels_col.update_one({"_id": chat.id}, {"$set": upd})
        existing.update(upd)
        return False
    doc = {
        "_id": chat.id,
        "title": chat.title,
        "username": chat.username,
        "added_by": added_by,
        "creator_id": creator_id,
        "added_at": datetime.now(timezone.utc),
        "deleted": 0,
        **{f: True for f in FIELDS},
    }
    await channels_col.insert_one(doc)
    CHANNELS[chat.id] = doc
    return True


async def remove_channel(chat_id: int):
    await channels_col.delete_one({"_id": chat_id})
    CHANNELS.pop(chat_id, None)


def ch_line(cfg: dict) -> str:
    uname = f"@{cfg['username']}" if cfg.get("username") else "gizli kanal"
    return f"{escape(cfg.get('title') or 'Kanal')} ({uname})"


# --------------------------------------------------------------------------
# Klavyeler ve metinler
# --------------------------------------------------------------------------
MENU_TEXT = (
    "🛡 <b>Kanal Medya Temizleyici</b>\n\n"
    "Kayıtlı kanallarda paylaşılan <b>foto ve videoları</b> otomatik siler.\n\n"
    "Kanal eklemek için botu kanala <b>admin</b> yap (Mesaj silme yetkisi ver) "
    "ya da aşağıdaki <b>Kanal ekle</b> butonunu kullan."
)


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("➕ Kanal ekle", "add_channel"), btn("📋 Kanallarım", "channels")],
    ]
    if is_owner(uid):
        rows.append([btn("👥 Adminler", "admins")])
    if can_advertise(uid):
        rows.append([btn("📣 Reklam dağıt", "ad")])
    rows.append([btn("📊 İstatistik", "stats"), btn("ℹ️ Yardım", "help")])
    return kb(*rows)


def back_menu() -> InlineKeyboardMarkup:
    return kb([btn("⬅️ Menü", "menu")])


def channel_kb(cid: int, cfg: dict) -> InlineKeyboardMarkup:
    toggles = []
    for field, label in FIELDS.items():
        mark = "✅" if cfg.get(field, True) else "❌"
        toggles.append(btn(f"{mark} {label}", f"cht:{cid}:{field}"))
    mode_txt = "Herkes" if cfg.get("mode", "all") == "all" else "Sadece seçili yöneticiler"
    return kb(
        toggles[:2],
        toggles[2:],
        [btn(f"🎯 Mod: {mode_txt}", f"chm:{cid}")],
        [btn("👮 Kanal yöneticileri", f"cha:{cid}")],
        [btn("🔍 İzinleri kontrol et", f"chk:{cid}")],
        [btn("🗑 Kanalı kaldır", f"chrm:{cid}")],
        [btn("⬅️ Kanallar", "channels")],
    )


def channel_text(cfg: dict) -> str:
    mode = cfg.get("mode", "all")
    mode_info = (
        "🎯 <b>Mod: Herkes</b> — aşağıda ✅ olan türler herkesin postunda silinir. "
        "Yöneticiye özel kurallar bunun üstüne eklenir."
        if mode == "all"
        else "🎯 <b>Mod: Sadece seçili yöneticiler</b> — aşağıdaki türler kullanılmaz, "
        "sadece 👮 Yöneticiler menüsünde işaretlediklerinin mesajları silinir."
    )
    return (
        f"📢 <b>{escape(cfg.get('title') or 'Kanal')}</b>\n"
        f"🔗 {('@' + cfg['username']) if cfg.get('username') else 'gizli kanal'}\n"
        f"🆔 <code>{cfg['_id']}</code>\n"
        f"🗑 Silinen medya: <b>{cfg.get('deleted', 0)}</b>\n\n"
        f"{mode_info}"
    )


# --------------------------------------------------------------------------
# Filtreler / State
# --------------------------------------------------------------------------
class AdminFilter(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user) and has_access(user.id)


class Form(StatesGroup):
    add_channel = State()
    add_admin = State()
    ad_content = State()


# Kanal butonlarının callback_data'sı "<işlem>:<kanal_id>..." şeklinde.
# Her dokunuşta kullanıcının o kanalı yönetme yetkisi kontrol edilir.
CH_CB_RE = re.compile(r"^(?:ch|cht|chk|chrm|chry|chm|cha|cad|cadt|cadr):(-?\d+)")


class ChannelGuard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        m = CH_CB_RE.match(getattr(event, "data", None) or "")
        if m:
            cfg = CHANNELS.get(int(m.group(1)))
            if not cfg or not can_manage(event.from_user.id, cfg):
                await event.answer("⛔ Bu kanal için yetkin yok.", show_alert=True)
                return None
        return await handler(event, data)


events_router = Router(name="events")  # kanal olayları (admin filtresi yok)
admin_router = Router(name="admin")  # özel sohbet, sadece adminler
fallback_router = Router(name="fallback")  # yetkisiz kullanıcılar

admin_router.message.filter(F.chat.type == ChatType.PRIVATE, AdminFilter())
admin_router.callback_query.filter(AdminFilter())
admin_router.callback_query.middleware(ChannelGuard())


# --------------------------------------------------------------------------
# KANAL OLAYLARI: bot admin yapıldı / çıkarıldı
# --------------------------------------------------------------------------
@events_router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def on_bot_status(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    old, new = event.old_chat_member, event.new_chat_member
    who = event.from_user
    who_txt = mention(who.id, who.full_name) + f" (<code>{who.id}</code>)"
    creator = CHANNELS.get(chat.id, {}).get("creator_id")
    # bildirim: kurucu(bot sahibi) + botu ekleyen + kanalın kurucusu (botu /start yaptıysa gider)
    extra = [who.id] + ([creator] if creator else [])

    if new.status == ChatMemberStatus.ADMINISTRATOR:
        can_delete = bool(getattr(new, "can_delete_messages", False))
        was_admin = old.status == ChatMemberStatus.ADMINISTRATOR
        old_can = bool(getattr(old, "can_delete_messages", False))
        creator = await get_creator_id(bot, chat.id) or creator
        if creator and creator not in extra:
            extra.append(creator)
        is_new = await register_channel(chat, who.id, creator)

        # Yetki değişmediyse tekrar bildirim atma
        if was_admin and old_can == can_delete and not is_new:
            return

        perm = (
            "✅ Mesaj silme yetkisi: <b>var</b>"
            if can_delete
            else "⚠️ Mesaj silme yetkisi: <b>YOK</b>\n"
            "Botun çalışması için kanal yönetici ayarlarından "
            "<b>Mesajları sil</b> iznini aç!"
        )
        title = "✅ <b>Bot kanala admin yapıldı</b>" if is_new else "🔄 <b>Bot yetkileri güncellendi</b>"
        await notify(
            bot,
            f"{title}\n\n"
            f"📢 {ch_line(CHANNELS[chat.id])}\n"
            f"🆔 <code>{chat.id}</code>\n"
            f"👤 Ekleyen: {who_txt}\n\n{perm}\n\n"
            "Bu kanalda artık foto/video otomatik silinecek.",
            extra,
        )

    elif new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        if chat.id in CHANNELS:
            cfg = CHANNELS[chat.id]
            await remove_channel(chat.id)
            await notify(
                bot,
                f"❌ <b>Bot kanaldan çıkarıldı</b>\n\n"
                f"📢 {ch_line(cfg)}\n🆔 <code>{chat.id}</code>\n"
                f"👤 İşlemi yapan: {who_txt}\n\nKanal listeden kaldırıldı.",
                extra,
            )

    elif new.status == ChatMemberStatus.MEMBER and chat.id in CHANNELS:
        # Adminlikten düşürüldü
        await notify(
            bot,
            f"⚠️ <b>Bot adminlikten alındı</b>\n\n📢 {ch_line(CHANNELS[chat.id])}\n"
            f"👤 İşlemi yapan: {who_txt}\n\nSilme çalışmaz. Tekrar admin yap.",
            extra,
        )


# --------------------------------------------------------------------------
# KANAL POSTU: foto / video sil
# --------------------------------------------------------------------------
def find_rule(cfg: dict, signature: str | None):
    """Postun imzasına (author_signature) göre yöneticinin kuralını bulur."""
    if not signature:
        return None
    sig = signature.strip().lower()
    for rule in cfg.get("admin_rules", {}).values():
        if sig in [x.strip().lower() for x in rule.get("sigs", [])]:
            return rule
    return None


def should_delete(msg: Message, cfg: dict) -> bool:
    # 1) Yöneticiye özel kural
    rule = find_rule(cfg, msg.author_signature)
    if rule:
        if rule.get("del_all"):
            return True
        if msg.photo and rule.get("del_photo"):
            return True
        if msg.video and rule.get("del_video"):
            return True
        if msg.animation and rule.get("del_gif"):
            return True
        if msg.video_note and rule.get("del_round"):
            return True
        if msg.text and rule.get("del_text"):
            return True

    # 2) Kanal geneli kural (sadece "Herkes" modunda)
    if cfg.get("mode", "all") == "all":
        if msg.photo and cfg.get("del_photo", True):
            return True
        if msg.animation and cfg.get("del_gif", True):
            return True
        if msg.video and cfg.get("del_video", True):
            return True
        if msg.video_note and cfg.get("del_round", True):
            return True
    return False


@events_router.channel_post()
@events_router.edited_channel_post()
async def on_channel_post(message: Message, bot: Bot):
    if (message.chat.id, message.message_id) in AD_IDS:
        return  # botun kendi gönderdiği reklam
    cfg = CHANNELS.get(message.chat.id)
    if not cfg or not should_delete(message, cfg):
        return

    for _ in range(2):
        try:
            await message.delete()
            await channels_col.update_one({"_id": message.chat.id}, {"$inc": {"deleted": 1}})
            cfg["deleted"] = cfg.get("deleted", 0) + 1
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            if "not found" in str(e).lower():
                return
            log.warning("Silinemedi %s: %s", message.chat.id, e)
            now = time.time()
            if now - WARN_TS.get(message.chat.id, 0) > 600:
                WARN_TS[message.chat.id] = now
                await notify(
                    bot,
                    f"⚠️ <b>Medya silinemedi</b>\n\n📢 {ch_line(cfg)}\n"
                    "Botun bu kanalda <b>Mesajları sil</b> yetkisi olduğundan emin ol.\n"
                    f"<i>{escape(str(e))}</i>",
                )
            return


# --------------------------------------------------------------------------
# ÖZEL SOHBET: /start ve menü
# --------------------------------------------------------------------------
@admin_router.message(CommandStart())
@admin_router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await track_user(message.from_user)
    await message.answer(MENU_TEXT, reply_markup=main_menu(message.from_user.id))


@admin_router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, MENU_TEXT, main_menu(call.from_user.id))
    await call.answer()


@admin_router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    text = (
        "ℹ️ <b>Nasıl çalışır?</b>\n\n"
        "1️⃣ Botu kanalına <b>yönetici</b> yap.\n"
        "2️⃣ <b>Mesajları sil</b> iznini aç.\n"
        "3️⃣ Kanal otomatik kaydolur, kurucuya bildirim gelir.\n\n"
        "Otomatik olmadıysa: <b>➕ Kanal ekle</b> → kanaldan bir mesajı bana ilet "
        "ya da @kullaniciadi / -100... ID gönder.\n\n"
        "Kanal ayarlarından foto, video, GIF, yuvarlak video silmeyi ayrı ayrı aç/kapa yapabilirsin."
    )
    await safe_edit(call, text, back_menu())
    await call.answer()


@admin_router.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    uid = call.from_user.id
    chans = visible_channels(uid)
    total = sum(c.get("deleted", 0) for c in chans.values())
    text = (
        "📊 <b>İstatistik</b>\n\n"
        f"📢 Yönettiğin kanal: <b>{len(chans)}</b>\n"
        f"🗑 Silinen medya: <b>{total}</b>"
    )
    if is_admin(uid):
        n_users = await users_col.count_documents({})
        n_blocked = await users_col.count_documents({"blocked": True})
        text += (
            f"\n\n🌐 Botun olduğu toplam kanal: <b>{len(CHANNELS)}</b>"
            f"\n👤 Kullanıcı: <b>{n_users}</b> (botu engelleyen: {n_blocked})"
            f"\n👥 Bot admini: <b>{len(ADMINS) + 1}</b> (kurucu dahil)"
        )
    await safe_edit(call, text, back_menu())
    await call.answer()


# --------------------------------------------------------------------------
# KANAL EKLE (elle)
# --------------------------------------------------------------------------
@admin_router.callback_query(F.data == "add_channel")
async def cb_add_channel(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_channel)
    text = (
        "➕ <b>Kanal ekle</b>\n\n"
        "1️⃣ Önce botu kanala <b>admin</b> yap (Mesajları sil izni ver).\n"
        "2️⃣ Sonra şunlardan birini gönder:\n"
        "• Kanaldan bir mesajı <b>ilet</b>\n"
        "• Kanalın <code>@kullaniciadi</code>\n"
        "• Kanal ID'si (<code>-100...</code>)"
    )
    await safe_edit(call, text, kb([btn("❌ İptal", "menu")]))
    await call.answer()


async def resolve_channel(bot: Bot, msg: Message) -> Chat | None:
    fo = msg.forward_origin
    if isinstance(fo, MessageOriginChannel):
        return fo.chat
    text = (msg.text or "").strip()
    if not text:
        return None
    if "t.me/" in text:
        text = "@" + text.rstrip("/").split("/")[-1]
    try:
        if text.startswith("@"):
            return await bot.get_chat(text)
        if re.fullmatch(r"-100\d+", text):
            return await bot.get_chat(int(text))
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    return None


@admin_router.message(Form.add_channel)
async def msg_add_channel(message: Message, state: FSMContext, bot: Bot):
    chat = await resolve_channel(bot, message)
    if not chat or chat.type != ChatType.CHANNEL:
        await message.answer(
            "❌ Kanal bulunamadı. Kanaldan mesaj ilet ya da @kullaniciadi / -100... ID gönder.\n"
            "Bot kanalda olmalı.",
            reply_markup=kb([btn("❌ İptal", "menu")]),
        )
        return

    # Bot kanalda admin mi ve silme yetkisi var mı?
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("❌ Bot bu kanalda değil. Önce botu kanala admin yap.",
                             reply_markup=kb([btn("❌ İptal", "menu")]))
        return
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        await message.answer("❌ Bot bu kanalda <b>admin değil</b>. Önce admin yap, sonra tekrar dene.",
                             reply_markup=kb([btn("❌ İptal", "menu")]))
        return

    # Kullanıcı gerçekten bu kanalın yöneticisi mi? (güvenlik: herkes için zorunlu)
    try:
        u = await bot.get_chat_member(chat.id, message.from_user.id)
        ok = u.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except (TelegramBadRequest, TelegramForbiddenError):
        ok = False
    if not ok:
        await message.answer("❌ Bu kanalın yöneticisi değilsin.", reply_markup=back_menu())
        return

    is_new = await register_channel(chat, message.from_user.id, await get_creator_id(bot, chat.id))
    can_delete = bool(getattr(me, "can_delete_messages", False))
    await state.clear()
    warn = "" if can_delete else "\n\n⚠️ <b>Mesajları sil</b> yetkisi yok! Kanal yönetici ayarlarından aç."
    await message.answer(
        f"{'✅ Kanal eklendi' if is_new else 'ℹ️ Kanal zaten kayıtlıydı, güncellendi'}: "
        f"{ch_line(CHANNELS[chat.id])}{warn}",
        reply_markup=kb([btn("📋 Kanallarım", "channels")], [btn("⬅️ Menü", "menu")]),
    )
    if is_new and not is_owner(message.from_user.id):
        await notify(
            bot,
            f"✅ <b>Kanal eklendi</b> (butonla)\n\n📢 {ch_line(CHANNELS[chat.id])}\n"
            f"👤 Ekleyen: {mention(message.from_user.id, message.from_user.full_name)}",
        )


# --------------------------------------------------------------------------
# KANALLARIM
# --------------------------------------------------------------------------
@admin_router.callback_query(F.data == "channels")
async def cb_channels(call: CallbackQuery):
    chans = visible_channels(call.from_user.id)
    if not chans:
        await safe_edit(
            call,
            "📋 Henüz kanalın yok.\n\nBotu kanalına admin yap (Mesajları sil yetkisiyle) "
            "ya da <b>➕ Kanal ekle</b> kullan.",
            kb([btn("➕ Kanal ekle", "add_channel")], [btn("⬅️ Menü", "menu")]),
        )
        return await call.answer()
    rows = [
        [btn(f"📢 {c.get('title') or c['_id']}", f"ch:{cid}")]
        for cid, c in chans.items()
    ]
    rows.append([btn("⬅️ Menü", "menu")])
    await safe_edit(call, f"📋 <b>Kanallarım</b> ({len(chans)})", kb(*rows))
    await call.answer()


@admin_router.callback_query(F.data.startswith("ch:"))
async def cb_channel(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    cfg = CHANNELS.get(cid)
    if not cfg:
        return await call.answer("Kanal bulunamadı.", show_alert=True)
    await safe_edit(call, channel_text(cfg), channel_kb(cid, cfg))
    await call.answer()


@admin_router.callback_query(F.data.startswith("cht:"))
async def cb_toggle(call: CallbackQuery):
    _, cid, field = call.data.split(":")
    cid = int(cid)
    cfg = CHANNELS.get(cid)
    if not cfg or field not in FIELDS:
        return await call.answer("Geçersiz.", show_alert=True)
    new_val = not cfg.get(field, True)
    cfg[field] = new_val
    await channels_col.update_one({"_id": cid}, {"$set": {field: new_val}})
    await safe_edit(call, channel_text(cfg), channel_kb(cid, cfg))
    await call.answer(f"{FIELDS[field]}: {'silinecek' if new_val else 'silinmeyecek'}")


@admin_router.callback_query(F.data.startswith("chk:"))
async def cb_check(call: CallbackQuery, bot: Bot):
    cid = int(call.data.split(":")[1])
    try:
        me = await bot.get_chat_member(cid, bot.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return await call.answer("❌ Bot kanala erişemiyor.", show_alert=True)
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        return await call.answer("❌ Bot admin değil.", show_alert=True)
    if getattr(me, "can_delete_messages", False):
        await call.answer("✅ Bot admin ve mesaj silebilir.", show_alert=True)
    else:
        await call.answer("⚠️ Bot admin ama 'Mesajları sil' yetkisi yok!", show_alert=True)


@admin_router.callback_query(F.data.startswith("chrm:"))
async def cb_remove_ask(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    cfg = CHANNELS.get(cid)
    if not cfg:
        return await call.answer("Kanal bulunamadı.", show_alert=True)
    await safe_edit(
        call,
        f"🗑 <b>{escape(cfg.get('title') or '')}</b> kanalı kaldırılsın mı?\n"
        "Bot kanaldan da çıkar.",
        kb([btn("✅ Evet, kaldır", f"chry:{cid}"), btn("❌ Vazgeç", f"ch:{cid}")]),
    )
    await call.answer()


@admin_router.callback_query(F.data.startswith("chry:"))
async def cb_remove_yes(call: CallbackQuery, bot: Bot):
    cid = int(call.data.split(":")[1])
    cfg = CHANNELS.get(cid)
    await remove_channel(cid)
    try:
        await bot.leave_chat(cid)
    except Exception as e:
        log.warning("leave_chat hata: %s", e)
    await safe_edit(
        call,
        f"✅ Kaldırıldı: {ch_line(cfg) if cfg else cid}",
        kb([btn("📋 Kanallarım", "channels")], [btn("⬅️ Menü", "menu")]),
    )
    await call.answer()


# --------------------------------------------------------------------------
# KANAL YÖNETİCİLERİ: mod + yöneticiye özel silme kuralları
# --------------------------------------------------------------------------
def rule_summary(rule) -> str:
    if not rule:
        return "—"
    return "".join(i for f, i in RULE_ICONS.items() if rule.get(f)) or "—"


def ensure_rule(cfg: dict, user, custom_title) -> dict:
    rules = cfg.setdefault("admin_rules", {})
    rule = rules.setdefault(str(user.id), {f: False for f in ADMIN_FIELDS})
    sigs = [user.full_name]
    if custom_title:
        sigs.append(custom_title)
    rule["name"] = user.full_name
    rule["sigs"] = sigs
    return rule


async def save_rule(cid: int, uid: int):
    rule = CHANNELS[cid]["admin_rules"][str(uid)]
    await channels_col.update_one({"_id": cid}, {"$set": {f"admin_rules.{uid}": rule}})


async def render_ch_admin(call: CallbackQuery, cid: int, uid: int):
    cfg = CHANNELS[cid]
    rule = cfg["admin_rules"][str(uid)]
    toggles = [
        btn(f"{'✅' if rule.get(f) else '❌'} {label}", f"cadt:{cid}:{uid}:{f}")
        for f, label in ADMIN_FIELDS.items()
    ]
    rows = [toggles[i : i + 2] for i in range(0, len(toggles), 2)]
    rows.append([btn("🧹 Hepsini kapat", f"cadr:{cid}:{uid}")])
    rows.append([btn("⬅️ Yöneticiler", f"cha:{cid}")])
    sigs = ", ".join(escape(x) for x in rule.get("sigs", []))
    mode = "Herkes" if cfg.get("mode", "all") == "all" else "Sadece seçili yöneticiler"
    text = (
        f"👤 <b>{escape(rule.get('name', ''))}</b>\n"
        f"📢 {ch_line(cfg)}\n"
        f"🎯 Kanal modu: <b>{mode}</b>\n"
        f"✍️ İmza eşleşmesi: <code>{sigs}</code>\n\n"
        "✅ olan türde bu yöneticinin attığı mesajlar <b>otomatik silinir</b>.\n"
        "<b>Her şey</b> açıksa attığı tüm mesajlar silinir.\n\n"
        "⚠️ Bot kimin attığını sadece kanalda <b>Mesajları imzala</b> açıksa bilir."
    )
    await safe_edit(call, text, kb(*rows))


@admin_router.callback_query(F.data.startswith("chm:"))
async def cb_mode(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    cfg = CHANNELS.get(cid)
    if not cfg:
        return await call.answer("Kanal bulunamadı.", show_alert=True)
    cfg["mode"] = "selected" if cfg.get("mode", "all") == "all" else "all"
    await channels_col.update_one({"_id": cid}, {"$set": {"mode": cfg["mode"]}})
    await safe_edit(call, channel_text(cfg), channel_kb(cid, cfg))
    await call.answer("Mod: " + ("Herkes" if cfg["mode"] == "all" else "Sadece seçili yöneticiler"))


@admin_router.callback_query(F.data.startswith("cha:"))
async def cb_ch_admins(call: CallbackQuery, bot: Bot):
    cid = int(call.data.split(":")[1])
    cfg = CHANNELS.get(cid)
    if not cfg:
        return await call.answer("Kanal bulunamadı.", show_alert=True)
    try:
        members = await bot.get_chat_administrators(cid)
    except (TelegramBadRequest, TelegramForbiddenError):
        return await call.answer("❌ Yöneticiler alınamadı. Bot kanalda admin mi?", show_alert=True)
    rows = []
    for m in members:
        u = m.user
        if u.is_bot:
            continue
        crown = "👑" if m.status == ChatMemberStatus.CREATOR else "👤"
        rule = cfg.get("admin_rules", {}).get(str(u.id))
        rows.append([btn(f"{crown} {u.full_name} {rule_summary(rule)}"[:60], f"cad:{cid}:{u.id}")])
    rows.append([btn("⬅️ Kanal", f"ch:{cid}")])
    text = (
        f"👮 <b>{escape(cfg.get('title') or 'Kanal')}</b> yöneticileri ({len(rows) - 1})\n\n"
        "Bir yöneticiye dokun, hangi mesajlarının silineceğini seç.\n"
        "Yanındaki simgeler açık kuralları gösterir: 🖼 foto, 🎬 video, 🎞 GIF, ⭕ yuvarlak, 💬 yazı, 🚫 her şey.\n\n"
        "⚠️ Kimin attığı sadece kanalda <b>Mesajları imzala</b> açıksa bilinir."
    )
    await safe_edit(call, text, kb(*rows))
    await call.answer()


@admin_router.callback_query(F.data.startswith("cad:"))
async def cb_ch_admin(call: CallbackQuery, bot: Bot):
    _, cid, uid = call.data.split(":")
    cid, uid = int(cid), int(uid)
    cfg = CHANNELS.get(cid)
    if not cfg:
        return await call.answer("Kanal bulunamadı.", show_alert=True)
    try:
        m = await bot.get_chat_member(cid, uid)
    except (TelegramBadRequest, TelegramForbiddenError):
        return await call.answer("❌ Yönetici bilgisi alınamadı.", show_alert=True)
    ensure_rule(cfg, m.user, getattr(m, "custom_title", None))
    await save_rule(cid, uid)
    await render_ch_admin(call, cid, uid)
    await call.answer()


@admin_router.callback_query(F.data.startswith("cadt:"))
async def cb_ch_admin_toggle(call: CallbackQuery):
    _, cid, uid, field = call.data.split(":")
    cid, uid = int(cid), int(uid)
    cfg = CHANNELS.get(cid)
    if not cfg or field not in ADMIN_FIELDS or str(uid) not in cfg.get("admin_rules", {}):
        return await call.answer("Geçersiz.", show_alert=True)
    rule = cfg["admin_rules"][str(uid)]
    rule[field] = not rule.get(field, False)
    await save_rule(cid, uid)
    await render_ch_admin(call, cid, uid)
    await call.answer(f"{ADMIN_FIELDS[field]}: {'silinecek' if rule[field] else 'silinmeyecek'}")


@admin_router.callback_query(F.data.startswith("cadr:"))
async def cb_ch_admin_reset(call: CallbackQuery):
    _, cid, uid = call.data.split(":")
    cid, uid = int(cid), int(uid)
    cfg = CHANNELS.get(cid)
    if not cfg or str(uid) not in cfg.get("admin_rules", {}):
        return await call.answer("Geçersiz.", show_alert=True)
    for f in ADMIN_FIELDS:
        cfg["admin_rules"][str(uid)][f] = False
    await save_rule(cid, uid)
    await render_ch_admin(call, cid, uid)
    await call.answer("Tüm kurallar kapatıldı")


# --------------------------------------------------------------------------
# KULLANICI TAKİBİ (reklam için /start yapanlar)
# --------------------------------------------------------------------------
async def track_user(user):
    if not user or user.is_bot:
        return
    now = datetime.now(timezone.utc)
    await users_col.update_one(
        {"_id": user.id},
        {
            "$set": {
                "name": user.full_name,
                "username": user.username,
                "blocked": False,
                "last_seen": now,
            },
            "$setOnInsert": {"joined": now},
        },
        upsert=True,
    )


@events_router.my_chat_member(F.chat.type == ChatType.PRIVATE)
async def on_private_status(event: ChatMemberUpdated):
    """Kullanıcı botu engellerse/engeli kaldırırsa listeyi güncelle."""
    if event.new_chat_member.status in (ChatMemberStatus.KICKED, ChatMemberStatus.LEFT):
        await users_col.update_one({"_id": event.chat.id}, {"$set": {"blocked": True}})
    else:
        await track_user(event.from_user)


# --------------------------------------------------------------------------
# REKLAM DAĞITIMI (kurucu + adminler): tüm kanallara ve/veya tüm kullanıcılara
# --------------------------------------------------------------------------
def ad_counts() -> int:
    return len(CHANNELS)


async def send_copy(bot: Bot, chat_id: int, from_chat: int, mid: int):
    """Mesajı kopyalar (foto/video/yazı olduğu gibi). (yeni_mesaj_id, hata) döner."""
    for _ in range(3):
        try:
            res = await bot.copy_message(chat_id=chat_id, from_chat_id=from_chat, message_id=mid)
            return res.message_id, None
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramForbiddenError:
            return None, "forbidden"
        except TelegramBadRequest as e:
            return None, str(e)
    return None, "çok fazla istek (retry limit)"


async def run_broadcast(bot: Bot, admin_id: int, status: Message, from_chat: int, mid: int, target: str):
    BROADCAST["running"] = True
    ch_ok = ch_fail = us_ok = us_fail = 0
    failed = []
    try:
        channels = (
            [(cid, c) for cid, c in CHANNELS.items()]
            if target in ("ch", "all")
            else []
        )
        users = (
            [u["_id"] async for u in users_col.find({"blocked": {"$ne": True}}, {"_id": 1})]
            if target in ("us", "all")
            else []
        )
        users = [u for u in users if u != admin_id]

        async def progress():
            try:
                await status.edit_text(
                    "⏳ <b>Gönderiliyor...</b>\n\n"
                    f"📢 Kanal: {ch_ok + ch_fail}/{len(channels)}\n"
                    f"👤 Kullanıcı: {us_ok + us_fail}/{len(users)}"
                )
            except Exception:
                pass

        for i, (cid, cfg) in enumerate(channels, 1):
            new_id, err = await send_copy(bot, cid, from_chat, mid)
            if new_id:
                remember_ad(cid, new_id)
                ch_ok += 1
            else:
                ch_fail += 1
                failed.append(f"• {ch_line(cfg)} — {escape((err or '')[:70])}")
            if i % 20 == 0:
                await progress()
            await asyncio.sleep(0.06)

        for i, uid in enumerate(users, 1):
            new_id, err = await send_copy(bot, uid, from_chat, mid)
            if new_id:
                us_ok += 1
            else:
                us_fail += 1
                if err == "forbidden":
                    await users_col.update_one({"_id": uid}, {"$set": {"blocked": True}})
            if i % 25 == 0:
                await progress()
            await asyncio.sleep(0.05)

        await ads_col.insert_one(
            {
                "by": admin_id,
                "at": datetime.now(timezone.utc),
                "target": target,
                "ch_ok": ch_ok,
                "ch_fail": ch_fail,
                "us_ok": us_ok,
                "us_fail": us_fail,
            }
        )
        text = (
            "✅ <b>Reklam gönderimi bitti</b>\n\n"
            f"📢 Kanal: <b>{ch_ok}</b> başarılı, {ch_fail} hatalı\n"
            f"👤 Kullanıcı: <b>{us_ok}</b> başarılı, {us_fail} hatalı"
        )
        if failed:
            text += "\n\n⚠️ <b>Hatalı kanallar</b> (ilk 10):\n" + "\n".join(failed[:10])
            text += "\n\nİpucu: Botun kanalda <b>Mesaj gönder</b> yetkisi olmalı."
        try:
            await status.edit_text(text, reply_markup=back_menu())
        except Exception:
            await bot.send_message(admin_id, text, reply_markup=back_menu())
    except Exception as e:
        log.exception("Reklam gönderimi hata")
        await notify(bot, f"❌ Reklam gönderimi hata verdi: <code>{escape(str(e))}</code>", [admin_id])
    finally:
        BROADCAST["running"] = False


@admin_router.callback_query(F.data == "ad")
async def cb_ad(call: CallbackQuery, state: FSMContext):
    if not can_advertise(call.from_user.id):
        return await call.answer("⛔ Reklam vermek için admin olmalısın.", show_alert=True)
    if BROADCAST["running"]:
        return await call.answer("⏳ Devam eden bir reklam gönderimi var, bitmesini bekle.", show_alert=True)
    await state.set_state(Form.ad_content)
    await safe_edit(
        call,
        "📣 <b>Reklam dağıt</b>\n\n"
        "Reklamı <b>tek mesaj</b> olarak gönder: yazı, foto, video ya da GIF "
        "(altyazı, biçim ve linkler olduğu gibi korunur).\n\n"
        "Sonra kime gideceğini seçip onaylayacaksın.\n"
        "<i>Not: Albüm (birden fazla foto) desteklenmez.</i>",
        kb([btn("❌ İptal", "menu")]),
    )
    await call.answer()


@admin_router.message(Form.ad_content)
async def msg_ad_content(message: Message, state: FSMContext):
    if not can_advertise(message.from_user.id):
        await state.clear()
        return
    if message.media_group_id:
        return await message.answer(
            "❌ Albüm desteklenmiyor. Reklamı tek mesaj olarak gönder.",
            reply_markup=kb([btn("❌ İptal", "menu")]),
        )
    await state.update_data(ad_chat=message.chat.id, ad_mid=message.message_id)
    n_ch = ad_counts()
    n_us = await users_col.count_documents({"blocked": {"$ne": True}})
    await message.reply(
        "👆 Reklam bu. Kime gönderilsin?\n\n"
        f"📢 Kanal: <b>{n_ch}</b> (botun olduğu tüm kanallar)\n"
        f"👤 Kullanıcı: <b>{n_us}</b> (botu başlatıp engellemeyenler)\n\n"
        "<i>Beğenmediysen yeni bir mesaj göndererek reklamı değiştirebilirsin.</i>",
        reply_markup=kb(
            [btn(f"📢 Sadece kanallara ({n_ch})", "adt:ch")],
            [btn(f"👤 Sadece kullanıcılara ({n_us})", "adt:us")],
            [btn("🌍 İkisine de", "adt:all")],
            [btn("❌ İptal", "menu")],
        ),
    )


@admin_router.callback_query(F.data.startswith("adt:"))
async def cb_ad_target(call: CallbackQuery, state: FSMContext):
    if not can_advertise(call.from_user.id):
        return await call.answer("⛔ Yetkin yok.", show_alert=True)
    data = await state.get_data()
    target = call.data.split(":")[1]
    if "ad_mid" not in data or target not in ("ch", "us", "all"):
        return await call.answer("Önce reklamı mesaj olarak gönder.", show_alert=True)
    await state.update_data(ad_target=target)
    n_ch = ad_counts() if target in ("ch", "all") else 0
    n_us = await users_col.count_documents({"blocked": {"$ne": True}}) if target in ("us", "all") else 0
    await safe_edit(
        call,
        "⚠️ <b>Onay</b>\n\n"
        f"Reklam <b>{n_ch}</b> kanala ve <b>{n_us}</b> kullanıcıya gönderilecek.\n"
        "Gönderim başlayınca geri alınamaz. Emin misin?",
        kb([btn("✅ Evet, gönder", "adgo")], [btn("❌ İptal", "menu")]),
    )
    await call.answer()


@admin_router.callback_query(F.data == "adgo")
async def cb_ad_go(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not can_advertise(call.from_user.id):
        return await call.answer("⛔ Yetkin yok.", show_alert=True)
    if BROADCAST["running"]:
        return await call.answer("⏳ Devam eden bir gönderim var.", show_alert=True)
    data = await state.get_data()
    if "ad_mid" not in data or "ad_target" not in data:
        return await call.answer("Oturum bitti, baştan başla.", show_alert=True)
    await state.clear()
    await safe_edit(call, "⏳ <b>Gönderim başladı...</b>", back_menu())
    await call.answer("Gönderim başladı")
    task = asyncio.create_task(
        run_broadcast(bot, call.from_user.id, call.message, data["ad_chat"], data["ad_mid"], data["ad_target"])
    )
    BG_TASKS.add(task)
    task.add_done_callback(BG_TASKS.discard)


# --------------------------------------------------------------------------
# ADMİN YÖNETİMİ (sadece kurucu)
# --------------------------------------------------------------------------
async def owner_only(call: CallbackQuery) -> bool:
    if not is_owner(call.from_user.id):
        await call.answer("Bu işlem sadece kurucu içindir.", show_alert=True)
        return False
    return True


async def render_admins(call: CallbackQuery):
    rows = []
    async for a in admins_col.find():
        rows.append([btn(f"🗑 {a.get('name') or a['_id']} ({a['_id']})", f"adm_del:{a['_id']}")])
    rows.append([btn("➕ Admin ekle", "adm_add")])
    rows.append([btn("⬅️ Menü", "menu")])
    text = (
        "👥 <b>Adminler</b>\n\n"
        f"👑 Kurucu: <code>{OWNER_ID}</code>\n"
        f"Ek adminler: <b>{len(ADMINS)}</b>\n\n"
        "Silmek için adminin üstüne bas."
    )
    await safe_edit(call, text, kb(*rows))


@admin_router.callback_query(F.data == "admins")
async def cb_admins(call: CallbackQuery):
    if not await owner_only(call):
        return
    await render_admins(call)
    await call.answer()


@admin_router.callback_query(F.data == "adm_add")
async def cb_adm_add(call: CallbackQuery, state: FSMContext):
    if not await owner_only(call):
        return
    await state.set_state(Form.add_admin)
    await safe_edit(
        call,
        "➕ <b>Admin ekle</b>\n\nYeni adminin <b>Telegram ID</b>'sini yaz "
        "ya da onun bir mesajını bana <b>ilet</b>.\n\n"
        "<i>Not: Yeni admin botu bir kez /start yapmalı.</i>",
        kb([btn("❌ İptal", "admins")]),
    )
    await call.answer()


@admin_router.message(Form.add_admin)
async def msg_add_admin(message: Message, state: FSMContext, bot: Bot):
    if not is_owner(message.from_user.id):
        return
    uid, name = None, None
    fo = message.forward_origin
    if isinstance(fo, MessageOriginUser):
        uid, name = fo.sender_user.id, fo.sender_user.full_name
    elif message.text and message.text.strip().lstrip("-").isdigit():
        uid = int(message.text.strip())
        try:
            name = (await bot.get_chat(uid)).full_name
        except Exception:
            name = str(uid)
    if uid is None:
        return await message.answer(
            "❌ Geçersiz. ID yaz ya da kullanıcının mesajını ilet.\n"
            "(Profili gizli kullanıcılarda iletme çalışmaz, ID yaz.)",
            reply_markup=kb([btn("❌ İptal", "admins")]),
        )
    if uid == OWNER_ID:
        return await message.answer("Zaten kurucusun 👑", reply_markup=kb([btn("⬅️ Adminler", "admins")]))
    if uid in ADMINS:
        return await message.answer("Bu kişi zaten admin.", reply_markup=kb([btn("⬅️ Adminler", "admins")]))

    await admins_col.insert_one(
        {"_id": uid, "name": name, "added_by": OWNER_ID, "added_at": datetime.now(timezone.utc)}
    )
    ADMINS.add(uid)
    await state.clear()
    await message.answer(
        f"✅ Admin eklendi: {mention(uid, name or str(uid))} (<code>{uid}</code>)",
        reply_markup=kb([btn("👥 Adminler", "admins")], [btn("⬅️ Menü", "menu")]),
    )
    try:
        await bot.send_message(uid, "✅ Bot admini yapıldın. /start yaz.")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("adm_del:"))
async def cb_adm_del(call: CallbackQuery, bot: Bot):
    if not await owner_only(call):
        return
    uid = int(call.data.split(":")[1])
    await admins_col.delete_one({"_id": uid})
    ADMINS.discard(uid)
    await render_admins(call)
    await call.answer("Admin silindi ✅")
    try:
        await bot.send_message(uid, "⛔ Bot adminliğin kaldırıldı.")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Yetkisiz kullanıcılar
# --------------------------------------------------------------------------
@fallback_router.message(F.chat.type == ChatType.PRIVATE)
async def not_allowed_msg(message: Message):
    await track_user(message.from_user)
    await message.answer(
        "⛔ Bu botu sadece <b>botu kanala ekleyen kişi</b> ve <b>kanalın kurucusu</b> kullanabilir.\n\n"
        "Kullanmak için:\n"
        "1️⃣ Botu kanalına <b>admin</b> yap (Mesajları sil + Mesaj gönder yetkisiyle)\n"
        "2️⃣ Sonra buraya gelip /start yaz.\n\n"
        f"ID'n: <code>{message.from_user.id}</code>"
    )


@fallback_router.callback_query()
async def not_allowed_cb(call: CallbackQuery):
    await call.answer("⛔ Yetkin yok.", show_alert=True)


# --------------------------------------------------------------------------
# Render için health-check + başlatma
# --------------------------------------------------------------------------
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "ok"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    log.info("Flask health server :%s", port)
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


async def refresh_creators(bot: Bot):
    """Kurucusu (creator) kayıtlı olmayan kanalların kurucusunu bulur."""
    for cid, cfg in list(CHANNELS.items()):
        if cfg.get("creator_id"):
            continue
        creator = await get_creator_id(bot, cid)
        if creator:
            cfg["creator_id"] = creator
            await channels_col.update_one({"_id": cid}, {"$set": {"creator_id": creator}})
        await asyncio.sleep(0.2)


async def keep_alive():
    """Kendi Render URL'ine düzenli istek atar, servis uyumasın."""
    if "BURAYA" in RENDER_URL:
        log.warning("RENDER_URL ayarlanmadı, keep-alive kapalı.")
        return
    log.info("Keep-alive başladı: %s (her %s sn)", RENDER_URL, PING_INTERVAL)
    await asyncio.sleep(20)  # Flask'ın açılmasını bekle, ilk ping hemen atılsın
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    log.info("Keep-alive ping: %s", r.status)
        except Exception as e:
            log.warning("Keep-alive hata: %s", e)
        await asyncio.sleep(PING_INTERVAL)


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(events_router, admin_router, fallback_router)

    await load_cache()
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(keep_alive())
    task = asyncio.create_task(refresh_creators(bot))
    BG_TASKS.add(task)
    task.add_done_callback(BG_TASKS.discard)

    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Bot başladı.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
