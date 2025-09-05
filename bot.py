import asyncio
import logging
import os
import json
import psycopg2
import google.generativeai as genai
import urllib.parse # <-- Этот импорт нужен для новой админки

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
# --- НОВАЯ ФУНКЦИЯ ДЛЯ УДАЛЕНИЯ РАЗДЕЛА ---
def delete_section_db(section_name):
    try:
        conn = psycopg2.connect(DATABASE_URL); cursor = conn.cursor()
        cursor.execute("DELETE FROM sections WHERE name = %s", (section_name,))
        conn.commit(); cursor.close(); conn.close(); return True
    except Exception as e:
        logging.error(f"Ошибка при удалении раздела: {e}")
        return False

# --- КЛАВИАТУРЫ ---
main_app_keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📝 Портфолио и Запись", web_app=WebAppInfo(url=BOOKING_APP_URL))]], resize_keyboard=True)
contact_keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📞 Отправить мой номер", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
finish_upload_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Завершить загрузку", callback_data="finish_upload")]])
def generate_portfolio_sections_keyboard(data, for_admin=False):
    builder = [];
    for section_name in data.keys():
        builder.append([InlineKeyboardButton(text=section_name, callback_data=f"admin_section_{section_name}")])
    return InlineKeyboardMarkup(inline_keyboard=builder)

# --- FSM ---
class Booking(StatesGroup): waiting_for_contact = State()
class PortfolioAdmin(StatesGroup): uploading_photos = State()

# --- ФУНКЦИЯ GEMINI ---
async def get_gemini_response(text: str) -> str:
    if not GEMINI_API_KEY: return ""
    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        prompt = f"""Ты — вежливый ассистент фотографа Марины Заугольниковой. Отвечай на вопросы клиентов в Telegram.
        ИНФО: Стоимость от 5000р/час. Для записи нужно нажать кнопку "Портфолио и Запись". Ты не знаешь свободные даты.
        ПРАВИЛА: Будь кратким. Не выдумывай. Если не знаешь ответ, скажи, что Марина скоро ответит сама.
        Сообщение клиента: "{text}"
        Твой ответ:"""
        response = await model.generate_content_async(prompt)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Ошибка Gemini API: {e}"); return ""

# --- ЛОГИКА БОТА ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

@dp.message(CommandStart())
async def send_welcome(message: Message):
    await message.answer(f"Здравствуйте, {message.from_user.first_name}! Я бот-ассистент фотографа Марины Заугольниковой.", reply_markup=main_app_keyboard)

# --- НОВЫЙ ОБРАБОТЧИК /admin ---
@dp.message(Command('admin'))
async def admin_panel(message: Message):
    if message.from_user.id == ADMIN_ID:
        portfolio_data = get_portfolio_data()
        section_names = list(portfolio_data.keys())
        encoded_sections = urllib.parse.quote(','.join(section_names))
        url_with_params = f"{ADMIN_APP_URL}?sections={encoded_sections}" if section_names else ADMIN_APP_URL
        
        dynamic_admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Админ-панель", web_app=WebAppInfo(url=url_with_params))],
            [InlineKeyboardButton(text="📸 Добавить фото", callback_data="add_photo")]
        ])
        await message.answer("Добро пожаловать в админ-панель!", reply_markup=dynamic_admin_keyboard)

