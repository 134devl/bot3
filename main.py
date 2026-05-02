import asyncio
import io
import logging
import os
import re
import sys
import traceback
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
    ReplyKeyboardRemove,
    ErrorEvent
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN не найден! Бот не может быть запущен.")
    sys.exit(1)

admin_ids_str = os.getenv("ADMIN_IDS", "")
try:
    ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]
except Exception:
    ADMIN_IDS = []
    logger.warning("ADMIN_IDS не настроены или имеют неверный формат.")

TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", 0))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
WELCOME_BG = os.getenv("WELCOME_BG", "welcome_bg.jpg")
NIGHT_START = int(os.getenv("NIGHT_START", 0))
MORNING_START = int(os.getenv("MORNING_START", 8))

WEBHOOK_URL = os.getenv("WEBHOOK_URL")     
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")   
WEB_SERVER_HOST = "0.0.0.0" 
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))

TEXT_PC = (
    "💻 <b>Касательно версии для ПК:</b>\n\n"
    "Разработка десктопной версии требует много времени и ресурсов, "
    "поэтому точную дату релиза назвать пока невозможно."
)

TEXT_IOS = (
    "🍏 <b>Касательно версии для iOS:</b>\n\n"
    "Разработчиков всего двое. Мы занимаемся этим по мере возможностей, "
    "но точных сроков выхода приложения на данный момент нет."
)

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


async def safe_send_message(chat_id: int, text: str, **kwargs):
    """Безопасная отправка сообщения, чтобы не крашить при блокировке бота пользователем"""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения {chat_id}: {e}")

async def get_avatar_bytes(user_id: int) -> bytes | None:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos: return None
        file = await bot.get_file(photos.photos[0][-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"Не удалось получить аватар {user_id}: {e}")
        return None

def _process_image_sync(avatar_bytes: bytes | None) -> io.BytesIO | None:
    """Синхронная обработка Pillow. Вызывается в треде."""
    if not os.path.exists(WELCOME_BG):
        logger.warning(f"Фон {WELCOME_BG} не найден!")
        return None
    try:
        bg = Image.open(WELCOME_BG).convert("RGBA")
        if avatar_bytes:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            av = av.resize((500, 500)) # Resize
            
            mask = Image.new("L", (500, 500), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 499, 499), fill=255)
            
            pos = (1250, 397)
            bg.paste(av, pos, mask)
        
        out = io.BytesIO()
        bg.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
        out.seek(0)
        return out
    except Exception as e:
        logger.error(f"CRITICAL: Ошибка при обработке изображения: {e}", exc_info=True)
        return None

async def build_welcome_image(avatar_bytes: bytes | None) -> io.BytesIO | None:
    """Обертка для запуска тяжелой задачи в потоке"""
    return await asyncio.to_thread(_process_image_sync, avatar_bytes)


async def task_send_welcome(chat_id: int, user_first_name: str, user_id: int, is_private: bool):
    """
    Фоновая задача отправки приветствия.
    Она выполняется параллельно ответу вебхука, поэтому не задерживает сервер.
    """
    try:
        avatar = await get_avatar_bytes(user_id)
        img_io = await build_welcome_image(avatar)
        
        if is_private:
            caption = (
                f"👋 <b>Привет, {user_first_name}!</b>\n\n"
                f"Добро пожаловать в бота поддержки LANE.\n"
                f"Выберите тип обращения:"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="type_bug")],
                [InlineKeyboardButton(text="💡 Предложить идею", callback_data="type_feature")],
                [InlineKeyboardButton(text="🎵 Проблема с треком", callback_data="type_track")]
            ])
            
            if img_io:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=BufferedInputFile(img_io.read(), filename="welcome.jpg"),
                    caption=caption,
                    reply_markup=kb
                )
            else:
                await bot.send_message(chat_id=chat_id, text=caption, reply_markup=kb)
        
        else:
            caption = f"👋 <b>Привет, {user_first_name}!</b>\n\nДобро пожаловать в комьюнити LANE. Мы строим будущее музыки."
            if img_io:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=BufferedInputFile(img_io.read(), filename="welcome.jpg"),
                    caption=caption
                )
            else:
                await bot.send_message(chat_id=chat_id, text=caption)
                
    except Exception as e:
        logger.error(f"Ошибка в фоновой задаче task_send_welcome: {e}", exc_info=True)


@dp.error()
async def global_error_handler(event: ErrorEvent):
    """
    Перехватывает ЛЮБУЮ необработанную ошибку в хендлерах.
    Предотвращает падение бота.
    """
    logger.critical("🚨 Unhandled exception detected:", exc_info=event.exception)
    
    error_msg = f"⚠️ <b>В боте произошла ошибка:</b>\n<code>{str(event.exception)[:100]}</code>"
    for admin in ADMIN_IDS:
        await safe_send_message(admin, error_msg)


