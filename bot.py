import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
import psycopg2
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

BISHKEK_TZ = pytz.timezone("Asia/Bishkek")

# Настраиваемый график работы по Asia/Bishkek (10:00 - 21:00)
WORK_START_MINUTES = 10 * 60      # 600 (10:00)
WORK_END_MINUTES = 21 * 60        # 1260 (21:00)
SLOT_STEP_MINUTES = 60            # Шаг кнопок быстрого выбора времени

# Настраиваемая длительность каждой услуги (в минутах)
SERVICE_CONFIG = {
    "srv_haircut": {"name": "Мужская стрижка", "price": 800, "duration": 60},
    "srv_beard": {"name": "Стрижка бороды", "price": 500, "duration": 30},
    "srv_complex": {"name": "Комплекс (Стрижка + Борода)", "price": 1200, "duration": 90},
}

SERVICE_NAME_TO_DURATION = {
    cfg["name"].strip().lower(): int(cfg["duration"])
    for cfg in SERVICE_CONFIG.values()
}

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]
DAYS_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"
]


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()


Thread(target=run_web_server, daemon=True).start()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 7272996140

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("Не задана переменная окружения DATABASE_URL")
    return psycopg2.connect(DATABASE_URL)


class BookingState(StatesGroup):
    waiting_for_service = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_custom_time = State()
    waiting_for_late_choice = State()
    waiting_for_phone = State()
    waiting_for_callback_phone = State()


def get_service_duration(service_name: str | None, stored_duration: int | None = None) -> int:
    if service_name:
        normalized = service_name.strip().lower()
        if normalized in SERVICE_NAME_TO_DURATION:
            return SERVICE_NAME_TO_DURATION[normalized]
    if stored_duration and int(stored_duration) > 0:
        return int(stored_duration)
    return SERVICE_CONFIG["srv_haircut"]["duration"]


