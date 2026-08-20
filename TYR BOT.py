#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TIR / Ýük daşama platformasy - Telegram bot
Diller: Türkmençe (tm) we Rusça (ru)
Stack: pyTelegramBotAPI (telebot) + Flask (health-check üçin) + MongoDB (pymongo)
Deploy: Render.com (Environment Variables arkaly)

Environment Variables:
    BOT_TOKEN     - Telegram bot token (@BotFather)
    MONGODB_URI   - MongoDB birikme salgysy (aňsatlyk üçin, başga bot bilen paýlaşylsa-da,
                     kolleksiýa atlary aşakda ýörite üýtgedildi, üstünden geçme ýok bolar)
    DATABASE_NAME - MongoDB baza ady
    ADMIN_IDS     - Admin Telegram ID-leri, otur bilen bölünen. Mysal: 111111,222222
    RENDER_URL    - Botuň Render.com-daky doly salgysy. Mysal: https://seniň-botuň.onrender.com
                     (bot her 60 sekuntda özüniň /health endpoint-ine sorag iberer, "sleep"
                      bolmazlygy üçin)
    PORT          - Render tarapyndan awtomatik berilýär (Flask şuny ulanar)
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone

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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MONGODB_URI = os.environ.get("MONGODB_URI", "").strip()
DATABASE_NAME = os.environ.get("DATABASE_NAME", "").strip()
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "").strip()
# RENDER_URL - botuň öz-özüne "uklamazlyk" üçin ping iberjek salgysy.
# Render.com-da "Settings" bölüminden ýa-da şu ýere göni ýazyp bilersiň:
RENDER_URL = os.environ.get("RENDER_URL", "").strip()  # <-- BU ÝERE (ýa-da Environment Variables-a) öz Render salgyňy ýaz. Mysal: https://seniň-botuň.onrender.com
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

VIP_PRICE = 100  # $ - VIP bahasy
PAGE_SIZE = 5    # Sahypadaky ýük/ulanyjy sany

# ---------------------------------------------------------------------------
# MONGODB
# ---------------------------------------------------------------------------
# ÜNS: MONGODB_URI başga (sponsor) bot bilen paýlaşylýan bolsa-da, bu ýerdäki
# kolleksiýa atlary ýörite "_tirplt" ýaly goşmaça bilen üýtgedildi welin,
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
        col_soforler.create_index([("telegram_id", ASCENDING)], background=True)
        col_soforler.create_index([("created_at", DESCENDING)], background=True)
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


# ---------------------------------------------------------------------------
# ULANYJY KÖMEKÇI FUNKSIÝALARY
# ---------------------------------------------------------------------------
def get_user(chat_id):
    return col_users.find_one({"telegram_id": chat_id})


def is_admin(chat_id):
    return chat_id in ADMIN_IDS


def is_registered(user_doc):
    return bool(user_doc and user_doc.get("phone") and user_doc.get("username") and user_doc.get("lang"))


