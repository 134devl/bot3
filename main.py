import asyncio
import io
import logging
import os
import re
import sys
from datetime import datetime

import pytz
from aiohttp import web
from dotenv import load_dotenv
from PIL import Image, ImageDraw

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import ChatMemberUpdatedFilter, MEMBER, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, 
    ChatPermissions, 
    ChatMemberUpdated, 
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id_str) for id_str in os.getenv("ADMIN_IDS", "").split(",") if id_str]
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
WELCOME_BG = os.getenv("WELCOME_BG", "welcome_bg.jpg")
NIGHT_START = int(os.getenv("NIGHT_START", 0))
MORNING_START = int(os.getenv("MORNING_START", 8))

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")
WEB_SERVER_HOST = os.getenv("WEB_SERVER_HOST", "0.0.0.0")
WEB_SERVER_PORT = int(os.getenv("WEB_SERVER_PORT", 8080))

TEXT_PC = (
    "💻 <b>Касательно версии для ПК:</b>\n\n"
    "Над проектом работают всего два студента. "
    "Разработка десктопной версии требует много времени и ресурсов, "
    "поэтому точную дату релиза назвать пока невозможно."
)

TEXT_IOS = (
    "🍏 <b>Касательно версии для iOS:</b>\n\n"
    "Разработчиков всего двое. Мы занимаемся этим по мере возможностей, "
    "но точных сроков выхода приложения на данный момент нет."
)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
tz = pytz.timezone(TIMEZONE)
scheduler = AsyncIOScheduler(timezone=tz)

class BugState(StatesGroup):
    waiting_for_version = State()
    waiting_for_device = State()
    waiting_for_steps = State()
    waiting_for_expected = State()
    waiting_for_actual = State()
    waiting_for_media = State()

class FeatureState(StatesGroup):
    waiting_for_description = State()
    waiting_for_media = State()

class TrackState(StatesGroup):
    waiting_for_name = State()
    waiting_for_issue = State()
    waiting_for_media = State()

async def get_avatar_bytes(user_id: int) -> bytes | None:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos: return None
        file = await bot.get_file(photos.photos[0][-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        return buf.getvalue()
    except Exception: return None

async def build_welcome_image(avatar_bytes: bytes | None) -> io.BytesIO | None:
    if not os.path.exists(WELCOME_BG): return None
    try:
        bg = Image.open(WELCOME_BG).convert("RGBA")
        if avatar_bytes:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((500, 500))
            mask = Image.new("L", (500, 500), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 499, 499), fill=255)
            pos = (1250, 397)
            bg.paste(av, pos, mask)
        out = io.BytesIO()
        bg.convert("RGB").save(out, format="JPEG", quality=92)
        out.seek(0)
        return out
    except Exception as e:
        logging.error(f"Error building image: {e}")
        return None

async def send_report_to_admins(report_text: str, message: Message):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=report_text)
            if message.content_type in ['photo', 'video', 'document', 'audio']:
                 await message.send_copy(chat_id=admin_id)
        except Exception as e:
            logging.error(f"Failed to send to admin {admin_id}: {e}")

async def set_night_mode():
    try:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_audios=False, can_send_documents=False,
            can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
            can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
            can_add_web_page_previews=False
        )
        await bot.set_chat_permissions(TARGET_GROUP_ID, permissions)
        await bot.send_message(TARGET_GROUP_ID, "🌙 <b>Ночной режим включен.</b>\nМедиафайлы отключены до утра.")
    except Exception as e: logging.error(f"Night mode error: {e}")

async def set_day_mode():
    try:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await bot.set_chat_permissions(TARGET_GROUP_ID, permissions)
        await bot.send_message(TARGET_GROUP_ID, "☀️ <b>Доброе утро!</b>\nЧат открыт.")
    except Exception as e: logging.error(f"Day mode error: {e}")