def format_minutes_to_hhmm(total_minutes: int) -> str:
    hours = (total_minutes // 60) % 24
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def parse_user_time(raw_text: str) -> tuple[bool, int | None, str | None]:
    """
    Принимает HH:MM, H:MM, 11.15, 13 45 и возвращает (is_valid, minutes_from_midnight, formatted_hhmm).
    """
    if not raw_text:
        return False, None, None
    cleaned = raw_text.strip()
    match = re.fullmatch(r"(\d{1,2})[:.\s](\d{2})", cleaned)
    if not match:
        return False, None, None

    hours = int(match.group(1))
    minutes = int(match.group(2))

    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        return False, None, None

    total_minutes = hours * 60 + minutes
    return True, total_minutes, f"{hours:02d}:{minutes:02d}"


def get_bishkek_today_str() -> str:
    now_bishkek = datetime.now(BISHKEK_TZ)
    return f"{now_bishkek.day} {MONTHS_RU[now_bishkek.month - 1]} {now_bishkek.year}"


def parse_date_str(date_str: str):
    month_map = {name: idx + 1 for idx, name in enumerate(MONTHS_RU)}
    parts = date_str.strip().split()
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        month = month_map.get(parts[1])
        year = int(parts[2])
        if not month:
            return None
        return datetime(year, month, day).date()
    except ValueError:
        return None


def is_past_date_str(date_str: str) -> bool:
    now_bishkek = datetime.now(BISHKEK_TZ)
    today_date = now_bishkek.date()
    parsed = parse_date_str(date_str)
    if not parsed:
        return True
    return parsed < today_date


def is_time_in_past(date_str: str, start_minutes: int) -> bool:
    now_bishkek = datetime.now(BISHKEK_TZ)
    target_date = parse_date_str(date_str)
    if not target_date:
        return True
    if target_date < now_bishkek.date():
        return True
    if target_date == now_bishkek.date():
        now_minutes = now_bishkek.hour * 60 + now_bishkek.minute
        return start_minutes <= now_minutes
    return False


def verify_db_schema(cur):
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ('users', 'bookings', 'callback_requests')
    """)
    tables = {row[0] for row in cur.fetchall()}
    missing_tables = {"users", "bookings", "callback_requests"} - tables
    if missing_tables:
        raise RuntimeError(f"Неполная структура БД: отсутствуют таблицы {sorted(missing_tables)}")

    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'bookings'
          AND column_name IN ('id', 'user_id', 'service', 'date', 'time', 'status', 'duration_minutes', 'start_minutes', 'end_minutes')
    """)
    columns = {row[0] for row in cur.fetchall()}
    required_columns = {"id", "user_id", "service", "date", "time", "status", "duration_minutes", "start_minutes", "end_minutes"}
    missing_columns = required_columns - columns
    if missing_columns:
        raise RuntimeError(f"Неполная структура таблицы bookings: отсутствуют колонки {sorted(missing_columns)}")


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1. Основная транзакция: таблицы, колонки, бэкфилл интервалов
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                user_name TEXT,
                phone TEXT,
                service TEXT,
                date TEXT,
                time TEXT,
                status TEXT DEFAULT 'confirmed',
                duration_minutes INTEGER DEFAULT 60,
                start_minutes INTEGER,
                end_minutes INTEGER
            )
        """)
        cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS duration_minutes INTEGER DEFAULT 60")
        cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS start_minutes INTEGER")
        cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS end_minutes INTEGER")

        cur.execute("""
            SELECT id, service, time, duration_minutes, start_minutes, end_minutes
            FROM bookings
        """)
        existing_rows = cur.fetchall()
        for row_id, service_name, time_str, stored_dur, s_min, e_min in existing_rows:
            duration_val = get_service_duration(service_name, stored_dur)
            ok, mins, _ = parse_user_time(time_str or "")
            new_start = s_min if s_min is not None else (mins if ok else None)
            new_end = e_min if e_min is not None else (new_start + duration_val if new_start is not None else None)
            if (
                stored_dur != duration_val
                or new_start != s_min
                or new_end != e_min
            ):
                cur.execute(
                    """
                    UPDATE bookings
                    SET duration_minutes = %s, start_minutes = %s, end_minutes = %s
                    WHERE id = %s
                    """,
                    (duration_val, new_start, new_end, row_id)
                )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS callback_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                user_name TEXT,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                desired_date TEXT NOT NULL,
                desired_time TEXT NOT NULL,
                status TEXT DEFAULT 'pending_callback',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # 2. Отдельная транзакция / SAVEPOINT для GiST-ограничения, чтобы ошибка не затрагивала структуру БД
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
            cur.execute("SAVEPOINT gist_constraint_sp")
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'bookings_no_overlap_confirmed'
                    ) THEN
                        ALTER TABLE bookings
                        ADD CONSTRAINT bookings_no_overlap_confirmed
                        EXCLUDE USING gist (
                            date WITH =,
                            int4range(start_minutes, end_minutes, '[)') WITH &&
                        )
                        WHERE (status = 'confirmed' AND start_minutes IS NOT NULL AND end_minutes IS NOT NULL);
                    END IF;
                END$$;
            """)
            cur.execute("RELEASE SAVEPOINT gist_constraint_sp")
            conn.commit()
        except Exception as e:
            logging.warning(f"Не удалось создать GIST constraint (проверка пересечений в транзакции активна): {e}")
            try:
                cur.execute("ROLLBACK TO SAVEPOINT gist_constraint_sp")
            except Exception:
                conn.rollback()
            conn.commit()

        # 3. Финальная проверка целостности структуры БД
        verify_db_schema(cur)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def get_booked_intervals(cur, date_str: str) -> list[tuple[int, int]]:
    cur.execute(
        """
        SELECT service, time, duration_minutes, start_minutes, end_minutes
        FROM bookings
        WHERE date = %s AND status = 'confirmed'
        """,
        (date_str,)
    )
    intervals = []
    for service_name, time_str, dur, s_min, e_min in cur.fetchall():
        if s_min is not None and e_min is not None:
            intervals.append((int(s_min), int(e_min)))
        else:
            ok, mins, _ = parse_user_time(time_str or "")
            if ok and mins is not None:
                duration_val = get_service_duration(service_name, dur)
                intervals.append((mins, mins + duration_val))
    return intervals


def has_interval_conflict(intervals: list[tuple[int, int]], start_m: int, end_m: int) -> bool:
    for b_start, b_end in intervals:
        if start_m < b_end and end_m > b_start:
            return True
    return False


