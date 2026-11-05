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

# Загружаем .env по абсолютному пути (работает локально и на серверах)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise ValueError("Критическая ошибка: BOT_TOKEN не найден в переменных окружения / .env файле!")

if not ADMIN_ID_RAW:
    raise ValueError("Критическая ошибка: ADMIN_ID не найден в переменных окружения / .env файле!")

ADMIN_ID = int(ADMIN_ID_RAW)
DB_NAME = str(BASE_DIR / "tt_payouts.db")  # База создается в папке проекта

# ----------------- БАЗА ДАННЫХ ----------------- #
# (далее весь остальной код остается без изменений)


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
                status TEXT DEFAULT 'unregistered', -- unregistered, pending, approved, rejected
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
        await db.commit()

# ----------------- КЛАВИАТУРЫ ----------------- #
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📹 Отправить отчет по видео")],
            [KeyboardButton(text="💰 Баланс и Вывод"), KeyboardButton(text="👥 Реферальная система")],
            [KeyboardButton(text="ℹ️ Профиль")]
        ],
        resize_keyboard=True
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
                "👋 Привет! Чтобы начать работать и заливать видео, заполни короткую заявку.\n\n"
                "Введите ваш @username в TikTok:"
            )
            await state.set_state(Registration.tt_username)
            return

        status = user[0]
        if status == "unregistered":
            await message.answer("Введите ваш @username в TikTok:")
            await state.set_state(Registration.tt_username)
        elif status == "pending":
            await message.answer("⏳ Ваша анкета находится на проверке у администратора. Ожидайте.")
        elif status == "approved":
            await message.answer("Главное меню:", reply_markup=main_kb())
        elif status == "rejected":
            await message.answer("❌ Ваша прошлая заявка была отклонена.")

@dp.message(Registration.tt_username)
async def reg_tt_username(message: Message, state: FSMContext):
    await state.update_data(tt_username=message.text.strip())
    await message.answer("Сколько вам лет?")
    await state.set_state(Registration.age)

@dp.message(Registration.age)
async def reg_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите возраст числом:")
        return
    await state.update_data(age=int(message.text))
    await message.answer("Сколько TikTok аккаунтов вы готовы держать и вести одновременно?")
    await state.set_state(Registration.accounts_count)

@dp.message(Registration.accounts_count)
async def reg_accounts(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите количество числом:")
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

    await message.answer("✅ Ваша анкета отправлена! Ожидайте подтверждения от модератора.")

    # Оповещение администратора
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"appr_user_{message.from_user.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decl_user_{message.from_user.id}")
        ]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"📝 <b>Новая анкета воркера:</b>\n"
        f"TG: @{message.from_user.username or 'без_юзернейма'} (ID: {message.from_user.id})\n"
        f"TikTok: {data['tt_username']}\n"
        f"Возраст: {data['age']}\n"
        f"Готов держать аккаунтов: {accs}",
        reply_markup=admin_kb,
        parse_mode="HTML"
    )

# ----------------- ОБРАБОТКА АНКЕТЫ АДМИНОМ ----------------- #
@dp.callback_query(F.data.startswith("appr_user_"))
async def admin_approve_user(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (target_id,))
        await db.commit()

    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Одобрен</b>", parse_mode="HTML")
    await bot.send_message(target_id, "🎉 Ваша заявка одобрена! Теперь вам доступен весь функционал.", reply_markup=main_kb())

@dp.callback_query(F.data.startswith("decl_user_"))
async def admin_decline_user(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (target_id,))
        await db.commit()

    await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Отклонен</b>", parse_mode="HTML")
    await bot.send_message(target_id, "❌ Ваша заявка на работу была отклонена модератором.")

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
        f"Приглашайте воркеров и получайте 15% от каждой начисленной им суммы за просмотры!\n\n"
        f"Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"Всего рефералов: <b>{refs_count}</b>",
        parse_mode="HTML"
    )

# ----------------- ОТЧЕТ ПО ВИДЕО ----------------- #
@dp.message(F.text == "📹 Отправить отчет по видео")
async def report_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT status FROM users WHERE user_id = ?", (message.from_user.id,))
        user = await cursor.fetchone()

    if not user or user[0] != 'approved':
        await message.answer("⚠️ Вы еще не прошли модерацию.")
        return

    await message.answer("Отправьте прямую ссылку на ваше TikTok видео:")
    await state.set_state(VideoReport.url)

@dp.message(VideoReport.url)
async def report_url(message: Message, state: FSMContext):
    if not message.text.startswith("http"):
        await message.answer("Пожалуйста, отправьте корректную ссылку:")
        return

    await state.update_data(video_url=message.text.strip())
    await message.answer("Прикрепите скриншот, подтверждающий количество просмотров:")
    await state.set_state(VideoReport.views_photo)

@dp.message(VideoReport.views_photo, F.photo)
async def report_views_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(views_photo_id=photo_id)
    await message.answer("Отлично! Теперь отправьте скриншот лайков к видео:")
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

    await message.answer("✅ Отчет принят и отправлен модератору на проверку!", reply_markup=main_kb())

    # Отправка администратору
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
    await bot.send_photo(ADMIN_ID, photo=data['views_photo_id'], caption=f"Скриншот просмотров (Отчет #{report_id})")
    await bot.send_photo(ADMIN_ID, photo=photo_id, caption=f"Скриншот лайков (Отчет #{report_id})", reply_markup=admin_kb)

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
        await bot.send_message(rep[0], f"❌ Ваш отчет по видео #{report_id} был отклонен модератором.")