@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start_private(message: Message):
    asyncio.create_task(
        task_send_welcome(message.chat.id, message.from_user.first_name, message.from_user.id, True)
    )

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_user_joined(event: ChatMemberUpdated):
    if event.chat.id != TARGET_GROUP_ID: return
    asyncio.create_task(
        task_send_welcome(event.chat.id, event.new_chat_member.user.first_name, event.new_chat_member.user.id, False)
    )

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

async def send_report_to_admins(report_text: str, message: Message):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=report_text)
            if message.content_type in ['photo', 'video', 'document', 'audio']:
                 await message.send_copy(chat_id=admin_id)
        except Exception as e:
            logger.error(f"Failed to send to admin {admin_id}: {e}")

@dp.message(BugState.waiting_for_media)
async def bug_finish(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        user = message.from_user
        text = (
            f"🚨 <b>БАГ-РЕПОРТ</b>\n"
            f"👤 От: {user.mention_html()} (ID: <code>{user.id}</code>)\n"
            f"📱 Device: {data.get('device', 'N/A')} | Ver: {data.get('version', 'N/A')}\n\n"
            f"👣 <b>Шаги:</b>\n{data.get('steps', 'N/A')}\n\n"
            f"✅ <b>Ожидание:</b>\n{data.get('expected', 'N/A')}\n\n"
            f"❌ <b>Факт:</b>\n{data.get('actual', 'N/A')}"
        )
        await send_report_to_admins(text, message)
        await message.answer("✅ Баг-репорт отправлен!", reply_markup=ReplyKeyboardRemove())
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка при отправке бага: {e}")
        await message.answer("Произошла ошибка при отправке, но мы это записали.")
        await state.clear()

# --- Фичи ---
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
        f"💬 <b>Суть предложения:</b>\n{data.get('desc', 'N/A')}"
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
        f"🎼 <b>Трек:</b> {data.get('track', 'N/A')}\n"
        f"⚠️ <b>Проблема:</b> {data.get('issue', 'N/A')}"
    )
    await send_report_to_admins(text, message)
    await message.answer("✅ Жалоба на контент отправлена!", reply_markup=ReplyKeyboardRemove())
    await state.clear()


PC_REGEX = re.compile(r"\b(пк|pc)\b", re.IGNORECASE)
IOS_REGEX = re.compile(r"\b(ios|айос)\b", re.IGNORECASE)

@dp.message(F.text, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_message_handler(message: Message):
    if message.text.startswith("/"): return
    text_lower = message.text.lower()
    
    if PC_REGEX.search(text_lower): 
        await message.reply(TEXT_PC)
    elif IOS_REGEX.search(text_lower): 
        await message.reply(TEXT_IOS)
    
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        user_name = str(message.from_user.first_name).replace("<", "&lt;").replace(">", "&gt;")
        await message.reply(f"{user_name}, я всего лишь бот. Пожалуйста, дождитесь администратора.")


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
    except Exception as e: logger.error(f"Night mode error: {e}")

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
    except Exception as e: logger.error(f"Day mode error: {e}")

async def check_mode_on_startup():
    if TARGET_GROUP_ID == 0: return
    try:
        now_hour = datetime.now(tz).hour
        if NIGHT_START <= now_hour < MORNING_START:
            permissions = ChatPermissions(
                can_send_messages=True, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False
            )
            await bot.set_chat_permissions(TARGET_GROUP_ID, permissions)
        else:
            permissions = ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True
            )
            await bot.set_chat_permissions(TARGET_GROUP_ID, permissions)
    except Exception as e:
        logger.warning(f"Ошибка проверки режима при старте: {e}")

async def on_startup(bot: Bot):
    try:
        # УДАЛЯЕМ WEBHOOK С ФЛАГОМ drop_pending_updates=True, ЧТОБЫ СБРОСИТЬ ОЧЕРЕДЬ
        await bot.delete_webhook(drop_pending_updates=True)
        
        await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}")
        logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}{WEBHOOK_PATH}")
    except Exception as e:
        logger.error(f"❌ Ошибка установки вебхука: {e}")
        
    try:
        scheduler.add_job(set_night_mode, 'cron', hour=NIGHT_START, minute=0)
        scheduler.add_job(set_day_mode, 'cron', hour=MORNING_START, minute=0)
        scheduler.start()
        logger.info("🕒 Планировщик запущен")
    except Exception as e:
        logger.error(f"❌ Ошибка запуска планировщика: {e}")
        
    await check_mode_on_startup()

async def on_shutdown(bot: Bot):
    if scheduler.running:
        scheduler.shutdown()
    logger.info("🛑 Бот остановлен.")

async def health_check(request):
    return web.Response(text="Bot is running OK", status=200)

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get('/', health_check)

    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)
    
    try:
        web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)
    except Exception as e:
        logger.critical(f"FATAL: Web server crashed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