def find_nearest_free_times(date_str: str, duration_minutes: int, desired_minutes: int, limit: int = 4) -> list[str]:
    conn = get_db_connection()
    cur = conn.cursor()
    intervals = get_booked_intervals(cur, date_str)
    cur.close()
    conn.close()

    candidates = []
    step = 15
    for m in range(WORK_START_MINUTES, WORK_END_MINUTES - duration_minutes + 1, step):
        if is_time_in_past(date_str, m):
            continue
        if not has_interval_conflict(intervals, m, m + duration_minutes):
            candidates.append((abs(m - desired_minutes), m))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [format_minutes_to_hhmm(m) for _, m in candidates[:limit]]


def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💇‍♂️ Записаться"), KeyboardButton(text="📋 Мои записи")],
            [KeyboardButton(text="📍 Адрес и контакты"), KeyboardButton(text="ℹ️ О нас")]
        ],
        resize_keyboard=True
    )


def get_phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📲 Отправить мой номер телефона", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, username, full_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (message.from_user.id, message.from_user.username, message.from_user.full_name)
    )
    conn.commit()
    cur.close()
    conn.close()

    await message.answer(
        "👋 Привет! Добро пожаловать в наш барбершоп.\nВыберите нужный раздел в меню ниже:",
        reply_markup=get_main_menu()
    )


@dp.message(F.text == "📍 Адрес и контакты")
async def cmd_contacts(message: types.Message):
    start_str = format_minutes_to_hhmm(WORK_START_MINUTES)
    end_str = format_minutes_to_hhmm(WORK_END_MINUTES)
    await message.answer(
        f"📍 **Наш адрес:** г. Ош\n"
        f"📞 **Телефон:** +996 (XX) XX-XX-XX\n"
        f"⏰ **График работы:** Каждый день с {start_str} до {end_str} (Asia/Bishkek)",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )


@dp.message(F.text == "ℹ️ О нас")
async def cmd_about(message: types.Message):
    await message.answer(
        "✂️ Мы — лучший барбершоп в городе. Стильные стрижки, борода, качественный сервис и крутая атмосфера!",
        reply_markup=get_main_menu()
    )


