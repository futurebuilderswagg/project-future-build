"""Настройки бота: токен, сроки, бонусы и звания."""
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from maxapi import Bot, Dispatcher

logging.basicConfig(level=logging.INFO)
ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = "bot.db"
TEMPLATE_PATH = ROOT_DIR / "test_template.xlsx"
CATALOG_DIR = ROOT_DIR / "catalog"
ASSETS_DIR = ROOT_DIR / "assets"
BOT_DESCRIPTION = ("«Академический импульс»: проходи тесты в удобное время, копи баллы "
                   "и обменивай их на образовательные бонусы.")
WELCOME_TEXT = "Привет! 👋 Я «Академический импульс». Здесь повторение тем превращается в игру.\nТы ученик или учитель?"
CACHE_TTL = 86400
DIFFICULTY_NAMES = {1: "Легкий", 2: "Средний", 3: "Сложный"}
ROLE_NAMES = {"teacher": "учитель", "student": "ученик"}
ANSWER_LETTERS = ("А", "Б", "В", "Г")
# Сколько тест открыт для класса: учитель выбирает срок при выдаче.
TEST_DURATION_CHOICES = {20: "20 минут", 1440: "1 день", 4320: "3 дня", 10080: "7 дней"}
# Сколько времени есть у ученика с момента начала прохождения.
TEST_ATTEMPT_MINUTES = 20
SWEEP_INTERVAL_SECONDS = 30
HELP_NEEDED_PERCENT = 50
HARD_QUESTION_PERCENT = 70
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 300
MAX_FULL_NAME_LENGTH = 150
MAX_CLASS_NAME_LENGTH = 40
# Цены и лимиты бонусов. limit_days — не чаще одного раза за столько дней.
BONUSES = (
    {"key": "title", "name": "Звание в чате класса", "cost": 4000, "limit_days": None},
    {"key": "topic", "name": "Выбрать тему для обсуждения на уроке", "cost": 400, "limit_days": 30},
    {"key": "homework", "name": "Отмена двойки за одну домашнюю работу", "cost": 1500, "limit_days": 63},
    {"key": "expert", "name": "Сертификат «Эксперт темы» (без дублирующей летучки)", "cost": 800, "limit_days": 30},
    {"key": "regular", "name": "Оценка за регулярную самостоятельную работу", "cost": 2500, "limit_days": 63},
)
# Уровень растёт от заработанных баллов, звание зависит от уровня.
POINTS_PER_LEVEL = 50
# Экономика баллов по расчёту Софии К.: баллы за ответ умножаются на сложность вопроса.
POINTS_PER_DIFFICULTY = 10
PASS_PERCENT = 80
TEST_BONUS_PASS = 50
TEST_BONUS_PERFECT = 100
SNIPER_BONUS = 30
STREAK_WEEK_BONUS = 100
STREAK_MILESTONE_BONUS = {30: 500, 100: 1500}
RANKS = ((1, "Новичок"), (10, "Исследователь"), (20, "Эрудит"), (40, "Магистр"), (65, "Архимаг"), (100, "Гуру"))
BADGES = {
    "sniper": "Снайпер: тест без единой ошибки",
    "week": "Первая неделя: 7 дней подряд",
    "month": "Месяц открытий: 30 дней подряд",
    "legend": "Легендарная привычка: 100 дней подряд",
}
STREAK_BADGES = {7: "week", 30: "month", 100: "legend"}
RESULTS_PAGE_SIZE = 10
STUDENT_CLASSES_PAGE_SIZE = 5
MAX_TEST_TITLE_LENGTH = 120
MAX_IMPORTED_QUESTIONS = 200
MAX_EXCEL_FILE_BYTES = 5 * 1024 * 1024
MAX_EXCEL_UNPACKED_BYTES = 25 * 1024 * 1024
MAX_EXCEL_ROWS = 2000

def load_env(path="token.env"):
    # Токен можно передать и обычной переменной окружения, без файла.
    if not os.path.exists(path):
        return
    # utf-8-sig читает и файлы с BOM, которые создаёт Блокнот.
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip("\"'")

load_env()
token = os.getenv("MAX_BOT_TOKEN", "")
if not token or token.startswith("PASTE_YOUR"):
    raise SystemExit("Токен не задан: укажите MAX_BOT_TOKEN в token.env (образец — token.env.example).")
TEACHER_CODE = os.getenv("TEACHER_CODE", "").strip()
if TEACHER_CODE.startswith("PASTE_YOUR"):
    TEACHER_CODE = ""
if not TEACHER_CODE:
    logging.warning("TEACHER_CODE не задан: учителем может зарегистрироваться любой пользователь.")
DISPLAY_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Omsk"))
bot = Bot()
dp = Dispatcher()
