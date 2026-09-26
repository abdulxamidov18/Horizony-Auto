import asyncio
import os
import re
import json
import sqlite3
import html
from io import BytesIO
from datetime import datetime

import reportlab
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
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


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
        SELECT
            id,
            user_id,
            username,
            full_name,
            created_at,
            data,
            status
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
        LIMIT 30
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_all_applications(limit=100):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            user_id,
            full_name,
            created_at,
            data,
            status
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
        SELECT
            id,
            user_id,
            full_name,
            created_at,
            data,
            status
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
            [
                KeyboardButton(text="🚚 Создать заявку"),
                KeyboardButton(text="📋 Мои заявки"),
            ]
        ],
        resize_keyboard=True
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📥 Новые заявки"),
                KeyboardButton(text="📋 Все заявки"),
            ],
            [
                KeyboardButton(text="🔎 Найти заявку"),
                KeyboardButton(text="📄 Выгрузить PDF"),
            ],
            [
                KeyboardButton(text="📊 Статистика"),
                KeyboardButton(text="🚚 Создать заявку"),
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


back_cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⬅️ Назад")],
        [KeyboardButton(text="❌ Отменить заявку")]
    ],
    resize_keyboard=True
)


skip_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="⬅️ Назад")],
        [KeyboardButton(text="❌ Отменить заявку")]
    ],
    resize_keyboard=True
)


files_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Все файлы загружены")],
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="⬅️ Назад")],
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
                    text="⬅️ Назад",
                    callback_data="confirm_back"
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
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменена",
                    callback_data=f"status_cancelled_{application_id}"
                ),
                InlineKeyboardButton(
                    text="📄 PDF",
                    callback_data=f"pdf_{application_id}"
                ),
            ]
        ]
    )


def user_application_keyboard(application_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📋 HA-{application_id:06d}",
                    callback_data=f"userapp_{application_id}"
                )
            ]
        ]
    )


# ============================================================
# ДАТЫ
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
# ТЕКСТ ЗАЯВКИ TELEGRAM
# ============================================================

def safe(value):
    return html.escape(str(value or "—"))


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

    for i in range(data.get("pickup_count", 0)):

        date = (
            pickup_dates[i]
            if i < len(pickup_dates)
            else "—"
        )

        text += f"📍 <b>Точка забора №{i + 1}</b>\n"
        text += f"Адрес: {safe(data['pickup_addresses'][i])}\n"
        text += f"Дата: {safe(date)}\n"
        text += f"Контакт: {safe(data['sender_names'][i])}\n"
        text += f"Телефон: {safe(data['sender_phones'][i])}\n\n"

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "🏁 <b>ВЫГРУЗКА</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    delivery_dates = data.get("delivery_dates", [])

    for i in range(data.get("delivery_count", 0)):

        date = (
            delivery_dates[i]
            if i < len(delivery_dates)
            else "—"
        )

        text += f"🏁 <b>Точка выгрузки №{i + 1}</b>\n"
        text += f"Адрес: {safe(data['delivery_addresses'][i])}\n"
        text += f"Дата: {safe(date)}\n"
        text += f"Контакт: {safe(data['receiver_names'][i])}\n"
        text += f"Телефон: {safe(data['receiver_phones'][i])}\n\n"

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "🚚 <b>ПОМЕТКА ВОДИТЕЛЮ</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    text += safe(data.get("driver_note") or "—")
    text += "\n\n"

    text += (
        f"📎 Прикреплено файлов: "
        f"{len(data.get('files', []))}"
    )

    return text


# ============================================================
# ШРИФТ PDF
# ============================================================

PDF_FONT = "HorizontSans"
PDF_FONT_BOLD = "HorizontSansBold"