async def check_mode_on_startup():
    now_hour = datetime.now(tz).hour
    if NIGHT_START <= now_hour < MORNING_START:
        # Мы не отправляем сообщение при старте бота, просто ставим права, чтобы не спамить при рестартах
        permissions = ChatPermissions(
            can_send_messages=True, can_send_audios=False, can_send_documents=False,
            can_send_photos=False, can_send_videos=False
        )
        try:
            await bot.set_chat_permissions(TARGET_GROUP_ID, permissions)
        except Exception: pass
    else:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True
        )
        try:
            await bot.set_chat_permissions(TARGET_GROUP_ID, permissions)
        except Exception: pass

@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start_private(message: Message):
    user = message.from_user
    avatar = await get_avatar_bytes(user.id)
    img_io = await build_welcome_image(avatar)
    
    caption = (
        f"👋 <b>Привет, {user.first_name}!</b>\n\n"
        f"Добро пожаловать в бота поддержки LANE.\n"
        f"Выберите тип обращения:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="type_bug")],
        [InlineKeyboardButton(text="💡 Предложить идею", callback_data="type_feature")],
        [InlineKeyboardButton(text="🎵 Проблема с треком", callback_data="type_track")]
    ])

    if img_io:
        await message.answer_photo(
            photo=BufferedInputFile(img_io.read(), filename="welcome.jpg"),
            caption=caption, reply_markup=kb
        )
    else:
        await message.answer(text=caption, reply_markup=kb)

@dp.callback_query(F.data == "type_bug")
async def cb_bug_start(call: CallbackQuery, state: FSMContext):
    await call.message.answer("🐛 <b>Новый баг-репорт</b>\n1️⃣ Укажите версию приложения (билд):")
    await state.set_state(BugState.waiting_for_version)
    await call.answer()

@dp.message(BugState.waiting_for_version)
async def bug_version(message: Message, state: FSMContext):
    await state.update_data(version=message.text)
    await message.answer("2️⃣ Модель устройства и версия ОС:")
    await state.set_state(BugState.waiting_for_device)

@dp.message(BugState.waiting_for_device)
async def bug_device(message: Message, state: FSMContext):
    await state.update_data(device=message.text)
    await message.answer("3️⃣ Шаги воспроизведения:")
    await state.set_state(BugState.waiting_for_steps)

@dp.message(BugState.waiting_for_steps)
async def bug_steps(message: Message, state: FSMContext):
    await state.update_data(steps=message.text)
    await message.answer("4️⃣ Ожидаемый результат:")
    await state.set_state(BugState.waiting_for_expected)

@dp.message(BugState.waiting_for_expected)
async def bug_expected(message: Message, state: FSMContext):
    await state.update_data(expected=message.text)
    await message.answer("5️⃣ Фактический результат:")
    await state.set_state(BugState.waiting_for_actual)

@dp.message(BugState.waiting_for_actual)
async def bug_actual(message: Message, state: FSMContext):
    await state.update_data(actual=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить")]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("6️⃣ Прикрепите скриншот/видео (или нажмите 'Пропустить'):", reply_markup=kb)
    await state.set_state(BugState.waiting_for_media)

@dp.message(BugState.waiting_for_media)
async def bug_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    
    text = (
        f"🚨 <b>БАГ-РЕПОРТ</b>\n"
        f"👤 От: {user.mention_html()} (ID: <code>{user.id}</code>)\n"
        f"📱 Device: {data['device']} | Ver: {data['version']}\n\n"
        f"👣 <b>Шаги:</b>\n{data['steps']}\n\n"
        f"✅ <b>Ожидание:</b>\n{data['expected']}\n\n"
        f"❌ <b>Факт:</b>\n{data['actual']}"
    )
    
    await send_report_to_admins(text, message)
    await message.answer("✅ Баг-репорт отправлен!", reply_markup=ReplyKeyboardRemove())
    await state.clear()

@dp.callback_query(F.data == "type_feature")
async def cb_feature_start(call: CallbackQuery, state: FSMContext):
    await call.message.answer("💡 <b>Предложение идеи</b>\nОпишите ваше предложение максимально подробно:")
    await state.set_state(FeatureState.waiting_for_description)
    await call.answer()

@dp.message(FeatureState.waiting_for_description)
async def feature_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить")]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Прикрепите референс (скриншот/пример) или нажмите 'Пропустить':", reply_markup=kb)
    await state.set_state(FeatureState.waiting_for_media)

