import os
import asyncio
from pathlib import Path
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise ValueError("Критическая ошибка: BOT_TOKEN не найден в .env!")
if not ADMIN_ID_RAW:
    raise ValueError("Критическая ошибка: ADMIN_ID не найден в .env!")

ADMIN_ID = int(ADMIN_ID_RAW)
DB_NAME = str(BASE_DIR / "tt_payouts.db")

DEFAULT_GUIDE_TEXT = (
    "📖 <b>Гайд и регламент по заливу TikTok</b>\n\n"
    "<b>1. Требования к видео:</b>\n"
    "• Формат: 9:16 (вертикально), 1080p, 30/60 fps.\n"
    "• Длина: 7–15 секунд, динамичный монтаж.\n"
    "• Звуки: актуальные тренды TikTok.\n"
    "• Хэштеги: 3–5 штук по тематике.\n\n"
    "<b>2. Тарифы и правила:</b>\n"
    "• Ставка: <b>$1.00</b> за 1 000 просмотров и 100 лайков.\n"
    "• Минимальный вывод: <b>$5.00</b>.\n"
    "• Накрутка запрещена (бан без выплат)."
)

DEFAULT_WM_TEXT = "@your_project_tag (накладывать по центру или в углу)"

# ----------------- БАЗА ДАННЫХ ----------------- #
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                tt_username TEXT,
                age INTEGER,
                accounts_count INTEGER,
                status TEXT DEFAULT 'unregistered',
                balance REAL DEFAULT 0.0,
                referrer_id INTEGER DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS video_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                video_url TEXT,
                views_photo_id TEXT,
                likes_photo_id TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                method TEXT,
                details TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        defaults = [
            ("guide_text", DEFAULT_GUIDE_TEXT),
            ("wm_text", DEFAULT_WM_TEXT),
            ("wm_photo", ""),
            ("example_views_photo", ""),
            ("example_likes_photo", "")
        ]
        for key, val in defaults:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            if not await cursor.fetchone():
                await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))
        await db.commit()

async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else ""

async def update_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

# ----------------- КЛАВИАТУРЫ ----------------- #
def main_kb(user_id: int):
    kb = [
        [KeyboardButton(text="📹 Отправить отчет по видео")],
        [KeyboardButton(text="📚 Гайд по видео"), KeyboardButton(text="💰 Баланс и Вывод")],
        [KeyboardButton(text="👥 Реферальная система"), KeyboardButton(text="ℹ️ Профиль")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="⚙️ Админ-панель гайдов")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def guide_inline_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏷 Посмотреть водяной знак", callback_data="show_wm_info")],
            [InlineKeyboardButton(text="🖼 Примеры правильных скриншотов", callback_data="show_examples_info")]
        ]
    )

def admin_guides_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Изменить текст регламента", callback_data="adm_edit_guide_text")],
            [InlineKeyboardButton(text="🏷 Изменить текст вотермарка", callback_data="adm_edit_wm_text")],
            [InlineKeyboardButton(text="🖼 Загрузить фото вотермарка/лого", callback_data="adm_upload_wm_photo")],
            [InlineKeyboardButton(text="📊 Загрузить пример скрина просмотров", callback_data="adm_upload_views_sample")],
            [InlineKeyboardButton(text="❤️ Загрузить пример скрина лайков", callback_data="adm_upload_likes_sample")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel_action")]
        ]
    )

def withdraw_methods_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇷🇺 Карта РФ (RUB)", callback_data="wm_rub")],
            [InlineKeyboardButton(text="🇰🇿 Карта KZ (KZT)", callback_data="wm_kzt")],
            [InlineKeyboardButton(text="💎 TON (Крипта)", callback_data="wm_ton")],
            [InlineKeyboardButton(text="💵 USD", callback_data="wm_usd")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
        ]
    )

# ----------------- FSM СОСТОЯНИЯ ----------------- #
class Registration(StatesGroup):
    tt_username = State()
    age = State()
    accounts_count = State()

