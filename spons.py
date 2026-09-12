import os
import asyncio
import logging
import requests
import json
import re
import time
import datetime
import threading
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    CallbackQuery,
    Message
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Botuň sazlamalary
BOT_TOKEN = os.getenv("BOT_TOKEN", "8702526230:AAGFXniyZS_ExepTh21Ec67v1zl25jiynQM")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "8192709521,7523674506").split(",") if x.strip()]

# MongoDB
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://mergenowlyagulyyew41_db_user:ZvZhOKOAF6ZMRbHX@cluster1.l8z8gll.mongodb.net/?appName=Cluster1")

# TGRASS
TGRASS_API_KEY = os.getenv("TGRASS_API_KEY", "")
TGRASS_API_URL = "https://tgrass.space/offers"

# PIARFLOW
PIARFLOW_API_KEY = os.getenv("PIARFLOW_API_KEY", "")
PIARFLOW_API_URL = "https://piarflow.com/v1"

# SUBGRAM
SUBGRAM_API_KEY = os.getenv("SUBGRAM_API_KEY", "")
SUBGRAM_API_URL = "https://api.subgram.org/get-sponsors"

# Railway PORT
PORT = int(os.environ.get("PORT", 8080))

# bot
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# MongoDB bağlantısı
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["SponsorGateBot2"]
col_users = db["users"]
col_sponsors = db["sponsors"]
col_addlists = db["addlists"]
col_settings = db["settings"]
col_post_channels = db["post_channels"]
col_sent_ads = db["sent_ads"]

# Indexler
async def init_db():
    try:
        await col_users.create_index("user_id", unique=True)
        await col_sponsors.create_index("position")
        await col_addlists.create_index("position")
        await col_post_channels.create_index("username", unique=True)
        await col_settings.create_index("key", unique=True)
        
        # Default settings
        if not await col_settings.find_one({"key": "start_text"}):
            await col_settings.insert_one({
                "key": "start_text",
                "value": ""
            })
        if not await col_settings.find_one({"key": "vpn_code"}):
            await col_settings.insert_one({
                "key": "vpn_code",
                "value": ""
            })
        if not await col_settings.find_one({"key": "tgrass_enabled"}):
            await col_settings.insert_one({
                "key": "tgrass_enabled",
                "value": "1"
            })
        if not await col_settings.find_one({"key": "piarflow_enabled"}):
            await col_settings.insert_one({
                "key": "piarflow_enabled",
                "value": "1"
            })
        if not await col_settings.find_one({"key": "subgram_enabled"}):
            await col_settings.insert_one({
                "key": "subgram_enabled",
                # Subgram entegrasyon kodu henüz eklenmedi, varsayılan kapalı
                "value": "0"
            })
        
        print("✅ MongoDB bağlantısı başarılı!")
    except Exception as e:
        print(f"❌ MongoDB hatası: {e}")

# FSM States
class AdminStates(StatesGroup):
    waiting_for_sponsor_channel_id = State()
    waiting_for_sponsor_link = State()
    waiting_for_remove_sponsor_id = State()
    waiting_for_start_text = State()
    waiting_for_vpn_code = State()
    waiting_for_addlist_name = State()
    waiting_for_addlist_link = State()
    waiting_for_remove_addlist_id = State()
    waiting_for_broadcast = State()
    waiting_for_sponsor_position = State()
    waiting_for_addlist_position = State()
    # Post kanalları
    waiting_for_post_channel_name = State()
    waiting_for_post_channel_username = State()
    waiting_for_post_content = State()       # post mesajı bekleniyor