# --- НОВЫЙ ОБРАБОТЧИК WebApp ---
@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message, state: FSMContext):
    data = json.loads(message.web_app_data.data)
    source = data.get('source')

    if source == 'admin_panel':
        if message.from_user.id != ADMIN_ID: return
        action = data.get('action')
        name = data.get('name')
        
        if action == 'add_section' and name:
            if add_section_db(name): await message.answer(f"✅ Раздел «{name}» создан! Откройте панель заново, чтобы увидеть изменения.")
            else: await message.answer(f"⚠️ Раздел «{name}» уже есть.")
        
        elif action == 'delete_section' and name:
            if delete_section_db(name): await message.answer(f"🗑️ Раздел «{name}» удален. Откройте панель заново, чтобы обновить список.")
            else: await message.answer(f"❌ Не удалось удалить раздел «{name}».")

    elif source == 'booking_form':
        await state.update_data(plan=data.get('plan'), hours=data.get('hours'), location=data.get('location'), comments=data.get('comments'))
        await message.answer("Спасибо! Нажмите кнопку ниже, чтобы поделиться контактом для связи.", reply_markup=contact_keyboard)
        await state.set_state(Booking.waiting_for_contact)
        
    elif source == 'show_portfolio':
        section_name = data.get('section')
        photos = get_portfolio_data().get(section_name, [])
        if not photos: await message.answer(f"В разделе «{section_name}» пока нет фотографий."); return
        await message.answer(f"Работы из раздела «{section_name}»:")
        media_group = MediaGroupBuilder()
        for photo_id in photos: media_group.add_photo(media=photo_id)
        if media_group.build(): await bot.send_media_group(message.chat.id, media=media_group.build()[:10])

@dp.message(Booking.waiting_for_contact, F.contact)
async def contact_received(message: Message, state: FSMContext):
    user_data = await state.get_data()
    info = (f"🎉 Новая заявка! 🎉\n\n👤 **Клиент:** {message.from_user.first_name}\n📞 **Телефон:** `{message.contact.phone_number}`\n\n"
            f"📝 **Детали:**\n- **План:** {user_data.get('plan')}\n- **Часы:** {user_data.get('hours')}\n"
            f"- **Локация:** {user_data.get('location')}\n- **Комментарий:** {user_data.get('comments') or 'Нет'}")
    await bot.send_message(ADMIN_ID, info, parse_mode="Markdown")
    await message.answer("Отлично! Ваша заявка отправлена. Марина скоро с вами свяжется.", reply_markup=main_app_keyboard)
    await state.clear()

# --- УПРАВЛЕНИЕ ПОРТФОЛИО ---
@dp.callback_query(F.data == "add_photo")
async def add_photo_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    if not get_portfolio_data(): await cb.message.answer("Сначала создайте раздел."); await cb.answer(); return
    await cb.message.answer("Куда грузить фото?", reply_markup=generate_portfolio_sections_keyboard(get_portfolio_data(), for_admin=True)); await state.set_state(PortfolioAdmin.uploading_photos); await cb.answer()
@dp.callback_query(PortfolioAdmin.uploading_photos, F.data.startswith("admin_section_"))
async def add_photo_section_chosen(cb: CallbackQuery, state: FSMContext):
    section_name = cb.data.split("_", 2)[-1]; await state.update_data(current_section=section_name)
    await cb.message.answer(f"Отправляйте фото для «{section_name}».", reply_markup=finish_upload_keyboard); await cb.answer()
@dp.message(PortfolioAdmin.uploading_photos, F.photo)
async def upload_photo(message: Message, state: FSMContext):
    data = await state.get_data(); add_photo_db(data.get("current_section"), message.photo[-1].file_id)
    await message.answer("Фото добавлено!")
@dp.callback_query(PortfolioAdmin.uploading_photos, F.data == "finish_upload")
async def finish_uploading(cb: CallbackQuery, state: FSMContext):
    await state.clear(); await cb.message.edit_text("Загрузка завершена.")
    await cb.message.answer("Вы в админ-панели.", reply_markup=cb.message.reply_markup) # Возвращаем ту же клавиатуру, что и была

# --- TELEGRAM BUSINESS ---
@dp.business_message()
async def handle_business_message(message: types.Message):
    logging.info(f"Бизнес-сообщение от {message.chat.id}: {message.text}")
    response_text = await get_gemini_response(message.text)
    if response_text: await message.reply(response_text)

# --- ЗАПУСК ---
async def main():
    init_db()
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