class VideoReport(StatesGroup):
    url = State()
    views_photo = State()
    likes_photo = State()

class AdminReview(StatesGroup):
    report_id = State()
    views = State()
    likes = State()

class Withdrawal(StatesGroup):
    method = State()
    amount = State()
    details = State()

class AdminSettingsState(StatesGroup):
    waiting_guide_text = State()
    waiting_wm_text = State()
    waiting_wm_photo = State()
    waiting_views_sample = State()
    waiting_likes_sample = State()

# ----------------- ИНИЦИАЛИЗАЦИЯ ----------------- #
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ----------------- СТАРТ И РЕГИСТРАЦИЯ ----------------- #
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    args = message.text.split(maxsplit=1)
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        ref_candidate = int(args[1])
        if ref_candidate != message.from_user.id:
            referrer_id = ref_candidate

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT status FROM users WHERE user_id = ?", (message.from_user.id,))
        user = await cursor.fetchone()

        if not user:
            await db.execute(
                "INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
                (message.from_user.id, message.from_user.username, referrer_id)
            )
            await db.commit()
            await message.answer(
                "👋 Привет! Чтобы начать работать и заливать видео, заполни заявку.\n\n"
                "Введите ваш @username в TikTok:"
            )
            await state.set_state(Registration.tt_username)
            return

        status = user[0]
        if status == "unregistered":
            await message.answer("Введите ваш @username в TikTok:")
            await state.set_state(Registration.tt_username)
        elif status == "pending":
            await message.answer("⏳ Ваша заявка находится на рассмотрении. Ожидайте подтверждения.")
        elif status == "approved":
            await message.answer("Главное меню:", reply_markup=main_kb(message.from_user.id))
        elif status == "rejected":
            await message.answer("❌ Ваша прошлая анкета была отклонена.")

@dp.message(Registration.tt_username)
async def reg_tt_username(message: Message, state: FSMContext):
    await state.update_data(tt_username=message.text.strip())
    await message.answer("Сколько вам лет?")
    await state.set_state(Registration.age)

@dp.message(Registration.age)
async def reg_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, укажите возраст числом:")
        return
    await state.update_data(age=int(message.text))
    await message.answer("Сколько TikTok аккаунтов вы готовы вести одновременно?")
    await state.set_state(Registration.accounts_count)

@dp.message(Registration.accounts_count)
async def reg_accounts(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Укажите число:")
        return

    accs = int(message.text)
    data = await state.get_data()
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET tt_username = ?, age = ?, accounts_count = ?, status = 'pending' WHERE user_id = ?",
            (data['tt_username'], data['age'], accs, message.from_user.id)
        )
        await db.commit()

    await message.answer("✅ Заявка отправлена модератору на проверку!")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"appr_user_{message.from_user.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decl_user_{message.from_user.id}")
        ]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"📝 <b>Новая заявка воркера:</b>\n"
        f"TG: @{message.from_user.username or 'без_юзернейма'} (ID: <code>{message.from_user.id}</code>)\n"
        f"TikTok: {data['tt_username']}\n"
        f"Возраст: {data['age']}\n"
        f"Аккаунтов: {accs}",
        reply_markup=admin_kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("appr_user_"))
async def admin_approve_user(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (target_id,))
        await db.commit()

    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Одобрен</b>", parse_mode="HTML")
    await bot.send_message(target_id, "🎉 Ваша заявка одобрена! Ознакомьтесь с гайдом перед отправкой видео.", reply_markup=main_kb(target_id))

@dp.callback_query(F.data.startswith("decl_user_"))
async def admin_decline_user(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (target_id,))
        await db.commit()

    await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Отклонен</b>", parse_mode="HTML")
    await bot.send_message(target_id, "❌ Ваша анкета отклонена модератором.")

# ----------------- ПРОСМОТР ГАЙДА ВОРКЕРАМИ ----------------- #
@dp.message(F.text == "📚 Гайд по видео")
async def show_guide(message: Message):
    guide_text = await get_setting("guide_text")
    await message.answer(guide_text, reply_markup=guide_inline_kb(), parse_mode="HTML", disable_web_page_preview=True)

