import asyncio
import os
import re
import json
import sqlite3
from io import BytesIO
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
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
    BufferedInputFile,
)

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER


# ============================================================
# HORIZONT AUTO
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN")

if not ADMIN_ID_RAW:
    raise RuntimeError("Не указан ADMIN_ID")

ADMIN_ID = int(ADMIN_ID_RAW)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB_NAME = "horizont_auto.db"


# ============================================================
# БАЗА
# ============================================================

def get_connection():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            created_at TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)

    # Добавляем статус, если база уже существовала
    cursor.execute("PRAGMA table_info(applications)")
    columns = [row[1] for row in cursor.fetchall()]

    if "status" not in columns:
        cursor.execute("""
            ALTER TABLE applications
            ADD COLUMN status TEXT DEFAULT 'new'
        """)

    conn.commit()
    conn.close()


def save_application(user, data):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO applications (
            user_id,
            username,
            full_name,
            created_at,
            data,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user.id,
        user.username or "",
        user.full_name or "",
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        json.dumps(data, ensure_ascii=False),
        "new"
    ))

    application_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return application_id


def get_application(application_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, user_id, username, full_name,
               created_at, data, status
        FROM applications
        WHERE id = ?
    """, (application_id,))

    row = cursor.fetchone()
    conn.close()

    return row


def get_user_applications(user_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, created_at, data, status
        FROM applications
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_all_applications(limit=50):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, user_id, full_name,
               created_at, data, status
        FROM applications
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_new_applications():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, user_id, full_name,
               created_at, data, status
        FROM applications
        WHERE status = 'new'
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()
    conn.close()

    return rows


def update_status(application_id, status):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE applications
        SET status = ?
        WHERE id = ?
    """, (status, application_id))

    conn.commit()
    conn.close()


def get_stats():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM applications")
    total = cursor.fetchone()[0]

    result = {"total": total}

    for status in ["new", "work", "done", "cancelled"]:
        cursor.execute(
            "SELECT COUNT(*) FROM applications WHERE status = ?",
            (status,)
        )
        result[status] = cursor.fetchone()[0]

    conn.close()

    return result


# ============================================================
# СТАТУСЫ
# ============================================================

STATUS_TEXT = {
    "new": "🔵 Новая",
    "work": "🟡 В работе",
    "done": "✅ Выполнена",
    "cancelled": "❌ Отменена",
}


def status_text(status):
    return STATUS_TEXT.get(status, status)


# ============================================================
# FSM
# ============================================================

class Form(StatesGroup):
    pickup_count = State()
    delivery_count = State()

    pickup_addresses = State()
    pickup_dates = State()

    delivery_addresses = State()
    delivery_dates = State()

    sender_names = State()
    sender_phones = State()

    receiver_names = State()
    receiver_phones = State()

    driver_note = State()
    files = State()
    confirmation = State()


class AdminSearch(StatesGroup):
    number = State()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚚 Создать заявку")],
            [KeyboardButton(text="📋 Мои заявки")]
        ],
        resize_keyboard=True
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📥 Новые заявки"),
                KeyboardButton(text="📋 Все заявки")
            ],
            [
                KeyboardButton(text="🔎 Найти заявку"),
                KeyboardButton(text="📄 Выгрузить PDF")
            ],
            [
                KeyboardButton(text="📊 Статистика"),
                KeyboardButton(text="🚚 Создать заявку")
            ]
        ],
        resize_keyboard=True
    )


def main_keyboard(user_id):
    if user_id == ADMIN_ID:
        return admin_keyboard()

    return user_keyboard()


cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="❌ Отменить заявку")]
    ],
    resize_keyboard=True
)


skip_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="❌ Отменить заявку")]
    ],
    resize_keyboard=True
)


files_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Все файлы загружены")],
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="❌ Отменить заявку")]
    ],
    resize_keyboard=True
)


def confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить заявку",
                    callback_data="confirm_send"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data="confirm_cancel"
                )
            ]
        ]
    )


