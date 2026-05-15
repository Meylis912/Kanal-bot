import telebot
import threading
import time
import os
import string
import random
import requests
import datetime
import certifi
from flask import Flask
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from bson import ObjectId
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ╔══════════════════════════════════════════════════════════╗
#                     КОНФИГУРАЦИЯ
# ╚══════════════════════════════════════════════════════════╝
BOT_TOKEN    = "8946941782:AAEySgJHyQE7IlMqfoyrtmQ5ePIoA-iCqfA"
ADMIN_ID     = 7523674506
REWARD_TMT   = 0.50
WITHDRAW_MIN = 1.0
RENDER_URL    = "https://kanal-bot.onrender.com"

MONGO_URI = (
    "mongodb+srv://mergenowlyagulyyew41_db_user:ZvZhOKOAF6ZMRbHX@cluster1.l8z8gll.mongodb.net/?appName=Cluster1"
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ╔══════════════════════════════════════════════════════════╗
#                   MONGODB ПОДКЛЮЧЕНИЕ
# ╚══════════════════════════════════════════════════════════╝
_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=10000
)
try:
    _client.admin.command("ping")
    print("✅ MongoDB подключён!")
except ConnectionFailure:
    print("❌ MongoDB ошибка подключения!")

_db          = _client["bot_data"]
col_users    = _db["users"]
col_sponsors = _db["sponsors"]     # Спонсоры
col_addlist  = _db["addlist"]      # Addlist
col_settings = _db["settings"]
col_reklam   = _db["reklam"]
col_promo    = _db["promo"]        # Промокоды
col_tgrass_channels = _db["tgrass_channels"]
col_post_channels   = _db["post_channels"]   # Рекламный каналы (ручное управление)

col_users.create_index("user_id", unique=True)

# ╔══════════════════════════════════════════════════════════╗
#                   НАСТРОЙКИ (MongoDB)
# ╚══════════════════════════════════════════════════════════╝
def get_setting(key, default=""):
    doc = col_settings.find_one({"key": key})
    return doc["value"] if doc else default

def set_setting(key, value):
    col_settings.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

# Дефолтные настройки
if not get_setting("vpn_code"):
    set_setting("vpn_code", "SHADOWVIP-2024")
if not get_setting("tgrass"):
    set_setting("tgrass", "on")
if not get_setting("welcome_text"):
    set_setting("welcome_text",
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Для получения VPN-кода вступите в наши каналы 👇"
    )

# ╔══════════════════════════════════════════════════════════╗
#                   ФУНКЦИИ БД — КАНАЛЫ
# ╚══════════════════════════════════════════════════════════╝
def _channel_list(col):
    return [(str(d["_id"]), d.get("link",""), d.get("name",""), d.get("username",""))
            for d in col.find()]

def _add_channel(col, link, name, username):
    uname = username.lstrip("@")
    col.update_one(
        {"username": uname},
        {"$set": {"link": link, "name": name, "username": uname}},
        upsert=True
    )

def _del_channel(col, doc_id):
    try:
        col.delete_one({"_id": ObjectId(doc_id)})
    except Exception:
        pass

def get_sponsors():   return _channel_list(col_sponsors)
def get_addlist():    return _channel_list(col_addlist)

def add_sponsor(link, name, username):  _add_channel(col_sponsors, link, name, username)
def add_addlist(link, name, username):  _add_channel(col_addlist,  link, name, username)

def del_sponsor(doc_id): _del_channel(col_sponsors, doc_id)
def del_addlist(doc_id): _del_channel(col_addlist,  doc_id)

# ── Пост-каналы: добавить/удалить/получить список ──────────────────────────
def get_post_channels():
    return list(col_post_channels.find())

def add_post_channel(name, username):
    uname = username.strip().lstrip("@")
    col_post_channels.update_one(
        {"username": uname},
        {"$set": {"name": name, "username": uname}},
        upsert=True
    )

def del_post_channel(doc_id):
    try:
        col_post_channels.delete_one({"_id": ObjectId(doc_id)})
    except Exception:
        pass

def parse_channel_args(text):
    """'/cmd name @chan' → (name, link, username)"""
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return None, None, None
    name = parts[1]
    raw  = parts[2].strip()
    if raw.startswith("@"):
        username = raw
        link     = "https://t.me/" + raw.lstrip("@")
    elif "t.me/" in raw:
        link     = raw
        username = "@" + raw.split("t.me/")[-1].split("/")[0]
    else:
        username = "@" + raw
        link     = "https://t.me/" + raw
    return name, link, username

# ╔══════════════════════════════════════════════════════════╗
#                   ФУНКЦИИ БД — ПОЛЬЗОВАТЕЛИ
# ╚══════════════════════════════════════════════════════════╝
def db_add_user(user_id, username, referred_by=None):
    if col_users.find_one({"user_id": user_id}):
        return False
    col_users.insert_one({
        "user_id":    user_id,
        "username":   username or "",
        "join_date":  datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "balance":    0.0,
        "referred_by":       referred_by,
        "referral_rewarded": False,
    })
    return True

def db_get_user(user_id):
    return col_users.find_one({"user_id": user_id})

def db_get_balance(user_id):
    doc = col_users.find_one({"user_id": user_id}, {"balance": 1})
    return round(doc["balance"], 2) if doc else 0.0

def db_add_balance(user_id, amount):
    col_users.update_one({"user_id": user_id}, {"$inc": {"balance": round(amount, 2)}})

def db_get_ref_count(user_id):
    return col_users.count_documents({"referred_by": user_id})

def db_set_rewarded(user_id):
    col_users.update_one({"user_id": user_id}, {"$set": {"referral_rewarded": True}})

def db_get_all_users():
    return [d["user_id"] for d in col_users.find({}, {"user_id": 1})]

def db_get_stats():
    now      = datetime.datetime.utcnow()
    day_ago  = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    week_ago = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    total    = col_users.count_documents({})
    today    = col_users.count_documents({"join_date": {"$gte": day_ago}})
    week     = col_users.count_documents({"join_date": {"$gte": week_ago}})
    return total, today, week

