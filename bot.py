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

ACTIVE_BOOKING_STATUSES = ("pending", "confirmed")

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

    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'callback_requests'
          AND column_name IN ('id', 'user_id', 'phone', 'service', 'desired_date', 'desired_time', 'status')
    """)
    cb_columns = {row[0] for row in cur.fetchall()}
    required_cb_columns = {"id", "user_id", "phone", "service", "desired_date", "desired_time", "status"}
    missing_cb_columns = required_cb_columns - cb_columns
    if missing_cb_columns:
        raise RuntimeError(f"Неполная структура таблицы callback_requests: отсутствуют колонки {sorted(missing_cb_columns)}")


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1. Основная транзакция: таблицы, колонки, безопасный бэкфилл интервалов без удаления данных
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
                status TEXT DEFAULT 'pending',
                duration_minutes INTEGER DEFAULT 60,
                start_minutes INTEGER,
                end_minutes INTEGER
            )
        """)
        cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS duration_minutes INTEGER DEFAULT 60")
        cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS start_minutes INTEGER")
        cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS end_minutes INTEGER")
        cur.execute("ALTER TABLE bookings ALTER COLUMN status SET DEFAULT 'pending'")

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
        cur.execute("ALTER TABLE callback_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP")
        conn.commit()

        # 2. Проверка существующих ограничений и обновление GiST-ограничения для status IN ('pending', 'confirmed')
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
            cur.execute("SAVEPOINT gist_constraint_sp")

            # Проверяем существующее определение ограничения
            cur.execute("""
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND t.relname = 'bookings'
                  AND c.conname = 'bookings_no_overlap_confirmed'
            """)
            old_def_row = cur.fetchone()
            needs_recreate = True
            if old_def_row and old_def_row[0]:
                def_text = old_def_row[0]
                if "pending" in def_text and "confirmed" in def_text:
                    needs_recreate = False

            if needs_recreate:
                # Проверяем, нет ли в базе уже конфликтующих активных (pending/confirmed) записей перед созданием ограничения
                cur.execute("""
                    SELECT b1.id, b2.id
                    FROM bookings b1
                    JOIN bookings b2
                      ON b1.id < b2.id
                     AND b1.date = b2.date
                     AND b1.status IN ('pending', 'confirmed')
                     AND b2.status IN ('pending', 'confirmed')
                     AND b1.start_minutes IS NOT NULL
                     AND b1.end_minutes IS NOT NULL
                     AND b2.start_minutes IS NOT NULL
                     AND b2.end_minutes IS NOT NULL
                     AND b1.start_minutes < b2.end_minutes
                     AND b1.end_minutes > b2.start_minutes
                    LIMIT 1
                """)
                conflict_pair = cur.fetchone()
                if conflict_pair:
                    logging.warning(
                        f"Обнаружены существующие пересекающиеся записи {conflict_pair}; "
                        f"данные сохранены без удаления, GiST-ограничение не пересоздано."
                    )
                else:
                    if old_def_row:
                        cur.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_no_overlap_confirmed")
                    cur.execute("""
                        ALTER TABLE bookings
                        ADD CONSTRAINT bookings_no_overlap_confirmed
                        EXCLUDE USING gist (
                            date WITH =,
                            int4range(start_minutes, end_minutes, '[)') WITH &&
                        )
                        WHERE (
                            status IN ('pending', 'confirmed')
                            AND start_minutes IS NOT NULL
                            AND end_minutes IS NOT NULL
                        )
                    """)
            cur.execute("RELEASE SAVEPOINT gist_constraint_sp")
            conn.commit()
        except Exception as e:
            logging.warning(f"Не удалось обновить GIST constraint (проверка пересечений в транзакции активна): {e}")
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
        WHERE date = %s AND status IN ('pending', 'confirmed')
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
        """
        SELECT id, service, date, time, status
        FROM bookings
        WHERE user_id = %s AND status IN ('pending', 'confirmed')
        ORDER BY id DESC
        """,
        (message.from_user.id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        await message.answer("У вас пока нет активных или ожидающих записей.", reply_markup=get_main_menu())
        return

    status_labels = {
        "pending": "🕒 Ожидает подтверждения администратора",
        "confirmed": "✅ Подтверждена"
    }

    for row in rows:
        booking_id, service, date_str, time_str, status = row
        status_text = status_labels.get(status, status)
        cancel_btn_text = "❌ Отменить заявку" if status == "pending" else "❌ Отменить запись"
        text = (
            f"📋 **Ваша запись:**\n"
            f"✂️ Услуга: {service}\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {time_str}\n"
            f"📌 Статус: {status_text}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=cancel_btn_text, callback_data=f"cancel_{booking_id}")]
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
        WHERE id = %s AND user_id = %s AND status IN ('pending', 'confirmed')
        RETURNING user_id, service, date, time, status
        """,
        (booking_id, callback.from_user.id)
    )
    cancelled_booking = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not cancelled_booking:
        await callback.answer(
            "❌ Не удалось отменить запись: она не найдена, уже отменена/отклонена или принадлежит другому пользователю.",
            show_alert=True
        )
        return

    user_id, service, date_str, time_str, _ = cancelled_booking

    await callback.message.edit_text(f"❌ Запись на {date_str} в {time_str} успешно отменена.")
    await callback.answer("Запись отменена.")

    try:
        admin_text = (
            f"⚠️ **ЗАПИСЬ ОТМЕНЕНА КЛИЕНТОМ!**\n"
            f"🆔 Заявка №{booking_id}\n"
            f"👤 Клиент ID: `{user_id}`\n"
            f"✂️ Услуга: {service}\n"
            f"📅 Дата: {date_str}\n"
            f"🕒 Время: {time_str}\n"
            f"❌ Статус: Отменено клиентом"
        )
        await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")


@dp.callback_query(F.data.startswith("admin_approve_"))
async def process_admin_approve(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав для управления заявками.", show_alert=True)
        return

    try:
        booking_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор заявки.", show_alert=True)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute(
            """
            UPDATE bookings
            SET status = 'confirmed'
            WHERE id = %s AND status = 'pending'
            RETURNING user_id, user_name, phone, service, date, time, duration_minutes
            """,
            (booking_id,)
        )
        approved_row = cur.fetchone()

        if not approved_row:
            cur.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
            current_row = cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            current_status = current_row[0] if current_row else "не найдена"
            await callback.answer(f"ℹ️ Заявка уже обработана (статус: {current_status}).", show_alert=True)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        logging.error(f"Ошибка при одобрении заявки #{booking_id}: {e}")
        await callback.answer("⚠️ Ошибка базы данных при одобрении заявки.", show_alert=True)
        return

    cur.close()
    conn.close()

    user_id, user_name, phone, service, date_str, time_str, duration_minutes = approved_row
    dur_val = duration_minutes or get_service_duration(service)

    updated_admin_text = (
        f"✅ **ЗАЯВКА ОДОБРЕНА!**\n"
        f"🆔 Заявка №{booking_id}\n"
        f"👤 Клиент: {user_name or 'Клиент'} (ID: `{user_id}`)\n"
        f"📞 Телефон: {phone}\n"
        f"✂️ Услуга: {service}\n"
        f"📅 Дата: {date_str}\n"
        f"🕒 Время: {time_str} ({dur_val} мин)\n"
        f"🟢 Статус: Подтверждено"
    )
    try:
        await callback.message.edit_text(updated_admin_text, parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer("✅ Запись одобрена!")

    client_text = (
        f"✅ **Ваша запись подтверждена администратором!**\n\n"
        f"📅 Дата: {date_str}\n"
        f"🕒 Время: {time_str} ({dur_val} мин)\n"
        f"✂️ Услуга: {service}\n"
        f"📍 Адрес барбершопа: г. Ош\n\n"
        f"Ждём вас! 💈"
    )
    try:
        await bot.send_message(user_id, client_text, reply_markup=get_main_menu(), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить подтверждение клиенту {user_id}: {e}")


@dp.callback_query(F.data.startswith("admin_reject_"))
async def process_admin_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав для управления заявками.", show_alert=True)
        return

    try:
        booking_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор заявки.", show_alert=True)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute(
            """
            UPDATE bookings
            SET status = 'rejected'
            WHERE id = %s AND status = 'pending'
            RETURNING user_id, user_name, phone, service, date, time, duration_minutes
            """,
            (booking_id,)
        )
        rejected_row = cur.fetchone()

        if not rejected_row:
            cur.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
            current_row = cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            current_status = current_row[0] if current_row else "не найдена"
            await callback.answer(f"ℹ️ Заявка уже обработана (статус: {current_status}).", show_alert=True)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        logging.error(f"Ошибка при отклонении заявки #{booking_id}: {e}")
        await callback.answer("⚠️ Ошибка базы данных при отклонении заявки.", show_alert=True)
        return

    cur.close()
    conn.close()

    user_id, user_name, phone, service, date_str, time_str, duration_minutes = rejected_row
    dur_val = duration_minutes or get_service_duration(service)

    updated_admin_text = (
        f"❌ **ЗАЯВКА ОТКЛОНЕНА!**\n"
        f"🆔 Заявка №{booking_id}\n"
        f"👤 Клиент: {user_name or 'Клиент'} (ID: `{user_id}`)\n"
        f"📞 Телефон: {phone}\n"
        f"✂️ Услуга: {service}\n"
        f"📅 Дата: {date_str}\n"
        f"🕒 Время: {time_str} ({dur_val} мин)\n"
        f"🔴 Статус: Отклонено (слот освобождён)"
    )
    try:
        await callback.message.edit_text(updated_admin_text, parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer("❌ Заявка отклонена, время освобождено.")

    client_text = (
        f"😔 К сожалению, администратор не смог подтвердить вашу запись на **{date_str} в {time_str}** ({service}).\n\n"
        f"Пожалуйста, выберите другое удобное время через кнопку «💇‍♂️ Записаться» — мы с радостью вас примем! ☺️"
    )
    try:
        await bot.send_message(user_id, client_text, reply_markup=get_main_menu(), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление об отклонении клиенту {user_id}: {e}")


@dp.callback_query(F.data.startswith("cb_contacted_"))
async def process_callback_contacted(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав для управления заявками.", show_alert=True)
        return

    try:
        cb_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор заявки.", show_alert=True)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE callback_requests
        SET status = 'contacted', updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'pending_callback'
        RETURNING user_name, phone, service, desired_date, desired_time
        """,
        (cb_id,)
    )
    updated_row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not updated_row:
        await callback.answer("ℹ️ Эта заявка на обратный звонок уже обработана.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    client_name, phone, service, desired_date, desired_time = updated_row
    new_text = (
        f"📞 ЗАЯВКА НА ОБРАТНЫЙ ЗВОНОК — СВЯЗАЛИСЬ!\n"
        f"🆔 Заявка №{cb_id}\n"
        f"👤 Клиент: {client_name or 'Клиент'}\n"
        f"📞 Телефон: {phone}\n"
        f"📅 Желаемая дата: {desired_date}\n"
        f"🕒 Желаемое время: {desired_time}\n"
        f"✂️ Услуга: {service}\n"
        f"🟢 Статус: Связался"
    )
    try:
        await callback.message.edit_text(new_text)
    except Exception:
        pass
    await callback.answer("📞 Статус обновлён: Связался")


@dp.callback_query(F.data.startswith("cb_close_"))
async def process_callback_close(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав для управления заявками.", show_alert=True)
        return

    try:
        cb_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор заявки.", show_alert=True)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE callback_requests
        SET status = 'closed', updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status IN ('pending_callback', 'contacted')
        RETURNING user_name, phone, service, desired_date, desired_time
        """,
        (cb_id,)
    )
    updated_row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not updated_row:
        await callback.answer("ℹ️ Эта заявка на обратный звонок уже закрыта.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    client_name, phone, service, desired_date, desired_time = updated_row
    new_text = (
        f"❌ ЗАЯВКА НА ОБРАТНЫЙ ЗВОНОК — ЗАКРЫТА\n"
        f"🆔 Заявка №{cb_id}\n"
        f"👤 Клиент: {client_name or 'Клиент'}\n"
        f"📞 Телефон: {phone}\n"
        f"📅 Желаемая дата: {desired_date}\n"
        f"🕒 Желаемое время: {desired_time}\n"
        f"✂️ Услуга: {service}\n"
        f"⚫️ Статус: Закрыта"
    )
    try:
        await callback.message.edit_text(new_text)
    except Exception:
        pass
    await callback.answer("❌ Заявка закрыта")


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
        RETURNING id
        """,
        (message.from_user.id, client_name, phone, service, desired_date, desired_time)
    )
    cb_row = cur.fetchone()
    cb_id = cb_row[0] if cb_row else 0
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
        f"🆔 Заявка №{cb_id}\n"
        f"👤 Клиент: {client_name}\n"
        f"📞 Телефон: {phone}\n"
        f"📅 Желаемая дата: {desired_date}\n"
        f"🕒 Желаемое время: {desired_time}\n"
        f"✂️ Услуга: {service}\n"
        f"🟡 Статус: Ожидает согласования"
    )
    admin_cb_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📞 Связался", callback_data=f"cb_contacted_{cb_id}"),
            InlineKeyboardButton(text="❌ Закрыть заявку", callback_data=f"cb_close_{cb_id}")
        ]
    ])
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=admin_cb_keyboard)
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
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
            RETURNING id
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
        booking_row = cur.fetchone()
        booking_id = booking_row[0] if booking_row else 0
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        cur.close()
        conn.close()
        logging.error(f"Ошибка при сохранении заявки в PostgreSQL: {e}")
        nearest = find_nearest_free_times(selected_date, duration_minutes, start_minutes)
        hint = f"\nБлижайшие свободные варианты: {', '.join(nearest)}." if nearest else ""
        await message.answer(
            f"⚠️ Не удалось оформить заявку — время уже занято.{hint}",
            reply_markup=get_main_menu()
        )
        await state.clear()
        return

    cur.close()
    conn.close()
    await state.update_data(booking_submitted=True)

    await message.answer(
        "🕒 Ваша заявка получена и ожидает подтверждения администратора. Мы скоро сообщим результат! ☺️",
        reply_markup=get_main_menu()
    )

    username_info = f"@{message.from_user.username}" if message.from_user.username else "нет username"
    admin_text = (
        f"🔔 **НОВАЯ ЗАЯВКА НА ЗАПИСЬ!**\n"
        f"🆔 Заявка №{booking_id}\n"
        f"👤 Клиент: {message.from_user.full_name}\n"
        f"📱 Telegram: {username_info} (ID: `{message.from_user.id}`)\n"
        f"📞 Телефон: {phone}\n"
        f"✂️ Услуга: {selected_service}\n"
        f"📅 Дата: {selected_date}\n"
        f"🕒 Время: {formatted_time} ({duration_minutes} мин)\n"
        f"🟡 Статус: Ожидает подтверждения"
    )
    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"admin_approve_{booking_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_{booking_id}")
        ]
    ])
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=admin_keyboard, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")

    await state.clear()




