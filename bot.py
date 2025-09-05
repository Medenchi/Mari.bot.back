import asyncio
import logging
import os
import json
import psycopg2
import google.generativeai as genai

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.media_group import MediaGroupBuilder

# --- НАСТРОЙКА ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_APP_URL = os.getenv("ADMIN_APP_URL")
BOOKING_APP_URL = os.getenv("BOOKING_APP_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Конфигурируем Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# --- РАБОТА С БАЗОЙ ДАННЫХ (PostgreSQL) ---
def init_db():
    conn = psycopg2.connect(DATABASE_URL); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS sections (id SERIAL PRIMARY KEY, name VARCHAR(255) UNIQUE NOT NULL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS photos (id SERIAL PRIMARY KEY, section_id INTEGER, file_id TEXT NOT NULL, FOREIGN KEY (section_id) REFERENCES sections (id) ON DELETE CASCADE)')
    conn.commit(); cursor.close(); conn.close()
def get_portfolio_data():
    conn = psycopg2.connect(DATABASE_URL); cursor = conn.cursor()
    cursor.execute("SELECT name FROM sections ORDER BY name"); sections = [row[0] for row in cursor.fetchall()]; portfolio = {name: [] for name in sections}
    cursor.execute("SELECT s.name, p.file_id FROM photos p JOIN sections s ON p.section_id = s.id"); photos_data = cursor.fetchall()
    cursor.close(); conn.close()
    for section_name, file_id in photos_data:
        if section_name in portfolio: portfolio[section_name].append(file_id)
    return portfolio
def add_section_db(section_name):
    try:
        conn = psycopg2.connect(DATABASE_URL); cursor = conn.cursor()
        cursor.execute("INSERT INTO sections (name) VALUES (%s)", (section_name,)); conn.commit(); cursor.close(); conn.close(); return True
    except psycopg2.IntegrityError: return False
def add_photo_db(section_name, photo_file_id):
    conn = psycopg2.connect(DATABASE_URL); cursor = conn.cursor()
    cursor.execute("SELECT id FROM sections WHERE name = %s", (section_name,)); section = cursor.fetchone()
    if section: cursor.execute("INSERT INTO photos (section_id, file_id) VALUES (%s, %s)", (section[0], photo_file_id)); conn.commit()
    cursor.close(); conn.close()

# --- КЛАВИАТУРЫ ---
main_app_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📝 Портфолио и Запись", web_app=WebAppInfo(url=BOOKING_APP_URL))]],
    resize_keyboard=True
)
admin_main_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🚀 Админ-панель", web_app=WebAppInfo(url=ADMIN_APP_URL))],
    [InlineKeyboardButton(text="📸 Добавить фото", callback_data="add_photo")]
])
contact_keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📞 Отправить мой номер", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
finish_upload_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Завершить загрузку", callback_data="finish_upload")]])
def generate_portfolio_sections_keyboard(data, for_admin=False):
    builder = [];
    for section_name in data.keys():
        builder.append([InlineKeyboardButton(text=section_name, callback_data=f"admin_section_{section_name}")])
    return InlineKeyboardMarkup(inline_keyboard=builder)

# --- FSM (МАШИНА СОСТОЯНИЙ) ---
class Booking(StatesGroup): waiting_for_contact = State()
class PortfolioAdmin(StatesGroup): uploading_photos = State()