def register_pdf_fonts():
    """Регистрирует DejaVu Sans, который поставляется вместе с matplotlib.
    Функция вызывается только при создании PDF, поэтому проблема со шрифтом
    никогда не остановит запуск Telegram-бота.
    """
    try:
        import matplotlib
    except ImportError as exc:
        raise RuntimeError(
            "Для PDF не установлен matplotlib. Добавьте matplotlib>=3.8,<4.0 в requirements.txt"
        ) from exc

    font_dir = os.path.join(
        os.path.dirname(matplotlib.__file__),
        "mpl-data", "fonts", "ttf"
    )
    regular_path = os.path.join(font_dir, "DejaVuSans.ttf")
    bold_path = os.path.join(font_dir, "DejaVuSans-Bold.ttf")

    if not os.path.exists(regular_path):
        raise FileNotFoundError(
            f"Не найден DejaVuSans.ttf внутри matplotlib: {regular_path}"
        )

    if not os.path.exists(bold_path):
        bold_path = regular_path

    registered = pdfmetrics.getRegisteredFontNames()
    if PDF_FONT not in registered:
        pdfmetrics.registerFont(TTFont(PDF_FONT, regular_path))
    if PDF_FONT_BOLD not in registered:
        pdfmetrics.registerFont(TTFont(PDF_FONT_BOLD, bold_path))

    pdfmetrics.registerFontFamily(
        PDF_FONT,
        normal=PDF_FONT,
        bold=PDF_FONT_BOLD,
        italic=PDF_FONT,
        boldItalic=PDF_FONT_BOLD
    )

    return regular_path


# ============================================================
# PDF СТИЛИ
# ============================================================

def pdf_styles():

    register_pdf_fonts()

    title = ParagraphStyle(
        "HorizontTitle",
        fontName=PDF_FONT_BOLD,
        fontSize=21,
        leading=25,
        alignment=TA_CENTER,
        spaceAfter=4
    )

    subtitle = ParagraphStyle(
        "HorizontSubtitle",
        fontName=PDF_FONT_BOLD,
        fontSize=13,
        leading=18,
        alignment=TA_CENTER,
        spaceAfter=18
    )

    section = ParagraphStyle(
        "HorizontSection",
        fontName=PDF_FONT_BOLD,
        fontSize=12,
        leading=16,
        spaceBefore=12,
        spaceAfter=8
    )

    normal = ParagraphStyle(
        "HorizontNormal",
        fontName=PDF_FONT,
        fontSize=9.5,
        leading=14
    )

    bold = ParagraphStyle(
        "HorizontBold",
        fontName=PDF_FONT_BOLD,
        fontSize=9.5,
        leading=14
    )

    footer = ParagraphStyle(
        "HorizontFooter",
        fontName=PDF_FONT,
        fontSize=7.5,
        leading=10,
        alignment=TA_CENTER
    )

    return title, subtitle, section, normal, bold, footer


def pdf_text(value):
    return html.escape(str(value or "—"))


# ============================================================
# PDF ОДНОЙ ЗАЯВКИ
# ============================================================

