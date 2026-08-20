#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TIR / Ýük daşama platformasy - Telegram bot
Diller: Türkmençe (tm) we Rusça (ru)
Stack: pyTelegramBotAPI (telebot) + Flask (health-check üçin) + MongoDB (pymongo)
Deploy: Render.com (Environment Variables arkaly)

Environment Variables:
    BOT_TOKEN       - Telegram bot token (@BotFather)
    MONGODB_URI     - MongoDB birikme salgysy
    DATABASE_NAME   - MongoDB baza ady
    ADMIN_IDS       - Admin Telegram ID-leri, otur bilen bölünen. Mysal: 111111,222222
    ADMIN_USERNAME  - Adminiň Telegram username-i (@ belgisiz). Balans doldurmak üçin
                       ulanyjylar şu username bilen habarlaşar.
    RENDER_URL      - Botuň Render.com-daky doly salgysy.
    PORT            - Render tarapyndan awtomatik berilýär.
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify

import telebot
from telebot import types

from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError
from bson import ObjectId
from bson.errors import InvalidId

# ---------------------------------------------------------------------------
# LOGLAMA
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tirbot")

# ---------------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# ---------------------------------------------------------------------------
# NOTE: Aşakdaky ikinji parametrler "default" bahalar - Render.com-da
# Environment Variables goýsaň, olar şu default-laryň üstünden geçer (has howpsuz ýol).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8876229736:AAFgSlS7r_WeXF-drlC0Pl1Epf4lB3UOJJU").strip()
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb+srv://mergenowlyagulyyew41_db_user:ZvZhOKOAF6ZMRbHX@cluster1.l8z8gll.mongodb.net/?appName=Cluster1").strip()
DATABASE_NAME = os.environ.get("DATABASE_NAME", "Tyrlar").strip()
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "7523674506,8407003010").strip()
# <-- BU ÝERE (ýa-da Environment Variables-a) adminiň Telegram username-ini ýaz (@ belgisiz).
# Ulanyjylar "VIP" düwmesine basanda, balans doldurmak üçin şu username bilen habarlaşmagy maslahat berler.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "TIR_admin").strip().lstrip("@")
RENDER_URL = os.environ.get("RENDER_URL", "https://tyrlar.onrender.com").strip()  # <-- Öz Render salgyňy ýaz. Mysal: https://seniň-botuň.onrender.com
PORT = int(os.environ.get("PORT", "10000"))

missing = []
if not BOT_TOKEN:
    missing.append("BOT_TOKEN")
if not MONGODB_URI:
    missing.append("MONGODB_URI")
if not DATABASE_NAME:
    missing.append("DATABASE_NAME")
if not ADMIN_IDS_RAW:
    missing.append("ADMIN_IDS")
if missing:
    logger.critical("Environment Variable ýetenok: %s", ", ".join(missing))
    sys.exit(1)

try:
    ADMIN_IDS = set(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip())
except ValueError:
    logger.critical("ADMIN_IDS nädogry format. Mysal: 111111,222222")
    sys.exit(1)

if not RENDER_URL:
    logger.warning(
        "RENDER_URL berilmedi. Self-ping işlemez (Render 'free' tarifde bot uklap biler). "
        "Environment Variables -> RENDER_URL = https://seniň-botuň.onrender.com goş."
    )

CURRENCY = "TMT"  # Balans / VIP nyrhlary şu pulda görkezilýär
LOAD_PAGE_SIZE = 10   # "Yük gözle" / "Ýüklerim" / "Boş maşyn gözle" sahypa ululygy
ADMIN_PAGE_SIZE = 5   # Admin panelindäki sanawlaryň sahypa ululygy

# VIP meýilnamalary: açar -> (dowamlylyk gün, bahasy TMT)
VIP_PLANS = {
    "1g": {"days": 1, "price": 10, "tm": "1 günlük", "ru": "1 день"},
    "1h": {"days": 7, "price": 50, "tm": "1 hepdelik", "ru": "1 неделя"},
    "1a": {"days": 30, "price": 150, "tm": "1 aýlyk", "ru": "1 месяц"},
}

# ---------------------------------------------------------------------------
# MONGODB
# ---------------------------------------------------------------------------
# ÜNS: MONGODB_URI başga (sponsor) bot bilen paýlaşylýan bolsa-da, bu ýerdäki
# kolleksiýa atlary "_tirplt" goşmaçasy bilen ýörite tapawutlandyryldy welin,
# şol beýleki botuň kolleksiýalary bilen gabat gelip, maglumatlar garyşmaz.
mongo_client = MongoClient(
    MONGODB_URI,
    maxPoolSize=50,
    minPoolSize=1,
    serverSelectionTimeoutMS=8000,
    connectTimeoutMS=8000,
    retryWrites=True,
)
db = mongo_client[DATABASE_NAME]

col_users = db["Tir_mongulanyjy_tirplt"]
col_yukler = db["Tir_mongyukler_tirplt"]
col_soforler = db["Tir_mongsoforler_tirplt"]
col_vip = db["Tir_mongvip_tirplt"]
col_zalwa = db["Tir_mongzalwa_tirplt"]


def ensure_indexes():
    try:
        col_users.create_index([("telegram_id", ASCENDING)], unique=True, background=True)
        col_yukler.create_index(
            [("status", ASCENDING), ("vip", DESCENDING), ("created_at", DESCENDING)],
            background=True,
        )
        col_yukler.create_index([("owner_id", ASCENDING)], background=True)
        col_soforler.create_index([("vip", DESCENDING), ("created_at", DESCENDING)], background=True)
        col_soforler.create_index([("telegram_id", ASCENDING)], background=True)
        col_vip.create_index([("user_id", ASCENDING)], background=True)
        col_zalwa.create_index([("created_at", DESCENDING)], background=True)
        logger.info("MongoDB indeksleri taýyn.")
    except PyMongoError as e:
        logger.error("Indeks döretmekde säwlik: %s", e)


try:
    mongo_client.admin.command("ping")
    logger.info("MongoDB birikme OK.")
except PyMongoError as e:
    logger.critical("MongoDB birikmedi: %s", e)
    sys.exit(1)

ensure_indexes()

# ---------------------------------------------------------------------------
# BOT INIT
# ---------------------------------------------------------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8)

# Ýady içindäki ýagdaý (FSM) - ulanyjynyň haýsy ädimde durandygyny saklaýar
# STATE[chat_id] = {"flow": str, "step": int, "data": {...}}
STATE = {}
STATE_LOCK = threading.Lock()

# Dil keşi (her gezek DB-den okamazlyk üçin)
LANG_CACHE = {}


def set_state(chat_id, flow, step=0, data=None):
    with STATE_LOCK:
        STATE[chat_id] = {"flow": flow, "step": step, "data": data or {}}


def get_state(chat_id):
    with STATE_LOCK:
        return STATE.get(chat_id)


def clear_state(chat_id):
    with STATE_LOCK:
        STATE.pop(chat_id, None)


# ---------------------------------------------------------------------------
# TERJIME KÖMEKÇISI
# ---------------------------------------------------------------------------
def L(lang, tm_text, ru_text):
    return tm_text if lang != "ru" else ru_text


def get_lang(chat_id):
    if chat_id in LANG_CACHE:
        return LANG_CACHE[chat_id]
    user = col_users.find_one({"telegram_id": chat_id}, {"lang": 1})
    lang = (user or {}).get("lang", "tm")
    LANG_CACHE[chat_id] = lang
    return lang


def set_lang(chat_id, lang):
    LANG_CACHE[chat_id] = lang
    col_users.update_one({"telegram_id": chat_id}, {"$set": {"lang": lang}}, upsert=False)


def money(amount):
    try:
        if float(amount) == int(amount):
            amount = int(amount)
    except (ValueError, TypeError):
        pass
    return f"{amount} {CURRENCY}"


# ---------------------------------------------------------------------------
# ULANYJY KÖMEKÇI FUNKSIÝALARY
# ---------------------------------------------------------------------------
def get_user(chat_id):
    return col_users.find_one({"telegram_id": chat_id})


def is_admin(chat_id):
    return chat_id in ADMIN_IDS


def is_registered(user_doc):
    return bool(user_doc and user_doc.get("phone") and user_doc.get("username") and user_doc.get("lang"))


def notify_button_label(lang, chat_id):
    user = get_user(chat_id)
    enabled = bool(user.get("notify", True)) if user else True
    if enabled:
        return L(lang, "🔔 Bildirişler: Açyk", "🔔 Уведомления: Вкл")
    return L(lang, "🔕 Bildirişler: Ýapyk", "🔕 Уведомления: Выкл")


def main_menu_kb(lang, chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton(L(lang, "📦 Yük goý", "📦 Разместить груз")),
        types.KeyboardButton(L(lang, "📋 Ýüklerim", "📋 Мои грузы")),
        types.KeyboardButton(L(lang, "📦 Yük gözle", "📦 Искать груз")),
        types.KeyboardButton(L(lang, "🚛 Maşynym bar — ýük gözleýärin", "🚛 У меня есть машина — ищу груз")),
        types.KeyboardButton(L(lang, "🚚 Boş maşyn gözle", "🚚 Искать свободную машину")),
        types.KeyboardButton(L(lang, "⭐ VIP", "⭐ VIP")),
        types.KeyboardButton(L(lang, "👤 Profil", "👤 Профиль")),
        types.KeyboardButton(L(lang, "🌐 Dil çalyş", "🌐 Сменить язык")),
        types.KeyboardButton(notify_button_label(lang, chat_id)),
    ]
    for i in range(0, len(buttons), 2):
        kb.row(*buttons[i:i + 2])
    if is_admin(chat_id):
        kb.row(types.KeyboardButton("🛠 Admin panel"))
    return kb


def lang_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🇹🇲 Türkmençe", callback_data="lang_tm"),
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
    )
    return kb


def phone_request_kb(lang):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(L(lang, "📞 Telefon belgimi paylaş", "📞 Поделиться номером"), request_contact=True))
    return kb


def username_check_kb(lang):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(L(lang, "✅ Barladym, dowam et", "✅ Проверить снова"), callback_data="check_username"))
    return kb


def back_kb(lang, callback_data):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(L(lang, "🔙 Yza", "🔙 Назад"), callback_data=callback_data))
    return kb