@dp.callback_query(F.data.startswith("pay_rep_"))
async def admin_pay_report_start(callback: CallbackQuery, state: FSMContext):
    report_id = int(callback.data.split("_")[2])
    await state.update_data(report_id=report_id)
    await callback.message.answer(f"Введите подтвержденное количество просмотров для отчета #{report_id}:")
    await state.set_state(AdminReview.views)

@dp.message(AdminReview.views)
async def admin_review_views(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите целое число просмотров:")
        return
    await state.update_data(views=int(message.text))
    await message.answer("Введите подтвержденное количество лайков:")
    await state.set_state(AdminReview.likes)

@dp.message(AdminReview.likes)
async def admin_review_likes(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите целое число лайков:")
        return

    data = await state.get_data()
    await state.clear()

    views = data['views']
    likes = int(message.text)
    report_id = data['report_id']

    # Расчет: $1 за 1000 просмотров и 100 лайков ($0.001 за просмотр при базовом соотношении)
    # Пропорциональный подсчет по просмотрам:
    amount = round((views / 1000.0) * 1.0, 2)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM video_reports WHERE id = ?", (report_id,))
        rep = await cursor.fetchone()
        if not rep:
            await message.answer("Отчет не найден.")
            return

        worker_id = rep[0]
        # Обновляем статус отчета и баланс воркера
        await db.execute("UPDATE video_reports SET status = 'approved' WHERE id = ?", (report_id,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, worker_id))

        # Реферальный бонус (15%)
        ref_cursor = await db.execute("SELECT referrer_id FROM users WHERE user_id = ?", (worker_id,))
        user_ref = await ref_cursor.fetchone()
        ref_payout = 0.0

        if user_ref and user_ref[0]:
            referrer_id = user_ref[0]
            ref_payout = round(amount * 0.15, 2)
            if ref_payout > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (ref_payout, referrer_id))
                await bot.send_message(
                    referrer_id,
                    f"🎁 <b>Реферальный бонус!</b>\nВам начислено <b>${ref_payout:.2f}</b> (15%) от работы вашего реферала.",
                    parse_mode="HTML"
                )

        await db.commit()

    await message.answer(f"✅ Баланс воркера пополнен на ${amount:.2f}. Отчет закрыт.")
    await bot.send_message(
        worker_id,
        f"🎉 <b>Отчет одобрен!</b>\n"
        f"Просмотры: {views} | Лайки: {likes}\n"
        f"Вам начислено: <b>${amount:.2f}</b>",
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
        f"💰 Ваш текущий баланс: <b>${balance:.2f}</b>\n\n"
        f"Минимальная сумма на вывод: <b>$5.00</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "start_withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE user_id = ?", (callback.from_user.id,))
        balance = (await cursor.fetchone())[0]

    if balance < 5.0:
        await callback.answer("❌ На балансе меньше $5. Минимальная выплата — 5$", show_alert=True)
        return

    await callback.message.edit_text("Выберите платежную систему для вывода:", reply_markup=withdraw_methods_kb())

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

    await callback.message.edit_text(f"Выбран метод: <b>{method_name}</b>\nВведите сумму вывода в долларах ($):", parse_mode="HTML")
    await state.set_state(Withdrawal.amount)

@dp.message(Withdrawal.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите сумму корректным числом:")
        return

    if amount < 5.0:
        await message.answer("Минимальная сумма вывода — $5. Введите сумму еще раз:")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE user_id = ?", (message.from_user.id,))
        balance = (await cursor.fetchone())[0]

    if amount > balance:
        await message.answer(f"Недостаточно средств. Ваш баланс: ${balance:.2f}\nВведите другую сумму:")
        return

    await state.update_data(amount=amount)
    await message.answer("Введите ваши реквизиты (номер карты, кошелька или адрес TON):")
    await state.set_state(Withdrawal.details)

@dp.message(Withdrawal.details)
async def withdraw_details(message: Message, state: FSMContext):
    details = message.text.strip()
    data = await state.get_data()
    amount = data['amount']
    method = data['withdraw_method']
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        # Списываем баланс в холдер
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, message.from_user.id))
        cursor = await db.execute(
            "INSERT INTO withdrawals (user_id, amount, method, details) VALUES (?, ?, ?, ?) RETURNING id",
            (message.from_user.id, amount, method, details)
        )
        withdraw_id = (await cursor.fetchone())[0]
        await db.commit()

    await message.answer(
        f"✅ <b>Заявка на вывод #{withdraw_id} создана!</b>\n"
        f"Сумма: ${amount:.2f}\nМетод: {method}\nРеквизиты: {details}\n\n"
        f"Ожидайте поступления средств на счет.",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )

    # Уведомление администратору
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выплачено", callback_data=f"done_wdr_{withdraw_id}")]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Заявка на вывод средств #{withdraw_id}</b>\n\n"
        f"Пользователь: @{message.from_user.username or 'нет'} (ID: <code>{message.from_user.id}</code>)\n"
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
            f"✅ <b>Выплата выполнена!</b>\nВам отправлено ${res[1]:.2f} на {res[2]}.",
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "cancel_action")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Действие отменено.", reply_markup=main_kb())

# ----------------- ЗАПУСК ----------------- #
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