def application_keyboard(application_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟡 В работу",
                    callback_data=f"status_work_{application_id}"
                ),
                InlineKeyboardButton(
                    text="✅ Выполнена",
                    callback_data=f"status_done_{application_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменена",
                    callback_data=f"status_cancelled_{application_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📄 PDF",
                    callback_data=f"pdf_{application_id}"
                )
            ]
        ]
    )


# ============================================================
# ПРОВЕРКА ДАТЫ
# ============================================================

def normalize_date(value):
    value = value.strip()

    formats = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ]

    for fmt in formats:
        try:
            date = datetime.strptime(value, fmt)
            return date.strftime("%d.%m.%Y")
        except ValueError:
            pass

    return None


# ============================================================
# ТЕЛЕФОН
# ============================================================

def normalize_phone(phone):
    digits = re.sub(r"\D", "", phone)

    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    elif len(digits) == 10:
        digits = "7" + digits

    if len(digits) != 11:
        return None

    if not digits.startswith("7"):
        return None

    return (
        f"+7 {digits[1:4]} "
        f"{digits[4:7]}-"
        f"{digits[7:9]}-"
        f"{digits[9:11]}"
    )


# ============================================================
# ТЕКСТ ЗАЯВКИ
# ============================================================

def build_application(data, application_id=None, status=None):

    if application_id:
        text = (
            "🚛 <b>HORIZONT AUTO</b>\n"
            f"📋 <b>ЗАЯВКА HA-{application_id:06d}</b>\n"
        )

        if status:
            text += f"{status_text(status)}\n"

        text += "\n"

    else:
        text = (
            "🚛 <b>HORIZONT AUTO</b>\n"
            "📋 <b>ПРОВЕРЬТЕ ЗАЯВКУ</b>\n\n"
        )

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "📦 <b>ЗАБОР ГРУЗА</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    pickup_dates = data.get("pickup_dates", [])

    for i in range(data["pickup_count"]):

        date = (
            pickup_dates[i]
            if i < len(pickup_dates)
            else "—"
        )

        text += f"📍 <b>Точка забора №{i + 1}</b>\n"
        text += f"Адрес: {data['pickup_addresses'][i]}\n"
        text += f"Дата: {date}\n"
        text += f"Контакт: {data['sender_names'][i]}\n"
        text += f"Телефон: {data['sender_phones'][i]}\n\n"

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "🏁 <b>ВЫГРУЗКА</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    delivery_dates = data.get("delivery_dates", [])

    for i in range(data["delivery_count"]):

        date = (
            delivery_dates[i]
            if i < len(delivery_dates)
            else "—"
        )

        text += f"🏁 <b>Точка выгрузки №{i + 1}</b>\n"
        text += f"Адрес: {data['delivery_addresses'][i]}\n"
        text += f"Дата: {date}\n"
        text += f"Контакт: {data['receiver_names'][i]}\n"
        text += f"Телефон: {data['receiver_phones'][i]}\n\n"

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "🚚 <b>ПОМЕТКА ВОДИТЕЛЮ</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    text += (data.get("driver_note") or "—")
    text += "\n\n"

    text += (
        f"📎 Прикреплено файлов: "
        f"{len(data.get('files', []))}"
    )

    return text


# ============================================================
# PDF
# ============================================================

def get_pdf_font():

    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]

    for path in possible_fonts:
        if os.path.exists(path):

            try:
                pdfmetrics.registerFont(
                    TTFont("HorizontFont", path)
                )
                return "HorizontFont"

            except Exception:
                pass

    return "Helvetica"