@dp.message(FeatureState.waiting_for_media)
async def feature_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    
    text = (
        f"💡 <b>НОВАЯ ИДЕЯ</b>\n"
        f"👤 От: {user.mention_html()} (ID: <code>{user.id}</code>)\n\n"
        f"💬 <b>Суть предложения:</b>\n{data['desc']}"
    )
    
    await send_report_to_admins(text, message)
    await message.answer("✅ Ваше предложение отправлено!", reply_markup=ReplyKeyboardRemove())
    await state.clear()

@dp.callback_query(F.data == "type_track")
async def cb_track_start(call: CallbackQuery, state: FSMContext):
    await call.message.answer("🎵 <b>Проблема с треком</b>\nПришлите название трека и исполнителя (или ссылку):")
    await state.set_state(TrackState.waiting_for_name)
    await call.answer()

@dp.message(TrackState.waiting_for_name)
async def track_name(message: Message, state: FSMContext):
    await state.update_data(track=message.text)
    await message.answer("Опишите проблему (не играет / цензура / неверная обложка и т.д.):")
    await state.set_state(TrackState.waiting_for_issue)

@dp.message(TrackState.waiting_for_issue)
async def track_issue(message: Message, state: FSMContext):
    await state.update_data(issue=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить")]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Есть скриншот ошибки? Прикрепите или нажмите 'Пропустить':", reply_markup=kb)
    await state.set_state(TrackState.waiting_for_media)

@dp.message(TrackState.waiting_for_media)
async def track_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    
    text = (
        f"🎵 <b>ПРОБЛЕМА С ТРЕКОМ</b>\n"
        f"👤 От: {user.mention_html()} (ID: <code>{user.id}</code>)\n\n"
        f"🎼 <b>Трек:</b> {data['track']}\n"
        f"⚠️ <b>Проблема:</b> {data['issue']}"
    )
    
    await send_report_to_admins(text, message)
    await message.answer("✅ Жалоба на контент отправлена!", reply_markup=ReplyKeyboardRemove())
    await state.clear()

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_user_joined(event: ChatMemberUpdated):
    if event.chat.id != TARGET_GROUP_ID: return
    user = event.new_chat_member.user
    avatar = await get_avatar_bytes(user.id)
    img_io = await build_welcome_image(avatar)
    caption = f"👋 <b>Привет, {user.first_name}!</b>\n\nДобро пожаловать в комьюнити LANE. Мы строим будущее музыки."
    
    if img_io:
        await bot.send_photo(chat_id=event.chat.id, photo=BufferedInputFile(img_io.read(), filename="welcome.jpg"), caption=caption)
    else:
        await bot.send_message(chat_id=event.chat.id, text=caption)

PC_REGEX = re.compile(r"\b(пк|pc|комп|windows|десктоп)\b", re.IGNORECASE)
IOS_REGEX = re.compile(r"\b(ios|айос|иос|iphone|айфон)\b", re.IGNORECASE)

@dp.message(F.text, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_message_handler(message: Message):
    if message.text.startswith("/"): return
    text_lower = message.text.lower()
    if PC_REGEX.search(text_lower): await message.reply(TEXT_PC)
    elif IOS_REGEX.search(text_lower): await message.reply(TEXT_IOS)
    
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        user_name = str(message.from_user.first_name).replace("<", "&lt;").replace(">", "&gt;")
        await message.reply(f"{user_name}, я всего лишь бот. Пожалуйста, дождитесь администратора.")

async def on_startup(bot: Bot):
    await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}")
    scheduler.add_job(set_night_mode, 'cron', hour=NIGHT_START, minute=0)
    scheduler.add_job(set_day_mode, 'cron', hour=MORNING_START, minute=0)
    scheduler.start()
    await check_mode_on_startup()
    logging.info(f"Webhook set to {WEBHOOK_URL}{WEBHOOK_PATH}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    scheduler.shutdown()
    logging.info("Webhook deleted")

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    main()