def db_get_growth():
    now    = datetime.datetime.utcnow()
    result = []
    for i in range(6, -1, -1):
        ds = (now - datetime.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        de = ds + datetime.timedelta(days=1)
        c  = col_users.count_documents({
            "join_date": {"$gte": ds.strftime("%Y-%m-%d %H:%M:%S"),
                          "$lt":  de.strftime("%Y-%m-%d %H:%M:%S")}
        })
        result.append((ds.strftime("%d.%m"), c))
    return result

# ╔══════════════════════════════════════════════════════════╗
#                   ФУНКЦИИ БД — ПРОМОКОДЫ
# ╚══════════════════════════════════════════════════════════╝
def gen_promo_code(length=8):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

def create_promo(amount, max_uses):
    code = gen_promo_code()
    col_promo.insert_one({
        "code":     code,
        "amount":   round(float(amount), 2),
        "max_uses": int(max_uses),
        "used":     0,
        "users":    [],
    })
    return code

def use_promo(code, user_id):
    """Returns (ok, message)"""
    doc = col_promo.find_one({"code": code.upper()})
    if not doc:
        return False, "❌ <b>Промокод не найден!</b>"
    if user_id in doc.get("users", []):
        return False, "❌ <b>Вы уже использовали этот промокод!</b>"
    if doc["used"] >= doc["max_uses"]:
        return False, "❌ <b>Промокод исчерпан!</b>"
    col_promo.update_one(
        {"code": code.upper()},
        {"$inc": {"used": 1}, "$push": {"users": user_id}}
    )
    db_add_balance(user_id, doc["amount"])
    return True, doc["amount"]

# ╔══════════════════════════════════════════════════════════╗
#                   ФУНКЦИИ БД — РЕКЛАМА
# ╚══════════════════════════════════════════════════════════╝
def save_reklam(chat_id, message_id):
    col_reklam.insert_one({"chat_id": str(chat_id), "message_id": message_id})

def get_reklamlar():
    return [(d["chat_id"], d["message_id"]) for d in col_reklam.find()]

def clear_reklamlar():
    col_reklam.delete_many({})

# ╔══════════════════════════════════════════════════════════╗
#                   TGRASS — API ENTEGRASYONU
# ╚══════════════════════════════════════════════════════════╝
# ── TGrass yeni döküman: POST https://tgrass.space/offers
# ── Header: {"Auth": TOKEN, "Content-Type": "application/json"}
TGRASS_ENDPOINT = "https://tgrass.space/offers"
TGRASS_HEADERS  = {
    "Content-Type": "application/json",
    "Auth":         "e5e1e1401d9e4515a211288a5a22bb48",
}

def tgrass_fetch_channels():
    """
    TGrass API — POST https://tgrass.space/offers
    Genel kanal listesini çeker, MongoDB tgrass_channels koleksiyonuna kaydeder.
    """
    if get_setting("tgrass", "on") != "on":
        return 0, "TGrass kapalı"
    try:
        resp = requests.post(
            TGRASS_ENDPOINT,
            json={
                "tg_user_id": 0,
                "is_premium": False,
                "lang":       "en",
            },
            headers=TGRASS_HEADERS,
            timeout=30
        )
        if resp.status_code != 200:
            msg = f"HTTP {resp.status_code}: {resp.text[:100]}"
            print(f"[TGrass fetch] {msg}")
            return 0, msg
        data = resp.json()
        # Cevap: liste | {"offers":[...]} | {"status":"ok","offers":[...]}
        if isinstance(data, list):
            offers = data
        elif isinstance(data, dict):
            offers = data.get("offers", data.get("channels", []))
        else:
            offers = []
        count = 0
        for offer in offers:
            username = (offer.get("username") or offer.get("login") or
                        offer.get("channel_username") or "")
            name     = (offer.get("name") or offer.get("title") or username)
            link     = (offer.get("link") or offer.get("url") or
                        (f"https://t.me/{username.lstrip('@')}" if username else ""))
            if username and link:
                _add_ch(col_tgrass_channels, link, name, username)
                count += 1
        print(f"[TGrass fetch] {count} kanal kaydedildi")
        return count, "ok"
    except requests.exceptions.ConnectionError as e:
        msg = f"Bağlantı hatası: {str(e)[:60]}"
        print(f"[TGrass fetch] {msg}")
        return 0, msg
    except requests.exceptions.Timeout:
        print("[TGrass fetch] Timeout")
        return 0, "Timeout (30s)"
    except Exception as e:
        msg = str(e)[:80]
        print(f"[TGrass fetch] {msg}")
        return 0, msg

def tgrass_get_offers(user):
    """
    TGrass API — POST https://tgrass.space/offers (kullanıcıya özel)
    Kullanıcının abonelik durumunu döndürür.
    """
    if get_setting("tgrass", "on") != "on":
        return []
    try:
        resp = requests.post(
            TGRASS_ENDPOINT,
            json={
                "tg_user_id": user.id,
                "is_premium": bool(getattr(user, "is_premium", False)),
                "lang":       getattr(user, "language_code", "en") or "en",
            },
            headers=TGRASS_HEADERS,
            timeout=30
        )
        if resp.status_code != 200:
            print(f"[TGrass offers] HTTP {resp.status_code}")
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("offers", data.get("channels", []))
    except requests.exceptions.ConnectionError as e:
        print(f"[TGrass offers] Bağlantı hatası: {str(e)[:60]}")
        return []
    except requests.exceptions.Timeout:
        print("[TGrass offers] Timeout")
        return []
    except Exception as e:
        print(f"[TGrass offers] {e}")
        return []

def check_tgrass_subscription(user):
    """
    TGrass kanallarında abonelik kontrolü.
    subscribed=False olanları döndürür: [(id, link, name), ...]
    Eğer API cevabında subscribed alanı yoksa, MongoDB'deki listeyi kullanır.
    """
    if get_setting("tgrass", "on") != "on":
        return []
    not_sub = []
    offers = tgrass_get_offers(user)
    if offers:
        # API cevabı varsa ondan kontrol et
        for offer in offers:
            if offer.get("type") not in ("channel", None):
                continue
            if not offer.get("subscribed", True):
                name = offer.get("name") or offer.get("title") or "TGrass"
                link = offer.get("link") or offer.get("url") or ""
                if link:
                    not_sub.append((f"tg_{offer.get('offer_id', '')}", link, name))
    else:
        # API'den cevap gelmezse MongoDB'deki TGrass kanallarını get_chat_member ile kontrol et
        for ch_id, ch_link, ch_name, username in _ch_list(col_tgrass_channels):
            if not username:
                continue
            try:
                m = bot.get_chat_member("@" + username.lstrip("@"), user.id)
                if m.status in ("left", "kicked", "banned"):
                    not_sub.append((ch_id, ch_link, ch_name))
            except telebot.apihelper.ApiTelegramException as e:
                err = str(e).lower()
                # Bot hesabı veya bulunamayan kanal → atla
                if "bot" in err or "not found" in err or "chat not found" in err:
                    continue
                not_sub.append((ch_id, ch_link, ch_name))
            except Exception:
                not_sub.append((ch_id, ch_link, ch_name))
    return not_sub

# ╔══════════════════════════════════════════════════════════╗
#                   FSM (СОСТОЯНИЯ)
# ╚══════════════════════════════════════════════════════════╝
user_states = {}
user_data   = {}

def set_state(uid, state, **kwargs):
    user_states[uid] = state
    user_data[uid]   = kwargs

def clear_state(uid):
    user_states.pop(uid, None)
    user_data.pop(uid, None)

def get_state(uid):
    return user_states.get(uid)

# ╔══════════════════════════════════════════════════════════╗
#                   ПРОВЕРКА ПОДПИСКИ
# ╚══════════════════════════════════════════════════════════╝
def check_subs(user_id):
    """Список каналов без подписки. Bot hesapları atlanır."""
    not_sub = []
    for ch_id, ch_link, ch_name, username in list(get_sponsors()) + list(get_addlist()):
        if not username:
            continue
        try:
            m = bot.get_chat_member("@" + username.lstrip("@"), user_id)
            if m.status in ("left", "kicked", "banned"):
                not_sub.append((ch_id, ch_link, ch_name))
        except telebot.apihelper.ApiTelegramException as e:
            err = str(e).lower()
            # Bot hesabı veya bulunamayan kanal → abonelik sayılmaz, atla
            if "bot" in err or "not found" in err or "chat not found" in err:
                continue
            not_sub.append((ch_id, ch_link, ch_name))
        except Exception:
            not_sub.append((ch_id, ch_link, ch_name))
    return not_sub
def build_main_keyboard(user_id=None, _tgrass_user=None):
    me       = bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user_id}" if user_id else f"https://t.me/{me.username}"
    share_url = (f"https://t.me/share/url?url={ref_link}"
                 f"&text=🔥%20Получи%20бесплатный%20VPN-код!")

    kb = InlineKeyboardMarkup(row_width=2)

    # Спонсоры (эмодзи 🌟)
    sponsor_btns = [
        InlineKeyboardButton(text=f"🌟 {name}", url=link)
        for _, link, name, _ in get_sponsors()
    ]
    # Addlist (эмодзи ✨)
    addlist_btns = [
        InlineKeyboardButton(text=f"✨ {name}", url=link)
        for _, link, name, _ in get_addlist()
    ]
    # TGrass — offers API'den kullanıcıya özel (abone olmadıkları kanallar)
    # Not: build_main_keyboard user=None ile çağrılırsa TGrass butonları gözükmez
    tgrass_btns = []
    if _tgrass_user is not None and get_setting("tgrass", "on") == "on":
        offers = tgrass_get_offers(_tgrass_user)
        for offer in offers:
            if offer.get("type") != "channel":
                continue
            if not offer.get("subscribed", True):
                _name = offer.get("name") or "Sponsor"
                _link = offer.get("link") or ""
                if _link:
                    tgrass_btns.append(InlineKeyboardButton(
                        text=f"📢 {_name}", url=_link))

    all_btns = sponsor_btns + addlist_btns + tgrass_btns
    if all_btns:
        kb.add(*all_btns)

    # Главные кнопки
    kb.row(
        InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub"),
        InlineKeyboardButton(text="📢 Поделиться",   url=share_url),
    )
    kb.row(
        InlineKeyboardButton(text="💰 Баланс",        callback_data="my_balance"),
        InlineKeyboardButton(text="🏆 Top 10",         callback_data="top10"),
    )
    kb.row(InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo"))
    return kb

def _show_post_channels_menu(chat_id):
    """Пост-каналы: список с кнопками Отправить / Удалить + Добавить."""
    channels = get_post_channels()
    kb = InlineKeyboardMarkup(row_width=2)
    if channels:
        for ch in channels:
            ch_id  = str(ch["_id"])
            name   = ch.get("name", "")
            uname  = ch.get("username", "")
            # Каждый канал: [📤 Имя @user] [🗑 Удалить]
            kb.row(
                InlineKeyboardButton(
                    text=f"📺 {name} @{uname}",
                    callback_data=f"pch_send_{ch_id}"
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"pch_del_{ch_id}"
                )
            )
    kb.row(
        InlineKeyboardButton(text="🚀 Отправить во все", callback_data="pch_send_all"),
        InlineKeyboardButton(text="➕ Добавить канал",   callback_data="pch_add"),
    )
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back"))
    total = len(channels)
    bot.send_message(
        chat_id,
        f"📡 <b>Пост-каналы</b>\n\nКаналов в списке: <b>{total}</b>\n\nВыберите канал для отправки или добавьте новый:",
        reply_markup=kb
    )

def build_admin_keyboard():
    total, today, _ = db_get_stats()
    tgrass  = get_setting("tgrass", "on")
    tg_icon = "✅" if tgrass == "on" else "❌"

    kb = InlineKeyboardMarkup(row_width=2)
    # Статистика — одна кнопка во всю ширину
    kb.row(InlineKeyboardButton(
        text=f"🏁 Польз: {total} (сег. +{today})",
        callback_data="adm_stats"
    ))
    # Рассылка + В каналы
    kb.row(
        InlineKeyboardButton(text="📢 Рассылка",      callback_data="adm_broadcast"),
        InlineKeyboardButton(text="📡 Пост в каналы", callback_data="adm_send_channel"),
    )
    # VPN + Промокод
    kb.row(
        InlineKeyboardButton(text="🔑 Изменить VPN",  callback_data="adm_code"),
        InlineKeyboardButton(text="🎟 Промокод",       callback_data="adm_promo"),
    )
    # Удалить рекламу + График
    kb.row(
        InlineKeyboardButton(text="🗑 Удалить рекл.", callback_data="adm_del_reklam"),
        InlineKeyboardButton(text="📈 График роста",  callback_data="adm_growth"),
    )
    # Спонсор добавить/удалить
    kb.row(
        InlineKeyboardButton(text="➕ Спонсор",       callback_data="adm_add_sponsor"),
        InlineKeyboardButton(text="🗑 Спонсор",       callback_data="adm_del_sponsor"),
    )
    # Addlist добавить/удалить
    kb.row(
        InlineKeyboardButton(text="➕ Addlist",       callback_data="adm_add_addlist"),
        InlineKeyboardButton(text="🗑 Addlist",       callback_data="adm_del_addlist"),
    )
    # TGrass вкл/выкл + Обновить TGrass
    kb.row(
        InlineKeyboardButton(text=f"⚙️ TGrass {tg_icon}", callback_data="adm_tgrass"),
        InlineKeyboardButton(text="🔄 Обновить TGrass",    callback_data="adm_tgrass_update"),
    )
    # Добавить/удалить администратора
    kb.row(
        InlineKeyboardButton(text="👤 Добавить адм.",  callback_data="adm_add_admin"),
        InlineKeyboardButton(text="👤 Удалить адм.",   callback_data="adm_del_admin"),
    )
    # Изменить приветствие
    kb.row(InlineKeyboardButton(text="✏️ Изм. текст", callback_data="adm_welcome"))
    return kb

def build_unsub_keyboard(not_sub):
    kb = InlineKeyboardMarkup(row_width=2)
    btns = [InlineKeyboardButton(text=name, url=link) for _, link, name in not_sub]
    kb.add(*btns)
    kb.row(InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub"))
    return kb

# ╔══════════════════════════════════════════════════════════╗
#                   /start
# ╚══════════════════════════════════════════════════════════╝
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    ref_id = None
    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id == user.id:
                ref_id = None
        except ValueError:
            pass

    is_new = db_add_user(user.id, user.username or user.first_name, referred_by=ref_id)

    welcome = get_setting("welcome_text") or "👋 <b>Добро пожаловать!</b>"
    bot.send_message(message.chat.id, welcome,
                     reply_markup=build_main_keyboard(user.id, _tgrass_user=user))

# ╔══════════════════════════════════════════════════════════╗
#                   /admin
# ╚══════════════════════════════════════════════════════════╝
def is_extra_admin(uid):
    return bool(col_settings.find_one({"key": f"extra_admin_{uid}"}))

def build_extra_admin_kb():
    """Extra adminler icin kisitli panel."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton(text="📢 Рассылка",      callback_data="adm_broadcast"),
        InlineKeyboardButton(text="📡 Пост в каналы", callback_data="adm_send_channel"),
    )
    kb.row(
        InlineKeyboardButton(text="🗑 Удалить рекл.", callback_data="adm_del_reklam"),
        InlineKeyboardButton(text="🔑 Изменить VPN",  callback_data="adm_code"),
    )
    return kb

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    uid = message.from_user.id
    if uid == ADMIN_ID:
        bot.send_message(message.chat.id,
            "⚙️ <b>Панель администратора</b>",
            reply_markup=build_admin_keyboard())
    elif is_extra_admin(uid):
        bot.send_message(message.chat.id,
            "⚙️ <b>Панель администратора</b>",
            reply_markup=build_extra_admin_kb())
    else:
        return

# ╔══════════════════════════════════════════════════════════╗
#              ПРОМОКОД КОМАНДЫ (/create_promo, /promo)
# ╚══════════════════════════════════════════════════════════╝
@bot.message_handler(commands=["create_promo"])
def cmd_create_promo(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) < 3:
        bot.send_message(message.chat.id,
            "📌 Использование:\n<code>/create_promo 0.50 10</code>\n\n"
            "Создаст промокод на 0.50 TMT для 10 активаций.")
        return
    try:
        amount   = float(parts[1])
        max_uses = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат!")
        return
    code = create_promo(amount, max_uses)
    bot.send_message(
        message.chat.id,
        f"✅ <b>Промокод создан!</b>\n\n"
        f"🎟 Код: <code>{code}</code>\n"
        f"💰 Сумма: <b>{amount:.2f} TMT</b>\n"
        f"🔢 Количество активаций: <b>{max_uses}</b>"
    )

@bot.message_handler(commands=["promo"])
def cmd_promo(message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.send_message(message.chat.id,
            "📌 Использование:\n<code>/promo КОД</code>")
        return
    code = parts[1].upper()
    ok, result = use_promo(code, message.from_user.id)
    if ok:
        bal = db_get_balance(message.from_user.id)
        bot.send_message(
            message.chat.id,
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"💰 Начислено: <b>{result:.2f} TMT</b>\n"
            f"💳 Ваш баланс: <b>{bal:.2f} TMT</b>"
        )
    else:
        bot.send_message(message.chat.id, result)

# ╔══════════════════════════════════════════════════════════╗
#          АДМИН КОМАНДЫ (/add_sponsor, /add_addlist, etc.)
# ╚══════════════════════════════════════════════════════════╝
@bot.message_handler(commands=["add_sponsor"])
def cmd_add_sponsor(message):
    if message.from_user.id != ADMIN_ID: return
    name, link, username = parse_channel_args(message.text)
    if not name:
        bot.send_message(message.chat.id, "📌 <code>/add_sponsor 🌟НазваниеКанала @username</code>")
        return
    add_sponsor(link, name, username)
    bot.send_message(message.chat.id,
        f"✅ Спонсор добавлен!\n📢 <b>{name}</b> — <code>{username}</code>")

@bot.message_handler(commands=["add_addlist"])
def cmd_add_addlist(message):
    if message.from_user.id != ADMIN_ID: return
    name, link, username = parse_channel_args(message.text)
    if not name:
        bot.send_message(message.chat.id, "📌 <code>/add_addlist ✨НазваниеКанала @username</code>")
        return
    add_addlist(link, name, username)
    bot.send_message(message.chat.id,
        f"✅ Addlist добавлен!\n📋 <b>{name}</b> — <code>{username}</code>")

@bot.message_handler(commands=["tgrass_on"])
def cmd_tgrass_on(message):
    if message.from_user.id != ADMIN_ID: return
    set_setting("tgrass", "on")
    bot.send_message(message.chat.id, "✅ <b>TGrass включён!</b>")

@bot.message_handler(commands=["tgrass_off"])
def cmd_tgrass_off(message):
    if message.from_user.id != ADMIN_ID: return
    set_setting("tgrass", "off")
    bot.send_message(message.chat.id, "❌ <b>TGrass выключен!</b>")

@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if message.from_user.id != ADMIN_ID: return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "↩️ Отменено.", reply_markup=build_admin_keyboard())

# ╔══════════════════════════════════════════════════════════╗
#              CALLBACK — ПОЛЬЗОВАТЕЛИ
# ╚══════════════════════════════════════════════════════════╝
@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def cb_check_sub(call):
    user = call.from_user
    all_ch = list(get_sponsors()) + list(get_addlist())
    tgrass = get_setting("tgrass", "on")
    tg_user = get_setting("tgrass_username", "")
    if tgrass == "on" and tg_user:
        all_ch.append(("tg", f"https://t.me/{tg_user}", "⚙️ TGrass", tg_user))

    if not all_ch:
        bot.answer_callback_query(call.id, "⚠️ Каналов ещё нет!", show_alert=True)
        return

    not_sub = check_subs(user.id)
    # TGrass kanallarını da kontrol et
    not_sub += check_tgrass_subscription(user)

    if not_sub:
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id,
                reply_markup=build_unsub_keyboard(not_sub)
            )
        except Exception:
            pass
        bot.answer_callback_query(
            call.id, "❌ Вы ещё не подписались на все каналы!", show_alert=True)
        return

    # Проверка прошла — начислить реферальную награду пригласившему
    doc = db_get_user(user.id)
    if doc and doc.get("referred_by") and not doc.get("referral_rewarded"):
        ref_id = doc["referred_by"]
        db_add_balance(ref_id, REWARD_TMT)
        db_set_rewarded(user.id)
        new_bal = db_get_balance(ref_id)
        try:
            bot.send_message(
                ref_id,
                f"🎉 <b>Ваш друг подписался!</b>\n\n"
                f"💰 На ваш счёт начислено: <b>+{REWARD_TMT:.2f} TMT</b>\n"
                f"💳 Текущий баланс: <b>{new_bal:.2f} TMT</b>"
            )
        except Exception:
            pass

    # Отправить VPN-код
    vpn = get_setting("vpn_code")
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>Подписка подтверждена!</b>\n\n"
        f"🔑 Ваш VPN-код:\n\n<code>{vpn}</code>"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "my_balance")
def cb_my_balance(call):
    uid     = call.from_user.id
    bal     = db_get_balance(uid)
    ref_cnt = db_get_ref_count(uid)
    me      = bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={uid}"
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(text="💸 Вывести средства", callback_data="withdraw"),
        InlineKeyboardButton(text="🔙 Назад",            callback_data="back_main"),
    )
    bot.send_message(
        call.message.chat.id,
        f"💳 <b>Ваш баланс</b>\n\n"
        f"💰 Баланс: <b>{bal:.2f} TMT</b>\n"
        f"👥 Приглашено: <b>{ref_cnt}</b> чел.\n"
        f"📊 Доход: <b>{ref_cnt * REWARD_TMT:.2f} TMT</b>\n\n"
        f"🔗 Ваша реферальная ссылка:\n<code>{ref_link}</code>",
        reply_markup=kb
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "withdraw")
def cb_withdraw(call):
    uid = call.from_user.id
    bal = db_get_balance(uid)
    if bal < WITHDRAW_MIN:
        bot.answer_callback_query(
            call.id,
            f"❌ Минимальная сумма вывода: {WITHDRAW_MIN:.2f} TMT\n"
            f"Ваш баланс: {bal:.2f} TMT",
            show_alert=True
        )
        return
    uname = call.from_user.username or f"ID:{uid}"
    try:
        bot.send_message(
            ADMIN_ID,
            f"💸 <b>Запрос на вывод!</b>\n\n"
            f"👤 Пользователь: @{uname} (ID: <code>{uid}</code>)\n"
            f"💰 Сумма: <b>{bal:.2f} TMT</b>"
        )
    except Exception:
        pass
    bot.answer_callback_query(
        call.id,
        f"✅ Запрос отправлен!\nСумма: {bal:.2f} TMT\nОжидайте обработки.",
        show_alert=True
    )

@bot.callback_query_handler(func=lambda c: c.data == "top10")
def cb_top10(call):
    from pymongo import DESCENDING
    docs = col_users.find(
        {"balance": {"$gt": 0}},
        {"user_id": 1, "username": 1, "balance": 1}
    ).sort("balance", DESCENDING).limit(10)
    rows = list(docs)
    if not rows:
        bot.answer_callback_query(call.id, "Heniz balans ýok!", show_alert=True)
        return
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = []
    for i, doc in enumerate(rows):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        name  = f"@{doc['username']}" if doc.get("username") else f"ID:{doc['user_id']}"
        bal   = round(doc.get("balance", 0), 2)
        lines.append(f"{medal} {name} — <b>{bal:.2f} TMT</b>")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_main"))
    bot.send_message(
        call.message.chat.id,
        "🏆 <b>Top 10 — Iň köp balans</b>\n\n" + "\n".join(lines),
        reply_markup=kb
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "enter_promo")
def cb_enter_promo(call):
    user = call.from_user
    # Abonelik kontrolü — önce tüm kanallara üye olmalı
    not_sub = check_subs(user.id)
    not_sub += check_tgrass_subscription(user)
    if not_sub:
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id,
                reply_markup=build_unsub_keyboard(not_sub)
            )
        except Exception:
            pass
        bot.answer_callback_query(
            call.id,
            "❌ Промокод kullanmak üçin ählisine agza boluň!",
            show_alert=True
        )
        return
    set_state(user.id, "promo_input")
    bot.send_message(call.message.chat.id,
        "🎟 Введите промокод:\n\n"
        "Или отмените: /cancel")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "back_main")
def cb_back_main(call):
    welcome = get_setting("welcome_text") or "👋 <b>Добро пожаловать!</b>"
    bot.send_message(call.message.chat.id, welcome,
                     reply_markup=build_main_keyboard(call.from_user.id,
                                                      _tgrass_user=call.from_user))
    bot.answer_callback_query(call.id)

# ╔══════════════════════════════════════════════════════════╗
#              CALLBACK — АДМИНИСТРАТОР
# ╚══════════════════════════════════════════════════════════╝
EXTRA_ADMIN_ALLOWED = {"adm_broadcast", "adm_send_channel", "adm_del_reklam", "adm_code", "pch_send_all"}

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_") and
    (c.from_user.id == ADMIN_ID or is_extra_admin(c.from_user.id)))
def admin_callbacks(call):
    data = call.data
    # Extra admin yetki kontrolu
    if call.from_user.id != ADMIN_ID and data not in EXTRA_ADMIN_ALLOWED:
        bot.answer_callback_query(call.id, "❌ Yetkiniz yok!", show_alert=True)
        return

    # ── Статистика ──────────────────────────────────────────────────────────────
    if data == "adm_stats":
        total, today, week = db_get_stats()
        sp_count  = col_sponsors.count_documents({})
        al_count  = col_addlist.count_documents({})
        tgrass    = get_setting("tgrass", "on")
        tg_status = "Aç" if tgrass == "on" else "Öçür"
        adm_count = col_settings.count_documents({"key": {"$regex": "^admin_"}})
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back"))
        bot.send_message(
            call.message.chat.id,
            f"📊 <b>Statistika:</b>\n\n"
            f"👥 Ulanyjylar: <b>{total}</b>\n"
            f"📢 Kanallar: <b>{sp_count}</b>\n"
            f"🔗 Addlistler: <b>{al_count}</b>\n"
            f"⚙️ TGRASS: <b>{tg_status}</b>",
            reply_markup=kb
        )

    # ── Добавить спонсора ──────────────────────────────────────────────────────
    elif data == "adm_add_sponsor":
        set_state(call.from_user.id, "adm_add_sponsor")
        bot.send_message(call.message.chat.id,
            "📢 Введите данные спонсора в формате:\n"
            "<code>🌟НазваниеКанала @username</code>\n\n"
            "Или: /cancel")
        bot.answer_callback_query(call.id)

    # ── Удалить спонсора ──────────────────────────────────────────────────────
    elif data == "adm_del_sponsor":
        sponsors = get_sponsors()
        if not sponsors:
            bot.answer_callback_query(call.id, "Спонсоров нет!", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for sid, _, name, uname in sponsors:
            kb.add(InlineKeyboardButton(
                text=f"🗑 {name} (@{uname})",
                callback_data=f"delspon_{sid}"
            ))
        kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back"))
        bot.send_message(call.message.chat.id, "Выберите спонсора для удаления:",
                         reply_markup=kb)
        bot.answer_callback_query(call.id)

    # ── Добавить addlist ──────────────────────────────────────────────────────
    elif data == "adm_add_addlist":
        set_state(call.from_user.id, "adm_add_addlist")
        bot.send_message(call.message.chat.id,
            "📋 Введите данные addlist в формате:\n"
            "<code>✨НазваниеКанала @username</code>\n\n"
            "Или: /cancel")
        bot.answer_callback_query(call.id)

    # ── Удалить addlist ───────────────────────────────────────────────────────
    elif data == "adm_del_addlist":
        addlist = get_addlist()
        if not addlist:
            bot.answer_callback_query(call.id, "Addlist пуст!", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for aid, _, name, uname in addlist:
            kb.add(InlineKeyboardButton(
                text=f"🗑 {name} (@{uname})",
                callback_data=f"deladdl_{aid}"
            ))
        kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back"))
        bot.send_message(call.message.chat.id, "Выберите для удаления:",
                         reply_markup=kb)
        bot.answer_callback_query(call.id)

    # ── Изменить VPN код ──────────────────────────────────────────────────────
    elif data == "adm_code":
        cur = get_setting("vpn_code")
        set_state(call.from_user.id, "adm_code")
        bot.send_message(call.message.chat.id,
            f"🔑 Текущий VPN-код:\n<code>{cur}</code>\n\n"
            f"Введите новый код:")
        bot.answer_callback_query(call.id)

    # ── Промокод ─────────────────────────────────────────────────────────────
    elif data == "adm_promo":
        set_state(call.from_user.id, "adm_promo")
        bot.send_message(call.message.chat.id,
            "🎟 Введите параметры промокода:\n"
            "<code>сумма количество_активаций</code>\n\n"
            "Например: <code>0.50 10</code>")
        bot.answer_callback_query(call.id)

    # ── Рассылка ─────────────────────────────────────────────────────────────
    elif data == "adm_broadcast":
        set_state(call.from_user.id, "adm_broadcast")
        bot.send_message(call.message.chat.id,
            "📢 Отправьте сообщение для рассылки всем пользователям.\n"
            "(Текст, фото, видео — любой тип)\n\n"
            "Отмена: /cancel")
        bot.answer_callback_query(call.id)

    # ── В каналы ─────────────────────────────────────────────────────────────
    elif data == "adm_send_channel":
        _show_post_channels_menu(call.message.chat.id)
        bot.answer_callback_query(call.id)

    # ── Удалить рекламу ──────────────────────────────────────────────────────
    elif data == "adm_del_reklam":
        reklamlar = get_reklamlar()
        if not reklamlar:
            bot.answer_callback_query(call.id, "Нет сохранённой рекламы!", show_alert=True)
            return
        ok = fail = 0
        for chat_id, msg_id in reklamlar:
            try:
                bot.delete_message(chat_id=chat_id, message_id=msg_id)
                ok += 1
            except Exception:
                fail += 1
        clear_reklamlar()
        bot.send_message(call.message.chat.id,
            f"🗑 <b>Реклама удалена!</b>\n\n"
            f"✔️ Удалено: <b>{ok}</b>\n"
            f"❌ Не найдено: <b>{fail}</b>",
            reply_markup=build_admin_keyboard())
        bot.answer_callback_query(call.id)

    # ── TGrass переключатель ─────────────────────────────────────────────────
    elif data == "adm_tgrass":
        tgrass = get_setting("tgrass", "on")
        if tgrass == "on":
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(
                InlineKeyboardButton(text="❌ Выключить TGrass", callback_data="tgrass_off_confirm"),
                InlineKeyboardButton(text="📝 Изменить канал",   callback_data="tgrass_set_ch"),
                InlineKeyboardButton(text="🔙 Назад",            callback_data="adm_back"),
            )
            tg_user = get_setting("tgrass_username", "—")
            bot.send_message(call.message.chat.id,
                f"⚙️ <b>TGrass</b>\n\n"
                f"Статус: ✅ Включен\n"
                f"Канал: @{tg_user}",
                reply_markup=kb)
        else:
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(
                InlineKeyboardButton(text="✅ Включить TGrass",  callback_data="tgrass_on_confirm"),
                InlineKeyboardButton(text="📝 Изменить канал",   callback_data="tgrass_set_ch"),
                InlineKeyboardButton(text="🔙 Назад",            callback_data="adm_back"),
            )
            bot.send_message(call.message.chat.id,
                "⚙️ <b>TGrass</b>\n\nСтатус: ❌ Выключен",
                reply_markup=kb)
        bot.answer_callback_query(call.id)

    # ── График роста ─────────────────────────────────────────────────────────
    elif data == "adm_growth":
        rows  = db_get_growth()
        max_v = max((c for _, c in rows), default=1) or 1
        lines = []
        for label, count in rows:
            bar = "█" * int((count / max_v) * 12) + "░" * (12 - int((count / max_v) * 12))
            lines.append(f"<code>{label}</code> {bar} <b>{count}</b>")
        total, today, week = db_get_stats()
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back"))
        bot.send_message(
            call.message.chat.id,
            f"📈 <b>Рост за 7 дней</b>\n\n" + "\n".join(lines) +
            f"\n\n👥 Всего: <b>{total}</b> | За неделю: <b>+{week}</b> | Сегодня: <b>+{today}</b>",
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)

    # ── Изменить приветствие ─────────────────────────────────────────────────
    elif data == "adm_welcome":
        cur = get_setting("welcome_text")
        set_state(call.from_user.id, "adm_welcome")
        bot.send_message(call.message.chat.id,
            f"✏️ Текущее приветствие:\n\n{cur}\n\nВведите новый текст:")
        bot.answer_callback_query(call.id)

    # ── TGrass Güncelle ──────────────────────────────────────────────────────
    # ── TGrass Güncelle ──────────────────────────────────────────────────────
    elif data == "adm_tgrass_update":
        bot.answer_callback_query(call.id, "🔄 TGrass güncelleniyor...")
        count, msg = tgrass_fetch_channels()
        if msg == "ok":
            text = (f"✅ <b>Kanallar TGrass'tan başarıyla çekildi!</b>\n\n"
                    f"📡 Kanal sayısı: <b>{count}</b>")
        else:
            text = (f"❌ <b>TGrass bağlantı hatası!</b>\n\n"
                    f"Hata: <code>{msg}</code>")
        bot.send_message(call.message.chat.id, text, reply_markup=build_admin_keyboard())

    # ── Добавить/удалить администратора ──────────────────────────────────────
    elif data == "adm_add_admin":
        set_state(call.from_user.id, "adm_add_admin")
        bot.send_message(call.message.chat.id,
            "👤 Admin edilecek kullanıcının ID'sini girin:\n\n"
            "⚠️ Bu admin şunları yapabilir:\n"
            "• 📢 Kullanıcılara reklam göndermek\n"
            "• 📡 Kanallara post atmak\n"
            "• 🗑 Reklam silmek\n"
            "• 🔑 VPN kodunu değiştirmek\n\nОтмена: /cancel")
        bot.answer_callback_query(call.id)

    elif data == "adm_del_admin":
        # Extra admin listesini goster
        extra_admins = list(col_settings.find({"key": {"$regex": "^extra_admin_"}}))
        if not extra_admins:
            bot.answer_callback_query(call.id, "Ek admin yok!", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for adm in extra_admins:
            adm_id = adm["key"].replace("extra_admin_", "")
            adm_name = adm.get("value", adm_id)
            kb.add(InlineKeyboardButton(
                text=f"🗑 {adm_name} (ID: {adm_id})",
                callback_data=f"del_extra_admin_{adm_id}"
            ))
        kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back"))
        bot.send_message(call.message.chat.id, "Silinecek admini seçin:", reply_markup=kb)
        bot.answer_callback_query(call.id)

    # ── Назад ─────────────────────────────────────────────────────────────────
    elif data == "adm_back":
        bot.send_message(
            call.message.chat.id,
            "⚙️ <b>Панель администратора</b>",
            reply_markup=build_admin_keyboard()
        )
        bot.answer_callback_query(call.id)

# ── Пост-каналы: добавить / удалить / отправить ───────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("pch_") and
    (c.from_user.id == ADMIN_ID or is_extra_admin(c.from_user.id)))
def cb_post_channels(call):
    uid  = call.from_user.id
    data = call.data

    # ── Добавить канал ────────────────────────────────────────────────────────
    if data == "pch_add":
        set_state(uid, "pch_add")
        bot.send_message(call.message.chat.id,
            "➕ <b>Добавить пост-канал</b>\n\n"
            "Введите в формате:\n"
            "<code>Название @username</code>\n\n"
            "Например: <code>HAPP_VPN @HAPP_VPN</code>\n\nОтмена: /cancel")
        bot.answer_callback_query(call.id)

    # ── Удалить канал ─────────────────────────────────────────────────────────
    elif data.startswith("pch_del_"):
        ch_id = data[len("pch_del_"):]
        del_post_channel(ch_id)
        bot.answer_callback_query(call.id, "✅ Канал удалён!")
        _show_post_channels_menu(call.message.chat.id)

    # ── Отправить во все ──────────────────────────────────────────────────────
    elif data == "pch_send_all":
        channels = get_post_channels()
        if not channels:
            bot.answer_callback_query(call.id, "Список пуст! Сначала добавьте каналы.", show_alert=True)
            return
        names = ", ".join(f"@{c['username']}" for c in channels)
        set_state(uid, "pch_send_post", target="all")
        bot.send_message(call.message.chat.id,
            f"🚀 <b>Отправить во все каналы</b>\n\n"
            f"Каналов: <b>{len(channels)}</b>\n{names}\n\n"
            f"Отправьте рекламный пост (текст, фото, видео — любой тип)\n\nОтмена: /cancel")
        bot.answer_callback_query(call.id)

    # ── Отправить в один канал ────────────────────────────────────────────────
    elif data.startswith("pch_send_"):
        ch_id = data[len("pch_send_"):]
        ch    = col_post_channels.find_one({"_id": ObjectId(ch_id)})
        if not ch:
            bot.answer_callback_query(call.id, "Канал не найден!", show_alert=True)
            return
        set_state(uid, "pch_send_post", target=ch_id)
        bot.send_message(call.message.chat.id,
            f"📺 <b>@{ch['username']}</b> каналына пост отправьте:\n\n"
            f"(Текст, фото, видео, кнопки — любой тип)\n\n"
            "Отмена: /cancel")
        bot.answer_callback_query(call.id)

# ── Extra admin сил ──────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("del_extra_admin_") and c.from_user.id == ADMIN_ID)
def cb_del_extra_admin(call):
    adm_id = call.data[len("del_extra_admin_"):]
    col_settings.delete_one({"key": f"extra_admin_{adm_id}"})
    bot.send_message(call.message.chat.id,
        f"✅ Admin <code>{adm_id}</code> silindi!",
        reply_markup=build_admin_keyboard())
    bot.answer_callback_query(call.id)

# ── TGrass on/off confirm ─────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data in ("tgrass_on_confirm", "tgrass_off_confirm",
                                                        "tgrass_set_ch") and c.from_user.id == ADMIN_ID)
def tgrass_actions(call):
    if call.data == "tgrass_on_confirm":
        set_setting("tgrass", "on")
        bot.answer_callback_query(call.id, "✅ TGrass включён!")
        bot.send_message(call.message.chat.id, "✅ TGrass включён!",
                         reply_markup=build_admin_keyboard())
    elif call.data == "tgrass_off_confirm":
        set_setting("tgrass", "off")
        bot.answer_callback_query(call.id, "❌ TGrass выключен!")
        bot.send_message(call.message.chat.id, "❌ TGrass выключен!",
                         reply_markup=build_admin_keyboard())
    elif call.data == "tgrass_set_ch":
        set_state(call.from_user.id, "tgrass_set_ch")
        bot.send_message(call.message.chat.id,
            "⚙️ Введите @username канала TGrass:")
        bot.answer_callback_query(call.id)

# ── Удалить спонсора/addlist ──────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("delspon_") and c.from_user.id == ADMIN_ID)
def cb_del_sponsor(call):
    doc_id = call.data[len("delspon_"):]
    del_sponsor(doc_id)
    bot.send_message(call.message.chat.id, "✅ Спонсор удалён!",
                     reply_markup=build_admin_keyboard())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("deladdl_") and c.from_user.id == ADMIN_ID)
def cb_del_addlist(call):
    doc_id = call.data[len("deladdl_"):]
    del_addlist(doc_id)
    bot.send_message(call.message.chat.id, "✅ Addlist удалён!",
                     reply_markup=build_admin_keyboard())
    bot.answer_callback_query(call.id)

# ── Отправка в каналы ─────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("sendch_") and (c.from_user.id == ADMIN_ID or is_extra_admin(c.from_user.id)))
def cb_sendch(call):
    target = call.data[len("sendch_"):]
    all_ch = list(get_sponsors()) + list(get_addlist())

    if target == "all":
        set_state(call.from_user.id, "adm_sendch_post", target="all")
        names = ", ".join(name for _, _, name, _ in all_ch)
        bot.send_message(call.message.chat.id,
            f"🚀 <b>Во все каналы</b>\n\nКаналы: <b>{names}</b>\n\n"
            f"Отправьте рекламный пост (текст, фото, видео — любой тип)\n\nОтмена: /cancel")
    else:
        found = [(cid, cl, nm, un) for cid, cl, nm, un in all_ch if cid == target]
        if not found:
            bot.answer_callback_query(call.id, "Канал не найден!", show_alert=True)
            return
        set_state(call.from_user.id, "adm_sendch_post", target=target)
        bot.send_message(call.message.chat.id,
            f"📺 Отправьте пост для канала <b>{found[0][2]}</b>\n\nОтмена: /cancel")
    bot.answer_callback_query(call.id)

# ╔══════════════════════════════════════════════════════════╗
#                   FSM — ОБРАБОТЧИК СООБЩЕНИЙ
# ╚══════════════════════════════════════════════════════════╝
@bot.message_handler(
    func=lambda m: get_state(m.from_user.id) is not None,
    content_types=["text","photo","video","document","audio","animation","sticker"]
)
def fsm_handler(message):
    uid   = message.from_user.id
    state = get_state(uid)

    # ── Промокод (пользователь) ───────────────────────────────────────────────
    if state == "promo_input":
        code = (message.text or "").strip().upper()
        ok, result = use_promo(code, uid)
        if ok:
            bal = db_get_balance(uid)
            bot.send_message(message.chat.id,
                f"✅ <b>Промокод активирован!</b>\n\n"
                f"💰 Начислено: <b>{result:.2f} TMT</b>\n"
                f"💳 Баланс: <b>{bal:.2f} TMT</b>",
                reply_markup=build_main_keyboard(uid))
        else:
            bot.send_message(message.chat.id, result,
                             reply_markup=build_main_keyboard(uid))
        clear_state(uid)
        return

    # Только для администратора — дальше
    if uid != ADMIN_ID and not is_extra_admin(uid):
        clear_state(uid)
        return
    # Extra admin sadece bu işlemleri yapabilir
    if is_extra_admin(uid) and uid != ADMIN_ID:
        allowed = {"adm_broadcast", "adm_sendch_post", "adm_code", "pch_add", "pch_send_post"}
        if state not in allowed:
            clear_state(uid)
            return

    # ── Добавить спонсора ─────────────────────────────────────────────────────
    if state == "adm_add_sponsor":
        txt = "/x " + (message.text or "")
        name, link, username = parse_channel_args(txt)
        if not name:
            bot.send_message(message.chat.id,
                "❌ Неверный формат!\nПример: <code>🌟НазваниеКанала @username</code>")
            return
        add_sponsor(link, name, username)
        clear_state(uid)
        bot.send_message(message.chat.id,
            f"✅ Спонсор добавлен!\n📢 <b>{name}</b> — <code>{username}</code>",
            reply_markup=build_admin_keyboard())

    # ── Добавить addlist ──────────────────────────────────────────────────────
    elif state == "adm_add_addlist":
        txt = "/x " + (message.text or "")
        name, link, username = parse_channel_args(txt)
        if not name:
            bot.send_message(message.chat.id,
                "❌ Неверный формат!\nПример: <code>✨НазваниеКанала @username</code>")
            return
        add_addlist(link, name, username)
        clear_state(uid)
        bot.send_message(message.chat.id,
            f"✅ Addlist добавлен!\n📋 <b>{name}</b> — <code>{username}</code>",
            reply_markup=build_admin_keyboard())

    # ── Изменить VPN код ──────────────────────────────────────────────────────
    elif state == "adm_code":
        new_vpn = (message.text or "").strip()
        if not new_vpn:
            bot.send_message(message.chat.id, "❌ Код boş olamaz!")
            return
        set_setting("vpn_code", new_vpn)
        clear_state(uid)
        kb = build_extra_admin_kb() if is_extra_admin(uid) and uid != ADMIN_ID else build_admin_keyboard()
        bot.send_message(
            message.chat.id,
            f"✅ <b>VPN-код обновлён!</b>\n\n🔑 Yeni kod:\n<code>{new_vpn}</code>",
            reply_markup=kb
        )

    # ── Создать промокод ──────────────────────────────────────────────────────
    elif state == "adm_promo":
        parts = (message.text or "").strip().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Формат: <code>сумма количество</code>")
            return
        try:
            amount   = float(parts[0])
            max_uses = int(parts[1])
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат!")
            return
        code = create_promo(amount, max_uses)
        clear_state(uid)
        bot.send_message(message.chat.id,
            f"✅ <b>Промокод создан!</b>\n\n"
            f"🎟 Код: <code>{code}</code>\n"
            f"💰 Сумма: <b>{amount:.2f} TMT</b>\n"
            f"🔢 Активаций: <b>{max_uses}</b>",
            reply_markup=build_admin_keyboard())

    # ── Рассылка ─────────────────────────────────────────────────────────────
    elif state == "adm_broadcast":
        users = db_get_all_users()
        total = len(users)
        ok = fail = 0
        prog = bot.send_message(message.chat.id, f"📢 Рассылка...\n0 / {total}")
        for i, uid2 in enumerate(users, 1):
            try:
                bot.copy_message(uid2, message.chat.id, message.message_id,
                                 reply_markup=message.reply_markup)
                ok += 1
            except Exception:
                fail += 1
            if i % 25 == 0 or i == total:
                try:
                    bot.edit_message_text(
                        f"📢 Рассылка...\n{i} / {total}",
                        message.chat.id, prog.message_id)
                except Exception:
                    pass
            time.sleep(0.05)
        clear_state(uid)
        bot.edit_message_text(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"✔️ Доставлено: <b>{ok}</b>\n"
            f"❌ Ошибок: <b>{fail}</b>\n"
            f"👥 Всего: <b>{total}</b>",
            message.chat.id, prog.message_id, parse_mode="HTML"
        )
        bot.send_message(message.chat.id, "Панель:", reply_markup=build_admin_keyboard())

    # ── Пост-каналы: добавить ────────────────────────────────────────────────
    elif state == "pch_add":
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(message.chat.id,
                "❌ Формат: <code>Название @username</code>\n\nПример: <code>HAPP_VPN @HAPP_VPN</code>")
            return
        name  = parts[0].strip()
        uname = parts[1].strip().lstrip("@")
        add_post_channel(name, uname)
        clear_state(uid)
        bot.send_message(message.chat.id,
            f"✅ Канал <b>{name}</b> (@{uname}) добавлен!")
        _show_post_channels_menu(message.chat.id)

    # ── Пост-каналы: отправить ────────────────────────────────────────────────
    elif state == "pch_send_post":
        d      = user_data.get(uid, {})
        target = d.get("target", "all")

        if target == "all":
            channels = get_post_channels()
        else:
            ch = col_post_channels.find_one({"_id": ObjectId(target)})
            channels = [ch] if ch else []

        if not channels:
            clear_state(uid)
            bot.send_message(message.chat.id, "❌ Каналов нет.",
                             reply_markup=build_admin_keyboard())
            return

        markup    = message.reply_markup
        ok        = 0
        fail      = 0
        fail_list = []

        prog = bot.send_message(
            message.chat.id,
            f"📡 Отправка...\n0 / {len(channels)}"
        )

        for i, ch in enumerate(channels, 1):
            tgt = "@" + ch.get("username", "").lstrip("@")
            if not tgt or tgt == "@":
                fail += 1
                fail_list.append(f"{ch.get('name','?')}: адрес отсутствует")
                continue
            try:
                sent = bot.copy_message(
                    tgt,
                    message.chat.id,
                    message.message_id,
                    reply_markup=markup
                )
                save_reklam(tgt, sent.message_id)
                ok += 1
            except telebot.apihelper.ApiTelegramException as e:
                fail += 1
                fail_list.append(f"{ch.get('name','?')} ({tgt}): {str(e)[:60]}")
            except Exception as e:
                fail += 1
                fail_list.append(f"{ch.get('name','?')} ({tgt}): {str(e)[:60]}")

            if i % 5 == 0 or i == len(channels):
                try:
                    bot.edit_message_text(
                        f"📡 Отправка...\n{i} / {len(channels)}",
                        message.chat.id, prog.message_id)
                except Exception:
                    pass
            time.sleep(0.3)

        fail_txt = ("\n\n❌ Ошибки:\n" + "\n".join(f"• {f}" for f in fail_list)) if fail_list else ""
        clear_state(uid)
        bot.edit_message_text(
            f"✅ <b>Отправка завершена!</b>\n\n"
            f"📡 Каналов: <b>{len(channels)}</b>\n"
            f"✔️ Успешно: <b>{ok}</b>\n"
            f"❌ Ошибок: <b>{fail}</b>{fail_txt}",
            message.chat.id, prog.message_id, parse_mode="HTML"
        )
        kb = build_extra_admin_kb() if is_extra_admin(uid) and uid != ADMIN_ID else build_admin_keyboard()
        bot.send_message(message.chat.id, "Панель:", reply_markup=kb)

    # ── Отправка в каналы (старая система) ───────────────────────────────────
    elif state == "adm_sendch_post":
        d      = user_data.get(uid, {})
        target = d.get("target", "all")
        all_ch = list(get_sponsors()) + list(get_addlist())
        send_list = all_ch if target == "all" else [c for c in all_ch if c[0] == target]
        if not send_list:
            clear_state(uid)
            bot.send_message(message.chat.id, "❌ Каналов нет.",
                             reply_markup=build_admin_keyboard())
            return
        markup = message.reply_markup
        ok = fail = 0
        fail_list = []
        prog = bot.send_message(message.chat.id,
                                f"📡 Отправка...\n0 / {len(send_list)}")
        for i, (ch_id, ch_link, name, uname) in enumerate(send_list, 1):
            tgt = "@" + uname.lstrip("@")
            try:
                sent = bot.copy_message(tgt, message.chat.id, message.message_id,
                                        reply_markup=markup)
                save_reklam(tgt, sent.message_id)
                ok += 1
            except Exception as e:
                fail += 1
                fail_list.append(f"{name}: {str(e)[:40]}")
            if i % 5 == 0 or i == len(send_list):
                try:
                    bot.edit_message_text(
                        f"📡 Отправка...\n{i} / {len(send_list)}",
                        message.chat.id, prog.message_id)
                except Exception:
                    pass
            time.sleep(0.3)
        fail_txt = ("\n\n❌ Ошибки:\n" + "\n".join(f"• {f}" for f in fail_list)) if fail_list else ""
        clear_state(uid)
        bot.edit_message_text(
            f"✅ <b>Отправка завершена!</b>\n\n"
            f"📡 Каналов: <b>{len(send_list)}</b>\n"
            f"✔️ Успешно: <b>{ok}</b>\n"
            f"❌ Ошибок: <b>{fail}</b>{fail_txt}",
            message.chat.id, prog.message_id, parse_mode="HTML"
        )
        bot.send_message(message.chat.id, "Панель:", reply_markup=build_admin_keyboard())

    # ── Admin ekleme ─────────────────────────────────────────────────────────
    elif state == "adm_add_admin":
        try:
            new_adm_id = int((message.text or "").strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Geçersiz ID!"); return
        # Extra admin kaydet (sadece broadcast + kanal post + reklam silme)
        try:
            adm_user = bot.get_chat(new_adm_id)
            adm_name = f"@{adm_user.username}" if adm_user.username else str(new_adm_id)
        except Exception:
            adm_name = str(new_adm_id)
        col_settings.update_one(
            {"key": f"extra_admin_{new_adm_id}"},
            {"$set": {"key": f"extra_admin_{new_adm_id}", "value": adm_name}},
            upsert=True
        )
        clear_state(uid)
        bot.send_message(message.chat.id,
            f"✅ <b>{adm_name}</b> admin yapıldı!\n\n"
            f"Yetkileri: Рассылка, Пост в каналы, Удалить рекламу",
            reply_markup=build_admin_keyboard())
        try:
            bot.send_message(new_adm_id, "🎉 Admin yetkiniz verildi!\n/admin komutunu kullanabilirsiniz.")
        except Exception:
            pass

    # ── TGrass канал ─────────────────────────────────────────────────────────
    elif state == "tgrass_set_ch":
        uname = (message.text or "").strip().lstrip("@")
        set_setting("tgrass_username", uname)
        clear_state(uid)
        bot.send_message(message.chat.id,
            f"✅ TGrass канал установлен: @{uname}",
            reply_markup=build_admin_keyboard())

    # ── Изменить приветствие ──────────────────────────────────────────────────
    elif state == "adm_welcome":
        set_setting("welcome_text", (message.text or "").strip())
        clear_state(uid)
        bot.send_message(message.chat.id, "✅ Приветствие обновлено!",
                         reply_markup=build_admin_keyboard())

# ╔══════════════════════════════════════════════════════════╗
#                   FLASK + SELF-PING
# ╚══════════════════════════════════════════════════════════╝
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    total = col_users.count_documents({})
    return f"✅ Bot is Alive! | Users: {total}", 200

@flask_app.route("/health")
def health():
    return "OK", 200

def self_ping():
    while True:
        try:
            r = requests.get(RENDER_URL, timeout=10)
            print(f"[Ping] {r.status_code}")
        except Exception as e:
            print(f"[Ping] Error: {e}")
        time.sleep(300)

def run_bot():
    print("🤖 ShadowVip Bot запущен (MongoDB + Flask)...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            print(f"[Polling] {e}")
            time.sleep(5)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ╔══════════════════════════════════════════════════════════╗

# ╔══════════════════════════════════════════════════════════╗
#                        ЗАПУСК
# ╚══════════════════════════════════════════════════════════╝

# ╔══════════════════════════════════════════════════════════╗
#                        ЗАПУСК
# ╚══════════════════════════════════════════════════════════╝
if __name__ == "__main__":
    threading.Thread(target=self_ping, daemon=True).start()
    threading.Thread(target=run_bot,   daemon=True).start()
    run_flask()et=run_bot,   daemon=True).start()
    run_flask()k()