def create_application_pdf(row):

    application_id = row[0]
    full_name = row[3]
    created_at = row[4]
    data = json.loads(row[5])
    status = row[6]

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=35,
        leftMargin=35,
        topMargin=35,
        bottomMargin=35
    )

    font = get_pdf_font()

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleRU",
        parent=styles["Title"],
        fontName=font,
        fontSize=18,
        alignment=TA_CENTER,
        spaceAfter=16
    )

    normal = ParagraphStyle(
        "NormalRU",
        parent=styles["Normal"],
        fontName=font,
        fontSize=10,
        leading=14
    )

    heading = ParagraphStyle(
        "HeadingRU",
        parent=normal,
        fontSize=13,
        spaceBefore=12,
        spaceAfter=8
    )

    story = []

    story.append(
        Paragraph(
            f"HORIZONT AUTO — Заявка HA-{application_id:06d}",
            title_style
        )
    )

    story.append(
        Paragraph(
            f"Статус: {status_text(status)}",
            normal
        )
    )

    story.append(
        Paragraph(
            f"Создана: {created_at}",
            normal
        )
    )

    story.append(
        Paragraph(
            f"Заказчик: {full_name or '—'}",
            normal
        )
    )

    story.append(Spacer(1, 12))

    story.append(
        Paragraph("ЗАБОР ГРУЗА", heading)
    )

    pickup_dates = data.get("pickup_dates", [])

    for i in range(data["pickup_count"]):

        date = (
            pickup_dates[i]
            if i < len(pickup_dates)
            else "—"
        )

        rows = [
            ["Точка", f"Забор №{i + 1}"],
            ["Адрес", data["pickup_addresses"][i]],
            ["Дата", date],
            ["Контакт", data["sender_names"][i]],
            ["Телефон", data["sender_phones"][i]],
        ]

        table = Table(
            rows,
            colWidths=[90, 410]
        )

        table.setStyle(
            TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ])
        )

        story.append(table)
        story.append(Spacer(1, 10))

    story.append(
        Paragraph("ВЫГРУЗКА", heading)
    )

    delivery_dates = data.get("delivery_dates", [])

    for i in range(data["delivery_count"]):

        date = (
            delivery_dates[i]
            if i < len(delivery_dates)
            else "—"
        )

        rows = [
            ["Точка", f"Выгрузка №{i + 1}"],
            ["Адрес", data["delivery_addresses"][i]],
            ["Дата", date],
            ["Контакт", data["receiver_names"][i]],
            ["Телефон", data["receiver_phones"][i]],
        ]

        table = Table(
            rows,
            colWidths=[90, 410]
        )

        table.setStyle(
            TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ])
        )

        story.append(table)
        story.append(Spacer(1, 10))

    story.append(
        Paragraph("ПОМЕТКА ВОДИТЕЛЮ", heading)
    )

    story.append(
        Paragraph(
            data.get("driver_note") or "—",
            normal
        )
    )

    doc.build(story)

    buffer.seek(0)

    return buffer.getvalue()


def create_all_pdf():

    applications = get_all_applications(500)

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30
    )

    font = get_pdf_font()
    styles = getSampleStyleSheet()

    title = ParagraphStyle(
        "TitleAll",
        parent=styles["Title"],
        fontName=font,
        alignment=TA_CENTER
    )

    normal = ParagraphStyle(
        "NormalAll",
        parent=styles["Normal"],
        fontName=font,
        fontSize=9,
        leading=12
    )

    story = [
        Paragraph(
            "HORIZONT AUTO — Реестр заявок",
            title
        ),
        Spacer(1, 15)
    ]

    if not applications:

        story.append(
            Paragraph(
                "Заявок пока нет.",
                normal
            )
        )

    for row in applications:

        application_id = row[0]
        full_name = row[2]
        created_at = row[3]
        data = json.loads(row[4])
        status = row[5]

        story.append(
            Paragraph(
                f"<b>HA-{application_id:06d}</b> — "
                f"{status_text(status)}",
                normal
            )
        )

        story.append(
            Paragraph(
                f"Заказчик: {full_name or '—'}",
                normal
            )
        )

        story.append(
            Paragraph(
                f"Создана: {created_at}",
                normal
            )
        )

        pickup = ", ".join(
            data.get("pickup_addresses", [])
        )

        delivery = ", ".join(
            data.get("delivery_addresses", [])
        )

        story.append(
            Paragraph(
                f"Забор: {pickup}",
                normal
            )
        )

        story.append(
            Paragraph(
                f"Выгрузка: {delivery}",
                normal
            )
        )

        story.append(Spacer(1, 12))

    doc.build(story)

    buffer.seek(0)

    return buffer.getvalue()


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):

    await state.clear()

    if message.from_user.id == ADMIN_ID:

        text = (
            "🚛 <b>HORIZONT AUTO</b>\n\n"
            "👨‍💼 Панель администратора"
        )

    else:

        text = (
            "🚛 <b>HORIZONT AUTO</b>\n\n"
            "Оформление заявки на перевозку."
        )

    await message.answer(
        text,
        reply_markup=main_keyboard(
            message.from_user.id
        ),
        parse_mode="HTML"
    )


