import asyncio
import os
import re
import json
import sqlite3
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
# БАЗА ДАННЫХ
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_NAME)
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

    conn.commit()
    conn.close()


def save_application(user, data):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO applications (
            user_id,
            username,
            full_name,
            created_at,
            data
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        user.id,
        user.username or "",
        user.full_name or "",
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        json.dumps(data, ensure_ascii=False)
    ))

    application_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return application_id


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class Form(StatesGroup):
    pickup_count = State()
    delivery_count = State()
    pickup_addresses = State()
    delivery_addresses = State()
    sender_names = State()
    sender_phones = State()
    receiver_names = State()
    receiver_phones = State()
    driver_note = State()
    files = State()
    confirmation = State()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚚 Создать заявку")]
    ],
    resize_keyboard=True
)

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
                    text="❌ Отменить заявку",
                    callback_data="confirm_cancel"
                )
            ]
        ]
    )


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

def build_application(data, application_id=None):

    if application_id:
        text = (
            "🚛 <b>HORIZONT AUTO</b>\n"
            f"📋 <b>ЗАЯВКА HA-{application_id:06d}</b>\n\n"
        )
    else:
        text = (
            "🚛 <b>HORIZONT AUTO</b>\n"
            "📋 <b>ПРОВЕРЬТЕ ЗАЯВКУ</b>\n\n"
        )

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "📦 <b>ЗАБОР ГРУЗА</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    for i in range(data["pickup_count"]):
        text += f"📍 <b>Точка забора №{i + 1}</b>\n"
        text += f"Адрес: {data['pickup_addresses'][i]}\n"
        text += f"Контакт: {data['sender_names'][i]}\n"
        text += f"Телефон: {data['sender_phones'][i]}\n\n"

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "🏁 <b>ВЫГРУЗКА</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    for i in range(data["delivery_count"]):
        text += f"🏁 <b>Точка выгрузки №{i + 1}</b>\n"
        text += f"Адрес: {data['delivery_addresses'][i]}\n"
        text += f"Контакт: {data['receiver_names'][i]}\n"
        text += f"Телефон: {data['receiver_phones'][i]}\n\n"

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "🚚 <b>ПОМЕТКА ВОДИТЕЛЮ</b>\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    note = data.get("driver_note", "")

    if note:
        text += f"{note}\n\n"
    else:
        text += "—\n\n"

    files = data.get("files", [])

    text += f"📎 Прикреплено файлов: {len(files)}"

    return text


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):

    await state.clear()

    await message.answer(
        "🚛 <b>HORIZONT AUTO</b>\n\n"
        "Оформление заявки на перевозку.\n\n"
        "Нажмите кнопку ниже, чтобы создать новую заявку.",
        reply_markup=main_keyboard,
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
        reply_markup=main_keyboard
    )


# ============================================================
# НОВАЯ ЗАЯВКА
# ============================================================

