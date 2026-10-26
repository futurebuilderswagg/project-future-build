import os
import asyncio
import logging
import sqlite3
import time

from passlib.context import CryptContext

from maxapi import Bot, Dispatcher, F
from maxapi.methods.set_commands import SetCommands
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types import CallbackButton, BotCommand, MessageCreated, Command, MessageCallback

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# ======================= НАСТРОЙКИ =======================
DB_PATH = "bot.db"
CACHE_TTL = 600  # 10 минут

# ======================= ЧТЕНИЕ .env =======================
def load_env(path="token.env"):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

load_env()
os.getenv("MAX_BOT_TOKEN")
bot = Bot()
dp = Dispatcher()

# ======================= БАЗА ДАННЫХ =======================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'student',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def register_user(full_name: str, password: str, role: str = "student") -> bool:
    try:
        with get_connection() as conn:
            hashed = pwd_context.hash(password)
            conn.execute(
                "INSERT INTO users (full_name, password_hash, role) VALUES (?, ?, ?)",
                (full_name.strip(), hashed, role)
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def authenticate_user(full_name: str, password: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, full_name, role, password_hash FROM users WHERE full_name = ?",
            (full_name.strip(),)
        ).fetchone()
    if row is None:
        return None
    if not pwd_context.verify(password, row["password_hash"]):
        return None
    return {"id": row["id"], "full_name": row["full_name"], "role": row["role"]}

# ======================= КЕШ =======================
class AuthCache:
    def __init__(self, ttl_seconds: int = CACHE_TTL):
        self._cache = {}
        self.ttl = ttl_seconds

    def set(self, user_id: str, user_data: dict):
        self._cache[user_id] = (user_data, time.time() + self.ttl)

    def get(self, user_id: str) -> dict | None:
        if user_id not in self._cache:
            return None
        user_data, expires_at = self._cache[user_id]
        if time.time() > expires_at:
            del self._cache[user_id]
            return None
        return user_data

    def clear(self, user_id: str):
        self._cache.pop(user_id, None)

auth_cache = AuthCache()

# Состояния диалога: {user_id: {"action": "...", "role": "...", "step": "...", ...}}
user_states = {}

# ======================= КОМАНДЫ БОТА =======================
async def set_bot_commands():
    commands = [
        BotCommand(name="start", description="Запустить бота"),
        BotCommand(name="help", description="Показать справку"),
        BotCommand(name="profile", description="Мой профиль"),
        BotCommand(name="cancel", description="Отменить действие"),
    ]
    await SetCommands(bot, commands).fetch()

# ======================= /start =======================
@dp.message_created(Command('start'))
async def hello(event: MessageCreated):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Учитель", payload="teacher"), 
        CallbackButton(text="Ученик", payload="student"))
    await event.message.answer(
        "Привет! 👋 Я твой учебный бот. Ты ученик или учитель?",
        attachments=[builder.as_markup()])

# ======================= КНОПКА НАЗАД =======================
@dp.message_callback(F.callback.payload == "back")
async def back(event: MessageCallback):
    # Сбрасываем состояние текущего диалога
    user_id = str(event.from_user.user_id)
    user_states.pop(user_id, None)

    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Учитель", payload="teacher"), 
        CallbackButton(text="Ученик", payload="student"))
    await event.edit(
        text="Привет! 👋 Я твой учебный бот. Ты ученик или учитель?",
        attachments=[builder.as_markup()])

# ======================= ВЫБОР РОЛИ: УЧИТЕЛЬ =======================
@dp.message_callback(F.callback.payload == "teacher")
async def teacher_menu(event: MessageCallback):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Войти", payload="teacher_log_in"), 
        CallbackButton(text="Зарегистрироваться", payload="teacher_reg"))
    builder.row(CallbackButton(text="Назад", payload="back"))
    await event.edit(
        text="Вы хотите войти или зарегистрироваться?",
        attachments=[builder.as_markup()])