def send_main_menu(chat_id, lang, text=None):
    if text is None:
        text = L(lang, "🏠 Baş menýu", "🏠 Главное меню")
    bot.send_message(chat_id, text, reply_markup=main_menu_kb(lang, chat_id))


def notify_banned(chat_id, lang):
    bot.send_message(chat_id, L(
        lang,
        "🚫 Siz admin tarapyndan gadagan edildiňiz. Bot bilen işläp bilmersiňiz.",
        "🚫 Вы заблокированы администратором. Использование бота недоступно."
    ))


@bot.message_handler(func=lambda m: m.text and m.text.startswith(("🔔 Bildirişler", "🔕 Bildirişler", "🔔 Уведомления", "🔕 Уведомления")))
def handle_notify_toggle(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    current = bool(user.get("notify", True))
    new_val = not current
    col_users.update_one({"telegram_id": chat_id}, {"$set": {"notify": new_val}})
    if new_val:
        txt = L(lang, "🔔 Bildirişler açyldy. Täze ýük / boş maşyn goýlanda habar berilýär.", "🔔 Уведомления включены. Вы будете получать уведомления о новых грузах и машинах.")
    else:
        txt = L(lang, "🔕 Bildirişler ýapyldy.", "🔕 Уведомления выключены.")
    bot.send_message(chat_id, txt)
    send_main_menu(chat_id, lang)


def notify_eligible_users(exclude_id=None):
    flt = {"banned": {"$ne": True}, "notify": {"$ne": False}, "phone": {"$ne": None}}
    cursor = col_users.find(flt, {"telegram_id": 1, "lang": 1})
    for u in cursor:
        tid = u.get("telegram_id")
        if exclude_id is not None and tid == exclude_id:
            continue
        yield tid, u.get("lang", "tm")


def _broadcast_new_load(load_doc, exclude_id):
    type_label = CARGO_TYPES.get(load_doc.get("cargo_type"), "-")
    for tid, lang in notify_eligible_users(exclude_id=exclude_id):
        txt = L(
            lang,
            "🔔 <b>Täze ýük goýuldy!</b>\n\n"
            f"📍 {load_doc['from_loc']} → {load_doc['to_loc']}\n"
            f"🚛 {type_label} | ⚖️ {load_doc['ton']}t | 💵 {load_doc['price']}$\n\n"
            "Görmek üçin: 📦 Yük gözle",
            "🔔 <b>Размещён новый груз!</b>\n\n"
            f"📍 {load_doc['from_loc']} → {load_doc['to_loc']}\n"
            f"🚛 {type_label} | ⚖️ {load_doc['ton']}т | 💵 {load_doc['price']}$\n\n"
            "Смотреть: 📦 Искать груз",
        )
        try:
            bot.send_message(tid, txt)
        except Exception as e:
            logger.warning("Bildiriş (täze ýük) iberilmedi (%s): %s", tid, e)


def _broadcast_new_driver(driver_doc, exclude_id):
    type_label = CARGO_TYPES.get(driver_doc.get("cargo_type"), "-")
    for tid, lang in notify_eligible_users(exclude_id=exclude_id):
        txt = L(
            lang,
            "🔔 <b>Täze boş maşyn goşuldy!</b>\n\n"
            f"📍 {driver_doc['from_loc']} → {driver_doc['to_loc']}\n"
            f"🚛 {driver_doc.get('model','-')} | ⚖️ {driver_doc.get('ton')}t | {type_label}\n\n"
            "Görmek üçin: 🚚 Boş maşyn gözle",
            "🔔 <b>Добавлена новая свободная машина!</b>\n\n"
            f"📍 {driver_doc['from_loc']} → {driver_doc['to_loc']}\n"
            f"🚛 {driver_doc.get('model','-')} | ⚖️ {driver_doc.get('ton')}т | {type_label}\n\n"
            "Смотреть: 🚚 Искать свободную машину",
        )
        try:
            bot.send_message(tid, txt)
        except Exception as e:
            logger.warning("Bildiriş (täze maşyn) iberilmedi (%s): %s", tid, e)


def notify_new_load(load_doc, exclude_id):
    threading.Thread(target=_broadcast_new_load, args=(load_doc, exclude_id), daemon=True).start()


def notify_new_driver(driver_doc, exclude_id):
    threading.Thread(target=_broadcast_new_driver, args=(driver_doc, exclude_id), daemon=True).start()


def guard_access(chat_id):
    """
    Dolandyryş: banly ulanyjylary saklaýar.
    Return: (user_doc, lang, ok:bool)
    """
    user = get_user(chat_id)
    lang = get_lang(chat_id)
    if user and user.get("banned"):
        notify_banned(chat_id, lang)
        return user, lang, False
    return user, lang, True


def build_page_nav(prefix, page, total, page_size):
    """
    Sahypa belgili düwmeler ("1", "2", "3"...) döredýär.
    Häzirki sahypa aýratyn bellik bilen görkezilýär.
    Ondan köp sahypa bar bolsa, birnäçe hatarda düzülýär.
    """
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages <= 1:
        return []
    buttons = []
    for p in range(total_pages):
        label = f"• {p + 1} •" if p == page else str(p + 1)
        buttons.append(types.InlineKeyboardButton(label, callback_data=f"{prefix}{p}"))
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    return rows


def expire_vips():
    """VIP möhleti geçen ýük/maşyn ilanlaryny adaty ýagdaýa gaýtarýar."""
    now = datetime.now(timezone.utc)
    flt = {"vip": True, "vip_until": {"$ne": None, "$lt": now}}
    upd = {"$set": {"vip": False, "vip_until": None}}
    try:
        col_yukler.update_many(flt, upd)
        col_soforler.update_many(flt, upd)
    except PyMongoError as e:
        logger.warning("VIP möhlet barlagynda säwlik: %s", e)


# ---------------------------------------------------------------------------
# /start - HASABA ALYŞ AKYMY
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    clear_state(chat_id)
    user = get_user(chat_id)

    if user and user.get("banned"):
        notify_banned(chat_id, user.get("lang", "tm"))
        return

    if user and is_registered(user):
        lang = user.get("lang", "tm")
        LANG_CACHE[chat_id] = lang
        col_users.update_one(
            {"telegram_id": chat_id},
            {"$set": {
                "full_name": message.from_user.full_name or "",
                "username": message.from_user.username or user.get("username"),
            }},
        )
        send_main_menu(chat_id, lang, L(lang, "👋 Hoş geldiňiz! Baş menýu:", "👋 Добро пожаловать! Главное меню:"))
        return

    bot.send_message(chat_id, "🇹🇲 Dili saýlaň / 🇷🇺 Выберите язык:", reply_markup=lang_kb())


@bot.callback_query_handler(func=lambda c: c.data in ("lang_tm", "lang_ru"))
def cb_lang_select(call):
    chat_id = call.message.chat.id
    lang = "tm" if call.data == "lang_tm" else "ru"

    existing = col_users.find_one({"telegram_id": chat_id})
    if existing:
        set_lang(chat_id, lang)
    else:
        col_users.update_one(
            {"telegram_id": chat_id},
            {"$setOnInsert": {
                "telegram_id": chat_id,
                "full_name": call.from_user.full_name or "",
                "username": call.from_user.username or "",
                "phone": None,
                "balance": 0,
                "vip": False,
                "banned": False,
                "notify": True,
                "created_at": datetime.now(timezone.utc),
            }, "$set": {"lang": lang}},
            upsert=True,
        )
        LANG_CACHE[chat_id] = lang

    try:
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    continue_registration(chat_id, lang, call.from_user)


def continue_registration(chat_id, lang, from_user):
    username = from_user.username
    if not username:
        bot.send_message(
            chat_id,
            L(
                lang,
                "⚠️ Sizde Telegram username (ulanyjy ady) ýok.\n\n"
                "Telegram Sazlamalar -> Username bölüminden username dörediň, "
                "soňra aşakdaky düwmä basyň.",
                "⚠️ У вас не установлен username в Telegram.\n\n"
                "Перейдите в Настройки Telegram -> Username и создайте его, "
                "затем нажмите кнопку ниже."
            ),
            reply_markup=username_check_kb(lang),
        )
        return

    col_users.update_one({"telegram_id": chat_id}, {"$set": {"username": username}})

    user = col_users.find_one({"telegram_id": chat_id})
    if user and user.get("phone"):
        send_main_menu(chat_id, lang, L(lang, "✅ Hasaba alyş tamamlandy!", "✅ Регистрация завершена!"))
        return

    bot.send_message(
        chat_id,
        L(
            lang,
            "📞 Botdan peýdalanmak üçin telefon belgiňizi paylaşyň:",
            "📞 Для использования бота поделитесь номером телефона:"
        ),
        reply_markup=phone_request_kb(lang),
    )


@bot.callback_query_handler(func=lambda c: c.data == "check_username")
def cb_check_username(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    bot.answer_callback_query(call.id)
    continue_registration(chat_id, lang, call.from_user)


@bot.message_handler(content_types=["contact"])
def handle_contact(message):
    chat_id = message.chat.id
    lang = get_lang(chat_id)

    if message.contact.user_id != message.from_user.id:
        bot.send_message(
            chat_id,
            L(lang, "⚠️ Diňe öz belgiňizi paylaşyň.", "⚠️ Пожалуйста, поделитесь только своим номером."),
            reply_markup=phone_request_kb(lang),
        )
        return

    col_users.update_one(
        {"telegram_id": chat_id},
        {"$set": {
            "phone": message.contact.phone_number,
            "full_name": message.from_user.full_name or "",
            "username": message.from_user.username or "",
        }},
    )
    bot.send_message(
        chat_id,
        L(lang, "✅ Hasaba alyş tamamlandy!", "✅ Регистрация завершена!"),
        reply_markup=types.ReplyKeyboardRemove(),
    )
    send_main_menu(chat_id, lang)


# ---------------------------------------------------------------------------
# DIL ÇALYŞMAK (baş menýudan)
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text in ("🌐 Dil çalyş", "🌐 Сменить язык"))
def handle_lang_switch(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    clear_state(chat_id)
    bot.send_message(chat_id, "🇹🇲 Dili saýlaň / 🇷🇺 Выберите язык:", reply_markup=lang_kb())


# ---------------------------------------------------------------------------
# PROFIL
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text in ("👤 Profil", "👤 Профиль"))
def handle_profile(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return

    created = user.get("created_at")
    date_str = created.strftime("%d.%m.%Y") if created else "-"
    vip_active_loads = col_yukler.count_documents({"owner_id": chat_id, "vip": True, "status": "active"})
    vip_active_drivers = col_soforler.count_documents({"telegram_id": chat_id, "vip": True})

    txt = L(
        lang,
        f"👤 <b>Profil</b>\n\n"
        f"👤 Ady: {user.get('full_name','-')}\n"
        f"🔗 Username: @{user.get('username','-')}\n"
        f"📞 Telefon: {user.get('phone','-')}\n"
        f"💰 Balans: {money(user.get('balance', 0))}\n"
        f"⭐ Aktiw VIP ýüklerim: {vip_active_loads}\n"
        f"⭐ Aktiw VIP maşynlarym: {vip_active_drivers}\n"
        f"🗓 Agza bolan senesi: {date_str}",
        f"👤 <b>Профиль</b>\n\n"
        f"👤 Имя: {user.get('full_name','-')}\n"
        f"🔗 Username: @{user.get('username','-')}\n"
        f"📞 Телефон: {user.get('phone','-')}\n"
        f"💰 Баланс: {money(user.get('balance', 0))}\n"
        f"⭐ Активных VIP грузов: {vip_active_loads}\n"
        f"⭐ Активных VIP машин: {vip_active_drivers}\n"
        f"🗓 Дата регистрации: {date_str}",
    )
    bot.send_message(chat_id, txt)


# ---------------------------------------------------------------------------
# ÝÜK GOÝMAK / ÜÝTGETMEK AKYMY
# ---------------------------------------------------------------------------
CARGO_TYPES = {"tent": "🚛 Tent", "ref": "❄️ Ref (sowadyjyly)"}


def type_kb(lang):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🚛 Tent", callback_data="ctype_tent"),
        types.InlineKeyboardButton("❄️ Ref", callback_data="ctype_ref"),
    )
    return kb


def confirm_kb(lang, prefix):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(L(lang, "✅ Tassykla", "✅ Подтвердить"), callback_data=f"{prefix}_confirm"))
    kb.add(types.InlineKeyboardButton(L(lang, "✏️ Üýtget", "✏️ Изменить"), callback_data=f"{prefix}_edit"))
    kb.add(types.InlineKeyboardButton(L(lang, "❌ Ýatyr", "❌ Отмена"), callback_data=f"{prefix}_cancel"))
    return kb


