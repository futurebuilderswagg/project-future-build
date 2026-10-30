import os
import asyncio
import logging
import sqlite3
import time
import re
import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from passlib.context import CryptContext

from maxapi import Bot, Dispatcher, F
from maxapi.methods.set_commands import SetCommands
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types import CallbackButton, BotCommand, MessageCreated, Command, MessageCallback

# ======================= НАСТРОЙКИ =======================
logging.basicConfig(level=logging.INFO)
DB_PATH = "bot.db"
CACHE_TTL = 86400
TEST_DURATION_MINUTES = 20
RESULTS_PAGE_SIZE = 10

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
                max_user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                teacher_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (teacher_id) REFERENCES users(id),
                UNIQUE(teacher_id, name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS class_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(class_id, student_id),
                FOREIGN KEY (class_id) REFERENCES classes(id),
                FOREIGN KEY (student_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                question_text TEXT NOT NULL,
                options TEXT NOT NULL,
                correct_answer TEXT NOT NULL,
                explanation TEXT,
                difficulty TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                class_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ends_at TIMESTAMP,
                FOREIGN KEY (teacher_id) REFERENCES users(id),
                FOREIGN KEY (class_id) REFERENCES classes(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                score INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                finished_at TIMESTAMP,
                UNIQUE(test_id, student_id),
                FOREIGN KEY (test_id) REFERENCES tests(id),
                FOREIGN KEY (student_id) REFERENCES users(id)
            )
        """)
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "max_user_id" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN max_user_id TEXT")
            logging.info("Добавлена колонка max_user_id в таблицу users")
        conn.commit()

# ======================= ЗАПОЛНЕНИЕ ВОПРОСОВ =======================
QUESTIONS_DATA = [
    # Механика (10)
    ("Механика", "Тело движется равномерно по окружности. Как направлено его ускорение?",
     "А — по касательной, Б — к центру, В — от центра, Г — ускорение = 0.", "Б",
     "центростремительное ускорение направлено к центру.", "Базовая"),
    ("Механика", "На тело массой 2 кг действует сила 10 Н. Чему равно ускорение?",
     "А — 0,2, Б — 5, В — 10, Г — 20 (м/с²).", "Б", "a = F/m = 10/2 = 5 м/с²", "Базовая"),
    ("Механика", "Мяч бросили вверх со скоростью 20 м/с. Через сколько секунд он достигнет верхней точки (g ≈ 10)?",
     "А — 1, Б — 2, В — 3, Г — 4 (с).", "Б", "t=v0/g=20/10=2 с", "Базовая"),
    ("Механика", "Два тела одинаковой массы движутся со скоростями v и 2v. Во сколько раз импульс второго больше?",
     "А — в 2, Б — в 3, В — в 4, Г — импульсы равны.", "А", "импульс p=mv, при удвоенной скорости импульс удваивается.", "Базовая"),
    ("Механика", "Санки скатываются без трения. Как меняется полная механическая энергия?",
     "А — растёт, Б — падает, В — не меняется, Г — сначала растёт, потом падает.", "В", "без трения энергия сохраняется.", "Базовая"),
    ("Механика", "Автомобиль 1 т разгоняется до 20 м/с за 10 с. Средняя мощность двигателя?",
     "А — 2, Б — 10, В — 20, Г — 40 (кВт).", "В", "A = mv²/2 = 200000 Дж, P = A/t = 20кВт", "Средняя"),
    ("Механика", "Брусок скользит с постоянной скоростью. Чему равна равнодействующая сил?",
     "А — больше нуля, Б — меньше нуля, В — равна нулю, Г — зависит от массы.", "В",
     "постоянная скорость → ускорение = 0 → равнодействующая = 0.", "Базовая"),
    ("Механика", "Расстояние между телами уменьшили в 2 раза. Как изменится сила тяготения?",
     "А — уменьшится в 2, Б — увеличится в 2, В — уменьшится в 4, Г — увеличится в 4 раза.", "Г",
     "F = 1 / r², при уменьшении r в 2 раза сила растёт в 4 раза.", "Базовая"),
    ("Механика", "Пружину жёсткостью 200 Н/м растянули на 5 см. Сила упругости?",
     "А — 1, Б — 10, В — 40, Г — 100 (Н).", "Б", "F = kΔx = 200*0,05 = 10 Н.", "Базовая"),
    ("Механика", "С какой высоты упало тело за 3 с без начальной скорости (g ≈ 10)?",
     "А — 15, Б — 30, В — 45, Г — 90 (м).", "В", "h = gt² / 2 = 45 м.", "Базовая"),
    # Термодинамика (10)
    ("Термодинамика", "Что характеризует температура с молекулярной точки зрения?",
     "А — потенциальную энергию, Б — среднюю кинетическую энергию, В — количество теплоты, Г — давление.", "Б",
     "температура — мера средней кинетической энергии молекул.", "Базовая"),
    ("Термодинамика", "Процесс при постоянной температуре?",
     "А — изобарный, Б — изохорный, В — изотермический, Г — адиабатный.", "В",
     "«изотермический» = постоянная температура.", "Базовая"),
    ("Термодинамика", "Газ получил 300 Дж теплоты и совершил работу 100 Дж. Как изменилась внутренняя энергия?",
     "А — на 200 Дж увеличилась, Б — на 400 Дж увеличилась, В — на 200 Дж уменьшилась, Г — на 400 Дж уменьшилась.", "А",
     "ΔU = Q - A = 300 - 100 = 200 Дж.", "Средняя"),
    ("Термодинамика", "Температура идеального газа увеличилась в 3 раза при постоянном объёме. Во сколько раз вырастет давление?",
     "А — в 1,5, Б — в 3, В — в 6, Г — в 9 раз.", "Б",
     "изохорный процесс: p/T=const, давление прямо пропорционально температуре.", "Средняя"),
    ("Термодинамика", "Удельная теплоёмкость: на нагрев 2 кг на 10 °C ушло 8 кДж. Чему она равна?",
     "А — 200, Б — 400, В — 800, Г — 1600 (Дж/(кг·°C)).", "Б",
     "c=Q/(mΔT)=8000/(2*10)=400 Дж/(кг·°C).", "Средняя"),
    ("Термодинамика", "При каком процессе нет теплообмена с окружающей средой?",
     "А — изотермическом, Б — изобарном, В — адиабатном, Г — изохорном.", "В",
     "адиабатный процесс — без теплообмена.", "Базовая"),
    ("Термодинамика", "Как изменится средняя квадратичная скорость молекул, если температура увеличится в 4 раза?",
     "А — не изменится, Б — увеличится в 2 раза, В — увеличится в 4 раза, Г — уменьшится в 2 раза.", "Б",
     "v ∼√T, при росте T в 4 раза скорость растёт в 2 раза.", "Средняя"),
    ("Термодинамика", "Что происходит с температурой жидкости при кипении?",
     "А — постоянно растёт, Б — постоянно падает, В — остаётся постоянной, Г — сначала растёт, потом падает.", "В",
     "энергия идёт на парообразование, температура не меняется.", "Базовая"),
    ("Термодинамика", "Сколько теплоты нужно, чтобы расплавить 1 кг льда при 0 °C",
     "А — 33, Б — 330, В — 660, Г — 990 (кДж).", "Б", "Q=λm=330*1=330 кДж.", "Базовая"),
    ("Термодинамика", "Что такое относительная влажность воздуха?",
     "А — отношение масс пара и воздуха, Б — отношение давлений пара и насыщенного пара, В — абсолютное количество пара в 1 м³, Г — температура конденсации.", "Б",
     "относительная влажность — это отношение парциального давления к давлению насыщенного пара при той же температуре.", "Базовая"),
    # Электродинамика (10)
    ("Электродинамика", "Как взаимодействуют одноимённые заряды?",
     "А — притягиваются, Б — отталкиваются, В — не взаимодействуют, Г — взаимодействуют только в вакууме.", "Б",
     "одноимённые заряды отталкиваются.", "Базовая"),
    ("Электродинамика", "Ток 2 А. Какой заряд пройдёт через сечение за 5 с?",
     "А — 2, Б — 5, В — 10, Г — 20 (Кл).", "В", "q=It=2⋅5=10 Кл.", "Базовая"),
    ("Электродинамика", "Сопротивление 10 Ом, напряжение 20 В. Какой ток?",
     "А — 0,5, Б — 2, В — 10, Г — 200 (А).", "Б", "по закону Ома I=U/R=20/10=2 А.", "Базовая"),
    ("Электродинамика", "Два резистора по 10 Ом соединены последовательно. Общее сопротивление?",
     "А — 5, Б — 10, В — 20, Г — 40 (Ом).", "В", "Rобщ = R1 + R2 = 20 Ом.", "Базовая"),
    ("Электродинамика", "Два одинаковых резистора по 10 Ом параллельно. Общее сопротивление?",
     "А — 5, Б — 10, В — 20, Г — 40 (Ом).", "А",
     "для двух одинаковых параллельных: Rобщ = R/2 = 5 Ом.", "Базовая"),
    ("Электродинамика", "Работа тока за 10 с: ток 3 А, напряжение 12 В?",
     "А — 36, Б — 120, В — 360, Г — 1200 (Дж).", "В", "A = UIt = 12*3*10 = 360 Дж.", "Базовая"),
    ("Электродинамика", "Напряжение увеличили в 2 раза, сопротивление не меняли. Как изменится ток?",
     "А — уменьшится в 2 раза, Б — не изменится, В — увеличится в 2 раза, Г — увеличится в 4 раза.", "В",
     "из закона Ома I=U/R, при увеличении U в 2 раза ток тоже растёт в 2 раза.", "Базовая"),
    ("Электродинамика", "В комнате включили ещё одну лампочку (параллельно к тем, что уже горят). Что произойдёт с общим сопротивлением цепи?",
     "А — увеличится, Б — уменьшится, В — не изменится, Г — сначала увеличится, потом уменьшится.", "Б",
     "при параллельном подключении общее сопротивление всегда уменьшается.", "Базовая"),
    ("Электродинамика", "Если разрезать провод пополам и соединить эти половинки параллельно, каким станет общее сопротивление по сравнению с исходным?",
     "А — таким же, Б — в 2 раза меньше, В — в 4 раза меньше, Г — в 2 раза больше.", "В",
     "каждая половинка имеет сопротивление в 2 раза меньше исходного. При параллельном соединении двух одинаковых сопротивлений общее будет ещё в 2 раза меньше. Итого: в 4 раза меньше.", "Средняя"),
    ("Электродинамика", "К источнику постоянного напряжения подключили резистор. Как изменится мощность, выделяемая на резисторе, если его сопротивление уменьшить в 2 раза?",
     "А — уменьшится в 2 раза, Б — уменьшится в 4 раза, В — увеличится в 2 раза, Г — увеличится в 4 раза.", "В",
     "мощность P=U²/R. Напряжение постоянно, R уменьшили в 2 раза → мощность увеличилась в 2 раза.", "Средняя"),
]

def seed_questions():
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        if count == 0:
            for q in QUESTIONS_DATA:
                conn.execute(
                    "INSERT INTO questions (topic, question_text, options, correct_answer, explanation, difficulty) VALUES (?, ?, ?, ?, ?, ?)",
                    q
                )
            conn.commit()
            logging.info(f"Загружено {len(QUESTIONS_DATA)} вопросов в базу.")

# ======================= CRUD =======================
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

def find_user_by_name(full_name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, full_name, role, max_user_id FROM users WHERE full_name = ?",
            (full_name.strip(),)
        ).fetchone()
    return dict(row) if row else None

def update_user_max_id(user_id: int, max_user_id: str):
    with get_connection() as conn:
        conn.execute("UPDATE users SET max_user_id = ? WHERE id = ?", (max_user_id, user_id))
        conn.commit()

def create_class(name: str, teacher_id: int) -> tuple[int | None, str | None]:
    name = name.strip()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM classes WHERE teacher_id = ? AND name = ?",
            (teacher_id, name)
        ).fetchone()
        if existing:
            return None, f"❌ Класс с названием «{name}» уже существует."
        try:
            cur = conn.execute(
                "INSERT INTO classes (name, teacher_id) VALUES (?, ?)",
                (name, teacher_id)
            )
            conn.commit()
            return cur.lastrowid, None
        except sqlite3.IntegrityError:
            return None, "❌ Не удалось создать класс."

def get_last_teacher_class(teacher_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, name FROM classes WHERE teacher_id = ? ORDER BY id DESC LIMIT 1",
            (teacher_id,)
        ).fetchone()
    return dict(row) if row else None

def get_teacher_classes(teacher_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name FROM classes WHERE teacher_id = ? ORDER BY id DESC",
            (teacher_id,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_class_students(class_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT u.id, u.full_name, u.max_user_id
            FROM class_members cm
            JOIN users u ON cm.student_id = u.id
            WHERE cm.class_id = ? AND cm.status = 'accepted'
            ORDER BY u.full_name
        """, (class_id,)).fetchall()
    return [dict(r) for r in rows]

def get_class_by_id(class_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT id, name, teacher_id FROM classes WHERE id = ?", (class_id,)).fetchone()
    return dict(row) if row else None

def add_invitation(class_id: int, student_id: int) -> bool:
    with get_connection() as conn:
        # Повторное приглашение не должно исключать принятого ученика из класса.
        cur = conn.execute("""
            INSERT INTO class_members (class_id, student_id, status)
            VALUES (?, ?, 'pending')
            ON CONFLICT(class_id, student_id) DO UPDATE SET status = 'pending'
            WHERE class_members.status != 'accepted'
        """, (class_id, student_id))
        conn.commit()
        return cur.rowcount > 0

def set_invitation_status(class_id: int, student_id: int, status: str) -> bool:
    if status not in ("accepted", "declined"):
        raise ValueError("Недопустимый статус приглашения")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE class_members SET status = ? "
            "WHERE class_id = ? AND student_id = ? AND status = 'pending'",
            (status, class_id, student_id)
        )
        conn.commit()
        return cur.rowcount > 0

def get_student_class(student_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT c.id, c.name, cm.status
            FROM classes c
            JOIN class_members cm ON c.id = cm.class_id
            WHERE cm.student_id = ? AND cm.status = 'accepted'
            ORDER BY cm.id DESC LIMIT 1
        """, (student_id,)).fetchone()
    return dict(row) if row else None

def get_pending_invitation(student_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT c.id, c.name, cm.id AS member_id
            FROM classes c
            JOIN class_members cm ON c.id = cm.class_id
            WHERE cm.student_id = ? AND cm.status = 'pending'
            ORDER BY cm.id DESC LIMIT 1
        """, (student_id,)).fetchone()
    return dict(row) if row else None

# ---------- Вопросы и тесты ----------
def get_topics() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT topic FROM questions").fetchall()
    return [r["topic"] for r in rows]

def get_questions_by_topic(topic: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, question_text, options, correct_answer, explanation, difficulty FROM questions WHERE topic = ? ORDER BY id",
            (topic,)
        ).fetchall()
    return [dict(r) for r in rows]

def create_test(teacher_id: int, topic: str, class_id: int | None) -> int:
    now = datetime.now()
    ends_at = now + timedelta(minutes=TEST_DURATION_MINUTES)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tests (teacher_id, topic, class_id, ends_at) VALUES (?, ?, ?, ?)",
            (teacher_id, topic, class_id, ends_at.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        return cur.lastrowid

def get_test(test_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
    return dict(row) if row else None

def is_test_active(test: dict) -> bool:
    try:
        ends_at = datetime.fromisoformat(test["ends_at"])
    except (KeyError, TypeError, ValueError):
        return False
    now = datetime.now(ends_at.tzinfo) if ends_at.tzinfo else datetime.now()
    return now < ends_at

def student_can_take_test(test: dict, student_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT 1 FROM class_members cm
            JOIN classes c ON c.id = cm.class_id
            WHERE cm.student_id = ? AND cm.status = 'accepted'
              AND c.teacher_id = ? AND (? IS NULL OR c.id = ?)
            LIMIT 1
        """, (student_id, test["teacher_id"], test["class_id"], test["class_id"])).fetchone()
    return row is not None

def get_available_tests(student_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT t.* FROM tests t
            JOIN classes c ON c.teacher_id = t.teacher_id
                AND (t.class_id IS NULL OR c.id = t.class_id)
            JOIN class_members cm ON cm.class_id = c.id
            LEFT JOIN test_results r ON r.test_id = t.id AND r.student_id = cm.student_id
            WHERE cm.student_id = ? AND cm.status = 'accepted' AND r.id IS NULL
            ORDER BY t.id DESC
        """, (student_id,)).fetchall()
    return [dict(row) for row in rows if is_test_active(dict(row))][:20]

def save_result(test_id: int, student_id: int, score: int, total: int):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO test_results (test_id, student_id, score, total, finished_at) VALUES (?, ?, ?, ?, ?)",
            (test_id, student_id, score, total, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()

def get_result(test_id: int, student_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT score, total FROM test_results WHERE test_id = ? AND student_id = ?",
            (test_id, student_id)
        ).fetchone()
    return dict(row) if row else None

def get_teacher_class_tests(teacher_id: int, class_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT t.* FROM tests t
            JOIN classes c ON c.id = ? AND c.teacher_id = t.teacher_id
            WHERE t.teacher_id = ? AND (t.class_id = c.id OR t.class_id IS NULL)
            ORDER BY t.id DESC
        """, (class_id, teacher_id)).fetchall()
    return [dict(row) for row in rows]

def summarize_test_results(results: list[dict]) -> dict:
    count = len(results)
    score_sum = sum(row["score"] for row in results)
    average = (
        (Decimal(score_sum) / Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if count else None
    )
    return {
        "completed_count": count,
        "score_sum": score_sum,
        "min_total": min((row["total"] for row in results), default=None),
        "max_total": max((row["total"] for row in results), default=None),
        "average_score": average,
    }

def get_class_test_report(teacher_id: int, class_id: int, test_id: int) -> dict | None:
    cls = get_class_by_id(class_id)
    test = get_test(test_id)
    if (
        not cls or cls["teacher_id"] != teacher_id
        or not test or test["teacher_id"] != teacher_id
        or test["class_id"] not in (None, class_id)
    ):
        return None
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT u.id, u.full_name, r.id AS result_id, r.score, r.total, r.finished_at
            FROM users u
            LEFT JOIN test_results r ON r.student_id = u.id AND r.test_id = ?
                AND r.finished_at IS NOT NULL AND r.score IS NOT NULL AND r.total > 0
            WHERE EXISTS (
                SELECT 1 FROM class_members cm
                WHERE cm.student_id = u.id AND cm.class_id = ? AND cm.status = 'accepted'
            ) OR (? = 1 AND r.id IS NOT NULL)
            ORDER BY u.full_name COLLATE NOCASE, u.id
        """, (test_id, class_id, int(test["class_id"] == class_id))).fetchall()
        # Общую статистику считаем без соединения с class_members:
        # ученик в нескольких классах учитывается здесь только один раз.
        all_results = conn.execute("""
            SELECT score, total FROM test_results
            WHERE test_id = ? AND finished_at IS NOT NULL AND score IS NOT NULL AND total > 0
        """, (test_id,)).fetchall()
    students = [dict(row) for row in rows]
    completed = [row for row in students if row["result_id"] is not None]
    return {
        "class": cls,
        "test": test,
        "students": students,
        "class_stats": summarize_test_results(completed),
        "overall_stats": summarize_test_results([dict(row) for row in all_results]),
    }

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
user_states = {}       # состояния диалога (регистрация, вход, создание класса, выдача теста)
active_tests = {}      # current — индекс вопроса, ожидающего ответа; student_id хранится до сохранения результата
test_locks = {}        # блокировки защищают от двойных и одновременных нажатий ученика

def get_test_lock(user_id: str) -> asyncio.Lock:
    if user_id not in test_locks:
        test_locks[user_id] = asyncio.Lock()
    return test_locks[user_id]

# ======================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =======================
def build_main_menu(role: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if role == "teacher":
        builder.row(
            CallbackButton(text="Создать класс", payload="menu_create_class"),
            CallbackButton(text="Пригласить ученика", payload="menu_invite"),
        )
        builder.row(
            CallbackButton(text="Мои классы", payload="menu_my_classes"),
            CallbackButton(text="Тесты", payload="menu_teacher_tests"),
        )
    else:
        builder.row(CallbackButton(text="Мой класс", payload="menu_my_class"))
        builder.row(CallbackButton(text="Мои тесты", payload="menu_my_tests"))
    builder.row(CallbackButton(text="Выйти", payload="logout"))
    return builder

def build_error_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    return builder

def build_teacher_tests_menu() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Выдать тест", payload="menu_give_test"))
    builder.row(CallbackButton(text="Результаты тестов", payload="menu_test_results"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    return builder

def build_teacher_tests_back_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    return builder

def build_invitation_keyboard(class_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Принять", payload=f"accept_{class_id}"),
        CallbackButton(text="Отклонить", payload=f"decline_{class_id}")
    )
    return builder

def build_invite_class_selection(teacher_id: int) -> tuple[str, InlineKeyboardBuilder]:
    classes = get_teacher_classes(teacher_id)
    if not classes:
        return "У вас пока нет классов. Сначала создайте класс командой /create_class.", build_error_keyboard()
    builder = InlineKeyboardBuilder()
    for cls in classes:
        builder.row(CallbackButton(text=cls["name"], payload=f"select_class_for_invite_{cls['id']}"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    return "Выберите класс, в который хотите пригласить ученика:", builder

def build_student_class_message(student_id: int) -> tuple[str, InlineKeyboardBuilder]:
    cls = get_student_class(student_id)
    text = f"📚 Вы в классе «{cls['name']}»." if cls else "Вы пока не вступили ни в один класс."
    pending = get_pending_invitation(student_id)
    if pending:
        # Показываем сохранённое приглашение, даже если личное сообщение не дошло
        # или ученик уже состоит в другом классе.
        text += f"\n\n📩 Вас приглашают в класс «{pending['name']}». Принять приглашение?"
        builder = build_invitation_keyboard(pending["id"])
        builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    else:
        builder = build_error_keyboard()
    return text, builder

async def send_start_message(event_or_message, user_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Учитель", payload="teacher"),
        CallbackButton(text="Ученик", payload="student")
    )
    if hasattr(event_or_message, "answer"):
        await event_or_message.answer(
            "Привет! 👋 Я твой учебный бот. Ты ученик или учитель?",
            attachments=[builder.as_markup()]
        )
    else:
        await bot.send_message(
            user_id=int(user_id),
            text="Привет! 👋 Я твой учебный бот. Ты ученик или учитель?",
            attachments=[builder.as_markup()]
        )

# ======================= КОМАНДЫ БОТА =======================
async def set_bot_commands():
    commands = [
        BotCommand(name="start", description="Запустить бота"),
        BotCommand(name="register", description="Регистрация"),
        BotCommand(name="profile", description="Мой профиль и меню"),
        BotCommand(name="create_class", description="Создать класс (учитель)"),
        BotCommand(name="invite", description="Пригласить ученика (учитель)"),
        BotCommand(name="my_class", description="Мой класс (ученик)"),
        BotCommand(name="tests", description="Тесты и результаты"),
    ]
    await SetCommands(bot=bot, commands=commands).fetch()

# ======================= /start и /register =======================
@dp.message_created(Command('start'))
async def hello(event: MessageCreated):
    max_user_id = str(event.from_user.user_id)
    user = auth_cache.get(max_user_id)
    if user:
        await event.message.answer(
            f"Вы вошли как {user['full_name']} ({user['role']}).\n"
            f"Для доступа к меню используйте /profile."
        )
        return
    await send_start_message(event.message, max_user_id)

@dp.message_created(Command('register'))
async def cmd_register(event: MessageCreated):
    max_user_id = str(event.from_user.user_id)
    user = auth_cache.get(max_user_id)
    if user:
        await event.message.answer(
            f"Вы уже авторизованы как {user['full_name']} ({user['role']}).\n"
            f"Для доступа к меню используйте /profile."
        )
        return
    await send_start_message(event.message, max_user_id)

# ======================= ВЫХОД =======================
@dp.message_callback(F.callback.payload == "logout")
async def logout_confirm(event: MessageCallback):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Да, выйти", payload="logout_yes"),
        CallbackButton(text="Отмена", payload="logout_no")
    )
    await event.edit(text="Вы уверены, что хотите выйти из аккаунта?", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload == "logout_yes")
async def logout_yes(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    auth_cache.clear(user_id)
    user_states.pop(user_id, None)
    active_tests.pop(user_id, None)

    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Учитель", payload="teacher"),
        CallbackButton(text="Ученик", payload="student")
    )
    await event.edit(text="Вы вышли из аккаунта. Выберите роль:", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload == "logout_no")
async def logout_no(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if user:
        builder = build_main_menu(user["role"])
        await event.edit(
            text=f"👤 Профиль:\nФИО: {user['full_name']}\nРоль: {user['role']}",
            attachments=[builder.as_markup()]
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.row(
            CallbackButton(text="Учитель", payload="teacher"),
            CallbackButton(text="Ученик", payload="student")
        )
        await event.edit(text="Вы не авторизованы. Выберите роль:", attachments=[builder.as_markup()])

# ======================= НАЗАД =======================
@dp.message_callback(F.callback.payload == "back")
async def back(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user_states.pop(user_id, None)
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Учитель", payload="teacher"),
        CallbackButton(text="Ученик", payload="student")
    )
    await event.edit(
        text="Привет! 👋 Я твой учебный бот. Ты ученик или учитель?",
        attachments=[builder.as_markup()]
    )

# ======================= ВЫБОР РОЛИ =======================
@dp.message_callback(F.callback.payload == "teacher")
async def teacher_menu(event: MessageCallback):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Войти", payload="teacher_log_in"),
        CallbackButton(text="Зарегистрироваться", payload="teacher_reg")
    )
    builder.row(CallbackButton(text="Назад", payload="back"))
    await event.edit(text="Вы хотите войти или зарегистрироваться?", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload == "student")
async def student_menu(event: MessageCallback):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Войти", payload="student_log_in"),
        CallbackButton(text="Зарегистрироваться", payload="student_reg")
    )
    builder.row(CallbackButton(text="Назад", payload="back"))
    await event.edit(text="Вы хотите войти или зарегистрироваться?", attachments=[builder.as_markup()])

# ======================= РЕГИСТРАЦИЯ / ВХОД =======================
@dp.message_callback(F.callback.payload.in_(["teacher_reg", "student_reg"]))
async def start_registration(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    role = "teacher" if event.callback.payload == "teacher_reg" else "student"
    user_states[user_id] = {"action": "register", "role": role, "step": "name"}
    await event.edit(text="Введите ваше ФИО (Фамилия Имя Отчество) одним сообщением:", attachments=[])

@dp.message_callback(F.callback.payload.in_(["teacher_log_in", "student_log_in"]))
async def start_login(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    resume_test_id = user_states.get(user_id, {}).get("resume_test_id")
    user_states[user_id] = {"action": "login", "step": "name"}
    if resume_test_id is not None:
        user_states[user_id]["resume_test_id"] = resume_test_id
    await event.edit(text="Введите ваше ФИО (Фамилия Имя Отчество) одним сообщением:", attachments=[])

# ======================= КОМАНДЫ УЧИТЕЛЯ =======================
@dp.message_created(Command("create_class"))
async def cmd_create_class(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.message.answer("Только учитель может создавать класс. Войдите как учитель.", attachments=[build_error_keyboard().as_markup()])
        return
    user_states[user_id] = {"action": "create_class", "step": "name"}
    await event.message.answer("Введите название класса (например, «9А»):")

@dp.message_created(Command("invite"))
async def cmd_invite(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.message.answer("Только учитель может приглашать учеников. Войдите как учитель.", attachments=[build_error_keyboard().as_markup()])
        return
    user_states.pop(user_id, None)
    text, builder = build_invite_class_selection(user["id"])
    await event.message.answer(text, attachments=[builder.as_markup()])

# ======================= КОМАНДА УЧЕНИКА =======================
@dp.message_created(Command("my_class"))
async def cmd_my_class(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user:
        await event.message.answer("Сначала войдите: /start → Войти. Затем откройте /my_class.", attachments=[build_error_keyboard().as_markup()])
        return
    if user["role"] != "student":
        await event.message.answer("Эта команда доступна только ученикам.", attachments=[build_error_keyboard().as_markup()])
        return
    text, builder = build_student_class_message(user["id"])
    await event.message.answer(text, attachments=[builder.as_markup()])

# ======================= ПРИГЛАШЕНИЯ =======================
async def respond_to_invitation(event: MessageCallback, status: str):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user:
        await event.answer(notification="Сессия истекла. Войдите через /start, затем откройте /my_class.")
        return
    if user["role"] != "student":
        await event.answer(notification="Приглашение предназначено ученику. Войдите как ученик.")
        return
    try:
        class_id = int(event.callback.payload.rsplit("_", 1)[-1])
        if not 0 < class_id <= 9223372036854775807:
            raise ValueError("Недопустимый ID класса")
    except (ValueError, AttributeError):
        await event.answer(notification="Некорректное приглашение. Откройте /my_class.")
        return
    cls = get_class_by_id(class_id)
    if not cls or not set_invitation_status(class_id, user["id"], status):
        await event.answer(notification="Приглашение не найдено или уже обработано. Откройте /my_class.")
        return
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Мой класс", payload="menu_my_class"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    text = (
        f"✅ Вы приняли приглашение в класс «{cls['name']}»!"
        if status == "accepted"
        else f"Вы отклонили приглашение в класс «{cls['name']}»."
    )
    await event.edit(text=text, attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload.startswith("accept_"))
async def accept_invite(event: MessageCallback):
    await respond_to_invitation(event, "accepted")

@dp.message_callback(F.callback.payload.startswith("decline_"))
async def decline_invite(event: MessageCallback):
    await respond_to_invitation(event, "declined")

# ======================= МЕНЮ-КНОПКИ УЧИТЕЛЯ =======================
@dp.message_callback(F.callback.payload == "menu_create_class")
async def menu_create_class(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Только учитель может создавать класс.")
        return
    user_states[user_id] = {"action": "create_class", "step": "name"}
    await event.edit(text="Введите название класса (например, «9А»):", attachments=[])

@dp.message_callback(F.callback.payload == "menu_invite")
async def menu_invite(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Только учитель может приглашать учеников.")
        return
    user_states.pop(user_id, None)
    text, builder = build_invite_class_selection(user["id"])
    await event.edit(text=text, attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload.startswith("select_class_for_invite_"))
async def select_class_for_invite(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Только учитель может приглашать учеников.")
        return
    try:
        class_id = int(event.callback.payload.rsplit("_", 1)[-1])
        if not 0 < class_id <= 9223372036854775807:
            raise ValueError("Недопустимый ID класса")
    except (ValueError, AttributeError):
        builder = build_error_keyboard()
        await event.edit(text="❌ Некорректный запрос. Попробуйте снова.", attachments=[builder.as_markup()])
        return
    cls = get_class_by_id(class_id)
    if not cls or cls["teacher_id"] != user["id"]:
        builder = build_error_keyboard()
        await event.edit(text="❌ Класс не найден или принадлежит другому учителю.", attachments=[builder.as_markup()])
        return
    user_states[user_id] = {"action": "invite", "step": "name", "class_id": class_id, "class_name": cls["name"]}
    await event.edit(
        text=f"Вы приглашаете в класс «{cls['name']}».\nВведите полное ФИО ученика (Фамилия Имя Отчество):",
        attachments=[build_error_keyboard().as_markup()]
    )

@dp.message_callback(F.callback.payload.startswith("invite_to_class_"))
async def invite_to_class_from_creation(event: MessageCallback):
    await select_class_for_invite(event)

@dp.message_callback(F.callback.payload == "menu_my_class")
async def menu_my_class(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user:
        await event.answer(notification="Сессия истекла. Войдите через /start, затем откройте /my_class.")
        return
    if user["role"] != "student":
        await event.answer(notification="Эта кнопка доступна только ученикам.")
        return
    text, builder = build_student_class_message(user["id"])
    await event.edit(text=text, attachments=[builder.as_markup()])

# ======================= МОИ КЛАССЫ =======================
@dp.message_callback(F.callback.payload == "menu_my_classes")
async def menu_my_classes(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Эта кнопка доступна только учителям.")
        return
    classes = get_teacher_classes(user["id"])
    builder = InlineKeyboardBuilder()
    for cls in classes:
        builder.row(CallbackButton(text=cls["name"], payload=f"view_class_{cls['id']}"))
    builder.row(CallbackButton(text="Назад", payload="back_to_profile"))
    await event.edit(text=f"Количество ваших классов: {len(classes)}", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload.startswith("view_class_"))
async def view_class(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Только учитель может просматривать классы.")
        return
    class_id_str = event.callback.payload.rsplit("_", 1)[-1]
    if not class_id_str.isdigit():
        builder = build_error_keyboard()
        await event.edit(text="❌ Некорректный запрос.", attachments=[builder.as_markup()])
        return
    class_id = int(class_id_str)
    students = get_class_students(class_id)
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM classes WHERE id = ?", (class_id,)).fetchone()
    class_name = row["name"] if row else "Класс"
    if students:
        lines = []
        for s in students:
            parts = s["full_name"].split()
            lines.append(f"{parts[0]}, {parts[1]}" if len(parts) >= 2 else s["full_name"])
        text = f"📚 Класс «{class_name}»:\n\n" + "\n".join(lines)
    else:
        text = f"📚 Класс «{class_name}»:\n\nУчеников пока нет."
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Назад к классам", payload="menu_my_classes"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    await event.edit(text=text, attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload == "back_to_profile")
async def back_to_profile(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user_states.pop(user_id, None)
    user = auth_cache.get(user_id)
    if user:
        builder = build_main_menu(user["role"])
        await event.edit(
            text=f"👤 Профиль:\nФИО: {user['full_name']}\nРоль: {user['role']}",
            attachments=[builder.as_markup()]
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.row(
            CallbackButton(text="Учитель", payload="teacher"),
            CallbackButton(text="Ученик", payload="student")
        )
        await event.edit(text="Вы не авторизованы. Выберите роль:", attachments=[builder.as_markup()])

# ======================= ПРОФИЛЬ =======================
@dp.message_created(Command("profile"))
async def cmd_profile(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    cached_user = auth_cache.get(user_id)
    if cached_user:
        builder = build_main_menu(cached_user["role"])
        await event.message.answer(
            f"👤 Профиль:\nФИО: {cached_user['full_name']}\nРоль: {cached_user['role']}",
            attachments=[builder.as_markup()]
        )
    else:
        await event.message.answer("Сначала войдите: /start → Войти.", attachments=[build_error_keyboard().as_markup()])

# ======================= МЕНЮ ТЕСТОВ И РЕЗУЛЬТАТЫ (УЧИТЕЛЬ) =======================
def short_report_text(value, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def format_report_date(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "дата не указана"


def format_average_stats(stats: dict) -> str:
    average = stats["average_score"]
    if average is None:
        return "— (нет завершённых прохождений)"
    text = format(average, ".2f").replace(".", ",")
    if stats["min_total"] == stats["max_total"]:
        text += f" из {stats['max_total']}"
    return text


def get_report_page(items: list, page: int) -> tuple[list, int, int, int]:
    total_pages = max(1, (len(items) + RESULTS_PAGE_SIZE - 1) // RESULTS_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * RESULTS_PAGE_SIZE
    return items[start:start + RESULTS_PAGE_SIZE], page, total_pages, start


def add_report_pagination(builder: InlineKeyboardBuilder, page: int, total_pages: int, prefix: str):
    buttons = []
    if page > 0:
        buttons.append(CallbackButton(text="← Назад", payload=f"{prefix}_{page - 1}"))
    if page + 1 < total_pages:
        buttons.append(CallbackButton(text="Далее →", payload=f"{prefix}_{page + 1}"))
    if buttons:
        builder.row(*buttons)


def parse_report_callback(payload, prefix: str, count: int) -> tuple[int, ...] | None:
    if not isinstance(payload, str) or not payload.startswith(prefix):
        return None
    try:
        values = tuple(int(part) for part in payload[len(prefix):].split("_"))
    except ValueError:
        return None
    if len(values) != count or any(not 0 <= value <= 9223372036854775807 for value in values):
        return None
    return values


async def require_teacher_for_tests(event: MessageCallback) -> dict | None:
    user = auth_cache.get(str(event.from_user.user_id))
    if not user:
        await safe_test_callback(event, notification="Сессия входа истекла. Войдите через /start.")
        return None
    if user["role"] != "teacher":
        await safe_test_callback(event, notification="Меню тестов и результаты классов доступны только учителю.")
        return None
    return user


@dp.message_callback(F.callback.payload == "menu_teacher_tests")
async def menu_teacher_tests(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    user_states.pop(str(event.from_user.user_id), None)
    await safe_test_callback(
        event, text="📝 Тесты\nВыдайте новый тест или посмотрите результаты учеников.",
        attachments=[build_teacher_tests_menu().as_markup()]
    )


async def show_results_classes(event: MessageCallback, teacher: dict, page: int = 0):
    classes = get_teacher_classes(teacher["id"])
    items, page, total_pages, _ = get_report_page(classes, page)
    builder = InlineKeyboardBuilder()
    for cls in items:
        builder.row(CallbackButton(
            text=short_report_text(cls["name"], 128),
            payload=f"results_tests_{cls['id']}_0"
        ))
    add_report_pagination(builder, page, total_pages, "results_classes")
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    text = (
        f"📊 Результаты тестов\nВыберите класс.\nСтраница {page + 1}/{total_pages}."
        if classes else "У вас пока нет классов. Создайте класс, чтобы выдавать тесты и смотреть результаты."
    )
    await safe_test_callback(event, text=text, attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload == "menu_test_results")
async def menu_test_results(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    user_states.pop(str(event.from_user.user_id), None)
    await show_results_classes(event, teacher)


@dp.message_callback(F.callback.payload.startswith("results_classes_"))
async def results_classes_page(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    values = parse_report_callback(event.callback.payload, "results_classes_", 1)
    if values is None:
        await safe_test_callback(event, notification="Некорректная страница. Откройте «Результаты тестов» заново.")
        return
    await show_results_classes(event, teacher, values[0])


@dp.message_callback(F.callback.payload.startswith("results_tests_"))
async def results_tests_page(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    values = parse_report_callback(event.callback.payload, "results_tests_", 2)
    if values is None:
        await safe_test_callback(event, notification="Некорректный запрос. Выберите класс заново.")
        return
    class_id, page = values
    cls = get_class_by_id(class_id)
    if not cls or cls["teacher_id"] != teacher["id"]:
        await safe_test_callback(event, notification="Этот класс недоступен.")
        return
    tests = get_teacher_class_tests(teacher["id"], class_id)
    items, page, total_pages, _ = get_report_page(tests, page)
    builder = InlineKeyboardBuilder()
    for test in items:
        label = f"№{test['id']} · {short_report_text(test['topic'], 65)} · {format_report_date(test['created_at'])}"
        builder.row(CallbackButton(
            text=short_report_text(label, 128),
            payload=f"results_view_{class_id}_{test['id']}_0"
        ))
    add_report_pagination(builder, page, total_pages, f"results_tests_{class_id}")
    builder.row(CallbackButton(text="Назад к классам", payload="menu_test_results"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    title = f"📊 Класс «{short_report_text(cls['name'], 120)}»"
    text = (
        f"{title}\nВыберите тест. В списке также доступны завершённые по сроку тесты.\n"
        f"Страница {page + 1}/{total_pages}."
        if tests else f"{title}\nЭтому классу ещё не выдавались тесты."
    )
    await safe_test_callback(event, text=text, attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload.startswith("results_view_"))
async def view_test_results(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    values = parse_report_callback(event.callback.payload, "results_view_", 3)
    if values is None:
        await safe_test_callback(event, notification="Некорректный запрос. Выберите тест заново.")
        return
    class_id, test_id, page = values
    report = get_class_test_report(teacher["id"], class_id, test_id)
    if report is None:
        await safe_test_callback(event, notification="Этот тест или класс недоступен.")
        return
    cls, test = report["class"], report["test"]
    students = report["students"]
    items, page, total_pages, start = get_report_page(students, page)
    class_stats, overall_stats = report["class_stats"], report["overall_stats"]
    lines = [
        f"📊 Результаты теста №{test_id}",
        f"Тема: {short_report_text(test['topic'], 180)}",
        f"Класс: {short_report_text(cls['name'], 120)}",
        f"Выдан: {format_report_date(test['created_at'])}",
        "",
        f"Завершили в классе: {class_stats['completed_count']} из {len(students)}.",
        f"Средний балл по классу: {format_average_stats(class_stats)}.",
    ]
    if test["class_id"] is None:
        lines.extend([
            "",
            f"Всего завершённых прохождений теста: {overall_stats['completed_count']}.",
            f"Средний балл по тесту (все классы): {format_average_stats(overall_stats)}.",
        ])
    lines.extend(["", "Средний балл — количество правильных ответов среди завершивших тест.", "", "Ученики:"])
    for number, student in enumerate(items, start=start + 1):
        name = short_report_text(student["full_name"], 180)
        result = (
            f"{student['score']} из {student['total']} правильных"
            if student["result_id"] is not None else "не пройден"
        )
        lines.append(f"{number}. {name} — {result}.")
    if not students:
        lines.append("В этом классе пока нет учеников.")
    if test["class_id"] is None:
        lines.extend(["", "Для теста, выданного всем классам, ученики сгруппированы по текущему составу классов."])
    lines.extend(["", f"Страница {page + 1}/{total_pages}."])
    builder = InlineKeyboardBuilder()
    prefix = f"results_view_{class_id}_{test_id}"
    add_report_pagination(builder, page, total_pages, prefix)
    builder.row(CallbackButton(text="Обновить", payload=f"{prefix}_{page}"))
    builder.row(CallbackButton(text="К списку тестов", payload=f"results_tests_{class_id}_0"))
    builder.row(CallbackButton(text="Выбрать класс", payload="menu_test_results"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    await safe_test_callback(event, text="\n".join(lines), attachments=[builder.as_markup()])


# ======================= ВЫДАЧА ТЕСТА (УЧИТЕЛЬ) =======================
@dp.message_callback(F.callback.payload == "menu_give_test")
async def menu_give_test(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Только учитель может выдавать тесты.")
        return
    topics = get_topics()
    if not topics:
        builder = build_teacher_tests_back_keyboard()
        await event.edit(text="Вопросы ещё не загружены.", attachments=[builder.as_markup()])
        return
    builder = InlineKeyboardBuilder()
    for i, topic in enumerate(topics):
        builder.row(CallbackButton(text=topic, payload=f"select_topic_{i}"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    # сохраняем список тем в состоянии, чтобы потом по индексу найти
    user_states[user_id] = {"action": "give_test", "step": "topic", "topics": topics}
    await event.edit(text="Выберите тему теста:", attachments=[builder.as_markup()])

@dp.message_callback(F.callback.payload.startswith("select_topic_"))
async def select_topic(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    state = user_states.get(user_id)
    teacher = auth_cache.get(user_id)
    if not teacher or teacher["role"] != "teacher" or not state or state.get("action") != "give_test":
        await event.answer(notification="Сессия истекла, начните заново.")
        return
    try:
        idx = int(event.callback.payload.rsplit("_", 1)[-1])
    except (ValueError, AttributeError):
        await event.answer(notification="Ошибка выбора темы.")
        return
    topics = state.get("topics", [])
    if not 0 <= idx < len(topics):
        await event.answer(notification="Тема не найдена.")
        return
    topic = topics[idx]
    state["topic"] = topic
    state["step"] = "class"
    # показываем список классов и кнопку "Всем классам"
    classes = get_teacher_classes(teacher["id"])
    if not classes:
        await event.edit(text="Сначала создайте класс и пригласите учеников.", attachments=[build_teacher_tests_back_keyboard().as_markup()])
        return
    builder = InlineKeyboardBuilder()
    for cls in classes:
        builder.row(CallbackButton(text=cls["name"], payload=f"test_class_{cls['id']}"))
    builder.row(CallbackButton(text="Всем классам", payload="test_class_all"))
    builder.row(CallbackButton(text="Назад к темам", payload="menu_give_test"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    await event.edit(
        text=f"Тема: «{topic}». Выберите класс, которому выдать тест:",
        attachments=[builder.as_markup()]
    )

@dp.message_callback(F.callback.payload.startswith("test_class_"))
async def test_class_selected(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    state = user_states.get(user_id)
    teacher = auth_cache.get(user_id)
    if not teacher or teacher["role"] != "teacher" or not state or state.get("action") != "give_test":
        await event.answer(notification="Сессия истекла, начните заново.")
        return
    topic = state.get("topic")
    if not topic:
        await event.answer(notification="Тема не выбрана.")
        return

    payload = event.callback.payload
    if payload == "test_class_all":
        # Всем классам учителя
        classes = get_teacher_classes(teacher["id"])
        if not classes:
            await event.answer(notification="У вас пока нет классов. Сначала создайте класс.")
            return
        class_ids = [c["id"] for c in classes]
        target_desc = "всем классам"
        class_id_for_test = None
    else:
        try:
            class_id = int(payload.rsplit("_", 1)[-1])
            if not 0 < class_id <= 9223372036854775807:
                raise ValueError("Недопустимый ID класса")
        except (ValueError, AttributeError):
            await event.answer(notification="Ошибка выбора класса.")
            return
        cls = get_class_by_id(class_id)
        if not cls or cls["teacher_id"] != teacher["id"]:
            await event.answer(notification="Класс не найден или принадлежит другому учителю.")
            return
        class_ids = [class_id]
        target_desc = f"классу «{cls['name']}»"
        class_id_for_test = class_id

    try:
        questions = prepare_test_questions(topic)
    except ValueError:
        logging.exception("Некорректные вопросы по теме %s", topic)
        await event.answer(notification="В вопросах этой темы ошибка: проверьте варианты и правильные ответы в базе.")
        return
    if not questions:
        await event.answer(notification="В этой теме пока нет вопросов.")
        return

    # Создаём тест
    test_id = create_test(teacher["id"], topic, class_id_for_test)

    # Собираем учеников из выбранных классов
    students = []
    for cid in class_ids:
        students.extend(get_class_students(cid))
    # Убираем дубликаты (если ученик в нескольких классах)
    unique_students = {s["id"]: s for s in students}.values()

    # Отправляем приглашение пройти тест
    sent_count = 0
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Пройти тест", payload=f"start_test_{test_id}"))
    for s in unique_students:
        if s["max_user_id"]:
            try:
                sent = await bot.send_message(
                    user_id=int(s["max_user_id"]),
                    text=f"📝 Учитель {teacher['full_name']} выдал тест по теме «{topic}»!\n"
                         f"Тест доступен {TEST_DURATION_MINUTES} минут с момента выдачи.\n"
                         "Нажмите «Пройти тест» или откройте /tests.",
                    attachments=[builder.as_markup()]
                )
                if sent is not None:
                    sent_count += 1
                else:
                    logging.warning("MAX не подтвердил отправку теста %s ученику %s", test_id, s["id"])
            except Exception as e:
                logging.error(f"Не удалось отправить тест ученику {s['full_name']}: {e}")

    user_states.pop(user_id, None)
    result_text = (
        f"✅ Тест по теме «{topic}» выдан {target_desc}.\n"
        f"Приглашение отправлено {sent_count} ученикам."
    )
    if sent_count == 0:
        result_text += "\n⚠️ Сообщения не доставлены. Ученики могут открыть тест после входа через «Мои тесты» или /tests."
    builder = build_teacher_tests_back_keyboard()
    await event.edit(text=result_text, attachments=[builder.as_markup()])

# ======================= ПРОХОЖДЕНИЕ ТЕСТА (УЧЕНИК) =======================
def parse_question_options(raw: str) -> list[tuple[str, str]]:
    """Разбираем метки вариантов, не разрезая числа с десятичной запятой."""
    letters = ("А", "Б", "В", "Г")
    raw = raw.strip()
    values = None
    if raw.startswith(("[", "{")):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректный JSON вариантов ответа") from exc
        if isinstance(decoded, list) and len(decoded) == 4:
            values = decoded
        elif isinstance(decoded, dict) and set(decoded) == set(letters):
            values = [decoded[letter] for letter in letters]
    else:
        # Метки начинаются с новой строки или после разделителя списка.
        # Единица измерения «(А)» внутри варианта не является новой меткой.
        matches = list(re.finditer(r"(?:^|(?<=[,;\n]))[ \t]*([АБВГ])\s*[—–\-).:]\s*", raw, re.IGNORECASE))
        if len(matches) == 4 and tuple(m[1].upper() for m in matches) == letters:
            values = [
                raw[match.end():matches[i + 1].start() if i + 1 < 4 else len(raw)]
                .strip().rstrip(",;").strip()
                for i, match in enumerate(matches)
            ]
        elif not matches:
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(lines) == 4:
                values = lines
    if values is None or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Нужны четыре непустых варианта А, Б, В, Г")
    return list(zip(letters, (value.strip() for value in values)))


def prepare_test_questions(topic: str) -> list[dict]:
    questions = get_questions_by_topic(topic)
    for question in questions:
        question["parsed_options"] = parse_question_options(question["options"])
        question["correct_answer"] = question["correct_answer"].strip().upper()
        if question["correct_answer"] not in ("А", "Б", "В", "Г"):
            raise ValueError(f"Некорректный правильный ответ в вопросе {question['id']}")
        options_text = "\n".join(f"{letter} — {value}" for letter, value in question["parsed_options"])
        if len(question["question_text"]) + len(options_text) + len(topic) + 100 > 4000:
            raise ValueError(f"Слишком длинный вопрос {question['id']}")
    return questions


async def safe_test_callback(event: MessageCallback, *, text=None, notification=None, attachments=None) -> bool:
    """Ошибка обновления старого сообщения не должна останавливать тест."""
    try:
        if text is None:
            result = await event.answer(notification=notification)
        else:
            result = await event.edit(
                text=text, attachments=attachments if attachments is not None else [],
                notification=notification
            )
        if result is None or result is False or getattr(result, "success", True) is False:
            logging.warning("MAX не подтвердил обработку callback теста")
            return False
        return True
    except Exception:
        logging.exception("Не удалось ответить на callback теста")
        return False


async def deliver_test_message(max_user_id, text: str, attachments=None) -> bool:
    try:
        sent = await bot.send_message(
            user_id=int(max_user_id), text=text,
            attachments=attachments if attachments is not None else []
        )
        if sent is None:
            logging.warning("MAX не подтвердил доставку сообщения теста пользователю %s", max_user_id)
            return False
        return True
    except Exception:
        logging.exception("Не удалось доставить сообщение теста пользователю %s", max_user_id)
        return False


def build_tests_message(student_id: int) -> tuple[str, InlineKeyboardBuilder]:
    tests = get_available_tests(student_id)
    builder = InlineKeyboardBuilder()
    for test in tests:
        builder.row(CallbackButton(
            text=f"Тест №{test['id']}: {test['topic']}"[:128],
            payload=f"start_test_{test['id']}"
        ))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    text = (
        "📝 Доступные тесты. Выберите тест, чтобы начать или продолжить прохождение:"
        if tests else "Сейчас нет доступных непройденных тестов."
    )
    return text, builder


@dp.message_created(Command("tests"))
async def cmd_tests(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user:
        await event.message.answer("Сначала войдите через /start, затем откройте /tests.")
        return
    if user["role"] == "teacher":
        user_states.pop(user_id, None)
        await event.message.answer(
            "📝 Тесты\nВыдайте новый тест или посмотрите результаты учеников.",
            attachments=[build_teacher_tests_menu().as_markup()]
        )
        return
    if user["role"] != "student":
        await event.message.answer("Эта команда доступна только ученикам.")
        return
    text, builder = build_tests_message(user["id"])
    await event.message.answer(text, attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload == "menu_my_tests")
async def menu_my_tests(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user:
        await safe_test_callback(event, notification="Сессия входа истекла. Войдите через /start и откройте /tests.")
        return
    if user["role"] != "student":
        await safe_test_callback(event, notification="Эта кнопка доступна только ученикам.")
        return
    text, builder = build_tests_message(user["id"])
    await safe_test_callback(event, text=text, attachments=[builder.as_markup()])


def build_test_resume_keyboard(test_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Продолжить тест", payload=f"start_test_{test_id}"))
    builder.row(CallbackButton(text="Мои тесты", payload="menu_my_tests"))
    return builder


async def notify_test_delivery_failure(user_id: str, test_id: int):
    builder = build_test_resume_keyboard(test_id)
    await deliver_test_message(
        user_id,
        "⚠️ Не удалось доставить вопрос или результат. Ваши принятые ответы сохранены в текущей сессии.\n"
        "Нажмите «Продолжить тест». Также тест можно открыть через /tests.",
        [builder.as_markup()]
    )


@dp.message_callback(F.callback.payload.startswith("start_test_"))
async def start_test(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    async with get_test_lock(user_id):
        try:
            test_id = int(event.callback.payload.rsplit("_", 1)[-1])
            if not 0 < test_id <= 9223372036854775807:
                raise ValueError("Недопустимый ID теста")
        except (ValueError, AttributeError):
            await safe_test_callback(event, notification="Некорректный тест. Откройте /tests.")
            return

        user = auth_cache.get(user_id)
        if not user:
            # Доставка приглашения не означает, что парольная сессия ещё активна.
            user_states[user_id] = {"action": "login", "step": "name", "resume_test_id": test_id}
            login_text = (
                "Для прохождения теста нужно войти в аккаунт ученика. Сессия входа могла истечь.\n"
                "Введите ваше ФИО, как при регистрации. После входа появится кнопка выбранного теста."
            )
            if not await safe_test_callback(event, text=login_text):
                await deliver_test_message(user_id, login_text)
            return
        if user["role"] != "student":
            await safe_test_callback(event, notification="Только ученик может проходить тест. Войдите как ученик.")
            return

        test = get_test(test_id)
        if not test:
            await safe_test_callback(event, notification="Тест не найден. Откройте /tests.")
            return
        if not student_can_take_test(test, user["id"]):
            await safe_test_callback(event, notification="Этот тест не назначен вашему классу.")
            return

        state = active_tests.get(user_id)
        if state and state["student_id"] != user["id"]:
            active_tests.pop(user_id, None)
            state = None

        existing = get_result(test_id, user["id"])
        if existing:
            if state and state["test_id"] == test_id:
                active_tests.pop(user_id, None)
            result_text = f"🏁 Вы уже прошли этот тест. Результат: {existing['score']}/{existing['total']}."
            if not await safe_test_callback(event, text=result_text, attachments=[build_main_menu("student").as_markup()]):
                await deliver_test_message(user_id, result_text)
            return
        if not is_test_active(test):
            if state and state["test_id"] == test_id:
                await finish_student_test(user_id, user_id, expired=True)
            await safe_test_callback(event, text="⏰ Срок прохождения этого теста истёк.", attachments=[build_main_menu("student").as_markup()])
            return

        if state and state["test_id"] != test_id:
            if is_test_active(state):
                await safe_test_callback(
                    event, text="Сначала завершите уже начатый тест.",
                    attachments=[build_test_resume_keyboard(state["test_id"]).as_markup()]
                )
                return
            await finish_student_test(user_id, user_id, expired=True)
            state = None

        if state is None:
            try:
                questions = prepare_test_questions(test["topic"])
            except ValueError:
                logging.exception("Ошибка вопросов теста %s", test_id)
                await safe_test_callback(event, notification="Вопросы теста заполнены некорректно. Сообщите учителю.")
                return
            if not questions:
                await safe_test_callback(event, notification="В тесте пока нет вопросов.")
                return
            active_tests[user_id] = {
                "test_id": test_id,
                "student_id": user["id"],
                "topic": test["topic"],
                "ends_at": test["ends_at"],
                "questions": questions,
                "current": 0,
                "score": 0,
                "question_sent": False,
            }

        user_states.pop(user_id, None)
        # Не сбрасываем прогресс при повторном нажатии. Доставляем текущий вопрос.
        delivered = await send_question(event.from_user.user_id, user_id)
        await safe_test_callback(
            event,
            notification="Вопрос отправлен." if delivered else "Не удалось отправить вопрос. Нажмите «Пройти тест» ещё раз."
        )
        if not delivered:
            await notify_test_delivery_failure(user_id, test_id)


async def finish_student_test(max_user_id, user_id: str, *, expired: bool = False) -> bool:
    state = active_tests.get(user_id)
    if not state:
        return False
    # Сохраняем по ID ученика, зафиксированному при старте, независимо от TTL входа.
    save_result(state["test_id"], state["student_id"], state["score"], len(state["questions"]))
    result = get_result(state["test_id"], state["student_id"])
    active_tests.pop(user_id, None)
    title = "⏰ Время на тест истекло." if expired else "🏁 Тест завершён!"
    text = f"{title}\nВаш результат: {result['score']}/{result['total']} правильных ответов."
    return await deliver_test_message(max_user_id, text, [build_main_menu("student").as_markup()])


async def send_question(max_user_id, user_id: str) -> bool:
    state = active_tests.get(user_id)
    if not state:
        return False
    idx = state["current"]
    questions = state["questions"]
    if idx >= len(questions):
        return await finish_student_test(max_user_id, user_id)
    if not is_test_active(state):
        return await finish_student_test(max_user_id, user_id, expired=True)

    question = questions[idx]
    options = question["parsed_options"]
    options_text = "\n\n".join(f"{letter} — {value}" for letter, value in options)
    text = (
        f"Вопрос {idx + 1}/{len(questions)} (тема: {state['topic']}):\n\n"
        f"{question['question_text']}\n\n{options_text}\n\nВыберите ответ:"
    )
    builder = InlineKeyboardBuilder()
    builder.row(*(CallbackButton(
        text=letter, payload=f"answer_{state['test_id']}_{idx}_{letter}"
    ) for letter, _ in options))
    builder.row(CallbackButton(text="Пропустить", payload=f"answer_{state['test_id']}_{idx}_X"))
    delivered = await deliver_test_message(max_user_id, text, [builder.as_markup()])
    if delivered:
        state["question_sent"] = True
    return delivered


@dp.message_callback(F.callback.payload.startswith("answer_"))
async def handle_answer(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    async with get_test_lock(user_id):
        state = active_tests.get(user_id)
        if not state:
            await safe_test_callback(event, notification="Тест не активен или уже завершён. Откройте /tests.")
            return
        user = auth_cache.get(user_id)
        if user and user["id"] != state["student_id"]:
            active_tests.pop(user_id, None)
            await safe_test_callback(event, notification="Вы вошли в другой аккаунт. Откройте /tests.")
            return
        try:
            parts = event.callback.payload.split("_")
            if len(parts) != 4 or parts[0] != "answer":
                raise ValueError("Некорректный ответ")
            test_id, idx = int(parts[1]), int(parts[2])
            letter = parts[3].upper()
            if letter not in ("А", "Б", "В", "Г", "X"):
                raise ValueError("Неизвестный вариант")
        except (ValueError, AttributeError):
            await safe_test_callback(event, notification="Некорректный ответ. Выберите вариант кнопкой под вопросом.")
            return

        if test_id != state["test_id"] or idx != state["current"] or not state["question_sent"]:
            await safe_test_callback(event, notification="Этот вопрос уже обработан или ещё не отправлен. Откройте /tests для продолжения.")
            return
        if not is_test_active(state):
            await finish_student_test(event.from_user.user_id, user_id, expired=True)
            await safe_test_callback(event, text="⏰ Время истекло. Результат сохранён.", attachments=[build_main_menu("student").as_markup()])
            return

        questions = state["questions"]
        if not 0 <= idx < len(questions):
            await safe_test_callback(event, notification="Этот вопрос уже неактуален.")
            return
        question = questions[idx]
        correct = question["correct_answer"]
        if letter == correct:
            state["score"] += 1
            feedback = "✅ Верно!"
        elif letter == "X":
            feedback = f"⏭ Пропущено. Правильный ответ: {correct}."
        else:
            feedback = f"❌ Неверно. Правильный ответ: {correct}."
        explanation = question.get("explanation")
        detail = feedback + (f"\n{explanation}" if explanation else "")

        # Фиксируем ответ до первого await: повторный callback не начислит балл снова.
        state["current"] += 1
        state["question_sent"] = False
        await safe_test_callback(
            event,
            text=f"Вопрос {idx + 1}/{len(questions)}:\n\n{question['question_text']}\n\nВаш ответ: {letter}.\n{detail}",
            attachments=[],
            notification=feedback[:200]
        )
        delivered = await send_question(event.from_user.user_id, user_id)
        if not delivered:
            await notify_test_delivery_failure(user_id, test_id)


# ======================= ОБРАБОТКА ТЕКСТА (В КОНЦЕ) =======================
@dp.message_created()
async def handle_text(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    state = user_states.get(user_id)
    text = (event.message.body.text or "").strip()

    if not state:
        if text.startswith("/"):
            return
        await event.message.answer("Используйте /start для начала работы.", attachments=[build_error_keyboard().as_markup()])
        return

    if not text:
        return

    # ---- Регистрация ----
    if state["action"] == "register":
        if state["step"] == "name":
            if len(text.split()) < 2:
                await event.message.answer("Введите ФИО полностью (Фамилия Имя Отчество).", attachments=[build_error_keyboard().as_markup()])
                return
            state["full_name"] = text
            state["step"] = "password"
            await event.message.answer("Введите пароль (не короче 6 символов):")
            return
        if state["step"] == "password":
            if len(text) < 6:
                await event.message.answer("Пароль слишком короткий. Введите пароль не короче 6 символов:", attachments=[build_error_keyboard().as_markup()])
                return
            success = register_user(state["full_name"], text, state["role"])
            if success:
                user = find_user_by_name(state["full_name"])
                if user:
                    update_user_max_id(user["id"], user_id)
                await event.message.answer("✅ Регистрация успешна! Теперь войдите: /start → Войти.")
            else:
                await event.message.answer("❌ Пользователь с таким ФИО уже существует.", attachments=[build_error_keyboard().as_markup()])
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
                update_user_max_id(user["id"], user_id)
                active = active_tests.get(user_id)
                if active and active["student_id"] != user["id"]:
                    active_tests.pop(user_id, None)
                builder = build_main_menu(user["role"])
                welcome_text = f"✅ Добро пожаловать, {user['full_name']}!\nВаша роль: {user['role']}"
                if user["role"] == "student":
                    resume_test_id = state.get("resume_test_id")
                    if resume_test_id is not None:
                        builder.row(CallbackButton(text="Пройти тест", payload=f"start_test_{resume_test_id}"))
                        welcome_text += "\n\nНажмите «Пройти тест», чтобы вернуться к выбранному заданию."
                    pending = get_pending_invitation(user["id"])
                    if pending:
                        welcome_text += (
                            f"\n\n📩 У вас есть приглашение в класс «{pending['name']}»."
                            "\nНажмите «Мой класс», чтобы принять или отклонить его."
                        )
                await event.message.answer(
                    welcome_text,
                    attachments=[builder.as_markup()]
                )
            else:
                resume_test_id = state.get("resume_test_id")
                if resume_test_id is not None:
                    user_states[user_id] = {"action": "login", "step": "name", "resume_test_id": resume_test_id}
                    await event.message.answer("❌ Неверное ФИО или пароль. Введите ФИО ещё раз; выбранный тест сохранён.")
                    return
                await event.message.answer("❌ Неверное ФИО или пароль. Попробуйте снова.")
                await send_start_message(event.message, user_id)
            user_states.pop(user_id, None)
            return

    # ---- Создание класса ----
    if state["action"] == "create_class":
        if state["step"] == "name":
            teacher = auth_cache.get(user_id)
            if not teacher or teacher["role"] != "teacher":
                await event.message.answer("Только учитель может создавать класс.", attachments=[build_error_keyboard().as_markup()])
                user_states.pop(user_id, None)
                return
            class_id, error = create_class(text, teacher["id"])
            if class_id:
                builder = InlineKeyboardBuilder()
                builder.row(CallbackButton(text="Пригласить в этот класс", payload=f"invite_to_class_{class_id}"))
                builder.row(
                    CallbackButton(text="Мои классы", payload="menu_my_classes"),
                    CallbackButton(text="В профиль", payload="back_to_profile"),
                )
                await event.message.answer(f"✅ Класс «{text}» создан! ID класса: {class_id}.", attachments=[builder.as_markup()])
            else:
                await event.message.answer(error, attachments=[build_error_keyboard().as_markup()])
            user_states.pop(user_id, None)
            return

    # ---- Приглашение ученика ----
    if state["action"] == "invite":
        if state["step"] == "name":
            if text.startswith("/"):
                return
            teacher = auth_cache.get(user_id)
            if not teacher or teacher["role"] != "teacher":
                await event.message.answer("Только учитель может приглашать учеников.", attachments=[build_error_keyboard().as_markup()])
                user_states.pop(user_id, None)
                return
            class_id = state.get("class_id")
            cls = get_class_by_id(class_id) if class_id else None
            if not cls or cls["teacher_id"] != teacher["id"]:
                await event.message.answer("❌ Выбранный класс недоступен. Выберите свой класс через /invite.", attachments=[build_error_keyboard().as_markup()])
                user_states.pop(user_id, None)
                return
            student = find_user_by_name(text)
            if not student:
                await event.message.answer("❌ Ученик с таким ФИО не найден. Введите ФИО ещё раз, точно как при регистрации.", attachments=[build_error_keyboard().as_markup()])
                return
            if student["role"] != "student":
                await event.message.answer("❌ Этот пользователь не является учеником. Введите ФИО ученика ещё раз.", attachments=[build_error_keyboard().as_markup()])
                return
            if not add_invitation(cls["id"], student["id"]):
                await event.message.answer(
                    f"ℹ️ Ученик {student['full_name']} уже состоит в классе «{cls['name']}».",
                    attachments=[build_error_keyboard().as_markup()]
                )
                user_states.pop(user_id, None)
                return
            if student["max_user_id"]:
                builder = build_invitation_keyboard(cls["id"])
                try:
                    sent = await bot.send_message(
                        # В базе хранится ID пользователя MAX, а не ID чата.
                        user_id=int(student["max_user_id"]),
                        text=f"📚 Вас приглашают в класс «{cls['name']}»!\nУчитель: {teacher['full_name']}",
                        attachments=[builder.as_markup()]
                    )
                    if sent is None:
                        raise RuntimeError("MAX API не подтвердил отправку сообщения")
                except Exception:
                    logging.exception("Не удалось отправить приглашение в класс %s ученику %s", cls["id"], student["id"])
                    result_text = (
                        "⚠️ Приглашение сохранено, но не удалось отправить сообщение ученику."
                        "\nУченик сможет принять его после входа через «Мой класс» или /my_class."
                    )
                else:
                    result_text = f"✅ Приглашение отправлено ученику {student['full_name']} в класс «{cls['name']}»."
            else:
                result_text = (
                    f"⚠️ Приглашение для {student['full_name']} сохранено, но его MAX-аккаунт ещё не привязан."
                    "\nПопросите ученика войти в бота через /start и открыть «Мой класс» или /my_class."
                )
            user_states.pop(user_id, None)
            await event.message.answer(result_text, attachments=[build_error_keyboard().as_markup()])
            return

# ======================= ЗАПУСК =======================
async def main():
    init_db()
    seed_questions()
    await set_bot_commands()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