def create_application_pdf(row):

    application_id = row[0]
    full_name = row[3] or "—"
    created_at = row[4]
    data = json.loads(row[5])
    status = row[6]

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=35,
        bottomMargin=35,
        title=f"Horizont Auto HA-{application_id:06d}"
    )

    (
        title_style,
        subtitle_style,
        section_style,
        normal_style,
        bold_style,
        footer_style
    ) = pdf_styles()

    story = []

    # ШАПКА

    story.append(
        Paragraph(
            "HORIZONT AUTO",
            title_style
        )
    )

    story.append(
        Paragraph(
            f"ЗАЯВКА HA-{application_id:06d}",
            subtitle_style
        )
    )

    info_rows = [
        [
            Paragraph("Дата создания", bold_style),
            Paragraph(pdf_text(created_at), normal_style)
        ],
        [
            Paragraph("Статус", bold_style),
            Paragraph(pdf_text(status_text(status)), normal_style)
        ],
        [
            Paragraph("Заказчик", bold_style),
            Paragraph(pdf_text(full_name), normal_style)
        ],
    ]

    info_table = Table(
        info_rows,
        colWidths=[125, 350]
    )

    info_table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ])
    )

    story.append(info_table)
    story.append(Spacer(1, 16))

    # ЗАБОР

    story.append(
        Paragraph(
            "ЗАБОР ГРУЗА",
            section_style
        )
    )

    pickup_dates = data.get("pickup_dates", [])

    for i in range(data.get("pickup_count", 0)):

        pickup_date = (
            pickup_dates[i]
            if i < len(pickup_dates)
            else "—"
        )

        rows = [
            [
                Paragraph(
                    f"Точка забора №{i + 1}",
                    bold_style
                ),
                ""
            ],
            [
                Paragraph("Адрес", bold_style),
                Paragraph(
                    pdf_text(data["pickup_addresses"][i]),
                    normal_style
                )
            ],
            [
                Paragraph("Дата забора", bold_style),
                Paragraph(
                    pdf_text(pickup_date),
                    normal_style
                )
            ],
            [
                Paragraph("Контакт", bold_style),
                Paragraph(
                    pdf_text(data["sender_names"][i]),
                    normal_style
                )
            ],
            [
                Paragraph("Телефон", bold_style),
                Paragraph(
                    pdf_text(data["sender_phones"][i]),
                    normal_style
                )
            ],
        ]

        table = Table(
            rows,
            colWidths=[125, 350],
            repeatRows=1
        )

        table.setStyle(
            TableStyle([
                ("SPAN", (0, 0), (1, 0)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 1), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ])
        )

        story.append(table)
        story.append(Spacer(1, 10))

    # ВЫГРУЗКА

    story.append(
        Paragraph(
            "ВЫГРУЗКА",
            section_style
        )
    )

    delivery_dates = data.get("delivery_dates", [])

    for i in range(data.get("delivery_count", 0)):

        delivery_date = (
            delivery_dates[i]
            if i < len(delivery_dates)
            else "—"
        )

        rows = [
            [
                Paragraph(
                    f"Точка выгрузки №{i + 1}",
                    bold_style
                ),
                ""
            ],
            [
                Paragraph("Адрес", bold_style),
                Paragraph(
                    pdf_text(data["delivery_addresses"][i]),
                    normal_style
                )
            ],
            [
                Paragraph("Дата выгрузки", bold_style),
                Paragraph(
                    pdf_text(delivery_date),
                    normal_style
                )
            ],
            [
                Paragraph("Контакт", bold_style),
                Paragraph(
                    pdf_text(data["receiver_names"][i]),
                    normal_style
                )
            ],
            [
                Paragraph("Телефон", bold_style),
                Paragraph(
                    pdf_text(data["receiver_phones"][i]),
                    normal_style
                )
            ],
        ]

        table = Table(
            rows,
            colWidths=[125, 350],
            repeatRows=1
        )

        table.setStyle(
            TableStyle([
                ("SPAN", (0, 0), (1, 0)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 1), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ])
        )

        story.append(table)
        story.append(Spacer(1, 10))

    # ПОМЕТКА

    story.append(
        Paragraph(
            "ПОМЕТКА ВОДИТЕЛЮ",
            section_style
        )
    )

    note = data.get("driver_note") or "—"

    note_table = Table(
        [[Paragraph(pdf_text(note), normal_style)]],
        colWidths=[475]
    )

    note_table.setStyle(
        TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ])
    )

    story.append(note_table)

    # ФАЙЛЫ

    story.append(Spacer(1, 12))

    story.append(
        Paragraph(
            f"Приложено файлов: {len(data.get('files', []))}",
            normal_style
        )
    )

    story.append(Spacer(1, 25))

    story.append(
        Paragraph(
            "HORIZONT AUTO • Заявка сформирована автоматически",
            footer_style
        )
    )

    doc.build(story)

    buffer.seek(0)
    return buffer.getvalue()


# ============================================================
# ОБЩИЙ PDF-РЕЕСТР
# ============================================================

