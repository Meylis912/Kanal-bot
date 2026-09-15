import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("8897093028:AAFzSX6fMSI5N4nYb3Rbzo69FuMboTxVupk")
MONGO_URI = os.getenv("mongodb+srv://mergenowlyagulyyew41_db_user:ZvZhOKOAF6ZMRbHX@cluster1.l8z8gll.mongodb.net/?appName=Cluster1")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "7523674506"))
INACTIVE_DAYS = 6

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["channel_manager"]
admins_col = db["admins"]
channels_col = db["channels"]
activity_col = db["activity"]
settings_col = db["settings"]

scheduler = AsyncIOScheduler()
cleanup_control = {"stop": False}


class States(StatesGroup):
    add_channel_id = State()
    add_channel_link = State()
    add_admin = State()
    remove_admin = State()
    activity_count = State()


# ---------- helpers ----------

async def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    return bool(await admins_col.find_one({"user_id": user_id}))


def main_menu_kb(auto_on: bool = False):
    kb = [
        [InlineKeyboardButton(text="➕ Kanal Ekle", callback_data="ch_add"),
         InlineKeyboardButton(text="➖ Kanal Sil", callback_data="ch_del")],
        [InlineKeyboardButton(text="🧹 Aktivite Onar", callback_data="act_repair"),
         InlineKeyboardButton(text=f"🔁 Auto Onar: {'AÇIK' if auto_on else 'KAPALI'}", callback_data="auto_toggle")],
        [InlineKeyboardButton(text="⛔️ Onarma Stop", callback_data="repair_stop")],
        [InlineKeyboardButton(text="👤 Admin Ekle", callback_data="adm_add"),
         InlineKeyboardButton(text="🚫 Admin Sil", callback_data="adm_del")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def admin_only(obj) -> bool:
    user_id = obj.from_user.id
    if not await is_admin(user_id):
        text = "⛔️ Bu botu kullanma yetkiniz yok."
        if isinstance(obj, Message):
            await obj.answer(text)
        else:
            await obj.answer(text, show_alert=True)
        return False
    return True


# ---------- /start ----------

@router.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    if not await admin_only(message):
        return
    setting = await settings_col.find_one({"_id": "global"})
    auto_on = bool(setting and setting.get("auto_onar"))
    await message.answer("Kanal Yönetim Botu\n\nİşlem seçin:", reply_markup=main_menu_kb(auto_on))


# ---------- Kanal Ekle ----------

@router.callback_query(F.data == "ch_add")
async def ch_add_start(cb: CallbackQuery, state: FSMContext):
    if not await admin_only(cb):
        return
    await state.set_state(States.add_channel_id)
    await cb.message.answer("Kanal ID'sini gönderin.\n(Örn: -1001234567890)")
    await cb.answer()


@router.message(States.add_channel_id)
async def ch_add_id(message: Message, state: FSMContext):
    try:
        channel_id = int(message.text.strip())
    except ValueError:
        await message.answer("Geçersiz ID. Sayısal bir kanal ID'si gönderin (örn: -1001234567890).")
        return
    await state.update_data(channel_id=channel_id)
    await state.set_state(States.add_channel_link)
    await message.answer("Şimdi kanal linkini gönderin.\n(Örn: https://t.me/kanaladi)")


@router.message(States.add_channel_link)
async def ch_add_link(message: Message, state: FSMContext):
    link = message.text.strip()
    data = await state.get_data()
    channel_id = data["channel_id"]

    try:
        me = await bot.get_chat_member(channel_id, bot.id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await message.answer(
            f"❌ Hata: Bota bu kanala erişemiyorum.\n"
            f"Botu önce kanala admin olarak ekleyin, sonra tekrar deneyin.\n\nDetay: {e}"
        )
        await state.clear()
        return

    if me.status not in ("administrator", "creator"):
        await message.answer("❌ Hata: Bot bu kanalda admin değil! Botu kanala admin olarak ekleyin ve tekrar deneyin.")
        await state.clear()
        return

    try:
        chat = await bot.get_chat(channel_id)
        title = chat.title or link
    except Exception:
        title = link

    await channels_col.update_one(
        {"channel_id": channel_id},
        {"$set": {"channel_id": channel_id, "link": link, "title": title}},
        upsert=True,
    )
    await state.clear()
    await message.answer(f"✅ Kanal eklendi: {title}\n{link}")


# ---------- Kanal Sil ----------

@router.callback_query(F.data == "ch_del")
async def ch_del_list(cb: CallbackQuery):
    if not await admin_only(cb):
        return
    channels = await channels_col.find().to_list(length=100)
    if not channels:
        await cb.message.answer("Kayıtlı kanal yok.")
        await cb.answer()
        return
    kb = [
        [InlineKeyboardButton(text=f"🗑 {c.get('title', c['channel_id'])}", callback_data=f"ch_del_confirm:{c['channel_id']}")]
        for c in channels
    ]
    await cb.message.answer("Silinecek kanalı seçin:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data.startswith("ch_del_confirm:"))
async def ch_del_confirm(cb: CallbackQuery):
    if not await admin_only(cb):
        return
    channel_id = int(cb.data.split(":")[1])
    await channels_col.delete_one({"channel_id": channel_id})
    await activity_col.delete_many({"chat_id": channel_id})
    await cb.message.answer("✅ Kanal silindi.")
    await cb.answer()


# ---------- Aktivite takibi ----------
# Not: Telegram Bot API, sessiz (yayın tipi) kanallarda üyelerin "son görülme"
# bilgisini vermez. Bu yüzden aktivite, üyenin gruba/kanala mesaj atmasına göre
# ölçülür (discussion group bağlıysa oradan). Hiç mesaj atmamış üyeler katılma
# tarihinden itibaren "aktif değil" sayılır.

@router.message(F.chat.type.in_({"group", "supergroup"}))
async def track_activity(message: Message):
    if not message.from_user:
        return
    await activity_col.update_one(
        {"chat_id": message.chat.id, "user_id": message.from_user.id},
        {"$set": {"last_seen": datetime.utcnow(), "username": message.from_user.username}},
        upsert=True,
    )


@router.chat_member()
async def track_join(update: ChatMemberUpdated):
    if update.new_chat_member.status in ("member", "administrator", "restricted"):
        await activity_col.update_one(
            {"chat_id": update.chat.id, "user_id": update.new_chat_member.user.id},
            {"$setOnInsert": {"last_seen": update.date, "username": update.new_chat_member.user.username}},
            upsert=True,
        )
    elif update.new_chat_member.status in ("left", "kicked"):
        await activity_col.delete_one({"chat_id": update.chat.id, "user_id": update.new_chat_member.user.id})


# ---------- Aktivite Onar (manuel) ----------

async def get_inactive_users(chat_id: int):
    threshold = datetime.utcnow() - timedelta(days=INACTIVE_DAYS)
    cursor = activity_col.find({"chat_id": chat_id, "last_seen": {"$lt": threshold}})
    return [u async for u in cursor]


@router.callback_query(F.data == "act_repair")
async def act_repair_pick_channel(cb: CallbackQuery):
    if not await admin_only(cb):
        return
    channels = await channels_col.find().to_list(length=100)
    if not channels:
        await cb.message.answer("Kayıtlı kanal yok.")
        await cb.answer()
        return
    kb = [
        [InlineKeyboardButton(text=c.get("title", str(c["channel_id"])), callback_data=f"act_pick:{c['channel_id']}")]
        for c in channels
    ]
    await cb.message.answer("Aktivite onarımı için kanal seçin:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data.startswith("act_pick:"))
async def act_repair_show_count(cb: CallbackQuery, state: FSMContext):
    if not await admin_only(cb):
        return
    chat_id = int(cb.data.split(":")[1])
    inactive = await get_inactive_users(chat_id)
    count = len(inactive)
    if count == 0:
        await cb.message.answer(f"{INACTIVE_DAYS} gündür aktif olmayan kullanıcı bulunamadı.")
        await cb.answer()
        return
    await state.update_data(repair_chat_id=chat_id, repair_count=count)
    await state.set_state(States.activity_count)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Hepsini Çıkar ({count})", callback_data="act_all")]
    ])
    await cb.message.answer(
        f"{count} kullanıcı {INACTIVE_DAYS} gündür aktif değil veya telegrama girmedi.\n\n"
        f"Tümünü çıkarmak için butona basın, ya da çıkarılacak sayıyı mesaj olarak yazın (örn: 20).",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "act_all")
async def act_repair_all(cb: CallbackQuery, state: FSMContext):
    if not await admin_only(cb):
        return
    data = await state.get_data()
    await do_cleanup(cb.message, data["repair_chat_id"], data["repair_count"])
    await state.clear()
    await cb.answer()


@router.message(States.activity_count)
async def act_repair_custom_count(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        n = int(message.text.strip())
    except ValueError:
        await message.answer("Lütfen geçerli bir sayı girin.")
        return
    n = min(n, data["repair_count"])
    await do_cleanup(message, data["repair_chat_id"], n)
    await state.clear()


async def do_cleanup(message: Message, chat_id: int, limit: int):
    cleanup_control["stop"] = False
    inactive = await get_inactive_users(chat_id)
    removed = 0
    await message.answer(f"🧹 Temizlik başladı: {limit} kullanıcı çıkarılacak...")
    for u in inactive[:limit]:
        if cleanup_control["stop"]:
            await message.answer(f"⛔️ Durduruldu. {removed} kullanıcı çıkarıldı.")
            return
        try:
            await bot.ban_chat_member(chat_id, u["user_id"])
            await bot.unban_chat_member(chat_id, u["user_id"])
            await activity_col.delete_one({"chat_id": chat_id, "user_id": u["user_id"]})
            removed += 1
        except Exception as e:
            logging.warning(f"Kick failed for {u['user_id']}: {e}")
        await asyncio.sleep(0.3)
    await message.answer(f"✅ Tamamlandı. {removed} kullanıcı kanaldan çıkarıldı.")


# ---------- Onarma Stop ----------

@router.callback_query(F.data == "repair_stop")
async def repair_stop(cb: CallbackQuery):
    if not await admin_only(cb):
        return
    cleanup_control["stop"] = True
    await settings_col.update_one({"_id": "global"}, {"$set": {"auto_onar": False}}, upsert=True)
    if scheduler.get_job("auto_repair"):
        scheduler.remove_job("auto_repair")
    await cb.message.answer("⛔️ Onarma durduruldu (manuel ve otomatik).")
    await cb.answer()


# ---------- Auto Onar ----------

async def auto_repair_job():
    channels = await channels_col.find().to_list(length=100)
    for c in channels:
        inactive = await get_inactive_users(c["channel_id"])
        for u in inactive:
            try:
                await bot.ban_chat_member(c["channel_id"], u["user_id"])
                await bot.unban_chat_member(c["channel_id"], u["user_id"])
                await activity_col.delete_one({"chat_id": c["channel_id"], "user_id": u["user_id"]})
            except Exception as e:
                logging.warning(f"Auto kick failed: {e}")
            await asyncio.sleep(0.3)


@router.callback_query(F.data == "auto_toggle")
async def auto_toggle(cb: CallbackQuery):
    if not await admin_only(cb):
        return
    setting = await settings_col.find_one({"_id": "global"})
    new_state = not bool(setting and setting.get("auto_onar"))
    await settings_col.update_one({"_id": "global"}, {"$set": {"auto_onar": new_state}}, upsert=True)

    if new_state:
        scheduler.add_job(auto_repair_job, "interval", hours=24, id="auto_repair", replace_existing=True)
        await cb.message.answer("🔁 Auto Onar açıldı. Her 24 saatte otomatik temizlik yapılacak.")
    else:
        if scheduler.get_job("auto_repair"):
            scheduler.remove_job("auto_repair")
        await cb.message.answer("Auto Onar kapatıldı.")

    await cb.message.edit_reply_markup(reply_markup=main_menu_kb(new_state))
    await cb.answer()


# ---------- Admin Ekle / Sil ----------

@router.callback_query(F.data == "adm_add")
async def adm_add_start(cb: CallbackQuery, state: FSMContext):
    if not await admin_only(cb):
        return
    await state.set_state(States.add_admin)
    await cb.message.answer("Eklenecek adminin Telegram ID'sini gönderin.")
    await cb.answer()


@router.message(States.add_admin)
async def adm_add_finish(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Geçersiz ID.")
        return
    await admins_col.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
    await state.clear()
    await message.answer(f"✅ {uid} admin olarak eklendi.")


@router.callback_query(F.data == "adm_del")
async def adm_del_start(cb: CallbackQuery, state: FSMContext):
    if not await admin_only(cb):
        return
    await state.set_state(States.remove_admin)
    await cb.message.answer("Silinecek adminin Telegram ID'sini gönderin.")
    await cb.answer()


@router.message(States.remove_admin)
async def adm_del_finish(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Geçersiz ID.")
        return
    await admins_col.delete_one({"user_id": uid})
    await state.clear()
    await message.answer(f"✅ {uid} admin listesinden çıkarıldı.")


# ---------- run ----------

async def main():
    scheduler.start()
    setting = await settings_col.find_one({"_id": "global"})
    if setting and setting.get("auto_onar"):
        scheduler.add_job(auto_repair_job, "interval", hours=24, id="auto_repair", replace_existing=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
