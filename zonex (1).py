import asyncio
import json
import os
import threading
import time
from datetime import datetime
from io import BytesIO

import certifi
import requests
from flask import Flask
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ╔══════════════════════════════════════════════════════════╗
#                     KONFIGÜRASYON
# ╚══════════════════════════════════════════════════════════╝
BOT_TOKEN  = "8678757671:AAFJumzi4NeeHk8lv736fFRQsQOontsrMkk"
ADMIN_ID   = 7523674506
API_URL    = "https://crypto.happ.su/api.php"
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://kanal-bot.onrender.com")

MONGO_URI = (
    "mongodb+srv://mergenowlyagulyyew41_db_user:ZvZhOKOAF6ZMRbHX"
    "@cluster1.l8z8gll.mongodb.net/?appName=Cluster1"
)

TGRASS_ENDPOINT = "https://tgrass.space/offers"
TGRASS_TOKEN    = "42c215beecac464a8642678244be12e3"
TGRASS_HEADERS  = {
    "Content-Type": "application/json",
    "Auth":         TGRASS_TOKEN,
}

# ╔══════════════════════════════════════════════════════════╗
#                     MONGODB BAĞLANTISI
# ╚══════════════════════════════════════════════════════════╝
_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=10000,
)
try:
    _client.admin.command("ping")
    print("✅ MongoDB bağlandı!")
except ConnectionFailure:
    print("❌ MongoDB bağlantı hatası!")

_db = _client["happvpn_data"]

col_users           = _db["users"]
col_logs            = _db["logs"]
col_settings        = _db["settings"]
col_tgrass_channels = _db["tgrass_channels"]
col_my_channels     = _db["my_channels"]  # Sizin ekleyip sileceğiniz kanalların koleksiyonu

col_users.create_index("user_id", unique=True)
col_my_channels.create_index("username", unique=True)

# İlk açılışta eğer veritabanı boşsa örnek olarak ekran görüntünüzdeki Efxproxy kanalını ekleyelim
if col_my_channels.count_documents({}) == 0:
    col_my_channels.insert_one({"username": "Efxproxy", "created_at": datetime.utcnow().strftime("%Y-%m-%d")})

# ╔══════════════════════════════════════════════════════════╗
#                     AYARLAR & KANAL YÖNETİMİ
# ╚══════════════════════════════════════════════════════════╝
def get_setting(key, default=""):
    doc = col_settings.find_one({"key": key})
    return doc["value"] if doc else default

def set_setting(key, value):
    col_settings.update_one(
        {"key": key}, {"$set": {"value": value}}, upsert=True
    )

if not get_setting("tgrass"):
    set_setting("tgrass", "on")