def create_all_pdf():

    applications = get_all_applications(500)

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30,
        title="Horizont Auto — Реестр заявок"
    )

    (
        title_style,
        subtitle_style,
        section_style,
        normal_style,
        bold_style,
        footer_style
    ) = pdf_styles()

    story = [
        Paragraph(
            "HORIZONT AUTO",
            title_style
        ),
        Paragraph(
            "РЕЕСТР ЗАЯВОК",
            subtitle_style
        ),
        Paragraph(
            f"Сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            normal_style
        ),
        Spacer(1, 15)
    ]

    if not applications:

        story.append(
            Paragraph(
                "Заявок пока нет.",
                normal_style
            )
        )

    for row in applications:

        application_id = row[0]
        full_name = row[2] or "—"
        created_at = row[3]
        data = json.loads(row[4])
        status = row[5]

        pickup = ", ".join(
            data.get("pickup_addresses", [])
        ) or "—"

        delivery = ", ".join(
            data.get("delivery_addresses", [])
        ) or "—"

        pickup_dates = ", ".join(
            data.get("pickup_dates", [])
        ) or "—"

        delivery_dates = ", ".join(
            data.get("delivery_dates", [])
        ) or "—"

        rows = [
            [
                Paragraph(
                    f"HA-{application_id:06d}",
                    bold_style
                ),
                Paragraph(
                    pdf_text(status_text(status)),
                    normal_style
                )
            ],
            [
                Paragraph("Заказчик", bold_style),
                Paragraph(
                    pdf_text(full_name),
                    normal_style
                )
            ],
            [
                Paragraph("Создана", bold_style),
                Paragraph(
                    pdf_text(created_at),
                    normal_style
                )
            ],
            [
                Paragraph("Забор", bold_style),
                Paragraph(
                    pdf_text(pickup),
                    normal_style
                )
            ],
            [
                Paragraph("Дата забора", bold_style),
                Paragraph(
                    pdf_text(pickup_dates),
                    normal_style
                )
            ],
            [
                Paragraph("Выгрузка", bold_style),
                Paragraph(
                    pdf_text(delivery),
                    normal_style
                )
            ],
            [
                Paragraph("Дата выгрузки", bold_style),
                Paragraph(
                    pdf_text(delivery_dates),
                    normal_style
                )
            ],
        ]

        table = Table(
            rows,
            colWidths=[120, 390]
        )

        table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ])
        )

        story.append(table)
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
# НАЗАД ПО ШАГАМ
# ============================================================

def _pop_last(data, key):
    values = list(data.get(key, []))
    if values:
        values.pop()
    return values