# --- Панель владельца: только чтение данных и существующие действия ---
ADMIN_PAGE_SIZE = 8


def admin_panel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Записи на сегодня", callback_data="panel_today_0")],
        [InlineKeyboardButton(text="⏳ Ожидают решения", callback_data="panel_pending_0")],
        [InlineKeyboardButton(text="📋 Ближайшие записи", callback_data="panel_upcoming_0")],
        [InlineKeyboardButton(text="📞 Обратные звонки", callback_data="panel_calls_0")],
    ])


def admin_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В админ-меню", callback_data="panel_home")]
    ])


@dp.message(Command("admin"))
async def cmd_admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer("💈 Админ-панель\nВыберите раздел:", reply_markup=admin_panel_keyboard())


@dp.callback_query(F.data == "panel_home")
async def panel_home(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text("💈 Админ-панель\nВыберите раздел:", reply_markup=admin_panel_keyboard())
    await callback.answer()


def _panel_sort_key(row):
    day = parse_date_str(row[4])
    ok, minutes, _ = parse_user_time(row[5] or "")
    return (day or datetime.max.date(), minutes if ok else 9999, row[0])


@dp.callback_query(F.data.startswith("panel_"))
async def panel_section(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    parts = (callback.data or "").split("_")
    if len(parts) != 3 or parts[1] not in {"today", "pending", "upcoming", "calls"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    section = parts[1]
    try:
        page = int(parts[2])
        if not 0 <= page <= 10000:
            raise ValueError()
    except ValueError:
        await callback.answer("Неверная страница.", show_alert=True)
        return

    now = datetime.now(BISHKEK_TZ)
    today = now.date()
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if section == "calls":
                cur.execute("""SELECT id, user_name, phone, service, desired_date, desired_time, status
                               FROM callback_requests WHERE status = 'pending_callback' ORDER BY id DESC LIMIT 500""")
                rows = cur.fetchall()
            else:
                cur.execute("""SELECT id, user_name, phone, service, date, time, status
                               FROM bookings WHERE status IN ('pending', 'confirmed') ORDER BY id DESC LIMIT 2000""")
                rows = cur.fetchall()
    except Exception:
        logging.exception("Ошибка чтения админ-панели")
        await callback.answer("Ошибка базы данных. Попробуйте позже.", show_alert=True)
        return
    finally:
        if conn:
            conn.close()

    if section == "today":
        rows = [r for r in rows if parse_date_str(r[4]) == today]
        title = "📅 Записи на сегодня"
    elif section == "pending":
        rows = [r for r in rows if r[6] == "pending" and (parse_date_str(r[4]) or datetime.min.date()) >= today]
        title = "⏳ Ожидают решения"
    elif section == "upcoming":
        rows = [r for r in rows if r[6] == "confirmed" and (parse_date_str(r[4]) or datetime.min.date()) >= today and
                (parse_date_str(r[4]) > today or (parse_user_time(r[5] or "")[1] or 0) >= now.hour * 60 + now.minute)]
        title = "📋 Ближайшие записи"
    else:
        title = "📞 Обратные звонки"

    if section != "calls":
        rows.sort(key=_panel_sort_key)
    total = len(rows)
    start = page * ADMIN_PAGE_SIZE
    if start >= total and page > 0:
        await callback.answer("Больше записей нет.", show_alert=True)
        return
    selected = rows[start:start + ADMIN_PAGE_SIZE]
    lines = [f"{title} ({total})", f"Страница {page + 1} из {max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)}"]
    buttons = []
    for r in selected:
        row_id, name, phone, service, date_str, time_str, status = r
        lines.append(f"\n№{row_id} | {date_str}, {time_str}\n👤 {name or 'Клиент'} | 📞 {phone or 'нет номера'}\n✂️ {service} | {status}")
        if section == "pending":
            buttons.append([
                InlineKeyboardButton(text=f"✅ №{row_id}", callback_data=f"admin_approve_{row_id}"),
                InlineKeyboardButton(text=f"❌ №{row_id}", callback_data=f"admin_reject_{row_id}")
            ])
        elif section == "calls":
            buttons.append([
                InlineKeyboardButton(text=f"📞 Связался №{row_id}", callback_data=f"cb_contacted_{row_id}"),
                InlineKeyboardButton(text=f"❌ Закрыть №{row_id}", callback_data=f"cb_close_{row_id}")
            ])
    if not selected:
        lines.append("\nПока записей нет.")
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"panel_{section}_{page - 1}"))
    if start + ADMIN_PAGE_SIZE < total:
        navigation.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"panel_{section}_{page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton(text="🔙 В админ-меню", callback_data="panel_home")])
    # Ограничение Telegram 4096 символов; размер страницы небольшой, но поля могут быть длинными.
    output = "\n".join(lines)
    if len(output) > 3900:
        output = output[:3890] + "…"
    await callback.message.edit_text(output, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