def start_yuk_goy_flow(chat_id, lang, vip_plan=None, edit_id=None):
    data = {}
    if vip_plan:
        data["vip_plan"] = vip_plan
    if edit_id:
        data["edit_id"] = edit_id
        # Öňki maglumatlary öňünden dolduryp, diňe üýtgedilenini täzeläris
        old = col_yukler.find_one({"_id": ObjectId(edit_id)})
        if old:
            data["_old"] = {k: old.get(k) for k in ("from_loc", "to_loc", "ton", "cargo_type", "price", "cargo", "notes")}
    set_state(chat_id, "yuk_goy", 1, data)
    bot.send_message(
        chat_id,
        L(lang, "📍 Nerden? (ýükleme ýeri)", "📍 Откуда? (место погрузки)"),
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(func=lambda m: m.text in ("📦 Yük goý", "📦 Разместить груз"))
def handle_yuk_goy_start(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    start_yuk_goy_flow(chat_id, lang)


def yuk_goy_router(message):
    chat_id = message.chat.id
    lang = get_lang(chat_id)
    st = get_state(chat_id)
    step = st["step"]
    data = st["data"]
    text = (message.text or "").strip()

    if step == 1:
        if not text:
            bot.send_message(chat_id, L(lang, "Ýalňyş. Ýene ýazyň:", "Ошибка. Введите снова:"))
            return
        data["from_loc"] = text
        set_state(chat_id, "yuk_goy", 2, data)
        bot.send_message(chat_id, L(lang, "📍 Nereye? (barjak ýeri)", "📍 Куда? (место назначения)"))

    elif step == 2:
        if not text:
            bot.send_message(chat_id, L(lang, "Ýalňyş. Ýene ýazyň:", "Ошибка. Введите снова:"))
            return
        data["to_loc"] = text
        set_state(chat_id, "yuk_goy", 3, data)
        bot.send_message(chat_id, L(lang, "⚖️ Ýük näçe tonna?", "⚖️ Сколько тонн груз?"))

    elif step == 3:
        try:
            ton = float(text.replace(",", "."))
            if ton <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, L(lang, "⚠️ San ýazyň. Mysal: 20", "⚠️ Введите число. Например: 20"))
            return
        data["ton"] = ton
        set_state(chat_id, "yuk_goy", 4, data)
        bot.send_message(chat_id, L(lang, "🚛 Prisep görnüşi:", "🚛 Тип прицепа:"), reply_markup=type_kb(lang))

    elif step == 5:
        try:
            price = float(text.replace(",", "."))
            if price <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, L(lang, "⚠️ San ýazyň. Mysal: 3000", "⚠️ Введите число. Например: 3000"))
            return
        data["price"] = price
        set_state(chat_id, "yuk_goy", 6, data)
        bot.send_message(chat_id, L(lang, "📦 Ýük näme?", "📦 Что за груз?"))

    elif step == 6:
        if not text:
            bot.send_message(chat_id, L(lang, "Ýalňyş. Ýene ýazyň:", "Ошибка. Введите снова:"))
            return
        data["cargo"] = text
        set_state(chat_id, "yuk_goy", 7, data)
        bot.send_message(
            chat_id,
            L(
                lang,
                "📝 Ýüke başga goşmaça maglumat goşmak isleýärsiňizmi? Ýazyň (ýa-da '-' iberip geçiň):",
                "📝 Хотите добавить дополнительную информацию о грузе? Напишите (или отправьте '-' чтобы пропустить):",
            ),
        )

    elif step == 7:
        data["notes"] = "" if text == "-" else text
        set_state(chat_id, "yuk_goy", 8, data)
        show_yuk_goy_summary(chat_id, lang, data)


def show_yuk_goy_summary(chat_id, lang, data):
    type_label = CARGO_TYPES.get(data.get("cargo_type"), "-")
    notes = data.get("notes") or "-"
    txt = L(
        lang,
        "📋 <b>Ýüküň maglumaty:</b>\n\n"
        f"📍 Nerden: {data['from_loc']}\n"
        f"📍 Nereye: {data['to_loc']}\n"
        f"⚖️ Tonna: {data['ton']}\n"
        f"🚛 Görnüşi: {type_label}\n"
        f"💵 Baha: {data['price']}$\n"
        f"📦 Ýük: {data['cargo']}\n"
        f"📝 Goşmaça: {notes}\n\n"
        "Tassyklaýarsyňyzmy?",
        "📋 <b>Данные груза:</b>\n\n"
        f"📍 Откуда: {data['from_loc']}\n"
        f"📍 Куда: {data['to_loc']}\n"
        f"⚖️ Тонн: {data['ton']}\n"
        f"🚛 Тип: {type_label}\n"
        f"💵 Цена: {data['price']}$\n"
        f"📦 Груз: {data['cargo']}\n"
        f"📝 Доп. информация: {notes}\n\n"
        "Подтверждаете?",
    )
    bot.send_message(chat_id, txt, reply_markup=confirm_kb(lang, "yg"))


@bot.callback_query_handler(func=lambda c: c.data in ("ctype_tent", "ctype_ref"))
def cb_cargo_type(call):
    """
    Tent/Ref saýlawy iki dürli akymda ulanylýar:
      - yuk_goy flow, step 4 -> 5 (baha soralýar)
      - sofor flow, step 5 -> 6 (goşmaça maglumat soralýar)
    """
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    st = get_state(chat_id)
    bot.answer_callback_query(call.id)
    if not st:
        return

    cargo_type = "ref" if call.data == "ctype_ref" else "tent"
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if st.get("flow") == "yuk_goy" and st.get("step") == 4:
        data = st["data"]
        data["cargo_type"] = cargo_type
        set_state(chat_id, "yuk_goy", 5, data)
        bot.send_message(chat_id, L(lang, "💵 Ýük krey/daşama tölegi (mysal: 3000):", "💵 Стоимость перевозки (например: 3000):"))

    elif st.get("flow") == "sofor" and st.get("step") == 5:
        data = st["data"]
        data["cargo_type"] = cargo_type
        set_state(chat_id, "sofor", 6, data)
        bot.send_message(chat_id, L(lang, "📝 Goşmaça maglumat ýazyň (ýa-da '-' ýazyň):", "📝 Введите доп. информацию (или отправьте '-'):"))


def apply_vip_purchase(chat_id, kind, item_id, plan_key):
    """
    Ulanyjynyň balansyndan VIP bahasyny aýyrýar we degişli ýüke/maşyna
    VIP belligini goýýar. Üstünlikli bolsa True, ýetmese False gaýtarýar.
    """
    plan = VIP_PLANS[plan_key]
    user = col_users.find_one_and_update(
        {"telegram_id": chat_id, "balance": {"$gte": plan["price"]}},
        {"$inc": {"balance": -plan["price"]}},
        return_document=ReturnDocument.AFTER,
    )
    if not user:
        return False

    until = datetime.now(timezone.utc) + timedelta(days=plan["days"])
    coll = col_yukler if kind == "load" else col_soforler
    coll.update_one({"_id": ObjectId(item_id)}, {"$set": {"vip": True, "vip_until": until}})
    col_vip.insert_one({
        "user_id": chat_id,
        "kind": kind,
        "item_id": str(item_id),
        "plan": plan_key,
        "price": plan["price"],
        "days": plan["days"],
        "created_at": datetime.now(timezone.utc),
    })
    return True