# Custom emoji ID'leri (icon olarak kullanılacak)
EMOJI_IDS = {
    "check": "5206607081334906820",      # ✔️
    "lock": "5463200466391298413",        # 🔐
    "stats": "5936143551854285132",       # 📊
    "refresh": "6030657343744644592",     # 🔄
    "broadcast": "6021418126061605425",   # 📢
    "edit": "5359488727158634349",        # ✏️
    "add": "5359651386160068849",         # ➕
    "remove": "5359651386160068849",      # ➖
    "vpn": "5206607081334906820",         # ✔️
    "sponsor": "5463200466391298413",     # 🔐
    "addlist": "5206607081334906820",     # ✔️
    "users": "5936143551854285132",       # 📊
    "warning": "5463200466391298413",     # 🔐
    "success": "5206607081334906820",     # ✔️
    "star": "5206607081334906820",        # ⭐
    "money": "5936143551854285132",       # 💰
    "phone": "6021418126061605425",       # 📱
    "people": "5463200466391298413",      # 👥
    "history": "6030657343744644592",     # 📋
    "info": "5359488727158634349",        # ℹ️
    "telegram": "5359651386160068849",    # 🇺🇸
    "thailand": "5206607081334906820",    # 🇹🇭
    "austria": "5463200466391298413",     # 🇦🇹
    "usa": "5359651386160068849",         # 🇺🇸
    "message": "6021418126061605425",     # 📨
    "time": "6030657343744644592",        # ⏰
    "link": "5359488727158634349",        # 🔗
    "tgrass": "5936143551854285132",      # 🌟
    "back": "5359488727158634349",        # ◀️
    "admin": "5463200466391298413",       # 👑
    "settings": "6030657343744644592",    # ⚙️
    "chanel": "5260268501515377807",      # 📣
    "chik": "5427009714745517609",        # ✅
    "del": "5841541824803509441",         # 🗑️
    "tekst": "5879841310902324730",       # ✏️
    "tgrassn": "6032742198179532882",     # ⚙️
    "post": "6021418126061605425",        # 📡
}

# Loglamagy sazlamak
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='bot.log'
)

logging.info(f"Admin ID: {ADMIN_IDS[0]}")

# ================= TGRASS FUNKSIÝALARY =================
def get_user_language(user_id):
    return 'ru'

async def check_tgrass_subscriptions(user_id, username=None, is_premium=False):
    try:
        import httpx
        payload = {
            "tg_user_id": int(user_id),
            "tg_login": username or "",
            "lang": get_user_language(user_id),
            "is_premium": bool(is_premium),
        }
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "Auth": TGRASS_API_KEY,
        }
        
        logging.info(f"TGrass API istek: {payload}")
        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            response = await client.post(TGRASS_API_URL, json=payload, headers=headers)
        
        if response.status_code == 200:
            resp_json = response.json()
            logging.info(f"TGrass API cevap: {resp_json}")
            
            if resp_json.get("status") == "not_ok":
                offers = resp_json.get("offers", [])
                formatted_offers = []
                for offer in offers:
                    channel_name = None
                    if "title" in offer and offer["title"]:
                        channel_name = offer["title"]
                    elif "name" in offer and offer["name"]:
                        channel_name = offer["name"]
                    elif "channel_name" in offer and offer["channel_name"]:
                        channel_name = offer["channel_name"]
                    elif "description" in offer and offer["description"]:
                        channel_name = offer["description"][:30]
                    else:
                        channel_name = "Спонсор канал"
                    
                    channel_link = None
                    if "link" in offer and offer["link"]:
                        channel_link = offer["link"]
                    elif "url" in offer and offer["url"]:
                        channel_link = offer["url"]
                    elif "channel_link" in offer and offer["channel_link"]:
                        channel_link = offer["channel_link"]
                    else:
                        channel_link = "#"
                    
                    formatted_offers.append({
                        "title": channel_name,
                        "link": channel_link,
                        "id": offer.get("id", 0)
                    })
                
                return formatted_offers
        return []
    except Exception as e:
        logging.error(f"TGrass error: {e}")
        return []

# ================= PIARFLOW FUNKSIÝALARY =================
async def check_piarflow_subscriptions(user_id, username=None, is_premium=False):
    """PiarFlow API'sinden kullanıcının henüz tamamlamadığı sponsor görevlerini döner."""
    if not PIARFLOW_API_KEY:
        return []
    try:
        import httpx
        headers = {
            "Authorization": f"Bearer {PIARFLOW_API_KEY}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            resp = await client.post(
                f"{PIARFLOW_API_URL}/sponsors",
                json={
                    "user_id": int(user_id),
                    "chat_id": int(user_id),
                    "max_sponsors": 5,
                },
                headers=headers,
            )

        if resp.status_code >= 400:
            logging.error(f"PiarFlow /sponsors hata: {resp.status_code} {resp.text}")
            return []

        data = resp.json()
        sponsors = data.get("sponsors") or []
        if not sponsors:
            return []

        links = [s.get("link") for s in sponsors if s.get("link")]

        statuses = {}
        if links:
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                check_resp = await client.post(
                    f"{PIARFLOW_API_URL}/sponsors/check",
                    json={"user_id": int(user_id), "links": links},
                    headers=headers,
                )
            if check_resp.status_code < 400:
                check_data = check_resp.json()
                for item in check_data.get("sponsors") or []:
                    statuses[item.get("link")] = item.get("status")
            else:
                logging.error(f"PiarFlow /sponsors/check hata: {check_resp.status_code} {check_resp.text}")

        pending = []
        for s in sponsors:
            link = s.get("link")
            if statuses.get(link) == "subscribed":
                continue
            pending.append({
                "title": s.get("title") or s.get("name") or "Спонсор канал",
                "link": link or "#",
                "id": s.get("id", 0),
            })
        return pending
    except Exception as e:
        logging.error(f"PiarFlow error: {e}")
        return []