@dp.message(F.text == "📋 Мои записи")
async def cmd_my_bookings(message: types.Message):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, service, date, time FROM bookings WHERE user_id = %s AND status = 'confirmed' ORDER BY id DESC",
        (message.from_user.id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        await message.answer("У вас пока нет активных записей.", reply_markup=get_main_menu())
        return

    for row in rows:
        booking_id, service, date_str, time_str = row
        text = (
            f"📋 **Ваша запись:**\n"
            f"✂️ Услуга: {service}\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {time_str}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel_{booking_id}")]
        ])
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cancel_"))
async def process_cancel(callback: types.CallbackQuery):
    try:
        booking_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE bookings
        SET status = 'cancelled'
        WHERE id = %s AND user_id = %s AND status = 'confirmed'
        RETURNING user_id, service, date, time
        """,
        (booking_id, callback.from_user.id)
    )
    cancelled_booking = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not cancelled_booking:
        await callback.answer(
            "❌ Не удалось отменить запись: она не найдена, уже отменена или принадлежит другому пользователю.",
            show_alert=True
        )
        return

    user_id, service, date_str, time_str = cancelled_booking

    await callback.message.edit_text(f"❌ Запись на {date_str} в {time_str} успешно отменена.")
    await callback.answer("Запись отменена.")

    try:
        admin_text = (
            f"⚠️ **ЗАПИСЬ ОТМЕНЕНА!**\n"
            f"👤 Клиент ID: `{user_id}`\n"
            f"✂️ Услуга: {service}\n"
            f"📅 Дата: {date_str}\n"
            f"🕒 Время: {time_str}\n"
            f"❌ Статус: Отменено"
        )
        await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")


@dp.message(F.text == "💇‍♂️ Записаться")
async def cmd_book(message: types.Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✂️ {SERVICE_CONFIG['srv_haircut']['name']} — {SERVICE_CONFIG['srv_haircut']['price']} сом ({SERVICE_CONFIG['srv_haircut']['duration']} мин)",
            callback_data="srv_haircut"
        )],
        [InlineKeyboardButton(
            text=f"🧔 {SERVICE_CONFIG['srv_beard']['name']} — {SERVICE_CONFIG['srv_beard']['price']} сом ({SERVICE_CONFIG['srv_beard']['duration']} мин)",
            callback_data="srv_beard"
        )],
        [InlineKeyboardButton(
            text=f"🔥 {SERVICE_CONFIG['srv_complex']['name']} — {SERVICE_CONFIG['srv_complex']['price']} сом ({SERVICE_CONFIG['srv_complex']['duration']} мин)",
            callback_data="srv_complex"
        )]
    ])
    await message.answer("Выберите услугу:", reply_markup=keyboard)
    await state.set_state(BookingState.waiting_for_service)


@dp.callback_query(BookingState.waiting_for_service, F.data.startswith("srv_"))
async def process_service(callback: types.CallbackQuery, state: FSMContext):
    cfg = SERVICE_CONFIG.get(callback.data, SERVICE_CONFIG["srv_haircut"])
    await state.update_data(service=cfg["name"], duration_minutes=cfg["duration"])

    now_bishkek = datetime.now(BISHKEK_TZ)
    keyboard_buttons = []
    for i in range(7):
        target_date = now_bishkek + timedelta(days=i)
        day_name = DAYS_RU[target_date.weekday()]
        month_name = MONTHS_RU[target_date.month - 1]
        date_str = f"{target_date.day} {month_name} {target_date.year}"
        btn_text = f"{target_date.day} {month_name} ({day_name})"
        keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"date_{date_str}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.edit_text("📅 Выберите удобную дату:", reply_markup=keyboard)
    await state.set_state(BookingState.waiting_for_date)
    await callback.answer()


@dp.callback_query(BookingState.waiting_for_date, F.data.startswith("date_"))
async def process_date(callback: types.CallbackQuery, state: FSMContext):
    selected_date = callback.data.replace("date_", "")
    if is_past_date_str(selected_date):
        await callback.answer("❌ Нельзя выбрать прошедшую дату.", show_alert=True)
        return

    data = await state.get_data()
    duration_minutes = get_service_duration(data.get("service"), data.get("duration_minutes"))
    await state.update_data(date=selected_date, duration_minutes=duration_minutes)

    conn = get_db_connection()
    cur = conn.cursor()
    intervals = get_booked_intervals(cur, selected_date)
    cur.close()
    conn.close()

    time_buttons = []
    for slot_minutes in range(WORK_START_MINUTES, WORK_END_MINUTES, SLOT_STEP_MINUTES):
        time_slot = format_minutes_to_hhmm(slot_minutes)
        if is_time_in_past(selected_date, slot_minutes):
            continue

        fits_before_close = (slot_minutes + duration_minutes) <= WORK_END_MINUTES
        is_busy = (not fits_before_close) or has_interval_conflict(intervals, slot_minutes, slot_minutes + duration_minutes)

        if is_busy:
            btn_text = f"🔴 {time_slot} (Занято)"
            time_buttons.append([InlineKeyboardButton(text=btn_text, callback_data="booked_slot")])
        else:
            btn_text = f"🟢 {time_slot} (Свободно)"
            time_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"time_{time_slot}")])

    time_buttons.append([InlineKeyboardButton(text="🕒 Выбрать своё время", callback_data="custom_time")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=time_buttons)
    await callback.message.edit_text(
        f"🕒 Выбранная дата: **{selected_date}**\n"
        f"⏱ Длительность услуги: **{duration_minutes} мин**\n"
        f"Выберите доступное время или укажите своё:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(BookingState.waiting_for_time)
    await callback.answer()


@dp.callback_query(F.data == "booked_slot")
async def alert_booked(callback: types.CallbackQuery):
    await callback.answer("❌ Это время уже занято или услуга не успевает завершиться до закрытия.", show_alert=True)


@dp.callback_query(BookingState.waiting_for_time, F.data == "custom_time")
async def process_custom_time_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "☺️ Укажите удобное для вас время. Например: 11:15 или 13:45"
    )
    await state.set_state(BookingState.waiting_for_custom_time)
    await callback.answer()


async def start_after_hours_callback(message: types.Message, state: FSMContext, desired_time_str: str):
    data = await state.get_data()
    await state.update_data(
        desired_time=desired_time_str,
        desired_date=data.get("date"),
        service=data.get("service", "Мужская стрижка"),
        callback_submitted=False
    )
    await message.answer(
        "☺️ К сожалению, в выбранное время наш барбершоп уже закрыт.\n"
        "Но мы с радостью поможем вам подобрать удобное время! ✂️\n"
        "📞 Оставьте свой номер телефона, и наш администратор свяжется с вами в ближайший рабочий день для уточнения записи.\n"
        "Спасибо, что выбираете нас! 💈👍🏻",
        reply_markup=get_phone_keyboard()
    )
    await state.set_state(BookingState.waiting_for_callback_phone)


async def offer_late_service_options(message: types.Message, state: FSMContext, selected_date: str, start_minutes: int, formatted_time: str, duration_minutes: int):
    close_str = format_minutes_to_hhmm(WORK_END_MINUTES)
    nearest = find_nearest_free_times(selected_date, duration_minutes, start_minutes)
    hint_text = f"\nБлижайшие свободные слоты: {', '.join(nearest)}." if nearest else ""

    await state.update_data(
        desired_date=selected_date,
        desired_time=formatted_time,
        duration_minutes=duration_minutes
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Оставить номер для обратного звонка", callback_data="late_callback")],
        [InlineKeyboardButton(text="🕒 Выбрать другое время", callback_data="late_other_time")]
    ])
    await message.answer(
        f"⚠️ Услуга длится {duration_minutes} мин и не успеет завершиться до закрытия ({close_str}).{hint_text}\n"
        f"Как вам будет удобнее поступить?",
        reply_markup=keyboard
    )
    await state.set_state(BookingState.waiting_for_late_choice)


@dp.callback_query(BookingState.waiting_for_late_choice, F.data == "late_callback")
async def process_late_callback_choice(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    desired_time = data.get("desired_time") or data.get("time") or ""
    await callback.answer()
    await start_after_hours_callback(callback.message, state, desired_time)


@dp.callback_query(BookingState.waiting_for_late_choice, F.data == "late_other_time")
async def process_late_other_time_choice(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "☺️ Укажите удобное для вас время. Например: 11:15 или 13:45"
    )
    await state.set_state(BookingState.waiting_for_custom_time)
    await callback.answer()


@dp.message(BookingState.waiting_for_custom_time)
async def process_custom_time_input(message: types.Message, state: FSMContext):
    is_valid, start_minutes, formatted_time = parse_user_time(message.text or "")
    if not is_valid or start_minutes is None or formatted_time is None:
        await message.answer(
            "❌ Неверный формат времени. Пожалуйста, укажите время в формате ЧЧ:ММ — например: 11:15, 13:45, 16:30 или 19:20 (также подойдут 11.15 или 13 45)."
        )
        return

    data = await state.get_data()
    selected_date = data.get("date")
    duration_minutes = get_service_duration(data.get("service"), data.get("duration_minutes"))

    if not selected_date or is_past_date_str(selected_date):
        await message.answer("❌ Выбранная дата уже прошла. Начните запись заново через «💇‍♂️ Записаться».", reply_markup=get_main_menu())
        await state.clear()
        return

    if is_time_in_past(selected_date, start_minutes):
        await message.answer("❌ Выбранное время уже прошло. Укажите более позднее время, например 11:15 или 13:45.")
        return

    # Проверка графика работы (10:00 - 21:00 по Asia/Bishkek)
    if start_minutes < WORK_START_MINUTES or start_minutes >= WORK_END_MINUTES:
        await start_after_hours_callback(message, state, formatted_time)
        return

    end_minutes = start_minutes + duration_minutes
    if end_minutes > WORK_END_MINUTES:
        await offer_late_service_options(message, state, selected_date, start_minutes, formatted_time, duration_minutes)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    intervals = get_booked_intervals(cur, selected_date)
    cur.close()
    conn.close()

    if has_interval_conflict(intervals, start_minutes, end_minutes):
        nearest = find_nearest_free_times(selected_date, duration_minutes, start_minutes)
        hint = f"\nБлижайшие свободные варианты: {', '.join(nearest)}." if nearest else ""
        await message.answer(
            f"❌ Выбранное время {formatted_time} занято или пересекается с другой записью.{hint}\n"
            f"Укажите другое время или нажмите /start для сброса."
        )
        return

    await state.update_data(
        time=formatted_time,
        start_minutes=start_minutes,
        end_minutes=end_minutes,
        duration_minutes=duration_minutes
    )
    await message.answer(
        "📱 Для подтверждения записи поделитесь своим номером телефона.",
        reply_markup=get_phone_keyboard()
    )
    await state.set_state(BookingState.waiting_for_phone)


@dp.callback_query(BookingState.waiting_for_time, F.data.startswith("time_"))
async def process_time(callback: types.CallbackQuery, state: FSMContext):
    selected_time = callback.data.replace("time_", "")
    is_valid, start_minutes, formatted_time = parse_user_time(selected_time)
    if not is_valid or start_minutes is None or formatted_time is None:
        await callback.answer("Некорректное время.", show_alert=True)
        return

    data = await state.get_data()
    selected_date = data.get("date")
    duration_minutes = get_service_duration(data.get("service"), data.get("duration_minutes"))

    if not selected_date or is_past_date_str(selected_date) or is_time_in_past(selected_date, start_minutes):
        await callback.answer("❌ Это время уже прошло.", show_alert=True)
        return

    if start_minutes < WORK_START_MINUTES or start_minutes >= WORK_END_MINUTES:
        await state.update_data(time=formatted_time)
        await start_after_hours_callback(callback.message, state, formatted_time)
        await callback.answer()
        return

    end_minutes = start_minutes + duration_minutes
    if end_minutes > WORK_END_MINUTES:
        await callback.answer()
        await offer_late_service_options(callback.message, state, selected_date, start_minutes, formatted_time, duration_minutes)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    intervals = get_booked_intervals(cur, selected_date)
    cur.close()
    conn.close()

    if has_interval_conflict(intervals, start_minutes, end_minutes):
        await callback.answer("❌ Это время уже занято.", show_alert=True)
        return

    await state.update_data(
        time=formatted_time,
        start_minutes=start_minutes,
        end_minutes=end_minutes,
        duration_minutes=duration_minutes
    )
    await callback.message.edit_text(f"🕒 Выбрано время: **{formatted_time}**", parse_mode="Markdown")
    await callback.message.answer(
        "📱 Для подтверждения записи поделитесь своим номером телефона.",
        reply_markup=get_phone_keyboard()
    )
    await state.set_state(BookingState.waiting_for_phone)
    await callback.answer()


@dp.message(BookingState.waiting_for_callback_phone)
async def process_callback_phone(message: types.Message, state: FSMContext):
    if not message.contact:
        await message.answer(
            "⚠️ Пожалуйста, воспользуйтесь кнопкой «📲 Отправить мой номер телефона» ниже, чтобы поделиться своим номером.",
            reply_markup=get_phone_keyboard()
        )
        return

    if message.contact.user_id != message.from_user.id:
        await message.answer(
            "⚠️ Отправлен чужой контакт. Пожалуйста, отправьте именно свой номер телефона через кнопку ниже.",
            reply_markup=get_phone_keyboard()
        )
        return

    data = await state.get_data()
    if data.get("callback_submitted"):
        return
    await state.update_data(callback_submitted=True)

    phone = message.contact.phone_number
    desired_date = data.get("desired_date") or data.get("date") or get_bishkek_today_str()
    desired_time = data.get("desired_time") or data.get("time") or "22:00"
    service = data.get("service") or "Мужская стрижка"
    client_name = message.from_user.full_name or "Клиент"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO callback_requests (user_id, user_name, phone, service, desired_date, desired_time, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending_callback')
        """,
        (message.from_user.id, client_name, phone, service, desired_date, desired_time)
    )
    conn.commit()
    cur.close()
    conn.close()

    await message.answer(
        "✅ Спасибо! Ваша заявка получена.\n"
        "Наш администратор свяжется с вами в ближайший рабочий день. Хорошего дня! ☺️",
        reply_markup=get_main_menu()
    )

    admin_text = (
        f"🔔 НОВАЯ ЗАЯВКА НА ОБРАТНЫЙ ЗВОНОК!\n"
        f"👤 Клиент: {client_name}\n"
        f"📞 Телефон: {phone}\n"
        f"📅 Желаемая дата: {desired_date}\n"
        f"🕒 Желаемое время: {desired_time}\n"
        f"✂️ Услуга: {service}\n"
        f"🟡 Статус: Ожидает согласования"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text)
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу о заявке на обратный звонок: {e}")

    await state.clear()