# ======================= ВЫБОР РОЛИ: УЧЕНИК =======================
@dp.message_callback(F.callback.payload == "student")
async def student_menu(event: MessageCallback):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Войти", payload="student_log_in"), 
        CallbackButton(text="Зарегистрироваться", payload="student_reg"))
    builder.row(CallbackButton(text="Назад", payload="back"))
    await event.edit(
        text="Вы хотите войти или зарегистрироваться?",
        attachments=[builder.as_markup()])

# ======================= РЕГИСТРАЦИЯ (КНОПКА) =======================
@dp.message_callback(F.callback.payload.in_(["teacher_reg", "student_reg"]))
async def start_registration(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    role = "teacher" if event.callback.payload == "teacher_reg" else "student"
    user_states[user_id] = {"action": "register", "role": role, "step": "name"}
    await event.edit(
        text="Введите ваше ФИО (Фамилия Имя Отчество) одним сообщением:",
        attachments=[])

# ======================= ВХОД (КНОПКА) =======================
@dp.message_callback(F.callback.payload.in_(["teacher_log_in", "student_log_in"]))
async def start_login(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user_states[user_id] = {"action": "login", "step": "name"}
    await event.edit(
        text="Введите ваше ФИО (Фамилия Имя Отчество) одним сообщением:",
        attachments=[])

# ======================= ПРОФИЛЬ (ЗАЩИЩЁННАЯ КОМАНДА) =======================
@dp.message_created(Command("profile"))
async def cmd_profile(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    cached_user = auth_cache.get(user_id)
    if cached_user:
        await event.message.answer(
            f"Вы вошли как: {cached_user['full_name']} ({cached_user['role']})"
        )
    else:
        await event.message.answer("Сначала войдите: /start → Войти.")

# ======================= ОТМЕНА =======================
@dp.message_created(Command("cancel"))
async def cmd_cancel(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    user_states.pop(user_id, None)
    await event.message.answer("Действие отменено. Используйте /start для начала.")

# ======================= ОБРАБОТКА ТЕКСТА (ДИАЛОГ) =======================
@dp.message_created()
async def handle_text(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    state = user_states.get(user_id)

    if not state:
        await event.message.answer("Используйте /start для начала работы.")
        return

    text = (event.message.body.text or "").strip()
    if not text:
        return

    # ---- Регистрация ----
    if state["action"] == "register":
        if state["step"] == "name":
            if len(text.split()) < 2:
                await event.message.answer("Пожалуйста, введите ФИО полностью (Фамилия Имя Отчество).")
                return
            state["full_name"] = text
            state["step"] = "password"
            await event.message.answer("Теперь введите пароль (не короче 6 символов):")
            return

        if state["step"] == "password":
            if len(text) < 6:
                await event.message.answer("Пароль слишком короткий. Введите пароль не короче 6 символов:")
                return
            success = register_user(state["full_name"], text, state["role"])
            if success:
                await event.message.answer("✅ Регистрация успешна! Теперь нажмите «Войти», чтобы авторизоваться.")
            else:
                await event.message.answer("❌ Пользователь с таким ФИО уже существует.")
            user_states.pop(user_id, None)
            return

    # ---- Вход ----
    if state["action"] == "login":
        if state["step"] == "name":
            state["full_name"] = text
            state["step"] = "password"
            await event.message.answer("Введите пароль:")
            return

        if state["step"] == "password":
            user = authenticate_user(state["full_name"], text)
            if user:
                auth_cache.set(user_id, user)
                await event.message.answer(
                    f"✅ Добро пожаловать, {user['full_name']}!\n"
                    f"Ваша роль: {user['role']}"
                )
            else:
                await event.message.answer("❌ Неверное ФИО или пароль.")
            user_states.pop(user_id, None)
            return

# ======================= ЗАПУСК =======================
async def main():
    init_db()  # создаём таблицы при старте
    await set_bot_commands()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