async def get_piarflow_enabled():
    doc = await col_settings.find_one({"key": "piarflow_enabled"})
    return doc["value"] == "1" if doc else True

async def set_piarflow_enabled(enabled):
    await col_settings.update_one(
        {"key": "piarflow_enabled"},
        {"$set": {"value": "1" if enabled else "0"}},
        upsert=True
    )

async def get_subgram_enabled():
    doc = await col_settings.find_one({"key": "subgram_enabled"})
    return doc["value"] == "1" if doc else False

async def set_subgram_enabled(enabled):
    await col_settings.update_one(
        {"key": "subgram_enabled"},
        {"$set": {"value": "1" if enabled else "0"}},
        upsert=True
    )

# ================= SUBGRAM FUNKSIÝALARY =================
async def check_subgram_subscriptions(user_id, username=None, is_premium=False):
    """Subgram API-den ulanyjynyň entäk ýerine ýetirmedik sponsor tabşyryklaryny gaýtarýar.

    Bot ähli sponsorlary (TGrass/PiarFlow/Subgram) bir ýerde birleşdirip görkezýär,
    şonuň üçin bu funksiýa diňe "Получать ссылки в API" (API arkaly link almak)
    режimi Subgram.org sazlamalarynda AÇYK bolanda doly işleýär. Ol режim ÖÇÜRILEN
    bolsa, Subgram jogabynda "additional" bolmaýar we ol ýagdaýda bot hiç bir
    sponsor kanaly görkezip bilmeýär (aşakdaky log ýazgysyny serediň).
    """
    if not SUBGRAM_API_KEY:
        return []
    try:
        import httpx
        headers = {"Auth": SUBGRAM_API_KEY}
        payload = {
            "user_id": int(user_id),
            "chat_id": int(user_id),
            "username": username,
            "is_premium": bool(is_premium),
        }

        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            resp = await client.post(SUBGRAM_API_URL, headers=headers, json=payload)

        if resp.status_code >= 400:
            logging.error(f"Subgram API hata: {resp.status_code} {resp.text}")
            return []

        data = resp.json()
        status = data.get("status")

        # "ok" -> hemme tabşyryk ýerine ýetirilen, "error" -> geçirilýär (bloklanmaýar)
        if status != "warning":
            return []

        additional = data.get("additional") or {}
        sponsors = additional.get("sponsors") or []

        if not sponsors:
            if "additional" not in data:
                logging.warning(
                    "Subgram: 'Получать ссылки в API' режimi öçürilen. "
                    "subgram.org bot sazlamalaryndan ony açyň, ýogsam bu bot "
                    "sponsor kanallaryny özi görkezip bilmeýär."
                )
            return []

        # links -> entäk ýerine ýetirilmedik sponsorlaryň linkleri
        pending = set(data.get("links") or [])

        offers = []
        for i, sponsor in enumerate(sponsors):
            link = sponsor.get("link")
            if not link:
                continue
            if pending:
                if link not in pending:
                    continue  # eýýäm ýerine ýetirilen
            elif not (sponsor.get("available_now") and sponsor.get("status") == "unsubscribed"):
                continue

            offers.append({
                "title": sponsor.get("button_text") or sponsor.get("title") or sponsor.get("name") or "Спонсор канал",
                "link": link,
                "id": sponsor.get("id", i),
            })

        return offers
    except Exception as e:
        logging.error(f"Subgram error: {e}")
        return []

def parse_premium_emoji(text):
    pattern = r'<tg-emoji emoji-id="([^"]+)">([^<]+)</tg-emoji>'
    
    def replace_emoji(match):
        emoji_id = match.group(1)
        emoji_char = match.group(2)
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji_char}</tg-emoji>'
    
    return re.sub(pattern, replace_emoji, text)

# ================= MongoDB VERİTABANI FONKSİYONLARI =================

async def get_setting(key):
    doc = await col_settings.find_one({"key": key})
    return doc["value"] if doc else ""

async def set_setting(key, value):
    await col_settings.update_one(
        {"key": key},
        {"$set": {"value": value}},
        upsert=True
    )