def main_menu_kb(lang, chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(types.KeyboardButton(L(lang, "📦 Yük goý", "📦 Разместить груз")))
    kb.add(types.KeyboardButton(L(lang, "📦 Yük gözle", "📦 Искать груз")))
    kb.add(types.KeyboardButton(L(lang, "🚛 Maşynym bar — ýük gözleýärin", "🚛 У меня есть машина — ищу груз")))
    kb.add(types.KeyboardButton(L(lang, "⭐ VIP", "⭐ VIP")))
    kb.add(types.KeyboardButton(L(lang, "🌐 Dil çalyş", "🌐 Сменить язык")))
    if is_admin(chat_id):
        kb.add(types.KeyboardButton("🛠 Admin panel"))
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
        # ady/username täzelenen bolsa täzele
        col_users.update_one(
            {"telegram_id": chat_id},
            {"$set": {
                "full_name": message.from_user.full_name or "",
                "username": message.from_user.username or user.get("username"),
            }},
        )
        send_main_menu(chat_id, lang, L(lang, "👋 Hoş geldiňiz! Baş menýu:", "👋 Добро пожаловать! Главное меню:"))
        return

    # Täze ulanyjy - ilki dil saýlansyn
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
def is_text(message, lang_key_tm, lang_key_ru):
    return message.text in (lang_key_tm, lang_key_ru)


@bot.message_handler(func=lambda m: m.text in ("🌐 Dil çalyş", "🌐 Сменить язык"))
def handle_lang_switch(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    clear_state(chat_id)
    bot.send_message(chat_id, "🇹🇲 Dili saýlaň / 🇷🇺 Выберите язык:", reply_markup=lang_kb())


# ---------------------------------------------------------------------------
# ÝÜK GOÝMAK AKYMY
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
    kb.add(types.InlineKeyboardButton(L(lang, "✅ Yük goý", "✅ Разместить"), callback_data=f"{prefix}_confirm"))
    kb.add(types.InlineKeyboardButton(L(lang, "✏️ Üýtget", "✏️ Изменить"), callback_data=f"{prefix}_edit"))
    kb.add(types.InlineKeyboardButton(L(lang, "❌ Ýatyr", "❌ Отмена"), callback_data=f"{prefix}_cancel"))
    return kb


@bot.message_handler(func=lambda m: m.text in ("📦 Yük goý", "📦 Разместить груз"))
def handle_yuk_goy_start(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    set_state(chat_id, "yuk_goy", 1, {})
    bot.send_message(
        chat_id,
        L(lang, "📍 Nerden? (ýükleme ýeri)", "📍 Откуда? (место погрузки)"),
        reply_markup=types.ReplyKeyboardRemove(),
    )


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
        st["step"] = 2
        set_state(chat_id, "yuk_goy", 2, data)
        bot.send_message(chat_id, L(lang, "📍 Nereye? (barjak ýeri)", "📍 Куда? (место назначения)"))

    elif step == 2:
        if not text:
            bot.send_message(chat_id, L(lang, "Ýalňyş. Ýene ýazyň:", "Ошибка. Введите снова:"))
            return
        data["to_loc"] = text
        st["step"] = 3
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
        st["step"] = 4
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
        st["step"] = 6
        set_state(chat_id, "yuk_goy", 6, data)
        bot.send_message(chat_id, L(lang, "📦 Ýük näme?", "📦 Что за груз?"))

    elif step == 6:
        if not text:
            bot.send_message(chat_id, L(lang, "Ýalňyş. Ýene ýazyň:", "Ошибка. Введите снова:"))
            return
        data["cargo"] = text
        st["step"] = 7
        set_state(chat_id, "yuk_goy", 7, data)
        show_yuk_goy_summary(chat_id, lang, data)


def show_yuk_goy_summary(chat_id, lang, data):
    type_label = CARGO_TYPES.get(data.get("cargo_type"), "-")
    txt = L(
        lang,
        "📋 <b>Ýüküň maglumaty:</b>\n\n"
        f"📍 Nerden: {data['from_loc']}\n"
        f"📍 Nereye: {data['to_loc']}\n"
        f"⚖️ Tonna: {data['ton']}\n"
        f"🚛 Görnüşi: {type_label}\n"
        f"💵 Baha: {data['price']}$\n"
        f"📦 Ýük: {data['cargo']}\n\n"
        "Tassyklaýarsyňyzmy?",
        "📋 <b>Данные груза:</b>\n\n"
        f"📍 Откуда: {data['from_loc']}\n"
        f"📍 Куда: {data['to_loc']}\n"
        f"⚖️ Тонн: {data['ton']}\n"
        f"🚛 Тип: {type_label}\n"
        f"💵 Цена: {data['price']}$\n"
        f"📦 Груз: {data['cargo']}\n\n"
        "Подтверждаете?",
    )
    bot.send_message(chat_id, txt, reply_markup=confirm_kb(lang, "yg"))


@bot.callback_query_handler(func=lambda c: c.data in ("ctype_tent", "ctype_ref"))
def cb_cargo_type(call):
    """
    Tent/Ref saýlawy iki dürli akymda ulanylýar:
      - yuk_goy flow, step 4 -> 5 (baha soralýar)
      - sofor flow, step 5 -> 6 (goşmaça maglumat soralýar)
    Şoňa görä STATE-däki "flow" barlanyp, degişli ugra gönükdirilýär.
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
        set_state(chat_id, "yuk_goy", 1, {})
        bot.send_message(chat_id, L(lang, "📍 Nerden? (ýükleme ýeri)", "📍 Откуда? (место погрузки)"))
        return

    if action == "confirm":
        user = get_user(chat_id)
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
            "status": "active",
            "vip": bool(user.get("vip")),
            "taker_id": None,
            "taker_username": None,
            "created_at": datetime.now(timezone.utc),
        }
        col_yukler.insert_one(doc)
        clear_state(chat_id)
        bot.send_message(chat_id, L(lang, "✅ Ýük üstünlikli goýuldy!", "✅ Груз успешно размещён!"))
        send_main_menu(chat_id, lang)


# ---------------------------------------------------------------------------
# ÝÜKLERI GÖRMEK / GÖZLEMEK
# ---------------------------------------------------------------------------
def format_yuk_label(doc):
    star = "⭐ " if doc.get("vip") else ""
    type_icon = "❄️ Ref" if doc.get("cargo_type") == "ref" else "🚛 Tent"
    return f"{star}{doc['from_loc']} - {doc['to_loc']} | {type_icon} | {doc['price']}$"


def yukler_list_kb(lang, page):
    skip = page * PAGE_SIZE
    cursor = (
        col_yukler.find({"status": "active"})
        .sort([("vip", DESCENDING), ("created_at", DESCENDING)])
        .skip(skip)
        .limit(PAGE_SIZE)
    )
    docs = list(cursor)
    total = col_yukler.count_documents({"status": "active"})

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        kb.add(types.InlineKeyboardButton(format_yuk_label(d), callback_data=f"yv_{d['_id']}"))

    nav_row = []
    if page > 0:
        nav_row.append(types.InlineKeyboardButton("⬅️", callback_data=f"ylp_{page-1}"))
    if skip + PAGE_SIZE < total:
        nav_row.append(types.InlineKeyboardButton("➡️", callback_data=f"ylp_{page+1}"))
    if nav_row:
        kb.row(*nav_row)

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
    kb, docs, total = yukler_list_kb(lang, 0)
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
    kb, docs, total = yukler_list_kb(lang, page)
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
    return L(
        lang,
        f"{star}📍 Nerden: {d['from_loc']}\n"
        f"📍 Nereye: {d['to_loc']}\n"
        f"⚖️ Tonna: {d['ton']}\n"
        f"🚛 Görnüşi: {type_label}\n"
        f"💵 Baha: {d['price']}$\n"
        f"📦 Ýük: {d['cargo']}\n"
        f"🗓 Ýerleşdirilen senesi: {date_str}",
        f"{star}📍 Откуда: {d['from_loc']}\n"
        f"📍 Куда: {d['to_loc']}\n"
        f"⚖️ Тонн: {d['ton']}\n"
        f"🚛 Тип: {type_label}\n"
        f"💵 Цена: {d['price']}$\n"
        f"📦 Груз: {d['cargo']}\n"
        f"🗓 Дата размещения: {date_str}",
    )


def yuk_detail_kb(lang, load_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(L(lang, "📦 Yük al", "📦 Взять груз"), callback_data=f"yal_{load_id}"))
    kb.add(types.InlineKeyboardButton(L(lang, "⚠️ Zalwa", "⚠️ Жалоба"), callback_data=f"yzw_{load_id}"))
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
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    load_id = call.data.split("_", 1)[1]

    try:
        oid = ObjectId(load_id)
    except InvalidId:
        bot.answer_callback_query(call.id, L(lang, "Ýalňyşlyk", "Ошибка"), show_alert=True)
        return

    d = col_yukler.find_one({"_id": oid})
    if d and d.get("owner_id") == chat_id:
        bot.answer_callback_query(
            call.id,
            L(lang, "⚠️ Öz ýüküňizi alyp bilmersiňiz.", "⚠️ Нельзя взять свой собственный груз."),
            show_alert=True,
        )
        return

    user = get_user(chat_id)

    # Atomik täzelenme - "race condition" öňüni almak üçin
    updated = col_yukler.find_one_and_update(
        {"_id": oid, "status": "active"},
        {"$set": {
            "status": "taken",
            "taker_id": chat_id,
            "taker_username": (user or {}).get("username", ""),
            "taker_name": (user or {}).get("full_name", ""),
            "taken_at": datetime.now(timezone.utc),
        }},
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        bot.answer_callback_query(
            call.id,
            L(lang, "⚠️ Bu ýük eýýäm alyndy.", "⚠️ Этот груз уже взят."),
            show_alert=True,
        )
        return

    bot.answer_callback_query(call.id)

    owner_username = updated.get("owner_username") or ""
    owner_name = updated.get("owner_name") or "-"
    link = f"@{owner_username}" if owner_username else L(lang, "(username ýok)", "(нет username)")
    bot.send_message(
        chat_id,
        L(
            lang,
            f"✅ Ýük alyndy!\n\n👤 {owner_name}\n🔗 {link}",
            f"✅ Груз взят!\n\n👤 {owner_name}\n🔗 {link}",
        ),
    )

    owner_id = updated.get("owner_id")
    taker_username = (user or {}).get("username", "")
    taker_link = f"@{taker_username}" if taker_username else L(lang, "(username ýok)", "(нет username)")
    owner_lang = get_lang(owner_id)
    sold_kb = types.InlineKeyboardMarkup()
    sold_kb.add(types.InlineKeyboardButton(L(owner_lang, "✅ Satylan", "✅ Продано"), callback_data=f"sold_{oid}"))
    try:
        bot.send_message(
            owner_id,
            L(
                owner_lang,
                f"📦 Ýüküňize bir ulanyjy gyzyklandy.\n👤 {taker_link}\n\n"
                "Eger ýüküňizi satsanyz, aşakdaky 'Satylan' düwmesine basyň.",
                f"📦 Вашим грузом заинтересовался пользователь.\n👤 {taker_link}\n\n"
                "Если груз продан, нажмите кнопку 'Продано' ниже.",
            ),
            reply_markup=sold_kb,
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
        {"_id": oid, "owner_id": chat_id},
        {"$set": {"status": "sold", "sold_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        bot.answer_callback_query(call.id, L(lang, "⚠️ Tapylmady.", "⚠️ Не найдено."), show_alert=True)
        return

    bot.answer_callback_query(call.id, L(lang, "✅ Bellendi.", "✅ Отмечено."))
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, L(lang, "✅ Ýük 'satylan' diýlip bellendi.", "✅ Груз отмечен как проданный."))
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
@bot.message_handler(func=lambda m: m.text in ("🚛 Maşynym bar — ýük gözleýärin", "🚛 У меня есть машина — ищу груз"))
def handle_sofor_start(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return
    set_state(chat_id, "sofor", 1, {})
    bot.send_message(
        chat_id,
        L(lang, "📍 Häzir nirede?", "📍 Где сейчас находитесь?"),
        reply_markup=types.ReplyKeyboardRemove(),
    )


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
        st["step"] = 2
        set_state(chat_id, "sofor", 2, data)
        bot.send_message(chat_id, L(lang, "📍 Nirä gitjek?", "📍 Куда направляетесь?"))

    elif step == 2:
        data["to_loc"] = text
        st["step"] = 3
        set_state(chat_id, "sofor", 3, data)
        bot.send_message(chat_id, L(lang, "🚛 Maşynyň modeli?", "🚛 Модель автомобиля?"))

    elif step == 3:
        data["model"] = text
        st["step"] = 4
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
        st["step"] = 5
        set_state(chat_id, "sofor", 5, data)
        bot.send_message(chat_id, L(lang, "🚛 Prisep görnüşi:", "🚛 Тип прицепа:"), reply_markup=type_kb(lang))

    elif step == 6:
        data["extra"] = text
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
        "created_at": datetime.now(timezone.utc),
    }
    col_soforler.insert_one(doc)
    clear_state(chat_id)
    bot.send_message(chat_id, L(lang, "✅ Maşynyňyz üstünlikli goşuldy!", "✅ Ваш автомобиль успешно добавлен!"))
    send_main_menu(chat_id, lang)


# ---------------------------------------------------------------------------
# VIP
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "⭐ VIP")
def handle_vip(message):
    chat_id = message.chat.id
    user, lang, ok = guard_access(chat_id)
    if not ok:
        return
    if not is_registered(user):
        cmd_start(message)
        return

    balance = user.get("balance", 0)
    vip = user.get("vip", False)

    txt = L(
        lang,
        f"⭐ <b>VIP</b>\n\n💰 Balansyňyz: {balance}$\n"
        f"{'✅ Siz häzir VIP ulanyjysyňyz.' if vip else f'VIP bahasy: {VIP_PRICE}$'}",
        f"⭐ <b>VIP</b>\n\n💰 Ваш баланс: {balance}$\n"
        f"{'✅ Вы уже VIP-пользователь.' if vip else f'Цена VIP: {VIP_PRICE}$'}",
    )
    kb = types.InlineKeyboardMarkup()
    if not vip:
        kb.add(types.InlineKeyboardButton(L(lang, f"⭐ VIP satyn al ({VIP_PRICE}$)", f"⭐ Купить VIP ({VIP_PRICE}$)"), callback_data="vip_buy"))
    bot.send_message(chat_id, txt, reply_markup=kb if not vip else None)


@bot.callback_query_handler(func=lambda c: c.data == "vip_buy")
def cb_vip_buy(call):
    chat_id = call.message.chat.id
    lang = get_lang(chat_id)
    bot.answer_callback_query(call.id)

    user = col_users.find_one_and_update(
        {"telegram_id": chat_id, "balance": {"$gte": VIP_PRICE}, "vip": {"$ne": True}},
        {"$inc": {"balance": -VIP_PRICE}, "$set": {"vip": True}},
        return_document=ReturnDocument.AFTER,
    )

    if not user:
        bot.send_message(
            chat_id,
            L(
                lang,
                "⚠️ Balansyňyz ýeterlik däl. Balans goşmak üçin admin bilen habarlaşyň.",
                "⚠️ Недостаточно средств на балансе. Обратитесь к администратору для пополнения.",
            ),
        )
        return

    col_vip.insert_one({
        "user_id": chat_id,
        "price": VIP_PRICE,
        "created_at": datetime.now(timezone.utc),
    })
    col_yukler.update_many({"owner_id": chat_id, "status": "active"}, {"$set": {"vip": True}})

    bot.send_message(chat_id, L(lang, "✅ VIP satyn alyndy!", "✅ VIP успешно куплен!"))


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
    total_taken_loads = col_yukler.count_documents({"status": "taken"})
    total_drivers = col_soforler.count_documents({})
    total_vip = col_users.count_documents({"vip": True})
    total_banned = col_users.count_documents({"banned": True})

    txt = (
        "📊 <b>Statistikalar</b>\n\n"
        f"👥 Ulanyjylar: {total_users}\n"
        f"🚫 Banlananlar: {total_banned}\n"
        f"⭐ VIP ulanyjylar: {total_vip}\n"
        f"📦 Aktiw ýükler: {total_active_loads}\n"
        f"📦 Alnan ýükler: {total_taken_loads}\n"
        f"📦 Satylan ýükler: {total_sold_loads}\n"
        f"🚛 Şofýor ýerleşdirmeleri: {total_drivers}\n"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


# --- Ulanyjylar sanawy ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_users_"))
def cb_adm_users(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    page = int(call.data.split("_")[-1])
    bot.answer_callback_query(call.id)

    skip = page * PAGE_SIZE
    total = col_users.count_documents({})
    docs = list(col_users.find({}).sort("created_at", DESCENDING).skip(skip).limit(PAGE_SIZE))

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        label = f"{d.get('full_name','-')} (@{d.get('username','-')})"
        ban_mark = " 🚫" if d.get("banned") else ""
        vip_mark = " ⭐" if d.get("vip") else ""
        kb.add(types.InlineKeyboardButton(f"{label}{vip_mark}{ban_mark}", callback_data=f"adu_{d['telegram_id']}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"adm_users_{page-1}"))
    if skip + PAGE_SIZE < total:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=f"adm_users_{page+1}"))
    if nav:
        kb.row(*nav)
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
    kb.add(types.InlineKeyboardButton("⭐ VIP açyk/ýapyk", callback_data=f"aduv_{target_id}"))
    if banned:
        kb.add(types.InlineKeyboardButton("🔓 Unban", callback_data=f"adun_{target_id}"))
    else:
        kb.add(types.InlineKeyboardButton("🚫 Ban", callback_data=f"adba_{target_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_users_0"))
    return kb


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

    txt = (
        f"👤 <b>{d.get('full_name','-')}</b>\n"
        f"🆔 ID: <code>{d.get('telegram_id')}</code>\n"
        f"🔗 Username: @{d.get('username','-')}\n"
        f"📞 Telefon: {d.get('phone','-')}\n"
        f"💰 Balans: {d.get('balance',0)}$\n"
        f"⭐ VIP: {'Hawa' if d.get('vip') else 'Ýok'}\n"
        f"🚫 Ban: {'Hawa' if d.get('banned') else 'Ýok'}"
    )
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=user_detail_kb(target_id, d.get("banned")))
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=user_detail_kb(target_id, d.get("banned")))


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
        f"💰 {'Goşjak' if action == 'add' else 'Aýyrjak'} mukdaryňyzy ($) ýazyň:",
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
    bot.send_message(chat_id, f"✅ Balans täzelendi ({'+' if delta > 0 else ''}{delta}$).")

    target_lang = get_lang(target_id)
    try:
        bot.send_message(
            target_id,
            L(
                target_lang,
                f"💰 Balansyňyza {'goşuldy' if delta > 0 else 'aýryldy'}: {abs(delta)}$",
                f"💰 Ваш баланс {'пополнен' if delta > 0 else 'уменьшен'} на: {abs(delta)}$",
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
    col_yukler.update_many({"owner_id": target_id, "status": "active"}, {"$set": {"vip": new_vip}})
    bot.answer_callback_query(call.id, "✅ VIP täzelendi.")
    d["vip"] = new_vip
    txt = (
        f"👤 <b>{d.get('full_name','-')}</b>\n"
        f"🆔 ID: <code>{d.get('telegram_id')}</code>\n"
        f"🔗 Username: @{d.get('username','-')}\n"
        f"📞 Telefon: {d.get('phone','-')}\n"
        f"💰 Balans: {d.get('balance',0)}$\n"
        f"⭐ VIP: {'Hawa' if d.get('vip') else 'Ýok'}\n"
        f"🚫 Ban: {'Hawa' if d.get('banned') else 'Ýok'}"
    )
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=user_detail_kb(target_id, d.get("banned")))
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
    d = col_users.find_one({"telegram_id": target_id})
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

    skip = page * PAGE_SIZE
    total = col_yukler.count_documents({})
    docs = list(col_yukler.find({}).sort("created_at", DESCENDING).skip(skip).limit(PAGE_SIZE))

    lines = [f"📦 Ýükler ({total}):\n"]
    for d in docs:
        status_icon = {"active": "🟢", "taken": "🟡", "sold": "🔴"}.get(d.get("status"), "⚪️")
        lines.append(f"{status_icon} {d['from_loc']} - {d['to_loc']} | {d['price']}$ | {d.get('status')}")

    kb = types.InlineKeyboardMarkup()
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"adm_loads_{page-1}"))
    if skip + PAGE_SIZE < total:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=f"adm_loads_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))

    txt = "\n".join(lines)
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


# --- Şofýorlar sanawy (admin) ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_drivers_"))
def cb_adm_drivers(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    page = int(call.data.split("_")[-1])
    bot.answer_callback_query(call.id)

    skip = page * PAGE_SIZE
    total = col_soforler.count_documents({})
    docs = list(col_soforler.find({}).sort("created_at", DESCENDING).skip(skip).limit(PAGE_SIZE))

    lines = [f"🚛 Şofýorlar ({total}):\n"]
    for d in docs:
        lines.append(f"@{d.get('username','-')} | {d['from_loc']} - {d['to_loc']} | {d.get('model','-')} | {d.get('ton')}t")

    kb = types.InlineKeyboardMarkup()
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"adm_drivers_{page-1}"))
    if skip + PAGE_SIZE < total:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=f"adm_drivers_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("🔙 Yza", callback_data="adm_back"))

    txt = "\n".join(lines)
    try:
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


# --- VIP sanawy (admin) ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_vip_"))
def cb_adm_vip_list(call):
    chat_id = call.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return
    page = int(call.data.split("_")[-1])
    bot.answer_callback_query(call.id)

    skip = page * PAGE_SIZE
    total = col_users.count_documents({"vip": True})
    docs = list(col_users.find({"vip": True}).skip(skip).limit(PAGE_SIZE))

    kb = types.InlineKeyboardMarkup()
    for d in docs:
        kb.add(types.InlineKeyboardButton(f"⭐ {d.get('full_name','-')} (@{d.get('username','-')})", callback_data=f"adu_{d['telegram_id']}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"adm_vip_{page-1}"))
    if skip + PAGE_SIZE < total:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=f"adm_vip_{page+1}"))
    if nav:
        kb.row(*nav)
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
        # Registrasiýa tamamlanmadyk bolup, main-menýu düwmelerinden başga zat ýazan bolsa
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
    RENDER_URL Environment Variable-yny goýmagy unutma!
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