@dp.message(F.text == "⬅️ Назад")
async def go_back(message: Message, state: FSMContext):
    current = await state.get_state()
    data = await state.get_data()

    if current == Form.delivery_count.state:
        await state.set_state(Form.pickup_count)
        await message.answer(
            "📦 <b>1. Сколько точек забора груза?</b>\n\nВведите число:",
            reply_markup=cancel_keyboard,
            parse_mode="HTML"
        )
        return

    if current == Form.pickup_addresses.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(
                pickup_addresses=_pop_last(data, "pickup_addresses"),
                current_index=idx - 1
            )
            await message.answer(
                f"📍 <b>Адрес забора №{idx}</b>\n\nВведите адрес заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        else:
            await state.set_state(Form.delivery_count)
            await message.answer(
                "🏁 <b>2. Сколько точек выгрузки?</b>\n\nВведите число:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.pickup_dates.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(
                pickup_dates=_pop_last(data, "pickup_dates"),
                current_index=idx - 1
            )
            await message.answer(
                f"📅 <b>Дата забора №{idx}</b>\n\nВведите дату заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        else:
            addresses = _pop_last(data, "pickup_addresses")
            idx2 = max(data.get("pickup_count", 1) - 1, 0)
            await state.update_data(pickup_addresses=addresses, current_index=idx2)
            await state.set_state(Form.pickup_addresses)
            await message.answer(
                f"📍 <b>Адрес забора №{idx2 + 1}</b>\n\nВведите адрес заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.delivery_addresses.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(
                delivery_addresses=_pop_last(data, "delivery_addresses"),
                current_index=idx - 1
            )
            await message.answer(
                f"🏁 <b>Адрес выгрузки №{idx}</b>\n\nВведите адрес заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        else:
            dates = _pop_last(data, "pickup_dates")
            idx2 = max(data.get("pickup_count", 1) - 1, 0)
            await state.update_data(pickup_dates=dates, current_index=idx2)
            await state.set_state(Form.pickup_dates)
            await message.answer(
                f"📅 <b>Дата забора №{idx2 + 1}</b>\n\nВведите дату заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.delivery_dates.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(
                delivery_dates=_pop_last(data, "delivery_dates"),
                current_index=idx - 1
            )
            await message.answer(
                f"📅 <b>Дата выгрузки №{idx}</b>\n\nВведите дату заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        else:
            addresses = _pop_last(data, "delivery_addresses")
            idx2 = max(data.get("delivery_count", 1) - 1, 0)
            await state.update_data(delivery_addresses=addresses, current_index=idx2)
            await state.set_state(Form.delivery_addresses)
            await message.answer(
                f"🏁 <b>Адрес выгрузки №{idx2 + 1}</b>\n\nВведите адрес заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.sender_names.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(sender_names=_pop_last(data, "sender_names"), current_index=idx - 1)
            await message.answer(
                f"👤 <b>Контакт отправителя — точка №{idx}</b>\n\nВведите заново или пропустите:",
                reply_markup=skip_keyboard,
                parse_mode="HTML"
            )
        else:
            dates = _pop_last(data, "delivery_dates")
            idx2 = max(data.get("delivery_count", 1) - 1, 0)
            await state.update_data(delivery_dates=dates, current_index=idx2)
            await state.set_state(Form.delivery_dates)
            await message.answer(
                f"📅 <b>Дата выгрузки №{idx2 + 1}</b>\n\nВведите дату заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.sender_phones.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(sender_phones=_pop_last(data, "sender_phones"), current_index=idx - 1)
            await message.answer(
                f"📞 <b>Телефон отправителя — точка №{idx}</b>\n\nВведите заново или пропустите:",
                reply_markup=skip_keyboard,
                parse_mode="HTML"
            )
        else:
            names = _pop_last(data, "sender_names")
            idx2 = max(data.get("pickup_count", 1) - 1, 0)
            await state.update_data(sender_names=names, current_index=idx2)
            await state.set_state(Form.sender_names)
            await message.answer(
                f"👤 <b>Контакт отправителя — точка №{idx2 + 1}</b>\n\nВведите заново или пропустите:",
                reply_markup=skip_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.receiver_names.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(receiver_names=_pop_last(data, "receiver_names"), current_index=idx - 1)
            await message.answer(
                f"👤 <b>Контакт получателя — точка №{idx}</b>\n\nВведите заново или пропустите:",
                reply_markup=skip_keyboard,
                parse_mode="HTML"
            )
        else:
            phones = _pop_last(data, "sender_phones")
            idx2 = max(data.get("pickup_count", 1) - 1, 0)
            await state.update_data(sender_phones=phones, current_index=idx2)
            await state.set_state(Form.sender_phones)
            await message.answer(
                f"📞 <b>Телефон отправителя — точка №{idx2 + 1}</b>\n\nВведите заново или пропустите:",
                reply_markup=skip_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.receiver_phones.state:
        idx = data.get("current_index", 0)
        if idx > 0:
            await state.update_data(receiver_phones=_pop_last(data, "receiver_phones"), current_index=idx - 1)
            await message.answer(
                f"📞 <b>Телефон получателя — точка №{idx}</b>\n\nВведите номер заново:",
                reply_markup=back_cancel_keyboard,
                parse_mode="HTML"
            )
        else:
            names = _pop_last(data, "receiver_names")
            idx2 = max(data.get("delivery_count", 1) - 1, 0)
            await state.update_data(receiver_names=names, current_index=idx2)
            await state.set_state(Form.receiver_names)
            await message.answer(
                f"👤 <b>Контакт получателя — точка №{idx2 + 1}</b>\n\nВведите заново или пропустите:",
                reply_markup=skip_keyboard,
                parse_mode="HTML"
            )
        return

    if current == Form.driver_note.state:
        phones = _pop_last(data, "receiver_phones")
        idx2 = max(data.get("delivery_count", 1) - 1, 0)
        await state.update_data(receiver_phones=phones, current_index=idx2)
        await state.set_state(Form.receiver_phones)
        await message.answer(
            f"📞 <b>Телефон получателя — точка №{idx2 + 1}</b>\n\nВведите номер заново:",
            reply_markup=back_cancel_keyboard,
            parse_mode="HTML"
        )
        return

    if current == Form.files.state:
        await state.set_state(Form.driver_note)
        await message.answer(
            "📝 <b>Пометка для водителя</b>\n\nВведите заново или пропустите:",
            reply_markup=skip_keyboard,
            parse_mode="HTML"
        )
        return

    await message.answer("На этом шаге назад вернуться нельзя.")


# ============================================================
# СОЗДАНИЕ ЗАЯВКИ
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
        reply_markup=back_cancel_keyboard,
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
        reply_markup=back_cancel_keyboard,
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

    # Выгрузка не может быть раньше последней даты забора.
    pickup_dates_check = data.get("pickup_dates", [])
    if pickup_dates_check:
        delivery_dt = datetime.strptime(date, "%d.%m.%Y")
        latest_pickup = max(
            datetime.strptime(x, "%d.%m.%Y")
            for x in pickup_dates_check
        )
        if delivery_dt < latest_pickup:
            await message.answer(
                "❌ Дата выгрузки не может быть раньше даты забора.\n\n"
                f"Последняя дата забора: <b>{latest_pickup.strftime('%d.%m.%Y')}</b>",
                parse_mode="HTML"
            )
            return

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
        reply_markup=back_cancel_keyboard,
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
# ПОДТВЕРЖДЕНИЕ
# ============================================================

@dp.callback_query(F.data == "confirm_back")
async def confirm_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.files)
    await callback.message.answer(
        "📎 <b>Счета от поставщиков</b>\n\n"
        "Можно добавить ещё файлы или вернуться назад к пометке водителю.",
        reply_markup=files_keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


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
            "🚛 HORIZONT AUTO\n"
            f"📋 HA-{application_id:06d}"
        )

        try:

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

        except Exception as error:
            print("FILE SEND ERROR:", error)

    await state.clear()

    await callback.message.answer(
        "✅ <b>Заявка создана!</b>\n\n"
        f"Номер: <b>HA-{application_id:06d}</b>\n"
        "Статус: 🔵 Новая\n\n"
        "Заявка сохранена в разделе "
        "«📋 Мои заявки».",
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
            "📋 У вас пока нет заявок."
        )
        return

    await message.answer(
        "📋 <b>МОИ ЗАЯВКИ</b>\n\n"
        "Нажмите на заявку, чтобы открыть её:",
        parse_mode="HTML"
    )

    for row in rows:

        application_id = row[0]
        created_at = row[1]
        data = json.loads(row[2])
        status = row[3]

        pickup_addresses = data.get(
            "pickup_addresses",
            []
        )

        delivery_addresses = data.get(
            "delivery_addresses",
            []
        )

        pickup = (
            pickup_addresses[0]
            if pickup_addresses
            else "—"
        )

        delivery = (
            delivery_addresses[0]
            if delivery_addresses
            else "—"
        )

        text = (
            f"<b>HA-{application_id:06d}</b>\n"
            f"{status_text(status)}\n"
            f"📦 {safe(pickup)}\n"
            f"🏁 {safe(delivery)}\n"
            f"🕒 {safe(created_at)}"
        )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=user_application_keyboard(
                application_id
            )
        )


# ============================================================
# ОТКРЫТЬ СВОЮ ЗАЯВКУ
# ============================================================

@dp.callback_query(F.data.startswith("userapp_"))
async def open_user_application(callback: CallbackQuery):

    application_id = int(
        callback.data.split("_")[1]
    )

    row = get_application(
        application_id
    )

    if not row:

        await callback.answer(
            "Заявка не найдена.",
            show_alert=True
        )
        return

    if (
        row[1] != callback.from_user.id
        and callback.from_user.id != ADMIN_ID
    ):
        await callback.answer(
            "Нет доступа к этой заявке.",
            show_alert=True
        )
        return

    data = json.loads(row[5])

    await callback.message.answer(
        build_application(
            data,
            row[0],
            row[6]
        ),
        parse_mode="HTML"
    )

    await callback.answer()


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

    for row in rows[:30]:

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

    rows = get_all_applications(30)

    if not rows:

        await message.answer(
            "Заявок пока нет."
        )
        return

    for row in rows:

        application_id = row[0]
        full_name = row[2] or "—"
        created_at = row[3]
        status = row[5]

        text = (
            f"📋 <b>HA-{application_id:06d}</b>\n"
            f"{status_text(status)}\n"
            f"👤 {safe(full_name)}\n"
            f"🕒 {safe(created_at)}"
        )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=application_keyboard(
                application_id
            )
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
        "🔎 <b>Введите номер заявки</b>\n\n"
        "Например:\n"
        "<code>HA-000015</code>\n\n"
        "Можно просто:\n"
        "<code>15</code>",
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
            "❌ Не удалось определить номер."
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
# ИЗМЕНЕНИЕ СТАТУСА
# ============================================================

@dp.callback_query(F.data.startswith("status_"))
async def change_status(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    parts = callback.data.split("_")

    if len(parts) != 3:
        await callback.answer()
        return

    status = parts[1]

    try:
        application_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    if status not in {
        "work",
        "done",
        "cancelled"
    }:
        await callback.answer()
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

    except Exception as error:
        print("EDIT STATUS ERROR:", error)

    try:

        await bot.send_message(
            row[1],
            "🚛 <b>HORIZONT AUTO</b>\n\n"
            f"Статус заявки "
            f"<b>HA-{application_id:06d}</b> "
            "изменён.\n\n"
            f"{status_text(status)}",
            parse_mode="HTML"
        )

    except Exception as error:
        print("CLIENT STATUS ERROR:", error)

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

    try:
        application_id = int(
            callback.data.split("_")[1]
        )
    except Exception:
        await callback.answer(
            "Неверный номер заявки."
        )
        return

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
                "🚛 HORIZONT AUTO\n"
                f"📄 Заявка HA-{application_id:06d}"
            )
        )

        await callback.answer()

    except Exception as error:

        print("PDF ERROR:", repr(error))

        await callback.answer(
            "Ошибка создания PDF. Смотрите лог Bothost.",
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
            caption=(
                "🚛 HORIZONT AUTO\n"
                "📄 Реестр заявок"
            )
        )

    except Exception as error:

        print("PDF EXPORT ERROR:", repr(error))

        await message.answer(
            "❌ Не удалось создать PDF.\n"
            "Посмотрите лог Bothost."
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
        f"🔵 Новые: <b>{stats['new']}</b>\n"
        f"🟡 В работе: <b>{stats['work']}</b>\n"
        f"✅ Выполнены: <b>{stats['done']}</b>\n"
        f"❌ Отменены: <b>{stats['cancelled']}</b>",
        parse_mode="HTML"
    )


# ============================================================
# ЗАПУСК
# ============================================================

async def main():

    init_db()

    # PDF-шрифт специально НЕ проверяем при старте.
    # Даже если с PDF возникнет проблема, сам бот продолжит работать.
    print("================================")
    print("HORIZONT AUTO BOT ЗАПУЩЕН")
    print("================================")

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