async def get_sponsors():
    cursor = col_sponsors.find().sort("position", 1)
    return await cursor.to_list(length=None)

async def add_sponsor(channel_id, link, position):
    await col_sponsors.insert_one({
        "channel_id": channel_id,
        "link": link,
        "position": position
    })

async def delete_sponsor(doc_id):
    await col_sponsors.delete_one({"_id": ObjectId(doc_id)})

async def get_addlists():
    cursor = col_addlists.find().sort("position", 1)
    return await cursor.to_list(length=None)

async def add_addlist(name, link, position):
    await col_addlists.insert_one({
        "name": name,
        "link": link,
        "position": position
    })

async def delete_addlist(doc_id):
    await col_addlists.delete_one({"_id": ObjectId(doc_id)})

async def get_all_users():
    cursor = col_users.find({}, {"user_id": 1})
    return [doc["user_id"] async for doc in cursor]

async def add_user(user_id, username, referred_by=None):
    existing = await col_users.find_one({"user_id": user_id})
    if existing:
        return False
    await col_users.insert_one({
        "user_id": user_id,
        "username": username or "",
        "join_date": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "balance": 0.0,
        "referred_by": referred_by,
        "referral_rewarded": False
    })
    return True

async def get_user(user_id):
    return await col_users.find_one({"user_id": user_id})

async def get_balance(user_id):
    doc = await col_users.find_one({"user_id": user_id}, {"balance": 1})
    return round(doc["balance"], 2) if doc else 0.0

async def add_balance(user_id, amount):
    await col_users.update_one(
        {"user_id": user_id},
        {"$inc": {"balance": round(amount, 2)}}
    )

async def get_ref_count(user_id):
    return await col_users.count_documents({"referred_by": user_id})

async def set_rewarded(user_id):
    await col_users.update_one(
        {"user_id": user_id},
        {"$set": {"referral_rewarded": True}}
    )

async def get_post_channels():
    cursor = col_post_channels.find().sort("_id", 1)
    return await cursor.to_list(length=None)

async def add_post_channel(name, username):
    uname = username.strip().lstrip("@")
    await col_post_channels.update_one(
        {"username": uname},
        {"$set": {"name": name, "username": uname}},
        upsert=True
    )
    return True

async def delete_post_channel(channel_id):
    await col_post_channels.delete_one({"_id": ObjectId(channel_id)})
    return True

async def save_sent_ad(chat_id, message_id):
    await col_sent_ads.insert_one({
        "chat_id": str(chat_id),
        "message_id": message_id
    })

async def get_sent_ads():
    cursor = col_sent_ads.find()
    return [(doc["chat_id"], doc["message_id"]) async for doc in cursor]

async def clear_sent_ads():
    await col_sent_ads.delete_many({})

async def get_tgrass_enabled():
    doc = await col_settings.find_one({"key": "tgrass_enabled"})
    return doc["value"] == "1" if doc else True

async def set_tgrass_enabled(enabled):
    await col_settings.update_one(
        {"key": "tgrass_enabled"},
        {"$set": {"value": "1" if enabled else "0"}},
        upsert=True
    )

async def get_stats():
    total = await col_users.count_documents({})
    return total, 0, 0

async def get_new_users_today():
    today_start = datetime.datetime.utcnow().strftime("%Y-%m-%d 00:00:00")
    return await col_users.count_documents({"join_date": {"$gte": today_start}})

async def get_vpn_stats():
    now = datetime.datetime.utcnow()
    today_start = now.strftime("%Y-%m-%d 00:00:00")
    week_start = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    month_start = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    today_count = await col_users.count_documents({"vpn_taken_date": {"$gte": today_start}})
    week_count = await col_users.count_documents({"vpn_taken_date": {"$gte": week_start}})
    month_count = await col_users.count_documents({"vpn_taken_date": {"$gte": month_start}})

    return today_count, week_count, month_count

# ================= TGRASS FUNKSIÝALARY (Async) =================

async def check_tgrass_subscriptions_async(user_id, username=None, is_premium=False):
    return await check_tgrass_subscriptions(user_id, username, is_premium)

async def get_channel_name(channel_id=None, link=None):
    try:
        if channel_id:
            chat = await bot.get_chat(channel_id)
            return chat.title or f"Канал {channel_id}"
        elif link and link.startswith('https://t.me/'):
            username = link.replace('https://t.me/', '@')
            chat = await bot.get_chat(username)
            return chat.title or username
        else:
            return link.s