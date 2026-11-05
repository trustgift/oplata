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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_STR = os.getenv("ADMIN_ID", "1602809044").strip()
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR.isdigit() else 1602809044

DB_PATH = BASE_DIR / "tt_payouts.db"

DEFAULT_GUIDE = (
    "📖 <b>Регламент залива видео TikTok</b>\n\n"
    "1. <b>Формат</b>: вертикальный 9:16 (1080x1920), 30/60 FPS, динамичный монтаж 7–15 секунд.\n"
    "2. <b>Водяной знак</b>: обязателен на протяжении всего видео.\n"
    "3. <b>Скриншоты</b>: четкие скрины просмотров и лайков из аналитики.\n"
    "4. <b>Тариф</b>: $1.00 за 1 000 просмотров и 100 лайков.\n"
    "5. <b>Выплаты</b>: от $5.00 на РФ карты, KZ карты, TON и USD."
)

DEFAULT_WM = "@project_tag"

# ----------------- БАЗА ДАННЫХ ----------------- #
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                video_url TEXT NOT NULL,
                views_photo TEXT NOT NULL,
                likes_photo TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                details TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                val TEXT
            )
        """)

        # Принудительно вносим или обновляем дефолтные значения
        defaults = [
            ("guide", DEFAULT_GUIDE),
            ("wm_tag", DEFAULT_WM),
            ("wm_photo", ""),
            ("sample_views", ""),
            ("sample_likes", "")
        ]
        for key, val in defaults:
            await db.execute("INSERT OR IGNORE INTO config (key, val) VALUES (?, ?)", (key, val))

        # Сразу делаем админа одобренным
        await db.execute("""
            INSERT INTO users (user_id, username, status)
            VALUES (?, 'admin', 'approved')
            ON CONFLICT(user_id) DO UPDATE SET status = 'approved'
        """, (ADMIN_ID,))

        await db.commit()

async def get_config(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT val FROM config WHERE key = ?", (key,))
        row = await res.fetchone()
        return row[0] if (row and row[0]) else ""

async def set_config(key: str, val: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO config (key, val) VALUES (?, ?)", (key, val))
        await db.commit()

# ----------------- КЛАВИАТУРЫ ----------------- #
def main_kb(user_id: int):
    if user_id == ADMIN_ID:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="⚙️ Админ-панель")],
                [KeyboardButton(text="📚 Регламент и Гайды"), KeyboardButton(text="💰 Баланс")],
                [KeyboardButton(text="📹 Сдать отчет"), KeyboardButton(text="👥 Рефералка")]
            ],
            resize_keyboard=True
        )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📹 Сдать отчет")],
            [KeyboardButton(text="📚 Регламент и Гайды"), KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="👥 Рефералка"), KeyboardButton(text="👤 Профиль")]
        ],
        resize_keyboard=True
    )

def guide_inline_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏷 Водяной знак", callback_data="show_wm")],
            [InlineKeyboardButton(text="🖼 Примеры скриншотов", callback_data="show_samples")]
        ]
    )

def admin_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Изменить текст гайда", callback_data="adm_set_guide")],
            [InlineKeyboardButton(text="🏷 Изменить тег вотермарка", callback_data="adm_set_wmtag")],
            [InlineKeyboardButton(text="🖼 Загрузить лого/фото знака", callback_data="adm_set_wmphoto")],
            [InlineKeyboardButton(text="📊 Образец просмотров", callback_data="adm_set_sample_views")],
            [InlineKeyboardButton(text="❤️ Образец лайков", callback_data="adm_set_sample_likes")],
            [InlineKeyboardButton(text="❌ Закрыть панель", callback_data="close_admin")]
        ]
    )

def cancel_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
        ]
    )

# ----------------- FSM СОСТОЯНИЯ ----------------- #
class WorkerReg(StatesGroup):
    tt_username = State()
    age = State()
    accounts = State()

class ReportFSM(StatesGroup):
    url = State()
    views_photo = State()
    likes_photo = State()

class AdminReviewFSM(StatesGroup):
    report_id = State()
    views = State()
    likes = State()

class WithdrawFSM(StatesGroup):
    method = State()
    amount = State()
    details = State()

class AdminEditFSM(StatesGroup):
    text_data = State()
    photo_data = State()

# ----------------- ИНИЦИАЛИЗАЦИЯ ----------------- #
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ----------------- СТАРТ ----------------- #
@dp.message(CommandStart())
@dp.message(Command("reset_admin"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    # ЖЕСТКАЯ ПРОВЕРКА АДМИНА
    if uid == ADMIN_ID:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (uid,))
            await db.commit()
        await message.answer("👑 Вы авторизованы как Администратор проекта.", reply_markup=main_kb(uid))
        return

    # ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ
    ref_id = None
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].isdigit():
        ref_candidate = int(parts[1])
        if ref_candidate != uid:
            ref_id = ref_candidate

    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT status FROM users WHERE user_id = ?", (uid,))
        user = await res.fetchone()

        if not user:
            await db.execute(
                "INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
                (uid, message.from_user.username, ref_id)
            )
            await db.commit()
            await message.answer(
                "👋 Привет! Чтобы начать заливать видео и получать выплаты, ответьте на пару вопросов.\n\n"
                "Введите ваш TikTok username (например: @username):"
            )
            await state.set_state(WorkerReg.tt_username)
            return

        status = user[0]
        if status == "approved":
            await message.answer("Главное меню:", reply_markup=main_kb(uid))
        elif status == "pending":
            await message.answer("⏳ Ваша анкета находится на проверке. Ожидайте решения модератора.")
        elif status == "rejected":
            await message.answer("❌ Ваша заявка отклонена администрацией.")
        else:
            await message.answer("Введите ваш TikTok username:")
            await state.set_state(WorkerReg.tt_username)

# ----------------- АДМИН-ПАНЕЛЬ И НАСТРОЙКИ ----------------- #
@dp.message(F.text == "⚙️ Админ-панель")
@dp.message(Command("admin"))
async def open_admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("⚙️ <b>Панель управления материалами и гайдами:</b>", reply_markup=admin_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "close_admin")
async def close_admin(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()

@dp.callback_query(F.data.in_({"adm_set_guide", "adm_set_wmtag"}))
async def admin_set_text_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = "guide" if call.data == "adm_set_guide" else "wm_tag"
    await state.update_data(target_key=key)

    cur = await get_config(key)
    await call.message.answer(
        f"Текущее значение <b>{key}</b>:\n\n{cur}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Отправьте новым сообщением текст для сохранения:",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await state.set_state(AdminEditFSM.text_data)
    await call.answer()

@dp.message(AdminEditFSM.text_data)
async def admin_save_text(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key = data.get("target_key")
    await set_config(key, message.text)
    await state.clear()
    await message.answer(f"✅ Настройка <b>{key}</b> успешно обновлена!", parse_mode="HTML", reply_markup=main_kb(ADMIN_ID))

@dp.callback_query(F.data.in_({"adm_set_wmphoto", "adm_set_sample_views", "adm_set_sample_likes"}))
async def admin_set_photo_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    mapping = {
        "adm_set_wmphoto": "wm_photo",
        "adm_set_sample_views": "sample_views",
        "adm_set_sample_likes": "sample_likes"
    }
    key = mapping[call.data]
    await state.update_data(target_key=key)
    await call.message.answer(f"Отправьте фотографию для раздела <b>{key}</b>:", parse_mode="HTML", reply_markup=cancel_kb())
    await state.set_state(AdminEditFSM.photo_data)
    await call.answer()

@dp.message(AdminEditFSM.photo_data, F.photo)
async def admin_save_photo(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key = data.get("target_key")
    photo_id = message.photo[-1].file_id
    await set_config(key, photo_id)
    await state.clear()
    await message.answer(f"✅ Изображение для <b>{key}</b> успешно сохранено!", parse_mode="HTML", reply_markup=main_kb(ADMIN_ID))

# ----------------- ПРОСМОТР ГАЙДА ----------------- #
@dp.message(F.text == "📚 Регламент и Гайды")
async def show_guide(message: Message):
    text = await get_config("guide")
    if not text:
        text = DEFAULT_GUIDE
    await message.answer(text, reply_markup=guide_inline_kb(), parse_mode="HTML", disable_web_page_preview=True)

@dp.callback_query(F.data == "show_wm")
async def show_wm(call: CallbackQuery):
    tag = await get_config("wm_tag")
    photo = await get_config("wm_photo")
    caption = f"🏷 <b>Водяной знак:</b> <code>{tag or DEFAULT_WM}</code>\n\nНакладывайте его поверх каждого видео."
    if photo:
        await call.message.answer_photo(photo=photo, caption=caption, parse_mode="HTML")
    else:
        await call.message.answer(caption, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "show_samples")
async def show_samples(call: CallbackQuery):
    v_photo = await get_config("sample_views")
    l_photo = await get_config("sample_likes")

    if not v_photo and not l_photo:
        await call.answer("Образцы скриншотов еще не загружены.", show_alert=True)
        return

    if v_photo:
        await call.message.answer_photo(photo=v_photo, caption="📊 Пример скриншота <b>просмотров</b> из аналитики TikTok", parse_mode="HTML")
    if l_photo:
        await call.message.answer_photo(photo=l_photo, caption="❤️ Пример скриншота <b>лайков</b>", parse_mode="HTML")
    await call.answer()

# ----------------- АНКЕТА ВОРКЕРА ----------------- #
@dp.message(WorkerReg.tt_username)
async def worker_tt_step(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await state.clear()
        await message.answer("👑 Вы администратор.", reply_markup=main_kb(ADMIN_ID))
        return
    await state.update_data(tt_username=message.text.strip())
    await message.answer("Сколько вам лет?")
    await state.set_state(WorkerReg.age)

@dp.message(WorkerReg.age)
async def worker_age_step(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число:")
        return
    await state.update_data(age=int(message.text))
    await message.answer("Сколько TikTok аккаунтов вы готовы держать?")
    await state.set_state(WorkerReg.accounts)

@dp.message(WorkerReg.accounts)
async def worker_accs_step(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите количество числом:")
        return
    accs = int(message.text)
    data = await state.get_data()
    await state.clear()

    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET tt_username = ?, age = ?, accounts_count = ?, status = 'pending' WHERE user_id = ?",
            (data['tt_username'], data['age'], accs, uid)
        )
        await db.commit()

    await message.answer("✅ Ваша анкета отправлена на проверку модератору!")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"usr_ok_{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"usr_no_{uid}")
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"📝 <b>Новая заявка воркера:</b>\n"
        f"TG: @{message.from_user.username or 'без_юзернейма'} (<code>{uid}</code>)\n"
        f"TikTok: <code>{data['tt_username']}</code>\n"
        f"Возраст: {data['age']}\n"
        f"Аккаунтов: {accs}",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("usr_ok_"))
async def adm_approve_user(call: CallbackQuery):
    uid = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (uid,))
        await db.commit()
    await call.message.edit_text(call.message.text + "\n\n✅ <b>ОДОБРЕН</b>", parse_mode="HTML")
    await bot.send_message(uid, "🎉 Ваша анкета одобрена! Теперь вам доступен весь функционал.", reply_markup=main_kb(uid))

@dp.callback_query(F.data.startswith("usr_no_"))
async def adm_reject_user(call: CallbackQuery):
    uid = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (uid,))
        await db.commit()
    await call.message.edit_text(call.message.text + "\n\n❌ <b>ОТКЛОНЕН</b>", parse_mode="HTML")
    await bot.send_message(uid, "❌ Ваша заявка отклонена.")

# ----------------- ОТЧЕТ ПО ВИДЕО ----------------- #
@dp.message(F.text == "📹 Сдать отчет")
async def report_start(message: Message, state: FSMContext):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT status FROM users WHERE user_id = ?", (uid,))
        user = await res.fetchone()
        if not user or user[0] != 'approved':
            await message.answer("⚠️ Вы не можете сдавать отчеты без одобренной анкеты.")
            return

        chk = await db.execute("SELECT id FROM reports WHERE user_id = ? AND status = 'pending'", (uid,))
        if await chk.fetchone():
            await message.answer("⏳ У вас уже есть отчет на проверке. Дождитесь его проверки модератором.")
            return

    await message.answer("Отправьте прямую ссылку на ваше опубликованное видео TikTok:", reply_markup=cancel_kb())
    await state.set_state(ReportFSM.url)

@dp.message(ReportFSM.url)
async def report_url(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("Отправьте корректную ссылку:")
        return
    await state.update_data(video_url=url)
    await message.answer("Прикрепите скриншот <b>просмотров</b> из аналитики:", parse_mode="HTML")
    await state.set_state(ReportFSM.views_photo)

@dp.message(ReportFSM.views_photo, F.photo)
async def report_views(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(views_photo=photo_id)
    await message.answer("Теперь прикрепите скриншот <b>лайков</b>:", parse_mode="HTML")
    await state.set_state(ReportFSM.likes_photo)

@dp.message(ReportFSM.likes_photo, F.photo)
async def report_likes(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    await state.clear()

    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute(
            "INSERT INTO reports (user_id, video_url, views_photo, likes_photo) VALUES (?, ?, ?, ?) RETURNING id",
            (uid, data['video_url'], data['views_photo'], photo_id)
        )
        rid = (await res.fetchone())[0]
        await db.commit()

    await message.answer("✅ Отчет отправлен на модерацию!", reply_markup=main_kb(uid))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💵 Начислить", callback_data=f"rep_ok_{rid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rep_no_{rid}")
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"📹 <b>Новый отчет #{rid}</b>\n"
        f"Воркер: @{message.from_user.username or 'нет'} (<code>{uid}</code>)\n"
        f"Ссылка: {data['video_url']}",
        parse_mode="HTML"
    )
    await bot.send_photo(ADMIN_ID, photo=data['views_photo'], caption=f"Просмотры #{rid}")
    await bot.send_photo(ADMIN_ID, photo=photo_id, caption=f"Лайки #{rid}", reply_markup=kb)

@dp.callback_query(F.data.startswith("rep_no_"))
async def adm_reject_report(call: CallbackQuery):
    rid = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT user_id, status FROM reports WHERE id = ?", (rid,))
        row = await res.fetchone()
        if not row or row[1] != 'pending':
            await call.answer("Отчет уже обработан.", show_alert=True)
            await call.message.delete_reply_markup()
            return
        await db.execute("UPDATE reports SET status = 'rejected' WHERE id = ?", (rid,))
        await db.commit()

    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"❌ Отчет #{rid} отклонен.")
    await bot.send_message(row[0], f"❌ Ваш отчет #{rid} отклонен.")

@dp.callback_query(F.data.startswith("rep_ok_"))
async def adm_pay_report_start(call: CallbackQuery, state: FSMContext):
    rid = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT status FROM reports WHERE id = ?", (rid,))
        row = await res.fetchone()
        if not row or row[0] != 'pending':
            await call.answer("Отчет уже обработан.", show_alert=True)
            await call.message.delete_reply_markup()
            return

    await call.message.edit_reply_markup(reply_markup=None)
    await state.update_data(report_id=rid)
    await call.message.answer(f"Введите подтвержденное количество <b>просмотров</b> для отчета #{rid}:", parse_mode="HTML")
    await state.set_state(AdminReviewFSM.views)

@dp.message(AdminReviewFSM.views)
async def adm_views_step(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите целое число:")
        return
    await state.update_data(views=int(message.text))
    await message.answer("Введите количество <b>лайков</b>:", parse_mode="HTML")
    await state.set_state(AdminReviewFSM.likes)

@dp.message(AdminReviewFSM.likes)
async def adm_likes_step(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число:")
        return
    data = await state.get_data()
    await state.clear()

    rid = data['report_id']
    views = data['views']
    likes = int(message.text)
    earned = round((views / 1000.0) * 1.0, 2)

    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT user_id, status FROM reports WHERE id = ?", (rid,))
        rep = await res.fetchone()
        if not rep or rep[1] != 'pending':
            await message.answer("Отчет уже был закрыт.")
            return

        w_uid = rep[0]
        await db.execute("UPDATE reports SET status = 'approved' WHERE id = ?", (rid,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (earned, w_uid))

        # 15% Рефералу
        ref_res = await db.execute("SELECT referrer_id FROM users WHERE user_id = ?", (w_uid,))
        ref_row = await ref_res.fetchone()
        if ref_row and ref_row[0]:
            bonus = round(earned * 0.15, 2)
            if bonus > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus, ref_row[0]))
                await bot.send_message(
                    ref_row[0],
                    f"🎁 <b>Реферальный бонус!</b> +${bonus:.2f} (15%) от работы реферала.",
                    parse_mode="HTML"
                )

        await db.commit()

    await message.answer(f"✅ Баланс воркера пополнен на ${earned:.2f}. Отчет #{rid} закрыт.")
    await bot.send_message(
        w_uid,
        f"🎉 <b>Отчет одобрен!</b>\nПросмотры: {views} | Лайки: {likes}\nВам начислено: <b>${earned:.2f}</b>",
        parse_mode="HTML"
    )

# ----------------- БАЛАНС И ВЫВОД ----------------- #
@dp.message(F.text == "💰 Баланс")
async def balance_screen(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
        row = await res.fetchone()
    bal = row[0] if row else 0.0

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💸 Вывести ($5+)", callback_data="start_withdraw")
    ]])
    await message.answer(f"💰 Ваш баланс: <b>${bal:.2f}</b>\nМинимум для вывода: $5.00", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "start_withdraw")
async def withdraw_start(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
        bal = (await res.fetchone())[0]

    if bal < 5.0:
        await call.answer("Минимальный баланс для вывода — $5.00", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Карта РФ (RUB)", callback_data="wm_rub")],
        [InlineKeyboardButton(text="🇰🇿 Карта KZ (KZT)", callback_data="wm_kzt")],
        [InlineKeyboardButton(text="💎 TON (Крипта)", callback_data="wm_ton")],
        [InlineKeyboardButton(text="💵 USD", callback_data="wm_usd")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
    ])
    await call.message.edit_text("Выберите способ выплаты:", reply_markup=kb)

@dp.callback_query(F.data.startswith("wm_"))
async def withdraw_method(call: CallbackQuery, state: FSMContext):
    methods = {
        "wm_rub": "Карта РФ (RUB)",
        "wm_kzt": "Карта KZ (KZT)",
        "wm_ton": "TON (Крипта)",
        "wm_usd": "USD"
    }
    m = methods[call.data]
    await state.update_data(method=m)
    await call.message.edit_text(f"Способ: <b>{m}</b>\nВведите сумму вывода ($):", parse_mode="HTML")
    await state.set_state(WithdrawFSM.amount)

@dp.message(WithdrawFSM.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите сумму числом:")
        return

    if val < 5.0:
        await message.answer("Минимум к выводу — $5.00:")
        return

    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
        bal = (await res.fetchone())[0]

    if val > bal:
        await message.answer(f"Недостаточно средств. Ваш баланс: ${bal:.2f}. Введите корректную сумму:")
        return

    await state.update_data(amount=val)
    await message.answer("Введите реквизиты (номер карты, кошелька или адрес TON):")
    await state.set_state(WithdrawFSM.details)

@dp.message(WithdrawFSM.details)
async def withdraw_details(message: Message, state: FSMContext):
    details = message.text.strip()
    data = await state.get_data()
    amount = data['amount']
    method = data['method']
    await state.clear()

    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, uid))
        ins = await db.execute(
            "INSERT INTO withdrawals (user_id, amount, method, details) VALUES (?, ?, ?, ?) RETURNING id",
            (uid, amount, method, details)
        )
        wid = (await ins.fetchone())[0]
        await db.commit()

    await message.answer(
        f"✅ <b>Заявка #{wid} принята!</b>\nСумма: ${amount:.2f}\nМетод: {method}\nРеквизиты: <code>{details}</code>",
        reply_markup=main_kb(uid),
        parse_mode="HTML"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выплачено", callback_data=f"wdr_ok_{wid}")
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Заявка на вывод #{wid}</b>\n"
        f"Воркер: @{message.from_user.username or 'нет'} (<code>{uid}</code>)\n"
        f"Сумма: <b>${amount:.2f}</b>\n"
        f"Метод: {method}\nРеквизиты: <code>{details}</code>",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("wdr_ok_"))
async def adm_payout_complete(call: CallbackQuery):
    wid = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT user_id, amount, method, status FROM withdrawals WHERE id = ?", (wid,))
        row = await res.fetchone()
        if not row or row[3] != 'pending':
            await call.answer("Заявка уже закрыта.", show_alert=True)
            await call.message.delete_reply_markup()
            return
        await db.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (wid,))
        await db.commit()

    await call.message.edit_text(call.message.text + "\n\n✅ <b>ВЫПЛАЧЕНО</b>", parse_mode="HTML")
    await bot.send_message(row[0], f"✅ <b>Выплата выполнена!</b>\nВам отправлено ${row[1]:.2f} на {row[2]}.", parse_mode="HTML")

# ----------------- ПРОФИЛЬ И РЕФЕРАЛКА ----------------- #
@dp.message(F.text == "👤 Профиль")
async def profile_view(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT balance, tt_username FROM users WHERE user_id = ?", (uid,))
        row = await res.fetchone()
    if not row:
        return
    await message.answer(
        f"👤 <b>Ваш профиль:</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"TikTok: <code>{row[1] or 'Не указан'}</code>\n"
        f"Баланс: <b>${row[0]:.2f}</b>",
        parse_mode="HTML"
    )

@dp.message(F.text == "👥 Рефералка")
async def refs_view(message: Message):
    uid = message.from_user.id
    b = await bot.get_me()
    ref_link = f"https://t.me/{b.username}?start={uid}"

    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (uid,))
        cnt = (await res.fetchone())[0]

    await message.answer(
        f"👥 <b>Реферальная программа (15%)</b>\n\n"
        f"Получайте 15% с каждой выплаты приведенного воркера.\n\n"
        f"Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"Приглашено рефералов: <b>{cnt}</b>",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "cancel_action")
async def cancel_handler(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer("Действие отменено.", reply_markup=main_kb(call.from_user.id))

# ----------------- ЗАПУСК ----------------- #
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
