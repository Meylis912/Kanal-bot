#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Düzedişli — Happ VPN bot (admin menýu, logs, SQLite + Flask Web Server).
Python 3.11+ bilen işleýändir. (python-telegram-bot v20+)
"""

import asyncio
import sqlite3
import json
import os
import time
import threading
from datetime import datetime
from io import BytesIO

import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ========= DUZGUNLEŞDIRME =========
BOT_TOKEN = "8678757671:AAEAnXgBv-of7BuAdKDG1eV7RD96kp3YZSw"
ADMIN_ID = 7523674506
API_URL = "https://crypto.happ.su/api.php"
DB_PATH = "happvpn_bot.db"
RENDER_URL = "https://kanal-bot-my5r.onrender.com"
# ===================================

# -------------------- FLASK WEB SERWER --------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is Alive!", 200

def self_ping():
    """Render'yň öçmezligi üçin her 5 minutdan (300 sekunt) özüne zapros iberýär."""
    while True:
        try:
            requests.get(RENDER_URL, timeout=10)
        except Exception:
            pass
        time.sleep(300)

# -------------------- SYNCHRONOUS DB HELPERS --------------------
def _db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_banned INTEGER DEFAULT 0,
            last_seen TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            input_url TEXT,
            encrypted_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def _db_upsert_user(user_id: int, username: str):
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users(user_id, username, last_seen) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            last_seen=excluded.last_seen
    """, (user_id, username, now))
    conn.commit()
    conn.close()


def _db_insert_log(user_id: int, username: str, input_url: str, encrypted_url: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO logs(user_id, username, input_url, encrypted_url, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, username, input_url, encrypted_url, datetime.utcnow().isoformat(sep=" ", timespec="seconds")))
    conn.commit()
    conn.close()


def _db_get_last_logs(limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, input_url, encrypted_url, created_at FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def _db_get_users(limit: int = 500):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, is_banned, last_seen FROM users ORDER BY last_seen DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def _db_ban_user(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def _db_unban_user(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def _db_get_counts():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE is_banned=0")
    active = cur.fetchone()[0]
    conn.close()
    return total, active


def _db_clear_logs():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM logs")
    conn.commit()
    conn.close()


# -------------------- ASYNC WRAPPERS --------------------
async def db_init():
    await asyncio.to_thread(_db_init)


async def db_upsert_user(user_id: int, username: str):
    await asyncio.to_thread(_db_upsert_user, user_id, username)


async def db_insert_log(user_id: int, username: str, input_url: str, encrypted_url: str):
    await asyncio.to_thread(_db_insert_log, user_id, username, input_url, encrypted_url)


async def db_get_last_logs(limit: int = 10):
    return await asyncio.to_thread(_db_get_last_logs, limit)


async def db_get_users(limit: int = 500):
    return await asyncio.to_thread(_db_get_users, limit)


async def db_ban_user(user_id: int):
    return await asyncio.to_thread(_db_ban_user, user_id)


async def db_unban_user(user_id: int):
    return await asyncio.to_thread(_db_unban_user, user_id)


async def db_get_counts():
    return await asyncio.to_thread(_db_get_counts)


async def db_clear_logs():
    return await asyncio.to_thread(_db_clear_logs)


# -------------------- HAPP API --------------------
def _encrypt_via_happ_api_sync(plain_url: str):
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(API_URL, headers=headers, json={"url": plain_url}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


async def encrypt_via_happ_api(plain_url: str):
    return await asyncio.to_thread(_encrypt_via_happ_api_sync, plain_url)


# -------------------- BOT HANDLERS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    await db_upsert_user(user.id, username)
    await update.message.reply_text(
        "👋 Salam!\nMen Happ VPN URL-lerini şifrleýän bot. 😊\n"
        "Her hili protokol üçin URL iberiň (http, vless, ss, trojan...).\n"
        "Men şifrlenen netijeni yzyna bererin."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("📭 Habar boş — URL iberiň.")
        return

    username = user.username or ""
    await db_upsert_user(user.id, username)

    def _get_banned(u_id):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT is_banned FROM users WHERE user_id = ?", (u_id,))
        r = cur.fetchone()
        conn.close()
        return r[0] if r else 0

    banned = await asyncio.to_thread(_get_banned, user.id)
    if banned == 1:
        await update.message.reply_text("🚫 Siz ban edilensiňiz. Administratore ýüz tutuň.")
        return

    waiting = await update.message.reply_text("🔒 Şifrlenýär, garaşyň...")

    result = await encrypt_via_happ_api(text)

    if isinstance(result, dict) and "encrypted_link" in result:
        enc = result["encrypted_link"]
        await db_insert_log(user.id, username, text, enc)
        await waiting.edit_text(f"Netije✅:\n```{enc}```", parse_mode="Markdown")
    else:
        try:
            txt = json.dumps(result, ensure_ascii=False, indent=2)
        except Exception:
            txt = str(result)
        await waiting.edit_text(f"❌ Säwlik ýa-da näbelli jogap:\n```{txt}```", parse_mode="Markdown")


def _admin_keyboard():
    kb = [
        [InlineKeyboardButton("👥 Ulanyjylar", callback_data="admin_users")],
        [InlineKeyboardButton("📜 Ýazgylary Gör", callback_data="admin_logs")],
        [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats"),
         InlineKeyboardButton("🧹 Ýazgylary Arassala", callback_data="admin_clear_logs")],
    ]
    return InlineKeyboardMarkup(kb)


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("🚫 Bu bölüm diňe administratore açyk.")
        return
    await update.message.reply_text("🛠 Admin menýu:", reply_markup=_admin_keyboard())


async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if uid != ADMIN_ID:
        await query.edit_message_text("🚫 Siz admin däl.")
        return

    data = query.data

    if data == "admin_users":
        users = await db_get_users(limit=500)
        if not users:
            await query.edit_message_text("📭 Veritabanynda ulanyjy ýok.")
            return
        lines = []
        for u in users:
            user_id, username, is_banned, last_seen = u
            uname = f"@{username}" if username else ""
            ban_text = "🔴 BAN" if is_banned == 1 else "🟢 OK"
            last_seen = last_seen or "-"
            lines.append(f"{user_id} {uname} — {ban_text} — {last_seen}")
        text = "👥 Ulanyjylar:\n\n" + "\n".join(lines)
        
        if len(text) > 3500:
            bio = BytesIO(text.encode("utf-8"))
            bio.name = "users.txt"
            await query.message.reply_document(bio)
            await query.edit_message_text("📥 Ulanyjy sanawy faýl hökmünde iberildi.")
        else:
            await query.edit_message_text(text)

    elif data == "admin_logs":
        logs = await db_get_last_logs(limit=50)
        if not logs:
            await query.edit_message_text("📭 Häzirlikçe ýazgy ýok.")
            return
        chunks = []
        for row in logs:
            id_, user_id, username, input_url, encrypted_url, created_at = row
            uname = f"@{username}" if username else ""
            chunks.append(f"#{id_} {created_at}\n{user_id} {uname}\n➡ {input_url}\n🔒 `{encrypted_url}`\n")
        text = "\n".join(chunks)
        if len(text) > 3500:
            bio = BytesIO(text.encode("utf-8"))
            bio.name = "logs.txt"
            await query.message.reply_document(bio)
            await query.edit_message_text("📥 Yazgylar faýl hökmünde iberildi.")
        else:
            await query.edit_message_text("📜 Iň soňky ýazgylar:\n\n" + text)

    elif data == "admin_stats":
        total, active = await db_get_counts()
        await query.edit_message_text(f"📊 Statistika:\nJemi ulanyjy: {total}\nAktiw (ban edilmedik): {active}")

    elif data == "admin_clear_logs":
        await db_clear_logs()
        await query.edit_message_text("🧹 ähli ýazgylar pozuldy.")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("🚫 Bu komanda diňe administratore açyk.")
        return
    if not context.args:
        await update.message.reply_text("Ulanyjy ID giriziň. Meselem: /ban 123456789")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Nädogry ID.")
        return
    ok = await db_ban_user(target)
    if ok:
        await update.message.reply_text(f"🚫 {target} ban edildi.")
    else:
        await update.message.reply_text("Ulanyjy tabylmady ýa-da öňden banly bolup biler.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("🚫 Bu komanda diňe administratore açyk.")
        return
    if not context.args:
        await update.message.reply_text("Ulanyjy ID giriziň. Meselem: /unban 123456789")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Nädogry ID.")
        return
    ok = await db_unban_user(target)
    if ok:
        await update.message.reply_text(f"✅ {target} banyndan çykaryldy.")
    else:
        await update.message.reply_text("Ulanyjy tabylmady ýa-da öňden banyndan aýrylyp bilner.")

# -------------------- BOT RUNNER FOR THREAD --------------------
def run_bot():
    """Boty aýratyn thread içinde işletmek üçin asynkron däl ýörite funksiýa."""
    _db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CallbackQueryHandler(callback_query))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))

    print("🤖 Happ VPN bot işe başlady.")
    app.run_polling()

# -------------------- MAIN --------------------
if __name__ == "__main__":
    # Self-ping mehanizmini arka planda (daemon) işletýäris
    threading.Thread(target=self_ping, daemon=True).start()
    
    # Telegram boty aýratyn thread-de işletýäris
    threading.Thread(target=run_bot, daemon=True).start()
    
    # Esasy thread-de Flask web serwerini açýarys (Render-iň PORT-uny awtomatik alýar)
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)