@dp.callback_query(F.data == "show_wm_info")
async def callback_show_wm_info(callback: CallbackQuery):
    wm_text = await get_setting("wm_text")
    wm_photo = await get_setting("wm_photo")

    caption = (
        f"🏷 <b>Информация о водяном знаке (Watermark):</b>\n\n"
        f"Текст/тег знака:\n<code>{wm_text}</code>\n\n"
        f"⚠️ Обязательно накладывайте его на каждый ролик перед публикацией."
    )
    if wm_photo:
        await callback.message.answer_photo(photo=wm_photo, caption=caption, parse_mode="HTML")
    else:
        await callback.message.answer(caption, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "show_examples_info")
async def callback_show_examples(callback: CallbackQuery):
    views_photo = await get_setting("example_views_photo")
    likes_photo = await get_setting("example_likes_photo")

    if not views_photo and not likes_photo:
        await callback.answer("Администратор еще не загрузил образцы скриншотов.", show_alert=True)
        return

    if views_photo:
        await callback.message.answer_photo(photo=views_photo, caption="📊 <b>Пример правильного скриншота просмотров</b>\nДолжно быть четко видно число просмотров и аккаунт.", parse_mode="HTML")
    if likes_photo:
        await callback.message.answer_photo(photo=likes_photo, caption="❤️ <b>Пример правильного скриншота лайков</b>\nДолжно быть четко видно количество отметок «Нравится».", parse_mode="HTML")
    await callback.answer()