@dp.message(BookingState.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    if not message.contact:
        await message.answer(
            "⚠️ Пожалуйста, воспользуйтесь кнопкой «📲 Отправить мой номер телефона» ниже, чтобы поделиться своим номером.",
            reply_markup=get_phone_keyboard()
        )
        return

    if message.contact.user_id != message.from_user.id:
        await message.answer(
            "⚠️ Отправлен чужой контакт. Пожалуйста, отправьте именно свой номер телефона через кнопку ниже.",
            reply_markup=get_phone_keyboard()
        )
        return

    data = await state.get_data()
    if data.get("booking_submitted"):
        return

    phone = message.contact.phone_number
    selected_date = data.get("date")
    selected_time = data.get("time")
    selected_service = data.get("service")
    duration_minutes = get_service_duration(selected_service, data.get("duration_minutes"))

    ok, start_minutes, formatted_time = parse_user_time(selected_time or "")
    if not ok or start_minutes is None or formatted_time is None or not selected_date:
        await message.answer("⚠️ Ошибка данных бронирования. Начните запись заново.", reply_markup=get_main_menu())
        await state.clear()
        return

    end_minutes = start_minutes + duration_minutes

    if is_past_date_str(selected_date) or is_time_in_past(selected_date, start_minutes):
        await message.answer("⚠️ Выбранное время уже прошло. Пожалуйста, выберите другое время.", reply_markup=get_main_menu())
        await state.clear()
        return

    if start_minutes < WORK_START_MINUTES or start_minutes >= WORK_END_MINUTES:
        await start_after_hours_callback(message, state, formatted_time)
        return

    if end_minutes > WORK_END_MINUTES:
        await offer_late_service_options(message, state, selected_date, start_minutes, formatted_time, duration_minutes)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute("LOCK TABLE bookings IN SHARE ROW EXCLUSIVE MODE")
        intervals = get_booked_intervals(cur, selected_date)

        if has_interval_conflict(intervals, start_minutes, end_minutes):
            conn.rollback()
            cur.close()
            conn.close()
            nearest = find_nearest_free_times(selected_date, duration_minutes, start_minutes)
            hint = f"\nБлижайшие свободные варианты: {', '.join(nearest)}." if nearest else ""
            await message.answer(
                f"⚠️ К сожалению, это время только что заняли.{hint}\n"
                f"Пожалуйста, выберите другое время.",
                reply_markup=get_main_menu()
            )
            await state.clear()
            return

        cur.execute(
            """
            INSERT INTO bookings (
                user_id, user_name, phone, service, date, time, status,
                duration_minutes, start_minutes, end_minutes
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'confirmed', %s, %s, %s)
            """,
            (
                message.from_user.id,
                message.from_user.full_name,
                phone,
                selected_service,
                selected_date,
                formatted_time,
                duration_minutes,
                start_minutes,
                end_minutes,
            )
        )
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        cur.close()
        conn.close()
        logging.error(f"Ошибка при сохранении брони в PostgreSQL: {e}")
        nearest = find_nearest_free_times(selected_date, duration_minutes, start_minutes)
        hint = f"\nБлижайшие свободные варианты: {', '.join(nearest)}." if nearest else ""
        await message.answer(
            f"⚠️ Не удалось подтвердить бронь — время уже занято.{hint}",
            reply_markup=get_main_menu()
        )
        await state.clear()
        return

    cur.close()
    conn.close()
    await state.update_data(booking_submitted=True)

    success_text = (
        f"✅ **Вы успешно записаны!**\n\n"
        f"📅 Дата: {selected_date}\n"
        f"🕒 Время: {formatted_time} ({duration_minutes} мин)\n"
        f"✂️ Услуга: {selected_service}\n"
        f"📞 Телефон: {phone}\n"
        f"📍 Адрес барбершопа: г. Ош"
    )
    await message.answer(success_text, reply_markup=get_main_menu(), parse_mode="Markdown")

    username_info = f"@{message.from_user.username}" if message.from_user.username else "нет username"
    admin_text = (
        f"🔔 **НОВАЯ ЗАПИСЬ!**\n"
        f"👤 Клиент: {message.from_user.full_name}\n"
        f"📱 Telegram: {username_info} (ID: `{message.from_user.id}`)\n"
        f"📞 Телефон: {phone}\n"
        f"✂️ Услуга: {selected_service}\n"
        f"📅 Дата: {selected_date}\n"
        f"🕒 Время: {formatted_time} ({duration_minutes} мин)\n"
        f"💈 Барбер: Основной зал\n"
        f"✅ Статус: Подтверждено"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")

    await state.clear()


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