# Admin kanallarını yönetmek için fonksiyonlar
def _db_add_channel(username: str) -> bool:
    clean_username = username.strip().lstrip("@")
    if not clean_username:
        return False
    try:
        col_my_channels.update_one(
            {"username": clean_username},
            {"$set": {"username": clean_username, "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}},
            upsert=True
        )
        return True
    except Exception:
        return False

def _db_delete_channel(username: str) -> bool:
    clean_username = username.strip().lstrip("@")
    result = col_my_channels.delete_one({"username": clean_username})
    return result.deleted_count > 0

def _db_get_all_channels():
    return [doc["username"] for doc in col_my_channels.find()]

# ╔══════════════════════════════════════════════════════════╗
#                     KULLANICI FONKSİYONLARI
# ╚══════════════════════════════════════════════════════════╝
def _db_upsert_user(user_id: int, username: str):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    col_users.update_one(
        {"user_id": user_id},
        {
            "$set": {"username": username, "last_seen": now},
            "$setOnInsert": {"join_date": now, "is_banned": False},
        },
        upsert=True,
    )

def _db_get_user(user_id: int):
    return col_users.find_one({"user_id": user_id})

def _db_ban_user(user_id: int) -> bool:
    r = col_users.update_one({"user_id": user_id}, {"$set": {"is_banned": True}})
    return r.matched_count > 0

def _db_unban_user(user_id: int) -> bool:
    r = col_users.update_one({"user_id": user_id}, {"$set": {"is_banned": False}})
    return r.matched_count > 0

def _db_get_users(limit: int = 500):
    return list(col_users.find().sort("last_seen", -1).limit(limit))

def _db_get_counts():
    total  = col_users.count_documents({})
    active = col_users.count_documents({"is_banned": {"$ne": True}})
    return total, active

# ╔══════════════════════════════════════════════════════════╗
#                     LOG FONKSİYONLARI
# ╚══════════════════════════════════════════════════════════╝
def _db_insert_log(user_id: int, username: str, input_url: str, encrypted_url: str):
    col_logs.insert_one({
        "user_id":       user_id,
        "username":      username,
        "input_url":     input_url,
        "encrypted_url": encrypted_url,
        "created_at":    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    })

def _db_get_last_logs(limit: int = 50):
    return list(col_logs.find().sort("_id", -1).limit(limit))

def _db_clear_logs():
    col_logs.delete_many({})

# ╔══════════════════════════════════════════════════════════╗
#                     ASYNC WRAPPERS
# ╚══════════════════════════════════════════════════════════╝
async def db_upsert_user(uid, un):
    await asyncio.to_thread(_db_upsert_user, uid, un)

async def db_get_user(uid):
    return await asyncio.to_thread(_db_get_user, uid)

async def db_ban_user(uid):
    return await asyncio.to_thread(_db_ban_user, uid)

async def db_unban_user(uid):
    return await asyncio.to_thread(_db_unban_user, uid)

async def db_get_users(n=500):
    return await asyncio.to_thread(_db_get_users, n)

async def db_get_counts():
    return await asyncio.to_thread(_db_get_counts)

async def db_insert_log(uid, un, inp, enc):
    await asyncio.to_thread(_db_insert_log, uid, un, inp, enc)

async def db_get_last_logs(n=50):
    return await asyncio.to_thread(_db_get_last_logs, n)

async def db_clear_logs():
    await asyncio.to_thread(_db_clear_logs)

# ╔══════════════════════════════════════════════════════════╗
#                     HAPP API
# ╚══════════════════════════════════════════════════════════╝
def _encrypt_sync(plain_url: str):
    try:
        resp = requests.post(
            API_URL,
            headers={"Content-Type": "application/json"},
            json={"url": plain_url},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

async def encrypt_via_happ_api(plain_url: str):
    return await asyncio.to_thread(_encrypt_sync, plain_url)

# ╔══════════════════════════════════════════════════════════╗
#                     TGRASS FONKSİYONLARI
# ╚══════════════════════════════════════════════════════════╝
def _tgrass_fetch_all_sync():
    if get_setting("tgrass", "on") != "on":
        return 0, "TGrass kapalı"
    try:
        resp = requests.post(
            TGRASS_ENDPOINT,
            json={"tg_user_id": 0, "is_premium": False, "lang": "en"},
            headers=TGRASS_HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            return 0, f"HTTP {resp.status_code}: {resp.text[:100]}"
        data   = resp.json()
        offers = data if isinstance(data, list) else data.get("offers", data.get("channels", []))
        count  = 0
        for offer in offers:
            username = (offer.get("username") or offer.get("login") or
                        offer.get("channel_username") or "")
            name     = offer.get("name") or offer.get("title") or username
            link     = (offer.get("link") or offer.get("url") or
                        (f"https://t.me/{username.lstrip('@')}" if username else ""))
            if username and link:
                col_tgrass_channels.update_one(
                    {"username": username.lstrip("@")},
                    {"$set": {"link": link, "name": name, "username": username.lstrip("@")}},
                    upsert=True,
                )
                count += 1
        print(f"[TGrass] {count} kanal kaydedildi")
        return count, "ok"
    except Exception as e:
        return 0, str(e)[:80]

async def tgrass_fetch_all():
    return await asyncio.to_thread(_tgrass_fetch_all_sync)

def _tgrass_get_offers_sync(user_id: int, is_premium: bool, lang: str):
    if get_setting("tgrass", "on") != "on":
        return []
    try:
        resp = requests.post(
            TGRASS_ENDPOINT,
            json={"tg_user_id": user_id, "is_premium": is_premium, "lang": lang},
            headers=TGRASS_HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else data.get("offers", data.get("channels", []))
    except Exception as e:
        print(f"[TGrass offers] {e}")
        return []

async def check_tgrass_subscription(bot_app, user) -> list:
    if get_setting("tgrass", "on") != "on":
        return []

    is_premium = bool(getattr(user, "is_premium", False))
    lang       = getattr(user, "language_code", "en") or "en"
    offers     = await asyncio.to_thread(_tgrass_get_offers_sync, user.id, is_premium, lang)

    not_sub = []

    if offers:
        for offer in offers:
            if offer.get("type") not in ("channel", None):
                continue
            if not offer.get("subscribed", True):
                name = offer.get("name") or offer.get("title") or "Kanal"
                link = offer.get("link") or offer.get("url") or ""
                if link:
                    not_sub.append((link, name))
    else:
        db_channels = list(col_tgrass_channels.find())
        for ch in db_channels:
            username = ch.get("username", "")
            if not username:
                not_sub.append((ch.get("link", ""), ch.get("name", "Kanal")))
                continue
            try:
                member = await bot_app.bot.get_chat_member(
                    "@" + username.lstrip("@"), user.id
                )
                if member.status in ("left", "kicked", "banned"):
                    not_sub.append((ch.get("link", ""), ch.get("name", "Kanal")))
            except Exception:
                not_sub.append((ch.get("link", ""), ch.get("name", "Kanal")))

    return not_sub

# ╔══════════════════════════════════════════════════════════╗
#                     KLAVYELER
# ╚══════════════════════════════════════════════════════════╝
def _sub_keyboard(not_sub: list) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(f"📢 {name}", url=link)] for link, name in not_sub]
    kb.append([InlineKeyboardButton("✅ Abone Oldum, Kontrol Et", callback_data="check_sub")])
    return InlineKeyboardMarkup(kb)

def _admin_keyboard() -> InlineKeyboardMarkup:
    tg_icon = "✅" if get_setting("tgrass", "on") == "on" else "❌"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Ulanyjylar",         callback_data="admin_users")],
        [InlineKeyboardButton("📜 Ýazgylary Gör",      callback_data="admin_logs")],
        [InlineKeyboardButton("📊 Statistika",          callback_data="admin_stats"),
         InlineKeyboardButton("🧹 Ýazgylary Arassala", callback_data="admin_clear_logs")],
        [InlineKeyboardButton(f"⚙️ TGrass {tg_icon}",  callback_data="admin_tgrass_toggle"),
         InlineKeyboardButton("🔄 TGrass Güncelle",     callback_data="admin_tgrass_refresh")],
        [InlineKeyboardButton("➕ Kanal Ekle",          callback_data="admin_add_ch"),
         InlineKeyboardButton("➖ Kanal Sil",           callback_data="admin_del_ch")],
        [InlineKeyboardButton("📋 Kayıtlı Kanallar",     callback_data="admin_list_ch")],
        [InlineKeyboardButton("🚀 Kanallara Reklam At", callback_data="admin_broadcast_prompt")]
    ])

# ╔══════════════════════════════════════════════════════════╗
#                     HANDLER'LAR
# ╚══════════════════════════════════════════════════════════╝
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db_upsert_user(user.id, user.username or "")

    not_sub = await check_tgrass_subscription(context.application, user)
    if not_sub:
        await update.message.reply_text(
            "👋 Salam! Botu ulanyp başlamak üçin\n"
            "ilki aşakdaky kanallara agza boluň:\n\n"
            "Agza bolanyňyzdan soň «✅ Abone Oldum, Kontrol Et» düwmesine basyň.",
            reply_markup=_sub_keyboard(not_sub),
        )
        return

    await update.message.reply_text(
        "👋 Salam!\nMen Happ VPN URL-lerini şifrleýän bot.\n"
        "Her hili protokol üçin URL iberiň (http, vless, ss, trojan...).\n"
        "Men şifrlenen netijeni yzyna bererin."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if not text:
        return

    # ADMIN AYAR DURUMLARI (STATE CONTROL)
    if user.id == ADMIN_ID:
        state = context.user_data.get("state")
        
        # 1. KANAL EKLEME DURUMU
        if state == "waiting_add_channel":
            context.user_data.pop("state", None)
            if _db_add_channel(text):
                await update.message.reply_text(f"✅ `@{text.lstrip('@')}` kanalı başarıyla listeye eklendi!", reply_markup=_admin_keyboard())
            else:
                await update.message.reply_text("❌ Kanal eklenirken bir hata oluştu.", reply_markup=_admin_keyboard())
            return
            
        # 2. KANAL SİLME DURUMU
        if state == "waiting_del_channel":
            context.user_data.pop("state", None)
            if _db_delete_channel(text):
                await update.message.reply_text(f"🗑 `@{text.lstrip('@')}` kanalı listeden silindi!", reply_markup=_admin_keyboard())
            else:
                await update.message.reply_text("❌ Bu kullanıcı adına sahip bir kanal listede bulunamadı.", reply_markup=_admin_keyboard())
            return

        # 3. TOPLU REKLAM GÖNDERME DURUMU
        if state == "waiting_broadcast_post":
            context.user_data.pop("state", None)
            status_msg = await update.message.reply_text("🚀 Reklam veritabanındaki kanallara ugradylýar...")
            
            my_channels = await asyncio.to_thread(_db_get_all_channels)
            if not my_channels:
                await status_msg.edit_text("❌ Listede kayıtlı kanal bulunamadı! Lütfen önce kanal ekleyin.")
                return
                
            success = 0
            failed = 0
            
            for ch_username in my_channels:
                try:
                    await context.bot.copy_message(
                        chat_id=f"@{ch_username}",
                        from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id
                    )
                    success += 1
                except Exception as e:
                    print(f"Kanalda yetki hatasy @{ch_username}: {e}")
                    failed += 1
                    
            await status_msg.edit_text(
                f"📢 **Reklam Ugratmak Tamamlandy!**\n\n"
                f"✅ Şowly (Başarılı): {success}\n"
                f"❌ Şowsuz (Admin däl): {failed}\n"
                f"📊 Jemi synalşan (Toplam): {len(my_channels)}"
            )
            return

    # NORMAL KULLANICI İŞLEMLERİ
    await db_upsert_user(user.id, user.username or "")

    doc = await db_get_user(user.id)
    if doc and doc.get("is_banned"):
        await update.message.reply_text("🚫 Siz ban edilensiňiz. Administratore ýüz tutuň.")
        return

    not_sub = await check_tgrass_subscription(context.application, user)
    if not_sub:
        context.user_data["pending_url"] = text
        await update.message.reply_text(
            "⚠️ URL-ni şifrlemek üçin aşakdaky kanallara agza boluň:\n\n"
            "Agza bolanyňyzdan soň «✅ Abone Oldum, Kontrol Et» düwmesine basyň.",
            reply_markup=_sub_keyboard(not_sub),
        )
        return

    await _do_encrypt(update.message, context, text, user)


async def _do_encrypt(msg_obj, context, url: str, user):
    waiting = await msg_obj.reply_text("🔒 Şifrlenýär, garaşyň...")
    result  = await encrypt_via_happ_api(url)

    if isinstance(result, dict) and "encrypted_link" in result:
        enc = result["encrypted_link"]
        await db_insert_log(user.id, user.username or "", url, enc)
        await waiting.edit_text(f"Netije ✅:\n```{enc}```", parse_mode="Markdown")
    else:
        try:
            txt = json.dumps(result, ensure_ascii=False, indent=2)
        except Exception:
            txt = str(result)
        await waiting.edit_text(
            f"❌ Säwlik ýa-da näbelli jogap:\n```{txt}```", parse_mode="Markdown"
        )


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if data == "check_sub":
        user    = query.from_user
        not_sub = await check_tgrass_subscription(context.application, user)
        if not_sub:
            await query.edit_message_reply_markup(reply_markup=_sub_keyboard(not_sub))
            await query.answer("⚠️ Henüz bazı kanallara abone olmadınız!", show_alert=True)
            return
        pending = context.user_data.pop("pending_url", None)
        if pending:
            await query.edit_message_text("✅ Abonelik tassyklandy! Şifrlenýär...")
            await _do_encrypt(query.message, context, pending, user)
        else:
            await query.edit_message_text(
                "✅ Abonelik tassyklandy!\n\n"
                "👋 Salam!\nMen Happ VPN URL-lerini şifrleýän bot.\n"
                "Her hili protokol üçin URL iberiň (http, vless, ss, trojan...).\n"
                "Men şifrlenen netijeni yzyna bererin."
            )
        return

    if uid != ADMIN_ID:
        await query.edit_message_text("🚫 Siz admin däl.")
        return

    # KANAL EKLEME BUTONU TETİKLEME
    if data == "admin_add_ch":
        context.user_data["state"] = "waiting_add_channel"
        await query.edit_message_text("➕ **Kanal Ekleme Modu**\n\nLütfen eklemek istediğiniz kanalın kullanıcı adını yazıp gönderin.\nÖrnek: `Efxproxy` veya `@Efxproxy`")
        return

    # KANAL SİLME BUTONU TETİKLEME
    if data == "admin_del_ch":
        context.user_data["state"] = "waiting_del_channel"
        await query.edit_message_text("➖ **Kanal Silme Modu**\n\nLütfen listeden silmek istediğiniz kanalın kullanıcı adını yazıp gönderin.\nÖrnek: `Efxproxy`")
        return

    # KANALLARI LİSTELEME
    if data == "admin_list_ch":
        channels = _db_get_all_channels()
        if not channels:
            await query.edit_message_text("📭 Kayıtlı reklam kanalı bulunamadı.", reply_markup=_admin_keyboard())
            return
        text = "📋 **Kayıtlı Reklam Kanalları Listesi:**\n\n"
        for idx, ch in enumerate(channels, 1):
            text += f"{idx}. @{ch}\n"
        await query.edit_message_text(text, reply_markup=_admin_keyboard())
        return

    if data == "admin_broadcast_prompt":
        context.user_data["state"] = "waiting_broadcast_post"
        await query.edit_message_text(
            "📢 **Addlist Reklam Bölümi**\n\n"
            "Ugratmak isleýän reklam postuňyzy (Tekst, Resminama ýa-da Suratly) şu ýere ugradyň.\n"
            "Bot ony sanawdaky ähli kanallara ugradar."
        )
        return

    if data == "admin_users":
        users = await db_get_users(500)
        if not users:
            await query.edit_message_text("📭 Ulanyjy ýok.")
            return
        lines = []
        for u in users:
            uname    = f"@{u.get('username','')}" if u.get("username") else ""
            ban_text = "🔴 BAN" if u.get("is_banned") else "🟢 OK"
            lines.append(f"{u['user_id']} {uname} — {ban_text} — {u.get('last_seen','-')}")
        text = "👥 Ulanyjylar:\n\n" + "\n".join(lines)
        if len(text) > 3500:
            bio = BytesIO(text.encode("utf-8")); bio.name = "users.txt"
            await query.message.reply_document(bio)
            await query.edit_message_text("📥 Ulanyjy sanawy faýl hökmünde iberildi.")
        else:
            await query.edit_message_text(text)

    elif data == "admin_logs":
        logs = await db_get_last_logs(50)
        if not logs:
            await query.edit_message_text("📭 Ýazgy ýok.")
            return
        chunks = []
        for row in logs:
            uname = f"@{row.get('username','')}" if row.get("username") else ""
            chunks.append(
                f"{row.get('created_at','-')}\n"
                f"{row.get('user_id','')} {uname}\n"
                f"➡ {row.get('input_url','')}\n"
                f"🔒 `{row.get('encrypted_url','')}`\n"
            )
        text = "\n".join(chunks)
        if len(text) > 3500:
            bio = BytesIO(text.encode("utf-8")); bio.name = "logs.txt"
            await query.message.reply_document(bio)
            await query.edit_message_text("📥 Ýazgylar faýl hökmünde iberildi.")
        else:
            await query.edit_message_text("📜 Iň soňky ýazgylar:\n\n" + text)

    elif data == "admin_stats":
        total, active = await db_get_counts()
        await query.edit_message_text(
            f"📊 Statistika:\nJemi ulanyjy: {total}\nAktiw (ban edilmedik): {active}"
        )

    elif data == "admin_clear_logs":
        await db_clear_logs()
        await query.edit_message_text("🧹 Ähli ýazgylar pozuldy.")

    elif data == "admin_tgrass_toggle":
        current = get_setting("tgrass", "on")
        new_val = "off" if current == "on" else "on"
        await asyncio.to_thread(set_setting, "tgrass", new_val)
        icon = "✅" if new_val == "on" else "❌"
        await query.edit_message_text(
            f"TGrass {icon} {'açıldı' if new_val == 'on' else 'kapatıldı'}.",
            reply_markup=_admin_keyboard(),
        )

    elif data == "admin_tgrass_refresh":
        await query.edit_message_text("🔄 TGrass kanalları güncelleniyor...")
        count, msg = await tgrass_fetch_all()
        await query.edit_message_text(
            f"{'✅' if msg == 'ok' else '❌'} TGrass: {count} kanal güncellendi.\nDurum: {msg}",
            reply_markup=_admin_keyboard(),
        )


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Bu bölüm diňe administratore açyk.")
        return
    await update.message.reply_text("🛠 Admin menýu:", reply_markup=_admin_keyboard())


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Bu komanda diňe administratore açyk."); return
    if not context.args:
        await update.message.reply_text("Meselem: /ban 123456789"); return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Nädogry ID."); return
    ok = await db_ban_user(target)
    await update.message.reply_text(
        f"🚫 {target} ban edildi." if ok else "Ulanyjy tabylmady."
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Bu komanda diňe administratore açyk."); return
    if not context.args:
        await update.message.reply_text("Meselem: /unban 123456789"); return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Nädogry ID."); return
    ok = await db_unban_user(target)
    await update.message.reply_text(
        f"✅ {target} banyndan çykaryldy." if ok else "Ulanyjy tabylmady."
    )

# ╔══════════════════════════════════════════════════════════╗
#                     FLASK + SELF-PING
# ╚══════════════════════════════════════════════════════════╝
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    try:
        total = col_users.count_documents({})
    except Exception:
        total = 0
    return f"✅ Happ VPN Bot is Alive! | Users: {total}", 200

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

# ╔══════════════════════════════════════════════════════════╗
#                     BAŞLATMA
# ╚══════════════════════════════════════════════════════════╝
def run_bot():
    if get_setting("tgrass", "on") == "on":
        count, msg = _tgrass_fetch_all_sync()
        print(f"[TGrass] Başlangıç: {count} kanal — {msg}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("admin",  admin_menu))
    app.add_handler(CommandHandler("ban",    cmd_ban))
    app.add_handler(CommandHandler("unban",  cmd_unban))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    print("🤖 Happ VPN Bot başlady (MongoDB + Flask + TGrass aktif).")
    app.run_polling(drop_pending_updates=True)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=self_ping, daemon=True).start()
    threading.Thread(target=run_bot,   daemon=True).start()
    run_flask()