# ----------------- АДМИН-ПАНЕЛЬ УПРАВЛЕНИЯ ГАЙДАМИ ----------------- #
@dp.message(F.text == "⚙️ Админ-панель гайдов")
@dp.message(Command("admin"))
async def cmd_admin_guides(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 <b>Управление гайдами и регламентом:</b>\n\n"
        "Выберите раздел, который хотите обновить:",
        reply_markup=admin_guides_menu_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_edit_guide_text")
async def adm_edit_guide_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    current_text = await get_setting("guide_text")
    await callback.message.answer(
        f"Текущий текст гайда:\n\n{current_text}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Отправьте сообщением <b>новый текст регламента</b> (поддерживается HTML-разметка):",
        parse_mode="HTML"
    )
    await state.set_state(AdminSettingsState.waiting_guide_text)
    await callback.answer()

@dp.message(AdminSettingsState.waiting_guide_text)
async def adm_save_guide_text(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await update_setting("guide_text", message.text)
    await state.clear()
    await message.answer("✅ Основной текст регламента успешно обновлен!", reply_markup=main_kb(ADMIN_ID))

@dp.callback_query(F.data == "adm_edit_wm_text")
async def adm_edit_wm_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    cur_wm = await get_setting("wm_text")
    await callback.message.answer(
        f"Текущий вотермарк: <code>{cur_wm}</code>\n\n"
        f"Отправьте новый текст/тег водяного знака:",
        parse_mode="HTML"
    )
    await state.set_state(AdminSettingsState.waiting_wm_text)
    await callback.answer()

@dp.message(AdminSettingsState.waiting_wm_text)
async def adm_save_wm_text(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await update_setting("wm_text", message.text.strip())
    await state.clear()
    await message.answer("✅ Текст водяного знака обновлен!", reply_markup=main_kb(ADMIN_ID))

@dp.callback_query(F.data == "adm_upload_wm_photo")
async def adm_upload_wm_photo_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Отправьте фото или логотип водяного знака, который воркеры должны накладывать:")
    await state.set_state(AdminSettingsState.waiting_wm_photo)
    await callback.answer()

@dp.message(AdminSettingsState.waiting_wm_photo, F.photo)
async def adm_save_wm_photo(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    photo_id = message.photo[-1].file_id
    await update_setting("wm_photo", photo_id)
    await state.clear()
    await message.answer("✅ Картинка водяного знака сохранена!", reply_markup=main_kb(ADMIN_ID))

@dp.callback_query(F.data == "adm_upload_views_sample")
async def adm_upload_views_sample_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Отправьте фото-образец скриншота просмотров:")
    await state.set_state(AdminSettingsState.waiting_views_sample)
    await callback.answer()

@dp.message(AdminSettingsState.waiting_views_sample, F.photo)
async def adm_save_views_sample(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    photo_id = message.photo[-1].file_id
    await update_setting("example_views_photo", photo_id)
    await state.clear()
    await message.answer("✅ Образец скриншота просмотров сохранен!", reply_markup=main_kb(ADMIN_ID))

@dp.callback_query(F.data == "adm_upload_likes_sample")
async def adm_upload_likes_sample_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Отправьте фото-образец скриншота лайков:")
    await state.set_state(AdminSettingsState.waiting_likes_sample)
    await callback.answer()

@dp.message(AdminSettingsState.waiting_likes_sample, F.photo)
async def adm_save_likes_sample(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    photo_id = message.photo[-1].file_id
    await update_setting("example_likes_photo", photo_id)
    await state.clear()
    await message.answer("✅ Образец скриншота лайков сохранен!", reply_markup=main_kb(ADMIN_ID))

# ----------------- ПРОФИЛЬ И РЕФЕРАЛЫ ----------------- #
@dp.message(F.text == "ℹ️ Профиль")
async def cmd_profile(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT balance, tt_username, status FROM users WHERE user_id = ?", (message.from_user.id,))
        user = await cursor.fetchone()

    if not user or user[2] != 'approved':
        return

    await message.answer(
        f"👤 <b>Ваш профиль:</b>\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        f"TikTok: {user[1]}\n"
        f"Баланс: <b>${user[0]:.2f}</b>",
        parse_mode="HTML"
    )

@dp.message(F.text == "👥 Реферальная система")
async def cmd_refs(message: Message):
    bot_me = await bot.get_me()
    ref_link = f"https://t.me/{bot_me.username}?start={message.from_user.id}"

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (message.from_user.id,))
        refs_count = (await cursor.fetchone())[0]

    await message.answer(
        f"👥 <b>Реферальная программа (15%):</b>\n\n"
        f"Вы получаете 15% от выплат каждого приглашенного воркера.\n\n"
        f"Ваша партнерская ссылка:\n<code>{ref_link}</code>\n\n"
        f"Приглашено рефералов: <b>{refs_count}</b>",
        parse_mode="HTML"
    )

# ----------------- ОТЧЕТ ПО ВИДЕО ----------------- #
@dp.message(F.text == "📹 Отправить отчет по видео")
async def report_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT status FROM users WHERE user_id = ?", (message.from_user.id,))
        user = await cursor.fetchone()

    if not user or user[0] != 'approved':
        await message.answer("⚠️ Ваша заявка еще не одобрена.")
        return

    await message.answer("Отправьте ссылку на ваше опубликованное видео в TikTok:")
    await state.set_state(VideoReport.url)

@dp.message(VideoReport.url)
async def report_url(message: Message, state: FSMContext):
    if not message.text.startswith("http"):
        await message.answer("Отправьте валидную ссылку (начинается с http/https):")
        return

    await state.update_data(video_url=message.text.strip())
    await message.answer("Отправьте скриншот просмотров из аналитики видео:")
    await state.set_state(VideoReport.views_photo)

@dp.message(VideoReport.views_photo, F.photo)
async def report_views_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(views_photo_id=photo_id)
    await message.answer("Теперь отправьте скриншот лайков:")
    await state.set_state(VideoReport.likes_photo)

@dp.message(VideoReport.likes_photo, F.photo)
async def report_likes_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO video_reports (user_id, video_url, views_photo_id, likes_photo_id) VALUES (?, ?, ?, ?) RETURNING id",
            (message.from_user.id, data['video_url'], data['views_photo_id'], photo_id)
        )
        report_id = (await cursor.fetchone())[0]
        await db.commit()

    await message.answer("✅ Отчет принят и отправлен на модерацию!", reply_markup=main_kb(message.from_user.id))

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 Начислить баланс", callback_data=f"pay_rep_{report_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_rep_{report_id}")
        ]
    ])

    await bot.send_message(
        ADMIN_ID,
        f"📹 <b>Новый отчет #{report_id}</b>\n"
        f"Воркер: @{message.from_user.username or 'без_юзернейма'} (ID: <code>{message.from_user.id}</code>)\n"
        f"Ссылка: {data['video_url']}",
        parse_mode="HTML"
    )
    await bot.send_photo(ADMIN_ID, photo=data['views_photo_id'], caption=f"Просмотры (Отчет #{report_id})")
    await bot.send_photo(ADMIN_ID, photo=photo_id, caption=f"Лайки (Отчет #{report_id})", reply_markup=admin_kb)

# ----------------- МОДЕРАЦИЯ ОТЧЕТА ----------------- #
@dp.callback_query(F.data.startswith("rej_rep_"))
async def admin_reject_report(callback: CallbackQuery):
    report_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM video_reports WHERE id = ?", (report_id,))
        rep = await cursor.fetchone()
        await db.execute("UPDATE video_reports SET status = 'rejected' WHERE id = ?", (report_id,))
        await db.commit()

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"❌ Отчет #{report_id} отклонен.")
    if rep:
        await bot.send_message(rep[0], f"❌ Ваш отчет #{report_id} отклонен. Убедитесь, что соблюден регламент, водяной знак и скриншоты.")

@dp.callback_query(F.data.startswith("pay_rep_"))
async def admin_pay_report_start(callback: CallbackQuery, state: FSMContext):
    report_id = int(callback.data.split("_")[2])
    await state.update_data(report_id=report_id)
    await callback.message.answer(f"Введите подтвержденное количество просмотров для отчета #{report_id}:")
    await state.set_state(AdminReview.views)

@dp.message(AdminReview.views)
async def admin_review_views(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число просмотров:")
        return
    await state.update_data(views=int(message.text))
    await message.answer("Введите подтвержденное количество лайков:")
    await state.set_state(AdminReview.likes)

@dp.message(AdminReview.likes)
async def admin_review_likes(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число лайков:")
        return

    data = await state.get_data()
    await state.clear()

    views = data['views']
    likes = int(message.text)
    report_id = data['report_id']

    amount = round((views / 1000.0) * 1.0, 2)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM video_reports WHERE id = ?", (report_id,))
        rep = await cursor.fetchone()
        if not rep:
            await message.answer("Отчет не найден.")
            return

        worker_id = rep[0]
        await db.execute("UPDATE video_reports SET status = 'approved' WHERE id = ?", (report_id,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, worker_id))

        ref_cursor = await db.execute("SELECT referrer_id FROM users WHERE user_id = ?", (worker_id,))
        user_ref = await ref_cursor.fetchone()

        if user_ref and user_ref[0]:
            referrer_id = user_ref[0]
            ref_payout = round(amount * 0.15, 2)
            if ref_payout > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (ref_payout, referrer_id))
                await bot.send_message(
                    referrer_id,
                    f"🎁 <b>Реферальный бонус!</b>\nВам начислено <b>${ref_payout:.2f}</b> (15%) от видео реферала.",
                    parse_mode="HTML"
                )

        await db.commit()

    await message.answer(f"✅ Баланс воркера пополнен на ${amount:.2f}. Отчет #{report_id} закрыт.")
    await bot.send_message(
        worker_id,
        f"🎉 <b>Отчет одобрен!</b>\n"
        f"Просмотры: {views} | Лайки: {likes}\n"
        f"Начислено: <b>${amount:.2f}</b>",
        parse_mode="HTML"
    )

# ----------------- БАЛАНС И ВЫВОД ----------------- #
@dp.message(F.text == "💰 Баланс и Вывод")
async def cmd_balance(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE user_id = ?", (message.from_user.id,))
        res = await cursor.fetchone()

    balance = res[0] if res else 0.0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Заказать вывод (от $5)", callback_data="start_withdraw")]
    ])
    await message.answer(
        f"💰 Ваш баланс: <b>${balance:.2f}</b>\n\n"
        f"Минимальная выплата: <b>$5.00</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "start_withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE user_id = ?", (callback.from_user.id,))
        balance = (await cursor.fetchone())[0]

    if balance < 5.0:
        await callback.answer("❌ На балансе меньше $5. Вывод доступен от $5.", show_alert=True)
        return

    await callback.message.edit_text("Выберите метод вывода:", reply_markup=withdraw_methods_kb())

@dp.callback_query(F.data.startswith("wm_"))
async def withdraw_select_method(callback: CallbackQuery, state: FSMContext):
    methods_dict = {
        "wm_rub": "Карта РФ (RUB)",
        "wm_kzt": "Карта KZ (KZT)",
        "wm_ton": "Crypto TON",
        "wm_usd": "USD"
    }
    method_name = methods_dict[callback.data]
    await state.update_data(withdraw_method=method_name)

    await callback.message.edit_text(f"Метод: <b>{method_name}</b>\nВведите сумму вывода ($):", parse_mode="HTML")
    await state.set_state(Withdrawal.amount)

@dp.message(Withdrawal.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите сумму числом:")
        return

    if amount < 5.0:
        await message.answer("Минимальная сумма — $5. Введите сумму заново:")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE user_id = ?", (message.from_user.id,))
        balance = (await cursor.fetchone())[0]

    if amount > balance:
        await message.answer(f"Недостаточно средств. Ваш баланс: ${balance:.2f}\nВведите корректную сумму:")
        return

    await state.update_data(amount=amount)
    await message.answer("Введите реквизиты (номер карты / адрес TON / номер счета):")
    await state.set_state(Withdrawal.details)

@dp.message(Withdrawal.details)
async def withdraw_details(message: Message, state: FSMContext):
    details = message.text.strip()
    data = await state.get_data()
    amount = data['amount']
    method = data['withdraw_method']
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, message.from_user.id))
        cursor = await db.execute(
            "INSERT INTO withdrawals (user_id, amount, method, details) VALUES (?, ?, ?, ?) RETURNING id",
            (message.from_user.id, amount, method, details)
        )
        withdraw_id = (await cursor.fetchone())[0]
        await db.commit()

    await message.answer(
        f"✅ <b>Заявка на вывод #{withdraw_id} принята!</b>\n"
        f"Сумма: ${amount:.2f}\n"
        f"Метод: {method}\n"
        f"Реквизиты: {details}\n\n"
        f"Ожидайте зачисления средств.",
        reply_markup=main_kb(message.from_user.id),
        parse_mode="HTML"
    )

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выплачено", callback_data=f"done_wdr_{withdraw_id}")]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Заявка на вывод #{withdraw_id}</b>\n\n"
        f"Воркер: @{message.from_user.username or 'без_юзернейма'} (ID: <code>{message.from_user.id}</code>)\n"
        f"Сумма: <b>${amount:.2f}</b>\n"
        f"Метод: <b>{method}</b>\n"
        f"Реквизиты: <code>{details}</code>",
        reply_markup=admin_kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("done_wdr_"))
async def admin_complete_withdraw(callback: CallbackQuery):
    w_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id, amount, method FROM withdrawals WHERE id = ?", (w_id,))
        res = await cursor.fetchone()
        await db.execute("UPDATE withdrawals SET status = 'completed' WHERE id = ?", (w_id,))
        await db.commit()

    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>ВЫПЛАЧЕНО</b>", parse_mode="HTML")
    if res:
        await bot.send_message(
            res[0],
            f"✅ <b>Выплата выполнена!</b>\nСредства (${res[1]:.2f}) отправлены на {res[2]}.",
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "cancel_action")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Действие отменено.", reply_markup=main_kb(callback.from_user.id))

# ----------------- ЗАПУСК ----------------- #
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