@bot.callback_query_handler(func=lambda c: c.data.startswith("yg_"))
def cb_yuk_goy_confirm(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    action = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    st = get_state(chat_id)
    if not st or st.get("flow") != "yuk_goy":
        return
    data = st["data"]

    if action == "cancel":
        clear_state(chat_id)
        bot.send_message(chat_id, L(lang, "❌ Ýatyryldy.", "❌ Отменено."))
        send_main_menu(chat_id, lang)
        return

    if action == "edit":
        edit_id = data.get("edit_id")
        vip_plan = data.get("vip_plan")
        start_yuk_goy_flow(chat_id, lang, vip_plan=vip_plan, edit_id=edit_id)
        return

    if action == "confirm":
        user = get_user(chat_id)
        edit_id = data.get("edit_id")

        if edit_id:
            col_yukler.update_one(
                {"_id": ObjectId(edit_id)},
                {"$set": {
                    "from_loc": data["from_loc"],
                    "to_loc": data["to_loc"],
                    "ton": data["ton"],
                    "cargo_type": data["cargo_type"],
                    "price": data["price"],
                    "cargo": data["cargo"],
                    "notes": data.get("notes", ""),
                }},
            )
            clear_state(chat_id)
            bot.send_message(chat_id, L(lang, "✅ Ýük täzelendi!", "✅ Груз обновлён!"))
            send_main_menu(chat_id, lang)
            return

        doc = {
            "owner_id": chat_id,
            "owner_username": user.get("username", ""),
            "owner_name": user.get("full_name", ""),
            "from_loc": data["from_loc"],
            "to_loc": data["to_loc"],
            "ton": data["ton"],
            "cargo_type": data["cargo_type"],
            "price": data["price"],
            "cargo": data["cargo"],
            "notes": data.get("notes", ""),
            "status": "active",
            "vip": False,
            "vip_until": None,
            "taker_id": None,
            "taker_username": None,
            "created_at": datetime.now(timezone.utc),
        }
        result = col_yukler.insert_one(doc)
        clear_state(chat_id)
        notify_new_load(doc, exclude_id=chat_id)

        vip_plan = data.get("vip_plan")
        if vip_plan:
            ok = apply_vip_purchase(chat_id, "load", result.inserted_id, vip_plan)
            if ok:
                bot.send_message(
                    chat_id,
                    L(lang, "✅ Ýük goýuldy we VIP edildi! ⭐", "✅ Груз размещён и получил VIP! ⭐"),
                )
            else:
                bot.send_message(
                    chat_id,
                    L(
                        lang,
                        f"✅ Ýük goýuldy, ýöne balansyňyz VIP üçin ýeterlik däl. Admin: @{ADMIN_USERNAME}",
                        f"✅ Груз размещён, но баланса недостаточно для VIP. Админ: @{ADMIN_USERNAME}",
                    ),
                )
        else:
            bot.send_message(chat_id, L(lang, "✅ Ýük üstünlikli goýuldy!", "✅ Груз успешно размещён!"))
        send_main_menu(chat_id, lang)


# ---------------------------------------------------------------------------
# ÝÜKLERIM (ulanyjynyň öz ýükleri - üýtget/poz)
# ---------------------------------------------------------------------------
STATUS_ICON = {"active": "🟢", "sold": "🔴"}
STATUS_LABEL_TM = {"active": "Aktiw", "sold": "Satylan"}
STATUS_LABEL_RU = {"active": "Активный", "sold": "Продан"}


def format_myload_label(doc):
    star = "⭐ " if doc.get("vip") else ""
    icon = STATUS_ICON.get(doc.get("status"), "⚪️")
    return f"{icon} {star}{doc['from_loc']} → {doc['to_loc']} | {doc['price']}$"


def myloads_list_kb(chat_id, page):
    skip = page * LOAD_PAGE_SIZE
    total = col_yukler.count_documents({"owner_id": chat_id})
    docs = list(
        col_yukler.find({"owner_id": chat_id})
        .sort("created_at", DESCENDING)
        .skip(skip)
        .limit(LOAD_PAGE_SIZE)
    )
    kb = types.InlineKeyboardMarkup()
    for d in docs:
        kb.add(types.InlineKeyboardButton(format_myload_label(d), callback_data=f"myl_{d['_id']}"))
    for row in build_page_nav("mylp_", page, total, LOAD_PAGE_SIZE):
        kb.row(*row)
    return kb, docs, total


@bot.message_handler(func=lambda m: m.text in ("📋 Ýüklerim", "📋 Мои грузы"))
def handle_my_loads(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    clear_state(chat_id)
    kb, docs, total = myloads_list_kb(chat_id, 0)
    if total == 0:
        bot.send_message(chat_id, L(lang, "😕 Entek ýük goýmadyňyz.", "😕 Вы ещё не разместили грузов."))
        return
    bot.send_message(chat_id, L(lang, f"📋 Ýüklerim ({total}):", f"📋 Мои грузы ({total}):"), reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("mylp_"))
def cb_myloads_page(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    page = int(call.data.split("_", 1)[1])
    bot.answer_callback_query(call.id)
    kb, docs, total = myloads_list_kb(chat_id, page)
    try:
        bot.edit_message_text(
            L(lang, f"📋 Ýüklerim ({total}):", f"📋 Мои грузы ({total}):"),
            chat_id, call.message.message_id, reply_markup=kb,
        )
    except Exception:
        bot.send_message(chat_id, L(lang, f"📋 Ýüklerim ({total}):", f"📋 Мои грузы ({total}):"), reply_markup=kb)


def myload_detail_kb(lang, load_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(L(lang, "✏️ Üýtget", "✏️ Изменить"), callback_data=f"myle_{load_id}"))
    kb.add(types.InlineKeyboardButton(L(lang, "🗑 Poz", "🗑 Удалить"), callback_data=f"myld_{load_id}"))
    kb.add(types.InlineKeyboardButton(L(lang, "🔙 Yza", "🔙 Назад"), callback_data="mylp_0"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data.startswith("myl_"))
def cb_myload_detail(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    try:
        d = col_yukler.find_one({"_id": ObjectId(load_id), "owner_id": chat_id})
    except InvalidId:
        d = None
    if not d:
        bot.send_message(chat_id, L(lang, "⚠️ Tapylmady.", "⚠️ Не найдено."))
        return

    status_label = STATUS_LABEL_TM.get(d.get("status"), "-") if lang != "ru" else STATUS_LABEL_RU.get(d.get("status"), "-")
    vip_line = ""
    if d.get("vip"):
        until = d.get("vip_until")
        until_str = until.strftime("%d.%m.%Y %H:%M") if until else L(lang, "çäksiz", "бессрочно")
        vip_line = L(lang, f"⭐ VIP (bitýär: {until_str})\n", f"⭐ VIP (до: {until_str})\n")

    txt = yuk_detail_text(lang, d) + f"\n\n{vip_line}" + L(lang, f"ℹ️ Status: {status_label}", f"ℹ️ Статус: {status_label}")
    bot.send_message(chat_id, txt, reply_markup=myload_detail_kb(lang, load_id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("myle_"))
def cb_myload_edit(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    d = col_yukler.find_one({"_id": ObjectId(load_id), "owner_id": chat_id})
    if not d:
        bot.send_message(chat_id, L(lang, "⚠️ Tapylmady.", "⚠️ Не найдено."))
        return
    start_yuk_goy_flow(chat_id, lang, edit_id=load_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("myld_"))
def cb_myload_delete(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]
    result = col_yukler.delete_one({"_id": ObjectId(load_id), "owner_id": chat_id})
    if result.deleted_count:
        bot.answer_callback_query(call.id, L(lang, "🗑 Pozuldy.", "🗑 Удалено."))
        try:
            bot.edit_message_text(L(lang, "🗑 Ýük pozuldy.", "🗑 Груз удалён."), chat_id, call.message.message_id)
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id, L(lang, "⚠️ Tapylmady.", "⚠️ Не найдено."), show_alert=True)


# ---------------------------------------------------------------------------
# ÝÜKLERI GÖRMEK / GÖZLEMEK
# ---------------------------------------------------------------------------
def format_yuk_label(doc):
    star = "⭐ " if doc.get("vip") else ""
    type_icon = "❄️ Ref" if doc.get("cargo_type") == "ref" else "🚛 Tent"
    ton = doc.get("ton")
    return f"{star}{doc['from_loc']} → {doc['to_loc']} | {type_icon} | {ton}t | {doc['price']}$"


def yukler_list_kb(page):
    expire_vips()
    skip = page * LOAD_PAGE_SIZE
    cursor = (
        col_yukler.find({"status": "active"})
        .sort([("vip", DESCENDING), ("created_at", DESCENDING)])
        .skip(skip)
        .limit(LOAD_PAGE_SIZE)
    )
    docs = list(cursor)
    total = col_yukler.count_documents({"status": "active"})

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        kb.add(types.InlineKeyboardButton(format_yuk_label(d), callback_data=f"yv_{d['_id']}"))

    for row in build_page_nav("ylp_", page, total, LOAD_PAGE_SIZE):
        kb.row(*row)

    return kb, docs, total


@bot.message_handler(func=lambda m: m.text in ("📦 Yük gözle", "📦 Искать груз"))
def handle_yuk_gozle(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    clear_state(chat_id)
    kb, docs, total = yukler_list_kb(0)
    if total == 0:
        bot.send_message(chat_id, L(lang, "😕 Häzirlikçe ýük ýok.", "😕 Сейчас грузов нет."))
        return
    bot.send_message(chat_id, L(lang, f"📦 Aktiw ýükler ({total}):", f"📦 Активные грузы ({total}):"), reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ylp_"))
def cb_yukler_page(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    page = int(call.data.split("_", 1)[1])
    bot.answer_callback_query(call.id)
    kb, docs, total = yukler_list_kb(page)
    try:
        bot.edit_message_text(
            L(lang, f"📦 Aktiw ýükler ({total}):", f"📦 Активные грузы ({total}):"),
            chat_id, call.message.message_id, reply_markup=kb,
        )
    except Exception:
        bot.send_message(chat_id, L(lang, f"📦 Aktiw ýükler ({total}):", f"📦 Активные грузы ({total}):"), reply_markup=kb)


def yuk_detail_text(lang, d):
    type_label = CARGO_TYPES.get(d.get("cargo_type"), "-")
    created = d.get("created_at")
    date_str = created.strftime("%d.%m.%Y %H:%M") if created else "-"
    star = "⭐ VIP\n" if d.get("vip") else ""
    notes = d.get("notes") or "-"
    return L(
        lang,
        f"{star}📍 Nerden: {d['from_loc']}\n"
        f"📍 Nereye: {d['to_loc']}\n"
        f"⚖️ Tonna: {d['ton']}\n"
        f"🚛 Görnüşi: {type_label}\n"
        f"💵 Baha: {d['price']}$\n"
        f"📦 Ýük: {d['cargo']}\n"
        f"📝 Goşmaça: {notes}\n"
        f"🗓 Ýerleşdirilen senesi: {date_str}",
        f"{star}📍 Откуда: {d['from_loc']}\n"
        f"📍 Куда: {d['to_loc']}\n"
        f"⚖️ Тонн: {d['ton']}\n"
        f"🚛 Тип: {type_label}\n"
        f"💵 Цена: {d['price']}$\n"
        f"📦 Груз: {d['cargo']}\n"
        f"📝 Доп. информация: {notes}\n"
        f"🗓 Дата размещения: {date_str}",
    )


def yuk_detail_kb(lang, load_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(L(lang, "📦 Yük al", "📦 Взять груз"), callback_data=f"yal_{load_id}"))
    kb.add(types.InlineKeyboardButton(L(lang, "⚠️ Zalwa", "⚠️ Жалоба"), callback_data=f"yzw_{load_id}"))
    kb.add(types.InlineKeyboardButton(L(lang, "🔙 Yza", "🔙 Назад"), callback_data="ylp_0"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data.startswith("yv_"))
def cb_yuk_view(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    try:
        d = col_yukler.find_one({"_id": ObjectId(load_id)})
    except InvalidId:
        d = None
    if not d:
        bot.send_message(chat_id, L(lang, "⚠️ Bu ýük tapylmady.", "⚠️ Груз не найден."))
        return
    bot.send_message(chat_id, yuk_detail_text(lang, d), reply_markup=yuk_detail_kb(lang, load_id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("yal_"))
def cb_yuk_al(call):
    """
    Ýüke gyzyklanma bildirmek. Ýük sanawdan aýrylmaýar - diňe ýük eýesine
    habar gidýär, we ol "✅ Satylan" ýa-da "❌ Satylanok" saýlaýar.
    """
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]

    try:
        oid = ObjectId(load_id)
    except InvalidId:
        bot.answer_callback_query(call.id, L(lang, "Ýalňyşlyk", "Ошибка"), show_alert=True)
        return

    d = col_yukler.find_one({"_id": oid})
    if not d or d.get("status") != "active":
        bot.answer_callback_query(
            call.id,
            L(lang, "⚠️ Bu ýük eýýäm satylan.", "⚠️ Этот груз уже продан."),
            show_alert=True,
        )
        return

    if d.get("owner_id") == chat_id:
        bot.answer_callback_query(
            call.id,
            L(lang, "⚠️ Öz ýüküňizi alyp bilmersiňiz.", "⚠️ Нельзя взять свой собственный груз."),
            show_alert=True,
        )
        return

    bot.answer_callback_query(call.id)
    user = get_user(chat_id)

    owner_username = d.get("owner_username") or ""
    owner_name = d.get("owner_name") or "-"
    link = f"@{owner_username}" if owner_username else L(lang, "(username ýok)", "(нет username)")
    bot.send_message(
        chat_id,
        L(
            lang,
            f"✅ Gyzyklanma iberildi!\n\n👤 {owner_name}\n🔗 {link}",
            f"✅ Интерес отправлен!\n\n👤 {owner_name}\n🔗 {link}",
        ),
    )

    owner_id = d.get("owner_id")
    taker_username = (user or {}).get("username", "")
    taker_link = f"@{taker_username}" if taker_username else L(lang, "(username ýok)", "(нет username)")
    owner_lang = get_lang(owner_id)
    resp_kb = types.InlineKeyboardMarkup()
    resp_kb.add(
        types.InlineKeyboardButton(L(owner_lang, "✅ Satylan", "✅ Продано"), callback_data=f"sold_{oid}"),
        types.InlineKeyboardButton(L(owner_lang, "❌ Satylanok", "❌ Не продано"), callback_data=f"notsold_{oid}"),
    )
    try:
        bot.send_message(
            owner_id,
            L(
                owner_lang,
                f"📦 Ýüküňize bir ulanyjy gyzyklandy.\n👤 {taker_link}\n\n"
                "Eger ýüküňizi satsanyz, aşakdaky 'Satylan' düwmesine basyň. "
                "Entek satylmadyk bolsa, 'Satylanok' basyp bilersiňiz.",
                f"📦 Вашим грузом заинтересовался пользователь.\n👤 {taker_link}\n\n"
                "Если груз продан, нажмите кнопку 'Продано'. Если ещё нет — 'Не продано'.",
            ),
            reply_markup=resp_kb,
        )
    except Exception as e:
        logger.warning("Ýük eýesine habar iberilmedi (%s): %s", owner_id, e)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sold_"))
def cb_sold(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]
    try:
        oid = ObjectId(load_id)
    except InvalidId:
        bot.answer_callback_query(call.id)
        return

    updated = col_yukler.find_one_and_update(
        {"_id": oid, "owner_id": chat_id, "status": {"$ne": "sold"}},
        {"$set": {"status": "sold", "sold_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        bot.answer_callback_query(call.id, L(lang, "⚠️ Tapylmady ýa-da eýýäm satylan.", "⚠️ Не найдено или уже продано."), show_alert=True)
        return

    bot.answer_callback_query(call.id, L(lang, "✅ Bellendi.", "✅ Отмечено."))
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, L(lang, "✅ Ýük 'satylan' diýlip bellendi we sanawdan aýryldy.", "✅ Груз отмечен как проданный и убран из списка."))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("notsold_"))
def cb_notsold(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    bot.answer_callback_query(call.id, L(lang, "✅ Bellendi, ýük aktiw galýar.", "✅ Отмечено, груз остаётся активным."))
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, L(lang, "ℹ️ Ýük aktiw sanawda galýar.", "ℹ️ Груз остаётся в активном списке."))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ZALWA (ŞIKAÝAT)
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("yzw_"))
def cb_zalwa_start(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    set_state(chat_id, "zalwa", 1, {"load_id": load_id})
    bot.send_message(chat_id, L(lang, "⚠️ Näme sebäpden?", "⚠️ По какой причине?"))


def zalwa_router(message):
    chat_id = message.chat.id
    lang = get_lang(chat_id)
    st = get_state(chat_id)
    data = st["data"]
    reason = (message.text or "").strip()
    if not reason:
        bot.send_message(chat_id, L(lang, "Ýalňyş. Ýene ýazyň:", "Ошибка. Введите снова:"))
        return

    load_id = data.get("load_id")
    try:
        d = col_yukler.find_one({"_id": ObjectId(load_id)})
    except InvalidId:
        d = None

    user = get_user(chat_id)
    col_zalwa.insert_one({
        "load_id": load_id,
        "reporter_id": chat_id,
        "reporter_username": (user or {}).get("username", ""),
        "reporter_name": (user or {}).get("full_name", ""),
        "reason": reason,
        "created_at": datetime.now(timezone.utc),
    })

    load_info = ""
    if d:
        load_info = f"{d['from_loc']} - {d['to_loc']} | {d['price']}$"

    admin_text = (
        f"⚠️ <b>Täze zalwa</b>\n\n"
        f"👤 {(user or {}).get('full_name','-')} (@{(user or {}).get('username','-')})\n"
        f"📞 {(user or {}).get('phone','-')}\n"
        f"📦 Ýük: {load_info}\n"
        f"📝 Sebäp: {reason}"
    )
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_text)
        except Exception as e:
            logger.warning("Admin (%s) habar alyp bilmedi: %s", admin_id, e)

    clear_state(chat_id)
    bot.send_message(chat_id, L(lang, "✅ Şikaýatyňyz iberildi.", "✅ Ваша жалоба отправлена."))
    send_main_menu(chat_id, lang)


# ---------------------------------------------------------------------------
# ŞOFÖR / MAŞYN ÝERLEŞDIRMEK
# ---------------------------------------------------------------------------
def start_sofor_flow(chat_id, lang, vip_plan=None):
    data = {}
    if vip_plan:
        data["vip_plan"] = vip_plan
    set_state(chat_id, "sofor", 1, data)
    bot.send_message(
        chat_id,
        L(lang, "📍 Häzir nirede?", "📍 Где сейчас находитесь?"),
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(func=lambda m: m.text in ("🚛 Maşynym bar — ýük gözleýärin", "🚛 У меня есть машина — ищу груз"))
def handle_sofor_start(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    start_sofor_flow(chat_id, lang)


def sofor_router(message):
    chat_id = message.chat.id
    lang = get_lang(chat_id)
    st = get_state(chat_id)
    step = st["step"]
    data = st["data"]
    text = (message.text or "").strip()

    if not text:
        bot.send_message(chat_id, L(lang, "Ýalňyş. Ýene ýazyň:", "Ошибка. Введите снова:"))
        return

    if step == 1:
        data["from_loc"] = text
        set_state(chat_id, "sofor", 2, data)
        bot.send_message(chat_id, L(lang, "📍 Nirä gitjek?", "📍 Куда направляетесь?"))

    elif step == 2:
        data["to_loc"] = text
        set_state(chat_id, "sofor", 3, data)
        bot.send_message(chat_id, L(lang, "🚛 Maşynyň modeli?", "🚛 Модель автомобиля?"))

    elif step == 3:
        data["model"] = text
        set_state(chat_id, "sofor", 4, data)
        bot.send_message(chat_id, L(lang, "⚖️ Näçe tonna göterip bilýär?", "⚖️ Сколько тонн может перевезти?"))

    elif step == 4:
        try:
            ton = float(text.replace(",", "."))
            if ton <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, L(lang, "⚠️ San ýazyň. Mysal: 20", "⚠️ Введите число. Например: 20"))
            return
        data["ton"] = ton
        set_state(chat_id, "sofor", 5, data)
        bot.send_message(chat_id, L(lang, "🚛 Prisep görnüşi:", "🚛 Тип прицепа:"), reply_markup=type_kb(lang))

    elif step == 6:
        data["extra"] = "" if text == "-" else text
        finish_sofor(chat_id, lang, data)


def finish_sofor(chat_id, lang, data):
    user = get_user(chat_id)
    doc = {
        "telegram_id": chat_id,
        "username": user.get("username", ""),
        "full_name": user.get("full_name", ""),
        "from_loc": data["from_loc"],
        "to_loc": data["to_loc"],
        "model": data["model"],
        "ton": data["ton"],
        "cargo_type": data["cargo_type"],
        "extra": data.get("extra", ""),
        "vip": False,
        "vip_until": None,
        "created_at": datetime.now(timezone.utc),
    }
    result = col_soforler.insert_one(doc)
    clear_state(chat_id)
    notify_new_driver(doc, exclude_id=chat_id)

    vip_plan = data.get("vip_plan")
    if vip_plan:
        ok = apply_vip_purchase(chat_id, "drv", result.inserted_id, vip_plan)
        if ok:
            bot.send_message(chat_id, L(lang, "✅ Maşyn goşuldy we VIP edildi! ⭐", "✅ Машина добавлена и получила VIP! ⭐"))
        else:
            bot.send_message(
                chat_id,
                L(
                    lang,
                    f"✅ Maşyn goşuldy, ýöne balansyňyz VIP üçin ýeterlik däl. Admin: @{ADMIN_USERNAME}",
                    f"✅ Машина добавлена, но баланса недостаточно для VIP. Админ: @{ADMIN_USERNAME}",
                ),
            )
    else:
        bot.send_message(chat_id, L(lang, "✅ Maşynyňyz üstünlikli goşuldy!", "✅ Ваш автомобиль успешно добавлен!"))
    send_main_menu(chat_id, lang)


# ---------------------------------------------------------------------------
# BOŞ MAŞYNLARY GÖZLEMEK (ýük eýeleri üçin - şofýor ilanlaryny görmek)
# ---------------------------------------------------------------------------
def format_sofor_label(doc):
    star = "⭐ " if doc.get("vip") else ""
    type_icon = "❄️ Ref" if doc.get("cargo_type") == "ref" else "🚛 Tent"
    return f"{star}{doc['from_loc']} → {doc['to_loc']} | {doc.get('model','-')} | {doc.get('ton')}t | {type_icon}"


def soforler_list_kb(page):
    expire_vips()
    skip = page * LOAD_PAGE_SIZE
    cursor = (
        col_soforler.find({})
        .sort([("vip", DESCENDING), ("created_at", DESCENDING)])
        .skip(skip)
        .limit(LOAD_PAGE_SIZE)
    )
    docs = list(cursor)
    total = col_soforler.count_documents({})

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        kb.add(types.InlineKeyboardButton(format_sofor_label(d), callback_data=f"sv_{d['_id']}"))

    for row in build_page_nav("svp_", page, total, LOAD_PAGE_SIZE):
        kb.row(*row)

    return kb, docs, total


@bot.message_handler(func=lambda m: m.text in ("🚚 Boş maşyn gözle", "🚚 Искать свободную машину"))
def handle_sofor_gozle(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    clear_state(chat_id)
    kb, docs, total = soforler_list_kb(0)
    if total == 0:
        bot.send_message(chat_id, L(lang, "😕 Häzirlikçe boş maşyn ýok.", "😕 Сейчас свободных машин нет."))
        return
    bot.send_message(chat_id, L(lang, f"🚚 Boş maşynlar ({total}):", f"🚚 Свободные машины ({total}):"), reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("svp_"))
def cb_soforler_page(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    page = int(call.data.split("_", 1)[1])
    bot.answer_callback_query(call.id)
    kb, docs, total = soforler_list_kb(page)
    try:
        bot.edit_message_text(
            L(lang, f"🚚 Boş maşynlar ({total}):", f"🚚 Свободные машины ({total}):"),
            chat_id, call.message.message_id, reply_markup=kb,
        )
    except Exception:
        bot.send_message(chat_id, L(lang, f"🚚 Boş maşynlar ({total}):", f"🚚 Свободные машины ({total}):"), reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sv_"))
def cb_sofor_view(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    driver_id = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    try:
        d = col_soforler.find_one({"_id": ObjectId(driver_id)})
    except InvalidId:
        d = None
    if not d:
        bot.send_message(chat_id, L(lang, "⚠️ Bu ilan tapylmady.", "⚠️ Объявление не найдено."))
        return

    type_label = CARGO_TYPES.get(d.get("cargo_type"), "-")
    created = d.get("created_at")
    date_str = created.strftime("%d.%m.%Y %H:%M") if created else "-"
    username = d.get("username") or ""
    link = f"@{username}" if username else L(lang, "(username ýok)", "(нет username)")
    extra = d.get("extra") or "-"
    star = "⭐ VIP\n" if d.get("vip") else ""

    txt = L(
        lang,
        f"{star}📍 Nirede: {d['from_loc']}\n"
        f"📍 Nirä gitjek: {d['to_loc']}\n"
        f"🚛 Model: {d.get('model','-')}\n"
        f"⚖️ Kuwwaty: {d.get('ton')} t\n"
        f"🚛 Görnüşi: {type_label}\n"
        f"📝 Goşmaça: {extra}\n"
        f"🗓 Ilan senesi: {date_str}\n\n"
        f"👤 {d.get('full_name','-')}\n"
        f"🔗 {link}",
        f"{star}📍 Где сейчас: {d['from_loc']}\n"
        f"📍 Куда направляется: {d['to_loc']}\n"
        f"🚛 Модель: {d.get('model','-')}\n"
        f"⚖️ Грузоподъёмность: {d.get('ton')} т\n"
        f"🚛 Тип: {type_label}\n"
        f"📝 Доп. информация: {extra}\n"
        f"🗓 Дата размещения: {date_str}\n\n"
        f"👤 {d.get('full_name','-')}\n"
        f"🔗 {link}",
    )
    bot.send_message(chat_id, txt, reply_markup=back_kb(lang, "svp_0"))


# ---------------------------------------------------------------------------
# VIP
# ---------------------------------------------------------------------------
def vip_plans_kb(lang):
    kb = types.InlineKeyboardMarkup()
    for key, plan in VIP_PLANS.items():
        label = L(lang, plan["tm"], plan["ru"])
        kb.add(types.InlineKeyboardButton(f"⭐ {label} - {money(plan['price'])}", callback_data=f"vipplan_{key}"))
    return kb


@bot.message_handler(func=lambda m: m.text == "⭐ VIP")
def handle_vip(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return

    clear_state(chat_id)
    balance = user.get("balance", 0)
    txt = L(
        lang,
        f"⭐ <b>VIP</b>\n\n💰 Balansyňyz: {money(balance)}\n\n"
        f"Balans doldurmak üçin admin bilen habarlaşyň: @{ADMIN_USERNAME}\n\n"
        "VIP satyn alyp, ýüküňizi ýa-da maşynyňyzy sanawyň iň ýokarsynda görkezip bilersiňiz. "
        "Meýilnamany saýlaň:",
        f"⭐ <b>VIP</b>\n\n💰 Ваш баланс: {money(balance)}\n\n"
        f"Для пополнения баланса обратитесь к администратору: @{ADMIN_USERNAME}\n\n"
        "Купив VIP, вы сможете показывать свой груз или машину в самом верху списка. "
        "Выберите план:",
    )
    bot.send_message(chat_id, txt, reply_markup=vip_plans_kb(lang))


@bot.callback_query_handler(func=lambda c: c.data.startswith("vipplan_"))
def cb_vip_plan(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    plan_key = call.data.split("_", 1)[1]
    plan = VIP_PLANS.get(plan_key)
    bot.answer_callback_query(call.id)
    if not plan:
        return

    user = get_user(chat_id)
    balance = (user or {}).get("balance", 0)
    if balance < plan["price"]:
        bot.send_message(
            chat_id,
            L(
                lang,
                f"⚠️ Balansyňyz ýeterlik däl (gerek: {money(plan['price'])}, siziňki: {money(balance)}).\n"
                f"Balans doldurmak üçin admin bilen habarlaşyň: @{ADMIN_USERNAME}",
                f"⚠️ Недостаточно баланса (нужно: {money(plan['price'])}, у вас: {money(balance)}).\n"
                f"Для пополнения обратитесь к администратору: @{ADMIN_USERNAME}",
            ),
        )
        return

    set_state(chat_id, "vip_pick_item", 1, {"plan": plan_key})
    show_vip_item_picker(chat_id, lang, plan_key)


def show_vip_item_picker(chat_id, lang, plan_key):
    loads = list(col_yukler.find({"owner_id": chat_id, "status": "active"}).sort("created_at", DESCENDING).limit(20))
    drivers = list(col_soforler.find({"telegram_id": chat_id}).sort("created_at", DESCENDING).limit(20))

    kb = types.InlineKeyboardMarkup()
    for d in loads:
        label = f"📦 {d['from_loc']} → {d['to_loc']} | {d['price']}$" + (" ⭐" if d.get("vip") else "")
        kb.add(types.InlineKeyboardButton(label, callback_data=f"vipit_load_{d['_id']}"))
    for d in drivers:
        label = f"🚛 {d['from_loc']} → {d['to_loc']} | {d.get('model','-')}" + (" ⭐" if d.get("vip") else "")
        kb.add(types.InlineKeyboardButton(label, callback_data=f"vipit_drv_{d['_id']}"))

    kb.add(types.InlineKeyboardButton(L(lang, "➕ Täze ýük goş", "➕ Добавить новый груз"), callback_data="vipadd_load"))
    kb.add(types.InlineKeyboardButton(L(lang, "➕ Täze maşyn goş", "➕ Добавить новую машину"), callback_data="vipadd_drv"))
    kb.add(types.InlineKeyboardButton(L(lang, "🔙 Yza", "🔙 Назад"), callback_data="vip_back"))

    txt = L(
        lang,
        "⭐ Haýsy ýküňizi ýa-da maşynyňyzy VIP etmek isleýärsiňiz?",
        "⭐ Какой груз или машину вы хотите сделать VIP?",
    )
    bot.send_message(chat_id, txt, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "vip_back")
def cb_vip_back(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    clear_state(chat_id)
    bot.answer_callback_query(call.id)
    user = get_user(chat_id)
    balance = (user or {}).get("balance", 0)
    txt = L(
        lang,
        f"⭐ <b>VIP</b>\n\n💰 Balansyňyz: {money(balance)}\n\nMeýilnamany saýlaň:",
        f"⭐ <b>VIP</b>\n\n💰 Ваш баланс: {money(balance)}\n\nВыберите план:",
    )
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=vip_plans_kb(lang))
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=vip_plans_kb(lang))


@bot.callback_query_handler(func=lambda c: c.data.startswith("vipit_"))
def cb_vip_item_pick(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    _, kind, item_id = call.data.split("_", 2)
    bot.answer_callback_query(call.id)

    st = get_state(chat_id)
    if not st or st.get("flow") != "vip_pick_item":
        bot.send_message(chat_id, L(lang, "⚠️ Wagt geçdi, ýene synanyşyň: ⭐ VIP", "⚠️ Время истекло, попробуйте снова: ⭐ VIP"))
        return
    plan_key = st["data"]["plan"]

    ok = apply_vip_purchase(chat_id, kind, item_id, plan_key)
    clear_state(chat_id)
    if ok:
        bot.send_message(chat_id, L(lang, "✅ VIP üstünlikli satyn alyndy! ⭐", "✅ VIP успешно куплен! ⭐"))
    else:
        bot.send_message(
            chat_id,
            L(
                lang,
                f"⚠️ Balansyňyz ýeterlik däl. Admin: @{ADMIN_USERNAME}",
                f"⚠️ Недостаточно баланса. Админ: @{ADMIN_USERNAME}",
            ),
        )
    send_main_menu(chat_id, lang)


@bot.callback_query_handler(func=lambda c: c.data in ("vipadd_load", "vipadd_drv"))
def cb_vip_add_new(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    bot.answer_callback_query(call.id)

    st = get_state(chat_id)
    if not st or st.get("flow") != "vip_pick_item":
        bot.send_message(chat_id, L(lang, "⚠️ Wagt geçdi, ýene synanyşyň: ⭐ VIP", "⚠️ Время истекло, попробуйте снова: ⭐ VIP"))
        return
    plan_key = st["data"]["plan"]

    if call.data == "vipadd_load":
        start_yuk_goy_flow(chat_id, lang, vip_plan=plan_key)
    else:
        start_sofor_flow(chat_id, lang, vip_plan=plan_key)


# ---------------------------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------------------------
def admin_panel_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("👥 Ulanyjylar", callback_data="adm_users_0"))
    kb.add(types.InlineKeyboardButton("📦 Ýükler", callback_data="adm_loads_0"))
    kb.add(types.InlineKeyboardButton("🚛 Şofýorlar", callback_data="adm_drivers_0"))
    kb.add(types.InlineKeyboardButton("⭐ VIP", callback_data="adm_vip_0"))
    kb.add(types.InlineKeyboardButton("📊 Statistikalar", callback_data="adm_stats"))
    kb.add(types.InlineKeyboardButton("📢 Bildiriş ugrat", callback_data="adm_broadcast_start"))
    return kb


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
    clear_state(chat_id)
    bot.send_message(chat_id, "🛠 Admin panel", reply_markup=admin_panel_kb())


@bot.message_handler(func=lambda m: m.text == "🛠 Admin panel")
def handle_admin_btn(message):
    cmd_admin(message)


@bot.callback_query_handler(func=lambda c: c.data == "adm_back")
def cb_adm_back(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("🛠 Admin panel", chat_id, call.message.message_id, reply_markup=admin_panel_kb())
    except Exception:
        bot.send_message(chat_id, "🛠 Admin panel", reply_markup=admin_panel_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_stats")
def cb_adm_stats(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)

    total_users = col_users.count_documents({})
    total_active_loads = col_yukler.count_documents({"status": "active"})
    total_sold_loads = col_yukler.count_documents({"status": "sold"})
    total_drivers = col_soforler.count_documents({})
    total_vip_loads = col_yukler.count_documents({"vip": True})
    total_vip_drivers = col_soforler.count_documents({"vip": True})
    total_banned = col_users.count_documents({"banned": True})

    txt = (
        "📊 <b>Statistikalar</b>\n\n"
        f"👥 Ulanyjylar: {total_users}\n"
        f"🚫 Banlananlar: {total_banned}\n"
        f"⭐ VIP ýükler: {total_vip_loads}\n"
        f"⭐ VIP maşynlar: {total_vip_drivers}\n"
        f"📦 Aktiw ýükler: {total_active_loads}\n"
        f"📦 Satylan ýükler: {total_sold_loads}\n"
        f"🚛 Şofýor ýerleşdirmeleri: {total_drivers}\n"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "adm_broadcast_start")
def cb_adm_broadcast_start(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    set_state(chat_id, "admin_broadcast", 1, {})
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Ýatyr", callback_data="adm_broadcast_cancel"))
    bot.send_message(
        chat_id,
        "📢 Ähli ulanyjylara iberiljek bildirişiň tekstini ýazyň:",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data == "adm_broadcast_cancel")
def cb_adm_broadcast_cancel(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    clear_state(chat_id)
    bot.answer_callback_query(call.id, "❌ Ýatyryldy.")
    try:
        bot.edit_message_text("🛠 Admin panel", chat_id, call.message.message_id, reply_markup=admin_panel_kb())
    except Exception:
        bot.send_message(chat_id, "🛠 Admin panel", reply_markup=admin_panel_kb())


def _run_admin_broadcast(admin_chat_id, text):
    sent, failed = 0, 0
    for u in col_users.find({"banned": {"$ne": True}}, {"telegram_id": 1}):
        tid = u.get("telegram_id")
        try:
            bot.send_message(tid, f"📢 <b>Admin bildirişi:</b>\n\n{text}")
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning("Bildiriş (admin broadcast) iberilmedi (%s): %s", tid, e)
    try:
        bot.send_message(admin_chat_id, f"✅ Bildiriş iberildi. Üstünlikli: {sent}, Säwlik: {failed}")
    except Exception:
        pass


def admin_broadcast_router(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text:
        bot.send_message(chat_id, "⚠️ Boş tekst. Ýene ýazyň:")
        return
    clear_state(chat_id)
    bot.send_message(chat_id, "⏳ Bildiriş iberilýär...")
    threading.Thread(target=_run_admin_broadcast, args=(chat_id, text), daemon=True).start()


# --- Ulanyjylar sanawy ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_users_"))
def cb_adm_users(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    page = int(call.data.split("_")[-1])
    bot.answer_callback_query(call.id)

    skip = page * ADMIN_PAGE_SIZE
    total = col_users.count_documents({})
    docs = list(col_users.find({}).sort("created_at", DESCENDING).skip(skip).limit(ADMIN_PAGE_SIZE))

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        label = f"{d.get('full_name','-')} (@{d.get('username','-')})"
        ban_mark = " 🚫" if d.get("banned") else ""
        vip_mark = " ⭐" if d.get("vip") else ""
        kb.add(types.InlineKeyboardButton(f"{label}{vip_mark}{ban_mark}", callback_data=f"adu_{d['telegram_id']}"))
    for row in build_page_nav("adm_users_", page, total, ADMIN_PAGE_SIZE):
        kb.row(*row)
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))

    txt = f"👥 Ulanyjylar ({total}):"
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


def user_detail_kb(target_id, banned):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("💰 + Bakiye", callback_data=f"adub_add_{target_id}"),
        types.InlineKeyboardButton("➖ Bakiye", callback_data=f"adub_sub_{target_id}"),
    )
    kb.add(types.InlineKeyboardButton("⭐ VIP açyk/ýapyk (ähli ýükler)", callback_data=f"aduv_{target_id}"))
    if banned:
        kb.add(types.InlineKeyboardButton("🔓 Unban", callback_data=f"adun_{target_id}"))
    else:
        kb.add(types.InlineKeyboardButton("🚫 Ban", callback_data=f"adba_{target_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_users_0"))
    return kb


def user_detail_text(d):
    return (
        f"👤 <b>{d.get('full_name','-')}</b>\n"
        f"🆔 ID: <code>{d.get('telegram_id')}</code>\n"
        f"🔗 Username: @{d.get('username','-')}\n"
        f"📞 Telefon: {d.get('phone','-')}\n"
        f"💰 Balans: {money(d.get('balance',0))}\n"
        f"⭐ VIP (admin bellän): {'Hawa' if d.get('vip') else 'Ýok'}\n"
        f"🚫 Ban: {'Hawa' if d.get('banned') else 'Ýok'}"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("adu_"))
def cb_adm_user_detail(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    target_id = int(call.data.split("_", 1)[1])
    bot.answer_callback_query(call.id)
    d = col_users.find_one({"telegram_id": target_id})
    if not d:
        bot.send_message(chat_id, "⚠️ Tapylmady.")
        return
    try:
        bot.edit_message_text(user_detail_text(d), chat_id, call.message.message_id, reply_markup=user_detail_kb(target_id, d.get("banned")))
    except Exception:
        bot.send_message(chat_id, user_detail_text(d), reply_markup=user_detail_kb(target_id, d.get("banned")))


@bot.callback_query_handler(func=lambda c: c.data.startswith("adub_"))
def cb_adm_balance_start(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    _, action, target_id = call.data.split("_", 2)
    target_id = int(target_id)
    bot.answer_callback_query(call.id)
    set_state(chat_id, "admin_balance", 1, {"action": action, "target_id": target_id})
    bot.send_message(
        chat_id,
        f"💰 {'Goşjak' if action == 'add' else 'Aýyrjak'} mukdaryňyzy ({CURRENCY}) ýazyň:",
    )


def admin_balance_router(message):
    chat_id = message.chat.id
    st = get_state(chat_id)
    data = st["data"]
    text = (message.text or "").strip()
    try:
        amount = float(text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ San ýazyň. Mysal: 50")
        return

    target_id = data["target_id"]
    delta = amount if data["action"] == "add" else -amount
    col_users.update_one({"telegram_id": target_id}, {"$inc": {"balance": delta}})
    clear_state(chat_id)
    bot.send_message(chat_id, f"✅ Balans täzelendi ({'+' if delta > 0 else ''}{money(delta)}).")

    target_lang = get_lang(target_id)
    try:
        bot.send_message(
            target_id,
            L(
                target_lang,
                f"💰 Balansyňyza {'goşuldy' if delta > 0 else 'aýryldy'}: {money(abs(delta))}",
                f"💰 Ваш баланс {'пополнен' if delta > 0 else 'уменьшен'} на: {money(abs(delta))}",
            ),
        )
    except Exception:
        pass

    bot.send_message(chat_id, "🛠 Admin panel", reply_markup=admin_panel_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("aduv_"))
def cb_adm_vip_toggle(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    target_id = int(call.data.split("_", 1)[1])
    d = col_users.find_one({"telegram_id": target_id})
    new_vip = not bool((d or {}).get("vip"))
    col_users.update_one({"telegram_id": target_id}, {"$set": {"vip": new_vip}})
    if new_vip:
        col_yukler.update_many({"owner_id": target_id, "status": "active"}, {"$set": {"vip": True, "vip_until": None}})
        col_soforler.update_many({"telegram_id": target_id}, {"$set": {"vip": True, "vip_until": None}})
    else:
        col_yukler.update_many({"owner_id": target_id}, {"$set": {"vip": False, "vip_until": None}})
        col_soforler.update_many({"telegram_id": target_id}, {"$set": {"vip": False, "vip_until": None}})
    bot.answer_callback_query(call.id, "✅ VIP täzelendi.")
    d["vip"] = new_vip
    try:
        bot.edit_message_text(user_detail_text(d), chat_id, call.message.message_id, reply_markup=user_detail_kb(target_id, d.get("banned")))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("adba_"))
def cb_adm_ban(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    target_id = int(call.data.split("_", 1)[1])
    if target_id in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⚠️ Admin banlanyp bilinmez.", show_alert=True)
        return
    col_users.update_one({"telegram_id": target_id}, {"$set": {"banned": True}})
    bot.answer_callback_query(call.id, "🚫 Banlandy.")
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=user_detail_kb(target_id, True))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("adun_"))
def cb_adm_unban(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    target_id = int(call.data.split("_", 1)[1])
    col_users.update_one({"telegram_id": target_id}, {"$set": {"banned": False}})
    bot.answer_callback_query(call.id, "🔓 Unban edildi.")
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=user_detail_kb(target_id, False))
    except Exception:
        pass


# --- Ýükler sanawy (admin) ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_loads_"))
def cb_adm_loads(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    page = int(call.data.split("_")[-1])
    bot.answer_callback_query(call.id)

    skip = page * ADMIN_PAGE_SIZE
    total = col_yukler.count_documents({})
    docs = list(col_yukler.find({}).sort("created_at", DESCENDING).skip(skip).limit(ADMIN_PAGE_SIZE))

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        status_icon = {"active": "🟢", "sold": "🔴"}.get(d.get("status"), "⚪️")
        vip_mark = " ⭐" if d.get("vip") else ""
        label = f"{status_icon} {d['from_loc']} → {d['to_loc']} | {d['price']}${vip_mark}"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"adlv_{d['_id']}"))
    for row in build_page_nav("adm_loads_", page, total, ADMIN_PAGE_SIZE):
        kb.row(*row)
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))

    txt = f"📦 Ýükler ({total}):"
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


def admin_load_detail_kb(load_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🗑 Poz", callback_data=f"adld_{load_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_loads_0"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data.startswith("adlv_"))
def cb_adm_load_view(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    load_id = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    try:
        d = col_yukler.find_one({"_id": ObjectId(load_id)})
    except InvalidId:
        d = None
    if not d:
        bot.send_message(chat_id, "⚠️ Tapylmady.")
        return
    status_label = STATUS_LABEL_TM.get(d.get("status"), "-")
    txt = (
        yuk_detail_text("tm", d) + f"\n\nℹ️ Status: {status_label}\n"
        f"👤 Eýesi: {d.get('owner_name','-')} (@{d.get('owner_username','-')}, ID: <code>{d.get('owner_id')}</code>)"
    )
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=admin_load_detail_kb(load_id))
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=admin_load_detail_kb(load_id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("adld_"))
def cb_adm_load_delete(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    load_id = call.data.split("_", 1)[1]
    try:
        result = col_yukler.delete_one({"_id": ObjectId(load_id)})
    except InvalidId:
        result = None
    if result and result.deleted_count:
        bot.answer_callback_query(call.id, "🗑 Pozuldy.")
        try:
            bot.edit_message_text("🗑 Ýük pozuldy.", chat_id, call.message.message_id, reply_markup=admin_panel_kb())
        except Exception:
            bot.send_message(chat_id, "🛠 Admin panel", reply_markup=admin_panel_kb())
    else:
        bot.answer_callback_query(call.id, "⚠️ Tapylmady.", show_alert=True)


# --- Şofýorlar sanawy (admin) ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_drivers_"))
def cb_adm_drivers(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    page = int(call.data.split("_")[-1])
    bot.answer_callback_query(call.id)

    skip = page * ADMIN_PAGE_SIZE
    total = col_soforler.count_documents({})
    docs = list(col_soforler.find({}).sort("created_at", DESCENDING).skip(skip).limit(ADMIN_PAGE_SIZE))

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        vip_mark = " ⭐" if d.get("vip") else ""
        label = f"🚛 {d['from_loc']} → {d['to_loc']} | {d.get('model','-')} | {d.get('ton')}t{vip_mark}"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"adsv_{d['_id']}"))
    for row in build_page_nav("adm_drivers_", page, total, ADMIN_PAGE_SIZE):
        kb.row(*row)
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))

    txt = f"🚛 Şofýorlar / Boş maşynlar ({total}):"
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


def admin_driver_detail_kb(driver_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🗑 Poz", callback_data=f"adsd_{driver_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_drivers_0"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data.startswith("adsv_"))
def cb_adm_driver_view(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    driver_id = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    try:
        d = col_soforler.find_one({"_id": ObjectId(driver_id)})
    except InvalidId:
        d = None
    if not d:
        bot.send_message(chat_id, "⚠️ Tapylmady.")
        return
    type_label = CARGO_TYPES.get(d.get("cargo_type"), "-")
    txt = (
        f"🚛 <b>Boş maşyn</b>\n\n"
        f"📍 {d['from_loc']} → {d['to_loc']}\n"
        f"🚛 Model: {d.get('model','-')}\n"
        f"⚖️ Kuwwaty: {d.get('ton')} t\n"
        f"🚛 Görnüşi: {type_label}\n"
        f"📝 Goşmaça: {d.get('extra','-') or '-'}\n\n"
        f"👤 {d.get('full_name','-')} (@{d.get('username','-')}, ID: <code>{d.get('telegram_id')}</code>)"
    )
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=admin_driver_detail_kb(driver_id))
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=admin_driver_detail_kb(driver_id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("adsd_"))
def cb_adm_driver_delete(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    driver_id = call.data.split("_", 1)[1]
    try:
        result = col_soforler.delete_one({"_id": ObjectId(driver_id)})
    except InvalidId:
        result = None
    if result and result.deleted_count:
        bot.answer_callback_query(call.id, "🗑 Pozuldy.")
        try:
            bot.edit_message_text("🗑 Maşyn ilany pozuldy.", chat_id, call.message.message_id, reply_markup=admin_panel_kb())
        except Exception:
            bot.send_message(chat_id, "🛠 Admin panel", reply_markup=admin_panel_kb())
    else:
        bot.answer_callback_query(call.id, "⚠️ Tapylmady.", show_alert=True)


# --- VIP sanawy (admin) ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_vip_"))
def cb_adm_vip_list(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    page = int(call.data.split("_")[-1])
    bot.answer_callback_query(call.id)

    skip = page * ADMIN_PAGE_SIZE
    total = col_users.count_documents({"vip": True})
    docs = list(col_users.find({"vip": True}).skip(skip).limit(ADMIN_PAGE_SIZE))

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        kb.add(types.InlineKeyboardButton(f"⭐ {d.get('full_name','-')} (@{d.get('username','-')})", callback_data=f"adu_{d['telegram_id']}"))
    for row in build_page_nav("adm_vip_", page, total, ADMIN_PAGE_SIZE):
        kb.row(*row)
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))

    txt = f"⭐ VIP ulanyjylar ({total}):"
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


# ---------------------------------------------------------------------------
# UMUMY TEKST DISPATCHER (FSM ýagdaýlaryny dolandyrýar)
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def generic_text_dispatcher(message):
    chat_id = message.chat.id

    if message.text and message.text.startswith("/"):
        return  # komanda handler-leri eýýäm işledi

    user, lang, ok = guard_access(chat_id)
    if not ok:
        return

    st = get_state(chat_id)
    if not st:
        if user and not is_registered(user):
            cmd_start(message)
        return

    flow = st["flow"]
    if flow == "yuk_goy":
        yuk_goy_router(message)
    elif flow == "sofor":
        sofor_router(message)
    elif flow == "zalwa":
        zalwa_router(message)
    elif flow == "admin_balance" and is_admin(chat_id):
        admin_balance_router(message)
    elif flow == "admin_broadcast" and is_admin(chat_id):
        admin_broadcast_router(message)
    else:
        clear_state(chat_id)


# ---------------------------------------------------------------------------
# FLASK - HEALTH CHECK (Render üçin)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return jsonify(status="ok", service="tir-bot"), 200


@flask_app.route("/health")
def health():
    return jsonify(status="healthy", time=datetime.now(timezone.utc).isoformat()), 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def self_ping_loop():
    """
    Render.com-yň mugt tarifinde bot 'sleep' bolmazlygy üçin, botuň özi
    her 60 sekuntda öz /health endpoint-ine HTTP request iberýär.
    """
    if not RENDER_URL:
        logger.warning("RENDER_URL berilmedi, self-ping togtadyldy.")
        return
    url = RENDER_URL.rstrip("/") + "/health"
    while True:
        try:
            requests.get(url, timeout=10)
            logger.info("Self-ping OK: %s", url)
        except Exception as e:
            logger.warning("Self-ping säwligi: %s", e)
        time.sleep(60)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask health-check %s portda başlady.", PORT)

    ping_thread = threading.Thread(target=self_ping_loop, daemon=True)
    ping_thread.start()

    logger.info("Bot polling başlaýar...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
        except Exception as e:
            logger.error("Polling säwligi, 5 sekuntdan täzeden başlaýar: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