# --- ФУНКЦИЯ ДЛЯ GEMINI ---
async def get_gemini_response(text: str) -> str:
    if not GEMINI_API_KEY:
        logging.warning("API ключ для Gemini не найден.")
        return ""
    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        prompt = f"""
        Ты — вежливый и дружелюбный ассистент фотографа Марины Заугольниковой. Твоя задача — отвечать на вопросы клиентов в Telegram.
        КЛЮЧЕВАЯ ИНФОРМАЦИЯ:
        - Стоимость съёмки: от 5000 рублей в час. Индивидуальная - 5000р/час, Love Story - 6000р/час.
        - Чтобы записаться, нужно использовать специальную кнопку "Портфолио и Запись" в меню.
        - Ты не можешь сам проверить свободные даты. Предлагай клиенту воспользоваться кнопкой для записи.
        ПРАВИЛА:
        - Будь кратким и по делу. Не выдумывай информацию.
        - Если тебя спрашивают о чём-то, чего ты не знаешь (например, "а вы снимаете под водой?"), вежливо отвечай, что этот вопрос лучше задать Марине напрямую и она скоро сама ответит.
        - Не используй эмодзи слишком часто.
        Вот сообщение от клиента: "{text}"
        Твой ответ:
        """
        response = await model.generate_content_async(prompt)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка при обращении к Gemini API: {e}")
        return ""

# --- ЛОГИКА БОТА ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

@dp.message(CommandStart())
async def send_welcome(message: Message):
    await message.answer(f"Здравствуйте, {message.from_user.first_name}! Я бот-ассистент фотографа Марины Заугольниковой.", reply_markup=main_app_keyboard)

@dp.message(Command('admin'))
async def admin_panel(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Добро пожаловать в админ-панель!", reply_markup=admin_main_keyboard)

@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message, state: FSMContext):
    data = json.loads(message.web_app_data.data)
    source = data.get('source')

    if source == 'admin_panel':
        if message.from_user.id != ADMIN_ID: return
        action, name = data.get('action'), data.get('name')
        if action == 'add_section' and name and add_section_db(name):
            await message.answer(f"✅ Раздел «{name}» создан!")
        else:
            await message.answer(f"⚠️ Раздел «{name}» уже есть.")
    
    elif source == 'booking_form':
        await state.update_data(
            plan=data.get('plan'), hours=data.get('hours'),
            location=data.get('location'), comments=data.get('comments')
        )
        await message.answer("Спасибо! Нажмите кнопку ниже, чтобы поделиться контактом для связи.", reply_markup=contact_keyboard)
        await state.set_state(Booking.waiting_for_contact)
        
    elif source == 'show_portfolio':
        section_name = data.get('section')
        portfolio = get_portfolio_data()
        photos = portfolio.get(section_name, [])
        if not photos:
            await message.answer(f"В разделе «{section_name}» пока нет фотографий.")
            return
        await message.answer(f"Работы из раздела «{section_name}»:")
        media_group = MediaGroupBuilder()
        for photo_id in photos:
            media_group.add_photo(media=photo_id)
        if media_group.build():
            await bot.send_media_group(message.chat.id, media=media_group.build()[:10])

@dp.message(Booking.waiting_for_contact, F.contact)
async def contact_received(message: Message, state: FSMContext):
    user_data = await state.get_data()
    info = (f"🎉 Новая заявка! 🎉\n\n👤 **Клиент:** {message.from_user.first_name}\n📞 **Телефон:** `{message.contact.phone_number}`\n\n"
            f"📝 **Детали:**\n- **План:** {user_data.get('plan')}\n- **Часы:** {user_data.get('hours')}\n"
            f"- **Локация:** {user_data.get('location')}\n- **Комментарий:** {user_data.get('comments') or 'Нет'}")
    await bot.send_message(ADMIN_ID, info, parse_mode="Markdown")
    await message.answer("Отлично! Ваша заявка отправлена. Марина скоро с вами свяжется.", reply_markup=main_app_keyboard)
    await state.clear()

# --- УПРАВЛЕНИЕ ПОРТФОЛИО (ДЛЯ АДМИНА) ---
@dp.callback_query(F.data == "add_photo")
async def add_photo_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    if not get_portfolio_data(): await cb.message.answer("Сначала создайте раздел."); await cb.answer(); return
    await cb.message.answer("Куда грузить фото?", reply_markup=generate_portfolio_sections_keyboard(get_portfolio_data(), for_admin=True)); await state.set_state(PortfolioAdmin.uploading_photos); await cb.answer()
@dp.cal