@dp.message(F.text == "🚚 Создать заявку")
async def create(message: Message, state: FSMContext):

    await state.clear()

    await state.set_state(Form.pickup_count)

    await message.answer(
        "📦 <b>1. Сколько точек забора груза?</b>\n\n"
        "Введите число, например: <b>2</b>",
        reply_markup=cancel_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# 1. КОЛИЧЕСТВО ЗАБОРОВ
# ============================================================

@dp.message(Form.pickup_count)
async def pickup_count(message: Message, state: FSMContext):

    if not message.text or not message.text.isdigit():
        await message.answer("❌ Введите количество цифрой.")
        return

    count = int(message.text)

    if count < 1 or count > 20:
        await message.answer("❌ Введите число от 1 до 20.")
        return

    await state.update_data(pickup_count=count)

    await state.set_state(Form.delivery_count)

    await message.answer(
        "🏁 <b>2. Сколько точек выгрузки?</b>\n\n"
        "Введите число:",
        parse_mode="HTML"
    )


# ============================================================
# 2. КОЛИЧЕСТВО ВЫГРУЗОК
# ============================================================

@dp.message(Form.delivery_count)
async def delivery_count(message: Message, state: FSMContext):

    if not message.text or not message.text.isdigit():
        await message.answer("❌ Введите количество цифрой.")
        return

    count = int(message.text)

    if count < 1 or count > 20:
        await message.answer("❌ Введите число от 1 до 20.")
        return

    await state.update_data(
        delivery_count=count,
        pickup_addresses=[],
        current_index=0
    )

    await state.set_state(Form.pickup_addresses)

    await message.answer(
        "📍 <b>3. Адрес забора №1</b>\n\n"
        "Введите полный адрес:",
        parse_mode="HTML"
    )


# ============================================================
# 3. АДРЕСА ЗАБОРА
# ============================================================

@dp.message(Form.pickup_addresses)
async def pickup_address(message: Message, state: FSMContext):

    if not message.text:
        await message.answer("❌ Введите адрес текстом.")
        return

    data = await state.get_data()

    addresses = data.get("pickup_addresses", [])
    addresses.append(message.text.strip())

    index = data.get("current_index", 0) + 1

    await state.update_data(
        pickup_addresses=addresses,
        current_index=index
    )

    if index < data["pickup_count"]:

        await message.answer(
            f"📍 <b>Адрес забора №{index + 1}</b>\n\n"
            "Введите полный адрес:",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        delivery_addresses=[],
        current_index=0
    )

    await state.set_state(Form.delivery_addresses)

    await message.answer(
        "🏁 <b>4. Адрес выгрузки №1</b>\n\n"
        "Введите полный адрес:",
        parse_mode="HTML"
    )


# ============================================================
# 4. АДРЕСА ВЫГРУЗКИ
# ============================================================

@dp.message(Form.delivery_addresses)
async def delivery_address(message: Message, state: FSMContext):

    if not message.text:
        await message.answer("❌ Введите адрес текстом.")
        return

    data = await state.get_data()

    addresses = data.get("delivery_addresses", [])
    addresses.append(message.text.strip())

    index = data.get("current_index", 0) + 1

    await state.update_data(
        delivery_addresses=addresses,
        current_index=index
    )

    if index < data["delivery_count"]:

        await message.answer(
            f"🏁 <b>Адрес выгрузки №{index + 1}</b>\n\n"
            "Введите полный адрес:",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        sender_names=[],
        current_index=0
    )

    await state.set_state(Form.sender_names)

    await message.answer(
        "👤 <b>5. Контактное лицо отправителя</b>\n\n"
        "📍 Точка забора №1\n\n"
        "Введите имя.\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# 5. КОНТАКТ ОТПРАВИТЕЛЯ
# ============================================================

@dp.message(Form.sender_names)
async def sender_name(message: Message, state: FSMContext):

    data = await state.get_data()
    names = data.get("sender_names", [])

    if message.text == "⏭ Пропустить":
        names.append("—")

    elif message.text:
        names.append(message.text.strip())

    else:
        await message.answer(
            "Введите имя или нажмите «⏭ Пропустить»."
        )
        return

    index = data.get("current_index", 0) + 1

    await state.update_data(
        sender_names=names,
        current_index=index
    )

    if index < data["pickup_count"]:

        await message.answer(
            "👤 <b>Контактное лицо отправителя</b>\n\n"
            f"📍 Точка забора №{index + 1}\n\n"
            "Введите имя или нажмите «⏭ Пропустить».",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        sender_phones=[],
        current_index=0
    )

    await state.set_state(Form.sender_phones)

    await message.answer(
        "📞 <b>6. Телефон отправителя</b>\n\n"
        "📍 Точка забора №1\n\n"
        "Введите российский номер:\n"
        "<code>+7 999 123-45-67</code>\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# 6. ТЕЛЕФОН ОТПРАВИТЕЛЯ
# ============================================================

@dp.message(Form.sender_phones)
async def sender_phone(message: Message, state: FSMContext):

    data = await state.get_data()
    phones = data.get("sender_phones", [])

    if message.text == "⏭ Пропустить":

        phones.append("—")

    else:

        if not message.text:
            await message.answer(
                "Введите номер или нажмите «⏭ Пропустить»."
            )
            return

        phone = normalize_phone(message.text)

        if not phone:
            await message.answer(
                "❌ Неверный номер.\n\n"
                "Введите в формате:\n"
                "<code>+7 999 123-45-67</code>\n\n"
                "Или нажмите «⏭ Пропустить».",
                parse_mode="HTML"
            )
            return

        phones.append(phone)

    index = data.get("current_index", 0) + 1

    await state.update_data(
        sender_phones=phones,
        current_index=index
    )

    if index < data["pickup_count"]:

        await message.answer(
            "📞 <b>Телефон отправителя</b>\n\n"
            f"📍 Точка забора №{index + 1}\n\n"
            "Введите номер или нажмите «⏭ Пропустить».",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        receiver_names=[],
        current_index=0
    )

    await state.set_state(Form.receiver_names)

    await message.answer(
        "👤 <b>7. Контактное лицо получателя</b>\n\n"
        "🏁 Точка выгрузки №1\n\n"
        "Введите имя.\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# 7. КОНТАКТ ПОЛУЧАТЕЛЯ
# ============================================================

@dp.message(Form.receiver_names)
async def receiver_name(message: Message, state: FSMContext):

    data = await state.get_data()
    names = data.get("receiver_names", [])

    if message.text == "⏭ Пропустить":
        names.append("—")

    elif message.text:
        names.append(message.text.strip())

    else:
        await message.answer(
            "Введите имя или нажмите «⏭ Пропустить»."
        )
        return

    index = data.get("current_index", 0) + 1

    await state.update_data(
        receiver_names=names,
        current_index=index
    )

    if index < data["delivery_count"]:

        await message.answer(
            "👤 <b>Контактное лицо получателя</b>\n\n"
            f"🏁 Точка выгрузки №{index + 1}\n\n"
            "Введите имя или нажмите «⏭ Пропустить».",
            parse_mode="HTML"
        )

        return

    await state.update_data(
        receiver_phones=[],
        current_index=0
    )

    await state.set_state(Form.receiver_phones)

    await message.answer(
        "📞 <b>8. Телефон получателя</b>\n\n"
        "🏁 Точка выгрузки №1\n\n"
        "⚠️ <b>Обязательное поле</b>\n\n"
        "Введите российский номер:\n"
        "<code>+7 999 123-45-67</code>",
        reply_markup=cancel_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# 8. ТЕЛЕФОН ПОЛУЧАТЕЛЯ
# ============================================================

@dp.message(Form.receiver_phones)
async def receiver_phone(message: Message, state: FSMContext):

    if not message.text:
        await message.answer("❌ Введите номер телефона.")
        return

    phone = normalize_phone(message.text)

    if not phone:
        await message.answer(
            "❌ Неверный номер.\n\n"
            "Введите в формате:\n"
            "<code>+7 999 123-45-67</code>",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    phones = data.get("receiver_phones", [])

    phones.append(phone)

    index = data.get("current_index", 0) + 1

    await state.update_data(
        receiver_phones=phones,
        current_index=index
    )

    if index < data["delivery_count"]:

        await message.answer(
            "📞 <b>Телефон получателя</b>\n\n"
            f"🏁 Точка выгрузки №{index + 1}\n\n"
            "Введите номер:\n"
            "<code>+7 999 123-45-67</code>",
            parse_mode="HTML"
        )

        return

    await state.set_state(Form.driver_note)

    await message.answer(
        "📝 <b>9. Пометка для водителя</b>\n\n"
        "Например:\n"
        "«Позвонить за час до приезда»\n"
        "«Заезд через вторые ворота»\n\n"
        "Поле необязательное.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# 9. ПОМЕТКА ВОДИТЕЛЮ
# ============================================================

@dp.message(Form.driver_note)
async def driver_note(message: Message, state: FSMContext):

    if message.text == "⏭ Пропустить":
        note = ""

    elif message.text:
        note = message.text.strip()

    else:
        await message.answer(
            "Введите пометку или нажмите «⏭ Пропустить»."
        )
        return

    await state.update_data(
        driver_note=note,
        files=[]
    )

    await state.set_state(Form.files)

    await message.answer(
        "📎 <b>10. Счета от поставщиков</b>\n\n"
        "Прикрепите файл или фотографию счета.\n\n"
        "Можно отправить несколько файлов по очереди.\n\n"
        "После загрузки нажмите:\n"
        "«✅ Все файлы загружены»\n\n"
        "Если файлов нет — «⏭ Пропустить».",
        reply_markup=files_keyboard,
        parse_mode="HTML"
    )


# ============================================================
# 10. ФАЙЛЫ
# ============================================================

@dp.message(Form.files, F.document)
async def document_received(message: Message, state: FSMContext):

    data = await state.get_data()
    files = data.get("files", [])

    files.append({
        "type": "document",
        "file_id": message.document.file_id,
        "name": message.document.file_name or "Документ"
    })

    await state.update_data(files=files)

    await message.answer(
        f"✅ Файл добавлен.\n"
        f"📎 Всего файлов: {len(files)}\n\n"
        "Можно отправить следующий."
    )


@dp.message(Form.files, F.photo)
async def photo_received(message: Message, state: FSMContext):

    data = await state.get_data()
    files = data.get("files", [])

    files.append({
        "type": "photo",
        "file_id": message.photo[-1].file_id,
        "name": "Фото счета"
    })

    await state.update_data(files=files)

    await message.answer(
        f"✅ Фото добавлено.\n"
        f"📎 Всего файлов: {len(files)}\n\n"
        "Можно отправить следующее."
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

    await state.set_state(Form.confirmation)

    application = build_application(data)

    await message.answer(
        application,
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

    await message.answer(
        "👆 Проверьте данные заявки.\n\n"
        "Если всё правильно — нажмите "
        "«✅ Отправить заявку».",
        reply_markup=confirm_keyboard()
    )


@dp.message(Form.files)
async def invalid_file(message: Message):

    await message.answer(
        "📎 Отправьте файл или фотографию.\n\n"
        "Либо нажмите «✅ Все файлы загружены»."
    )


# ============================================================
# ОТМЕНА ПЕРЕД ОТПРАВКОЙ
# ============================================================

@dp.callback_query(F.data == "confirm_cancel")
async def confirmation_cancel(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await callback.message.answer(
        "❌ Заявка отменена.",
        reply_markup=main_keyboard
    )

    await callback.answer()


# ============================================================
# ОТПРАВКА
# ============================================================

@dp.callback_query(F.data == "confirm_send")
async def confirmation_send(
    callback: CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    if not data:
        await callback.answer(
            "Заявка уже обработана.",
            show_alert=True
        )
        return

    application_id = save_application(
        callback.from_user,
        data
    )

    application = build_application(
        data,
        application_id
    )

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    admin_text = (
        f"{application}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>ЗАЯВИТЕЛЬ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Имя: {callback.from_user.full_name}\n"
        f"Telegram ID: "
        f"<code>{callback.from_user.id}</code>\n"
    )

    if callback.from_user.username:
        admin_text += (
            f"Telegram: @{callback.from_user.username}\n"
        )

    await bot.send_message(
        ADMIN_ID,
        admin_text,
        parse_mode="HTML"
    )

    files = data.get("files", [])

    for index, file in enumerate(files, start=1):

        caption = (
            "🚛 HORIZONT AUTO\n"
            f"📋 Заявка HA-{application_id:06d}\n"
            f"📎 Файл {index} из {len(files)}"
        )

        try:

            if file["type"] == "document":

                await bot.send_document(
                    ADMIN_ID,
                    file["file_id"],
                    caption=caption
                )

            elif file["type"] == "photo":

                await bot.send_photo(
                    ADMIN_ID,
                    file["file_id"],
                    caption=caption
                )

        except Exception as error:
            print("Ошибка отправки файла:", error)

    await state.clear()

    await callback.message.answer(
        "✅ <b>Заявка успешно отправлена!</b>\n\n"
        "Номер заявки:\n"
        f"<b>HA-{application_id:06d}</b>\n\n"
        "🚛 HORIZONT AUTO",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

    await callback.answer("Заявка отправлена")


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