# ============================================================
# ОТМЕНА
# ============================================================

@dp.message(F.text == "❌ Отменить заявку")
async def cancel(message: Message, state: FSMContext):

    await state.clear()

    await message.answer(
        "❌ Заявка отменена.",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# ============================================================
# СОЗДАНИЕ
# ============================================================

@dp.message(F.text == "🚚 Создать заявку")
async def create(message: Message, state: FSMContext):

    await state.clear()

    await state.set_state(Form.pickup_count)

    await message.answer(
        "📦 <b>1. Сколько точек забора груза?</b>\n\n"
        "Введите число:",
        reply_markup=cancel_keyboard,
        parse_mode="HTML"
    )


@dp.message(Form.pickup_count)
async def pickup_count(message: Message, state: FSMContext):

    if (
        not message.text
        or not message.text.isdigit()
        or not 1 <= int(message.text) <= 20
    ):
        await message.answer(
            "❌ Введите число от 1 до 20."
        )
        return

    await state.update_data(
        pickup_count=int(message.text)
    )

    await state.set_state(
        Form.delivery_count
    )

    await message.answer(
        "🏁 <b>2. Сколько точек выгрузки?</b>\n\n"
        "Введите число:",
        parse_mode="HTML"
    )


@dp.message(Form.delivery_count)
async def delivery_count(message: Message, state: FSMContext):

    if (
        not message.text
        or not message.text.isdigit()
        or not 1 <= int(message.text) <= 20
    ):
        await message.answer(
            "❌ Введите число от 1 до 20."
        )
        return

    await state.update_data(
        delivery_count=int(message.text),
        pickup_addresses=[],
        current_index=0
    )

    await state.set_state(
        Form.pickup_addresses
    )

    await message.answer(
        "📍 <b>3. Адрес забора №1</b>\n\n"
        "Введите полный адрес:",
        parse_mode="HTML"
    )


# ============================================================
# АДРЕСА ЗАБОРА
# ============================================================

@dp.message(Form.pickup_addresses)
async def pickup_address(message: Message, state: FSMContext):

    if not message.text:
        await message.answer(
            "Введите адрес текстом."
        )
        return

    data = await state.get_data()

    addresses = data.get(
        "pickup_addresses",
        []
    )

    addresses.append(
        message.text.strip()
    )

    index = data["current_index"] + 1

    await state.update_data(
        pickup_addresses=addresses,
        current_index=index
    )

    if index < data["pickup_count"]:

        await message.answer(
            f"📍 <b>Адрес забора №{index + 1}</b>",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        pickup_dates=[],
        current_index=0
    )

    await state.set_state(
        Form.pickup_dates
    )

    await message.answer(
        "📅 <b>Дата забора №1</b>\n\n"
        "Введите в формате:\n"
        "<code>28.09.2026</code>",
        parse_mode="HTML"
    )


# ============================================================
# ДАТЫ ЗАБОРА
# ============================================================

@dp.message(Form.pickup_dates)
async def pickup_date(message: Message, state: FSMContext):

    date = normalize_date(
        message.text or ""
    )

    if not date:

        await message.answer(
            "❌ Неверная дата.\n\n"
            "Введите, например:\n"
            "<code>28.09.2026</code>",
            parse_mode="HTML"
        )

        return

    data = await state.get_data()

    dates = data.get(
        "pickup_dates",
        []
    )

    dates.append(date)

    index = data["current_index"] + 1

    await state.update_data(
        pickup_dates=dates,
        current_index=index
    )

    if index < data["pickup_count"]:

        await message.answer(
            f"📅 <b>Дата забора №{index + 1}</b>",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        delivery_addresses=[],
        current_index=0
    )

    await state.set_state(
        Form.delivery_addresses
    )

    await message.answer(
        "🏁 <b>4. Адрес выгрузки №1</b>\n\n"
        "Введите полный адрес:",
        parse_mode="HTML"
    )


# ============================================================
# АДРЕСА ВЫГРУЗКИ
# ============================================================

@dp.message(Form.delivery_addresses)
async def delivery_address(message: Message, state: FSMContext):

    if not message.text:
        await message.answer(
            "Введите адрес текстом."
        )
        return

    data = await state.get_data()

    addresses = data.get(
        "delivery_addresses",
        []
    )

    addresses.append(
        message.text.strip()
    )

    index = data["current_index"] + 1

    await state.update_data(
        delivery_addresses=addresses,
        current_index=index
    )

    if index < data["delivery_count"]:

        await message.answer(
            f"🏁 <b>Адрес выгрузки №{index + 1}</b>",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        delivery_dates=[],
        current_index=0
    )

    await state.set_state(
        Form.delivery_dates
    )

    await message.answer(
        "📅 <b>Дата выгрузки №1</b>\n\n"
        "Введите в формате:\n"
        "<code>29.09.2026</code>",
        parse_mode="HTML"
    )


# ============================================================
# ДАТЫ ВЫГРУЗКИ
# ============================================================

@dp.message(Form.delivery_dates)
async def delivery_date(message: Message, state: FSMContext):

    date = normalize_date(
        message.text or ""
    )

    if not date:

        await message.answer(
            "❌ Неверная дата.\n"
            "Например: <code>29.09.2026</code>",
            parse_mode="HTML"
        )

        return

    data = await state.get_data()

    dates = data.get(
        "delivery_dates",
        []
    )

    dates.append(date)

    index = data["current_index"] + 1

    await state.update_data(
        delivery_dates=dates,
        current_index=index
    )

    if index < data["delivery_count"]:

        await message.answer(
            f"📅 <b>Дата выгрузки №{index + 1}</b>",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        sender_names=[],
        current_index=0
    )

    await state.set_state(
        Form.sender_names
    )

    await message.answer(
        "👤 <b>5. Контактное лицо отправителя</b>\n\n"
        "📍 Точка забора №1\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# ОТПРАВИТЕЛИ
# ============================================================

@dp.message(Form.sender_names)
async def sender_name(message: Message, state: FSMContext):

    data = await state.get_data()

    names = data.get(
        "sender_names",
        []
    )

    if message.text == "⏭ Пропустить":
        names.append("—")

    elif message.text:
        names.append(
            message.text.strip()
        )

    else:
        return

    index = data["current_index"] + 1

    await state.update_data(
        sender_names=names,
        current_index=index
    )

    if index < data["pickup_count"]:

        await message.answer(
            "👤 <b>Контактное лицо отправителя</b>\n\n"
            f"📍 Точка забора №{index + 1}",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        sender_phones=[],
        current_index=0
    )

    await state.set_state(
        Form.sender_phones
    )

    await message.answer(
        "📞 <b>6. Телефон отправителя</b>\n\n"
        "📍 Точка забора №1\n\n"
        "<code>+7 999 123-45-67</code>\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


@dp.message(Form.sender_phones)
async def sender_phone(message: Message, state: FSMContext):

    data = await state.get_data()

    phones = data.get(
        "sender_phones",
        []
    )

    if message.text == "⏭ Пропустить":

        phones.append("—")

    else:

        phone = normalize_phone(
            message.text or ""
        )

        if not phone:

            await message.answer(
                "❌ Введите российский номер:\n"
                "<code>+7 999 123-45-67</code>\n\n"
                "Или нажмите «Пропустить».",
                parse_mode="HTML"
            )

            return

        phones.append(phone)

    index = data["current_index"] + 1

    await state.update_data(
        sender_phones=phones,
        current_index=index
    )

    if index < data["pickup_count"]:

        await message.answer(
            "📞 <b>Телефон отправителя</b>\n\n"
            f"📍 Точка забора №{index + 1}",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        receiver_names=[],
        current_index=0
    )

    await state.set_state(
        Form.receiver_names
    )

    await message.answer(
        "👤 <b>7. Контактное лицо получателя</b>\n\n"
        "🏁 Точка выгрузки №1\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# ПОЛУЧАТЕЛИ
# ============================================================

@dp.message(Form.receiver_names)
async def receiver_name(message: Message, state: FSMContext):

    data = await state.get_data()

    names = data.get(
        "receiver_names",
        []
    )

    if message.text == "⏭ Пропустить":
        names.append("—")

    elif message.text:
        names.append(
            message.text.strip()
        )

    else:
        return

    index = data["current_index"] + 1

    await state.update_data(
        receiver_names=names,
        current_index=index
    )

    if index < data["delivery_count"]:

        await message.answer(
            "👤 <b>Контактное лицо получателя</b>\n\n"
            f"🏁 Точка выгрузки №{index + 1}",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        receiver_phones=[],
        current_index=0
    )

    await state.set_state(
        Form.receiver_phones
    )

    await message.answer(
        "📞 <b>8. Телефон получателя</b>\n\n"
        "🏁 Точка выгрузки №1\n\n"
        "⚠️ Обязательное поле\n\n"
        "<code>+7 999 123-45-67</code>",
        reply_markup=cancel_keyboard,
        parse_mode="HTML"
    )


@dp.message(Form.receiver_phones)
async def receiver_phone(message: Message, state: FSMContext):

    phone = normalize_phone(
        message.text or ""
    )

    if not phone:

        await message.answer(
            "❌ Неверный номер.\n"
            "Введите <code>+7 999 123-45-67</code>",
            parse_mode="HTML"
        )

        return

    data = await state.get_data()

    phones = data.get(
        "receiver_phones",
        []
    )

    phones.append(phone)

    index = data["current_index"] + 1

    await state.update_data(
        receiver_phones=phones,
        current_index=index
    )

    if index < data["delivery_count"]:

        await message.answer(
            "📞 <b>Телефон получателя</b>\n\n"
            f"🏁 Точка выгрузки №{index + 1}",
            parse_mode="HTML"
        )

        return

    await state.set_state(
        Form.driver_note
    )

    await message.answer(
        "📝 <b>9. Пометка для водителя</b>\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# ПОМЕТКА
# ============================================================

@dp.message(Form.driver_note)
async def driver_note(message: Message, state: FSMContext):

    if message.text == "⏭ Пропустить":
        note = ""

    elif message.text:
        note = message.text.strip()

    else:
        return

    await state.update_data(
        driver_note=note,
        files=[]
    )

    await state.set_state(
        Form.files
    )

    await message.answer(
        "📎 <b>10. Счета от поставщиков</b>\n\n"
        "Отправьте файлы или фотографии.\n"
        "Можно несколько.\n\n"
        "После загрузки нажмите "
        "«✅ Все файлы загружены».",
        reply_markup=files_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# ФАЙЛЫ
# ============================================================

@dp.message(Form.files, F.document)
async def document_received(message: Message, state: FSMContext):

    data = await state.get_data()

    files = data.get(
        "files",
        []
    )

    files.append({
        "type": "document",
        "file_id": message.document.file_id,
        "name": message.document.file_name or "Документ"
    })

    await state.update_data(
        files=files
    )

    await message.answer(
        f"✅ Файл добавлен. Всего: {len(files)}"
    )


@dp.message(Form.files, F.photo)
async def photo_received(message: Message, state: FSMContext):

    data = await state.get_data()

    files = data.get(
        "files",
        []
    )

    files.append({
        "type": "photo",
        "file_id": message.photo[-1].file_id,
        "name": "Фото"
    })

    await state.update_data(
        files=files
    )

    await message.answer(
        f"✅ Фото добавлено. Всего: {len(files)}"
    )


@dp.message(
    Form.files,
    F.text.in_({
        "✅ Все файлы загружены",
        "⏭ Пропустить"
    })
)
async def files_done(message: Message, state: FSMContext):

    data = await state.get_data()

    await state.set_state(
        Form.confirmation
    )

    await message.answer(
        build_application(data),
        parse_mode="HTML"
    )

    await message.answer(
        "Всё правильно?",
        reply_markup=confirm_keyboard()
    )


# ============================================================
# ОТПРАВИТЬ ЗАЯВКУ
# ============================================================

@dp.callback_query(F.data == "confirm_send")
async def confirm_send(callback: CallbackQuery, state: FSMContext):

    data = await state.get_data()

    if not data:
        await callback.answer(
            "Заявка уже обработана."
        )
        return

    application_id = save_application(
        callback.from_user,
        data
    )

    text = build_application(
        data,
        application_id,
        "new"
    )

    await bot.send_message(
        ADMIN_ID,
        text,
        parse_mode="HTML",
        reply_markup=application_keyboard(
            application_id
        )
    )

    for file in data.get("files", []):

        caption = (
            f"HORIZONT AUTO\n"
            f"Заявка HA-{application_id:06d}"
        )

        if file["type"] == "document":

            await bot.send_document(
                ADMIN_ID,
                file["file_id"],
                caption=caption
            )

        else:

            await bot.send_photo(
                ADMIN_ID,
                file["file_id"],
                caption=caption
            )

    await state.clear()

    await callback.message.answer(
        "✅ <b>Заявка создана!</b>\n\n"
        f"Номер: <b>HA-{application_id:06d}</b>\n"
        "Статус: 🔵 Новая",
        reply_markup=main_keyboard(
            callback.from_user.id
        ),
        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data == "confirm_cancel")
async def confirm_cancel(callback: CallbackQuery, state: FSMContext):

    await state.clear()

    await callback.message.answer(
        "❌ Заявка отменена.",
        reply_markup=main_keyboard(
            callback.from_user.id
        )
    )

    await callback.answer()


# ============================================================
# МОИ ЗАЯВКИ
# ============================================================

@dp.message(F.text == "📋 Мои заявки")
async def my_applications(message: Message):

    rows = get_user_applications(
        message.from_user.id
    )

    if not rows:

        await message.answer(
            "У вас пока нет заявок."
        )
        return

    text = "📋 <b>МОИ ЗАЯВКИ</b>\n\n"

    for row in rows:

        application_id = row[0]
        created_at = row[1]
        status = row[3]

        text += (
            f"<b>HA-{application_id:06d}</b>\n"
            f"{status_text(status)}\n"
            f"Создана: {created_at}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


# ============================================================
# АДМИН — НОВЫЕ
# ============================================================

@dp.message(F.text == "📥 Новые заявки")
async def admin_new(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    rows = get_new_applications()

    if not rows:

        await message.answer(
            "✅ Новых заявок нет."
        )
        return

    for row in rows[:20]:

        application_id = row[0]
        data = json.loads(row[4])
        status = row[5]

        await message.answer(
            build_application(
                data,
                application_id,
                status
            ),
            parse_mode="HTML",
            reply_markup=application_keyboard(
                application_id
            )
        )


# ============================================================
# АДМИН — ВСЕ
# ============================================================

@dp.message(F.text == "📋 Все заявки")
async def admin_all(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    rows = get_all_applications(20)

    if not rows:

        await message.answer(
            "Заявок пока нет."
        )
        return

    text = "📋 <b>ПОСЛЕДНИЕ ЗАЯВКИ</b>\n\n"

    for row in rows:

        text += (
            f"<b>HA-{row[0]:06d}</b> "
            f"{status_text(row[5])}\n"
            f"Заказчик: {row[2] or '—'}\n"
            f"{row[3]}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


# ============================================================
# ПОИСК
# ============================================================

@dp.message(F.text == "🔎 Найти заявку")
async def search_start(message: Message, state: FSMContext):

    if message.from_user.id != ADMIN_ID:
        return

    await state.set_state(
        AdminSearch.number
    )

    await message.answer(
        "🔎 Введите номер заявки.\n\n"
        "Например:\n"
        "<code>HA-000015</code>\n"
        "или просто <code>15</code>",
        parse_mode="HTML"
    )


@dp.message(AdminSearch.number)
async def search_application(message: Message, state: FSMContext):

    if message.from_user.id != ADMIN_ID:
        return

    digits = re.sub(
        r"\D",
        "",
        message.text or ""
    )

    if not digits:

        await message.answer(
            "Не удалось определить номер."
        )
        return

    application_id = int(digits)

    row = get_application(
        application_id
    )

    if not row:

        await message.answer(
            "❌ Заявка не найдена."
        )
        return

    data = json.loads(row[5])

    await state.clear()

    await message.answer(
        build_application(
            data,
            row[0],
            row[6]
        ),
        parse_mode="HTML",
        reply_markup=application_keyboard(
            row[0]
        )
    )


# ============================================================
# СТАТУСЫ
# ============================================================

@dp.callback_query(F.data.startswith("status_"))
async def change_status(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    parts = callback.data.split("_")

    status = parts[1]
    application_id = int(parts[2])

    if status not in {
        "work",
        "done",
        "cancelled"
    }:
        return

    row = get_application(
        application_id
    )

    if not row:
        await callback.answer(
            "Заявка не найдена."
        )
        return

    update_status(
        application_id,
        status
    )

    data = json.loads(row[5])

    try:
        await callback.message.edit_text(
            build_application(
                data,
                application_id,
                status
            ),
            parse_mode="HTML",
            reply_markup=application_keyboard(
                application_id
            )
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            row[1],
            "🚛 <b>HORIZONT AUTO</b>\n\n"
            f"Статус заявки "
            f"<b>HA-{application_id:06d}</b> изменён.\n\n"
            f"{status_text(status)}",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await callback.answer(
        "Статус изменён"
    )


# ============================================================
# PDF ОДНОЙ ЗАЯВКИ
# ============================================================

@dp.callback_query(F.data.startswith("pdf_"))
async def application_pdf(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    application_id = int(
        callback.data.split("_")[1]
    )

    row = get_application(
        application_id
    )

    if not row:

        await callback.answer(
            "Заявка не найдена."
        )
        return

    try:

        pdf = create_application_pdf(row)

        file = BufferedInputFile(
            pdf,
            filename=f"HA-{application_id:06d}.pdf"
        )

        await callback.message.answer_document(
            file,
            caption=(
                f"📄 Заявка HA-{application_id:06d}"
            )
        )

        await callback.answer()

    except Exception as error:

        print("PDF ERROR:", error)

        await callback.answer(
            "Ошибка создания PDF.",
            show_alert=True
        )


# ============================================================
# ОБЩИЙ PDF
# ============================================================

@dp.message(F.text == "📄 Выгрузить PDF")
async def export_pdf(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    try:

        pdf = create_all_pdf()

        file = BufferedInputFile(
            pdf,
            filename="Horizont_Auto_applications.pdf"
        )

        await message.answer_document(
            file,
            caption="📄 Реестр заявок HORIZONT AUTO"
        )

    except Exception as error:

        print("PDF EXPORT ERROR:", error)

        await message.answer(
            "❌ Не удалось создать PDF."
        )


# ============================================================
# СТАТИСТИКА
# ============================================================

@dp.message(F.text == "📊 Статистика")
async def statistics(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    stats = get_stats()

    await message.answer(
        "📊 <b>СТАТИСТИКА HORIZONT AUTO</b>\n\n"
        f"Всего заявок: <b>{stats['total']}</b>\n\n"
        f"🔵 Новые: {stats['new']}\n"
        f"🟡 В работе: {stats['work']}\n"
        f"✅ Выполнены: {stats['done']}\n"
        f"❌ Отменены: {stats['cancelled']}",
        parse_mode="HTML"
    )


# ============================================================
# ЗАПУСК
# ============================================================

async def main():

    init_db()

    print("HORIZONT AUTO BOT ЗАПУЩЕН")

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
