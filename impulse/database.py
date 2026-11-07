"""База SQLite: таблицы, запросы, тесты и результаты."""
import logging
import random
import secrets
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from passlib.context import CryptContext

from impulse import config
from impulse.config import (
    ANSWER_LETTERS, HARD_QUESTION_PERCENT, HELP_NEEDED_PERCENT, MAX_IMPORTED_QUESTIONS,
    MAX_TEST_TITLE_LENGTH, POINTS_PER_DIFFICULTY, TEST_ATTEMPT_MINUTES,
)
from impulse.excel import (
    parse_excel_difficulty, parse_excel_question_number, parse_question_options, parse_test_excel,
    validate_imported_question,
)
from impulse.questions_data import QUESTIONS_DATA
from impulse.utils import short_report_text, utc_now, utc_text

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

STUDENT_ANSWERS_TABLE = """
    CREATE TABLE IF NOT EXISTS {name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        test_id INTEGER NOT NULL REFERENCES tests(id),
        student_id INTEGER NOT NULL REFERENCES users(id),
        question_id INTEGER NOT NULL REFERENCES questions(id),
        answer TEXT NOT NULL CHECK(answer IN ('А', 'Б', 'В', 'Г', 'X')),
        is_correct INTEGER NOT NULL CHECK(is_correct IN (0, 1)),
        points INTEGER NOT NULL CHECK(points BETWEEN 0 AND 30),
        answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(test_id, student_id, question_id)
    )
"""


def scale_points_by_ten(conn):
    """Один раз переводит старую базу на шкалу Софии: баллы стали в 10 раз крупнее."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'student_answers'").fetchone()["sql"]
    if "BETWEEN 0 AND 3)" not in sql:
        return
    conn.execute(STUDENT_ANSWERS_TABLE.format(name="student_answers_new"))
    conn.execute("""
        INSERT INTO student_answers_new (id, test_id, student_id, question_id, answer, is_correct, points, answered_at)
        SELECT id, test_id, student_id, question_id, answer, is_correct, points * 10, answered_at FROM student_answers
    """)
    conn.execute("DROP TABLE student_answers")
    conn.execute("ALTER TABLE student_answers_new RENAME TO student_answers")
    conn.execute("UPDATE class_members SET points = points * 10")
    conn.execute("UPDATE users SET points = points * 10")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'bonus_orders'").fetchone():
        conn.execute("UPDATE bonus_orders SET cost = cost * 10")
    logging.info("Баллы в базе переведены на новую шкалу (умножены на 10).")


def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def generate_join_code(conn) -> str:
    while True:
        code = str(100000 + secrets.randbelow(900000))
        if not conn.execute("SELECT 1 FROM classes WHERE join_code = ?", (code,)).fetchone():
            return code


def init_db():
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'student',
                max_user_id TEXT,
                points INTEGER NOT NULL DEFAULT 0 CHECK(typeof(points) = 'integer' AND points >= 0),
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
                points INTEGER NOT NULL DEFAULT 0 CHECK(typeof(points) = 'integer' AND points >= 0),
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
                difficulty INTEGER NOT NULL DEFAULT 1 CHECK(difficulty IN (1, 2, 3))
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
        if "points" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0 CHECK(typeof(points) = 'integer' AND points >= 0)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_message_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (teacher_id) REFERENCES users(id),
                UNIQUE(teacher_id, source_message_id)
            )
        """)
        # У прежних вопросов и выдач template_id остаётся NULL.
        for table, additions in (
            ("questions", {"template_id": "INTEGER REFERENCES test_templates(id)", "question_number": "INTEGER",
                           "difficulty": "INTEGER NOT NULL DEFAULT 1 CHECK(difficulty IN (1, 2, 3))"}),
            ("tests", {"template_id": "INTEGER REFERENCES test_templates(id)", "closed_at": "TEXT"}),
            ("class_members", {"points": "INTEGER NOT NULL DEFAULT 0 CHECK(typeof(points) = 'integer' AND points >= 0)"}),
            ("classes", {"join_code": "TEXT"}),
        ):
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, declaration in additions.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                    if column == "closed_at":
                        # Старые завершённые тесты не должны прислать отчёты при обновлении.
                        conn.execute("UPDATE tests SET closed_at = ? WHERE ends_at <= ?", (utc_text(), utc_text()))
        for row in conn.execute("SELECT id FROM classes WHERE join_code IS NULL").fetchall():
            conn.execute("UPDATE classes SET join_code = ? WHERE id = ?", (generate_join_code(conn), row["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_class_join_code ON classes(join_code)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_template_question_number ON questions(template_id, question_number)")
        # Не пересоздаём старую таблицу: сохраняем ID, связи и пользовательские индексы.
        # SQLite сохраняет цифровые коды как TEXT в прежней колонке; при чтении берём int.
        legacy_levels = {"1": 1, "базовая": 1, "легкий": 1, "лёгкий": 1, "легкая": 1, "лёгкая": 1,
                         "2": 2, "средняя": 2, "средний": 2,
                         "3": 3, "сложная": 3, "сложный": 3}
        for row in conn.execute("SELECT id, difficulty FROM questions"):
            value = str(row["difficulty"] or "").strip().casefold()
            level = legacy_levels.get(value, 1)
            if str(row["difficulty"]) != str(level):
                conn.execute("UPDATE questions SET difficulty = ? WHERE id = ?", (level, row["id"]))
        conn.execute(STUDENT_ANSWERS_TABLE.format(name="student_answers"))
        scale_points_by_ten(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_bonuses (
                test_id INTEGER NOT NULL REFERENCES tests(id),
                student_id INTEGER NOT NULL REFERENCES users(id),
                points INTEGER NOT NULL,
                PRIMARY KEY(test_id, student_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_point_scopes (
                test_id INTEGER NOT NULL REFERENCES tests(id),
                student_id INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(test_id, student_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_student_classes (
                test_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                class_id INTEGER NOT NULL REFERENCES classes(id),
                PRIMARY KEY(test_id, student_id, class_id),
                FOREIGN KEY(test_id, student_id) REFERENCES test_point_scopes(test_id, student_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_attempts (
                test_id INTEGER NOT NULL REFERENCES tests(id),
                student_id INTEGER NOT NULL REFERENCES users(id),
                started_at TEXT NOT NULL,
                PRIMARY KEY(test_id, student_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bonus_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL REFERENCES classes(id),
                student_id INTEGER NOT NULL REFERENCES users(id),
                bonus_key TEXT NOT NULL,
                bonus_name TEXT NOT NULL,
                cost INTEGER NOT NULL CHECK(cost > 0),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_streaks (
                student_id INTEGER PRIMARY KEY REFERENCES users(id),
                current INTEGER NOT NULL DEFAULT 0,
                best INTEGER NOT NULL DEFAULT 0,
                last_date TEXT,
                freeze_week TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_badges (
                student_id INTEGER NOT NULL REFERENCES users(id),
                badge_key TEXT NOT NULL,
                earned_at TEXT NOT NULL,
                PRIMARY KEY(student_id, badge_key)
            )
        """)
        migrate_class_points(conn)
        conn.commit()


def seed_questions():
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM questions WHERE template_id IS NULL").fetchone()[0]
        if count == 0:
            for q in QUESTIONS_DATA:
                conn.execute(
                    "INSERT INTO questions (topic, question_text, options, correct_answer, explanation, difficulty) VALUES (?, ?, ?, ?, ?, ?)",
                    q
                )
            conn.commit()
            logging.info(f"Загружено {len(QUESTIONS_DATA)} вопросов в базу.")


def seed_catalog():
    """Каждый .xlsx из папки catalog становится блоком в «Тестах по темам»."""
    for path in sorted(config.CATALOG_DIR.glob("*.xlsx")):
        topic = path.stem
        with get_connection() as conn:
            exists = conn.execute("SELECT 1 FROM questions WHERE topic = ? AND template_id IS NULL LIMIT 1", (topic,)).fetchone()
        if exists:
            continue
        try:
            questions = parse_test_excel(path.read_bytes())
        except ValueError as exc:
            # Ошибка в одном файле не должна мешать запуску бота.
            logging.error("Блок «%s» не загружен: %s", topic, exc)
            continue
        with get_connection() as conn:
            conn.executemany("""
                INSERT INTO questions (topic, question_text, options, correct_answer, explanation, difficulty)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [(topic, q["question_text"], q["options"], q["correct_answer"], q["explanation"], q["difficulty"])
                  for q in questions])
        logging.info("Загружен блок «%s»: %s вопросов.", topic, len(questions))


def normalize_name(full_name: str) -> str:
    # SQLite не сравнивает кириллицу без учёта регистра, поэтому приводим ФИО сами.
    return " ".join(full_name.split()).casefold().replace("ё", "е")


def find_user_row(full_name: str):
    wanted = normalize_name(full_name)
    with get_connection() as conn:
        rows = conn.execute("SELECT id, full_name, role, max_user_id, password_hash FROM users").fetchall()
    return next((row for row in rows if normalize_name(row["full_name"]) == wanted), None)


def register_user(full_name: str, password: str, role: str = "student") -> bool:
    if find_user_row(full_name):
        return False
    try:
        with get_connection() as conn:
            hashed = pwd_context.hash(password)
            conn.execute(
                "INSERT INTO users (full_name, password_hash, role) VALUES (?, ?, ?)",
                (" ".join(full_name.split()), hashed, role)
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def authenticate_user(full_name: str, password: str) -> dict | None:
    row = find_user_row(full_name)
    if row is None:
        return None
    if not pwd_context.verify(password, row["password_hash"]):
        return None
    return {"id": row["id"], "full_name": row["full_name"], "role": row["role"]}


def find_user_by_name(full_name: str) -> dict | None:
    row = find_user_row(full_name)
    if not row:
        return None
    return {key: row[key] for key in ("id", "full_name", "role", "max_user_id")}


def find_user_by_max_id(max_user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, full_name, role FROM users WHERE max_user_id = ? ORDER BY id DESC LIMIT 1",
            (max_user_id,)
        ).fetchone()
    return dict(row) if row else None


def update_user_max_id(user_id: int, max_user_id: str):
    with get_connection() as conn:
        # Один аккаунт MAX привязан только к одному пользователю.
        conn.execute("UPDATE users SET max_user_id = NULL WHERE max_user_id = ? AND id != ?", (max_user_id, user_id))
        conn.execute("UPDATE users SET max_user_id = ? WHERE id = ?", (max_user_id, user_id))
        conn.commit()


def clear_user_max_id(max_user_id: str):
    with get_connection() as conn:
        conn.execute("UPDATE users SET max_user_id = NULL WHERE max_user_id = ?", (max_user_id,))
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
                "INSERT INTO classes (name, teacher_id, join_code) VALUES (?, ?, ?)",
                (name, teacher_id, generate_join_code(conn))
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
            SELECT u.id, u.full_name, u.max_user_id, cm.points
            FROM class_members cm
            JOIN users u ON cm.student_id = u.id
            WHERE cm.class_id = ? AND cm.status = 'accepted'
            ORDER BY u.full_name
        """, (class_id,)).fetchall()
    # SQLite NOCASE не сортирует кириллицу без учёта регистра.
    return sorted((dict(r) for r in rows), key=lambda student: (
        student["full_name"].casefold().replace("ё", "е"), student["id"]
    ))


def get_class_by_id(class_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT id, name, teacher_id, join_code FROM classes WHERE id = ?", (class_id,)).fetchone()
    return dict(row) if row else None


def join_class_by_code(code: str, student_id: int) -> tuple[dict | None, bool]:
    """Возвращает класс и признак того, что ученик вступил в него сейчас."""
    with get_connection() as conn:
        row = conn.execute("SELECT id, name, teacher_id FROM classes WHERE join_code = ?", (code,)).fetchone()
        if not row:
            return None, False
        cur = conn.execute("""
            INSERT INTO class_members (class_id, student_id, status) VALUES (?, ?, 'accepted')
            ON CONFLICT(class_id, student_id) DO UPDATE SET status = 'accepted'
            WHERE class_members.status != 'accepted'
        """, (row["id"], student_id))
        conn.commit()
    return dict(row), cur.rowcount > 0


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


def get_student_classes(student_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT c.id, c.name, c.teacher_id, teacher.full_name AS teacher_name, cm.status, cm.points
            FROM classes c
            JOIN class_members cm ON c.id = cm.class_id
            JOIN users teacher ON teacher.id = c.teacher_id
            WHERE cm.student_id = ? AND cm.status = 'accepted'
        """, (student_id,)).fetchall()
    return sorted((dict(row) for row in rows), key=lambda cls: (
        cls["name"].casefold().replace("ё", "е"), cls["teacher_name"].casefold().replace("ё", "е"), cls["id"]
    ))


def get_pending_invitations(student_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT c.id, c.name, c.teacher_id, teacher.full_name AS teacher_name, cm.id AS member_id
            FROM classes c
            JOIN class_members cm ON c.id = cm.class_id
            JOIN users teacher ON teacher.id = c.teacher_id
            WHERE cm.student_id = ? AND cm.status = 'pending'
            ORDER BY cm.id
        """, (student_id,)).fetchall()
    return [dict(row) for row in rows]


def get_student_class(student_id: int) -> dict | None:
    """Совместимость со старым кодом; интерфейс использует полный список."""
    classes = get_student_classes(student_id)
    return classes[0] if classes else None


def get_pending_invitation(student_id: int) -> dict | None:
    invitations = get_pending_invitations(student_id)
    return invitations[0] if invitations else None


# ---------- Вопросы и тесты ----------
def get_topics() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT topic FROM questions WHERE template_id IS NULL ORDER BY topic").fetchall()
    return [r["topic"] for r in rows]


def get_questions_by_topic(topic: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, question_text, options, correct_answer, explanation, difficulty FROM questions WHERE topic = ? AND template_id IS NULL ORDER BY id",
            (topic,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_test_template(template_id: int, teacher_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT t.*, (SELECT COUNT(*) FROM questions q WHERE q.template_id = t.id) AS question_count
            FROM test_templates t WHERE t.id = ? AND t.teacher_id = ?
        """, (template_id, teacher_id)).fetchone()
    return dict(row) if row else None


def get_teacher_test_templates(teacher_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT t.*, (SELECT COUNT(*) FROM questions q WHERE q.template_id = t.id) AS question_count
            FROM test_templates t WHERE t.teacher_id = ? ORDER BY t.id DESC
        """, (teacher_id,)).fetchall()
    return [dict(row) for row in rows]


def get_template_questions(template_id: int, teacher_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT q.* FROM questions q JOIN test_templates t ON t.id = q.template_id
            WHERE t.id = ? AND t.teacher_id = ? ORDER BY q.question_number, q.id
        """, (template_id, teacher_id)).fetchall()
    return [dict(row) for row in rows]


def create_test_template(teacher_id: int, title: str, questions: list[dict], source_message_id=None) -> int:
    title = " ".join(title.split())
    if not title or len(title) > MAX_TEST_TITLE_LENGTH:
        raise ValueError(f"Название должно содержать от 1 до {MAX_TEST_TITLE_LENGTH} символов.")
    if not 1 <= len(questions) <= MAX_IMPORTED_QUESTIONS:
        raise ValueError(f"В тесте должно быть от 1 до {MAX_IMPORTED_QUESTIONS} вопросов.")
    numbers = set()
    for question in questions:
        number = parse_excel_question_number(question.get("question_number"))
        if number in numbers:
            raise ValueError(f"Повторяется номер вопроса {number}.")
        numbers.add(number)
        validate_imported_question(question, title)
    with get_connection() as conn:
        teacher = conn.execute("SELECT role FROM users WHERE id = ?", (teacher_id,)).fetchone()
        if not teacher or teacher["role"] != "teacher":
            raise ValueError("Создавать тесты может только учитель.")
        if source_message_id:
            old = conn.execute("SELECT id FROM test_templates WHERE teacher_id = ? AND source_message_id = ?",
                               (teacher_id, str(source_message_id))).fetchone()
            if old:
                return old["id"]
        cur = conn.execute("INSERT INTO test_templates (teacher_id, title, source_message_id) VALUES (?, ?, ?)",
                           (teacher_id, title, str(source_message_id) if source_message_id else None))
        template_id = cur.lastrowid
        conn.executemany("""
            INSERT INTO questions (topic, question_text, options, correct_answer, explanation, difficulty, template_id, question_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (title, q["question_text"].strip(), q["options"], q["correct_answer"].strip().upper(),
             q.get("explanation") or "", parse_excel_difficulty(q.get("difficulty")), template_id,
             parse_excel_question_number(q["question_number"]))
            for q in questions
        ])
        # Контекст соединения фиксирует весь тест или откатывает все его строки.
    return template_id


def create_test(teacher_id: int, topic: str, class_id: int | None, *, template_id: int | None = None,
                duration_minutes: int = 1440) -> int:
    if template_id is not None:
        template = get_test_template(template_id, teacher_id)
        if not template:
            raise ValueError("Созданный тест не найден или принадлежит другому учителю.")
        topic = template["title"]
    ends_at = utc_now() + timedelta(minutes=duration_minutes)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tests (teacher_id, topic, class_id, ends_at, template_id) VALUES (?, ?, ?, ?, ?)",
            (teacher_id, topic, class_id, ends_at.strftime("%Y-%m-%d %H:%M:%S"), template_id)
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
    now = datetime.now(ends_at.tzinfo) if ends_at.tzinfo else utc_now()
    return now < ends_at


def start_attempt(test_id: int, student_id: int):
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO test_attempts (test_id, student_id, started_at) VALUES (?, ?, ?)",
                     (test_id, student_id, utc_text()))
        conn.commit()


def attempt_deadline(conn, test: dict, student_id: int) -> str:
    """Ранний из двух сроков: закрытие теста и личное время на прохождение."""
    row = conn.execute("SELECT started_at FROM test_attempts WHERE test_id = ? AND student_id = ?",
                       (test["id"], student_id)).fetchone()
    if not row:
        return test["ends_at"]
    limit = datetime.fromisoformat(row["started_at"]) + timedelta(minutes=TEST_ATTEMPT_MINUTES)
    return min(utc_text(limit), str(test["ends_at"]))


def get_student_deadline(test: dict, student_id: int) -> str:
    with get_connection() as conn:
        return attempt_deadline(conn, test, student_id)


def student_started_test(test_id: int, student_id: int) -> bool:
    with get_connection() as conn:
        return conn.execute("SELECT 1 FROM test_attempts WHERE test_id = ? AND student_id = ?",
                            (test_id, student_id)).fetchone() is not None


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
            (test_id, student_id, score, total, utc_text())
        )
        conn.commit()


def get_result(test_id: int, student_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT score, total FROM test_results WHERE test_id = ? AND student_id = ?",
            (test_id, student_id)
        ).fetchone()
    return dict(row) if row else None


def get_student_points(student_id: int, class_id: int | None = None) -> int:
    """Баллы выбранного класса; без class_id — прежняя служебная общая сумма."""
    with get_connection() as conn:
        if class_id is None:
            row = conn.execute("SELECT points FROM users WHERE id = ?", (student_id,)).fetchone()
        else:
            row = conn.execute("SELECT points FROM class_members WHERE student_id = ? AND class_id = ?",
                               (student_id, class_id)).fetchone()
    return int(row["points"]) if row else 0


def initialize_test_point_scope(conn, test: dict, student_id: int, *, legacy: bool = False) -> list[int]:
    """Фиксируем классы один раз; позднее вступление не переносит в них старые баллы."""
    inserted = conn.execute("INSERT OR IGNORE INTO test_point_scopes (test_id, student_id) VALUES (?, ?)",
                            (test["id"], student_id)).rowcount
    if inserted:
        # У старого теста одного класса сохраняем точное назначение, в том числе
        # если прежний участник сейчас не имеет статуса accepted.
        rows = conn.execute("""
            SELECT c.id FROM classes c JOIN class_members cm ON cm.class_id = c.id
            WHERE cm.student_id = ? AND c.teacher_id = ? AND (? IS NULL OR c.id = ?)
              AND (cm.status = 'accepted' OR (? = 1 AND ? IS NOT NULL))
        """, (student_id, test["teacher_id"], test["class_id"], test["class_id"], int(legacy), test["class_id"])).fetchall()
        conn.executemany("INSERT INTO test_student_classes (test_id, student_id, class_id) VALUES (?, ?, ?)",
                         [(test["id"], student_id, row["id"]) for row in rows])
    return [row["class_id"] for row in conn.execute(
        "SELECT class_id FROM test_student_classes WHERE test_id = ? AND student_id = ? ORDER BY class_id",
        (test["id"], student_id)
    )]


def add_class_points(conn, test: dict, student_id: int, points: int):
    if not points:
        return
    conn.execute("""
        UPDATE class_members SET points = points + ?
        WHERE student_id = ? AND class_id IN (
            SELECT scope.class_id FROM test_student_classes scope
            JOIN classes c ON c.id = scope.class_id
            WHERE scope.test_id = ? AND scope.student_id = ? AND c.teacher_id = ?
              AND (? IS NULL OR c.id = ?)
        )
    """, (points, student_id, test["id"], student_id, test["teacher_id"], test["class_id"], test["class_id"]))


def grant_test_bonus(test: dict, student_id: int, points: int) -> bool:
    """Бонус за завершённый тест начисляется один раз в те же классы, что и баллы за ответы."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("INSERT OR IGNORE INTO test_bonuses (test_id, student_id, points) VALUES (?, ?, ?)",
                            (test["id"], student_id, points)).rowcount:
            return False
        initialize_test_point_scope(conn, test, student_id)
        add_class_points(conn, test, student_id, points)
        conn.execute("UPDATE users SET points = points + ? WHERE id = ?", (points, student_id))
    return True


def migrate_class_points(conn):
    # Источником служат принятые ответы с сохранённым весом, а не общий users.points
    # и не число верных ответов в test_results. Маркер защищает и пустой набор классов.
    pending = conn.execute("""
        SELECT answers.test_id, answers.student_id, SUM(answers.points) AS points
        FROM student_answers answers LEFT JOIN test_point_scopes scope
          ON scope.test_id = answers.test_id AND scope.student_id = answers.student_id
        WHERE scope.test_id IS NULL GROUP BY answers.test_id, answers.student_id
    """).fetchall()
    for row in pending:
        test = conn.execute("SELECT * FROM tests WHERE id = ?", (row["test_id"],)).fetchone()
        if not test:
            continue
        test = dict(test)
        initialize_test_point_scope(conn, test, row["student_id"], legacy=True)
        add_class_points(conn, test, row["student_id"], int(row["points"] or 0))


def get_student_test_answers(test_id: int, student_id: int) -> dict[int, dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM student_answers WHERE test_id = ? AND student_id = ?",
                            (test_id, student_id)).fetchall()
    return {row["question_id"]: dict(row) for row in rows}


def record_student_answer(test_id: int, student_id: int, question_id: int, answer: str) -> dict:
    """Первый ответ и баллы сохраняются одной транзакцией, независимо от сессии MAX."""
    if answer not in ("А", "Б", "В", "Г", "X"):
        raise ValueError("Недопустимый вариант ответа.")
    with get_connection() as conn:
        # Сериализуем также разные процессы и разные MAX-сессии одного ученика.
        conn.execute("BEGIN IMMEDIATE")
        saved = conn.execute("SELECT * FROM student_answers WHERE test_id = ? AND student_id = ? AND question_id = ?",
                             (test_id, student_id, question_id)).fetchone()
        if saved:
            return {**dict(saved), "newly_recorded": False}
        test = conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
        student = conn.execute("SELECT role FROM users WHERE id = ?", (student_id,)).fetchone()
        if not test or not student or student["role"] != "student":
            raise ValueError("Тест или ученик не найден.")
        if not is_test_active({"ends_at": attempt_deadline(conn, dict(test), student_id)}):
            raise ValueError("Срок прохождения теста истёк.")
        member = conn.execute("""
            SELECT 1 FROM class_members cm JOIN classes c ON c.id = cm.class_id
            WHERE cm.student_id = ? AND cm.status = 'accepted' AND c.teacher_id = ?
              AND (? IS NULL OR c.id = ?) LIMIT 1
        """, (student_id, test["teacher_id"], test["class_id"], test["class_id"])).fetchone()
        if not member:
            raise ValueError("Этот тест не назначен вашему классу.")
        if conn.execute("SELECT 1 FROM test_results WHERE test_id = ? AND student_id = ?",
                        (test_id, student_id)).fetchone():
            raise ValueError("Этот тест уже завершён.")
        question = conn.execute("""
            SELECT * FROM questions WHERE id = ? AND
                ((? IS NOT NULL AND template_id = ?) OR
                 (? IS NULL AND template_id IS NULL AND topic = ?))
        """, (question_id, test["template_id"], test["template_id"], test["template_id"], test["topic"])).fetchone()
        if not question:
            raise ValueError("Вопрос не принадлежит этому тесту.")
        difficulty = parse_excel_difficulty(question["difficulty"])
        is_correct = int(answer == question["correct_answer"].strip().upper())
        points = difficulty * POINTS_PER_DIFFICULTY if is_correct else 0
        initialize_test_point_scope(conn, dict(test), student_id)
        cur = conn.execute("""
            INSERT INTO student_answers (test_id, student_id, question_id, answer, is_correct, points)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (test_id, student_id, question_id, answer, is_correct, points))
        if points:
            add_class_points(conn, dict(test), student_id, points)
            # Общую колонку сохраняем для совместимости; в интерфейсе — только баллы классов.
            conn.execute("UPDATE users SET points = points + ? WHERE id = ?", (points, student_id))
        saved = conn.execute("SELECT * FROM student_answers WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {**dict(saved), "newly_recorded": True}


def sync_test_progress(state: dict):
    answers = get_student_test_answers(state["test_id"], state["student_id"])
    questions = state["questions"]
    state["score"] = sum(answers[q["id"]]["is_correct"] for q in questions if q["id"] in answers)
    state["earned_points"] = sum(answers[q["id"]]["points"] for q in questions if q["id"] in answers)
    current = next((i for i, q in enumerate(questions) if q["id"] not in answers), len(questions))
    if current != state["current"]:
        state["question_sent"] = False
    state["current"] = current


def restore_test_state(test: dict, student_id: int) -> dict:
    questions = prepare_test_questions(test["topic"], template_id=test.get("template_id"), teacher_id=test["teacher_id"])
    if not questions:
        raise ValueError("В тесте пока нет вопросов.")
    # Порядок свой у каждого ученика и не меняется при продолжении теста.
    rng = random.Random(f"{test['id']}:{student_id}")
    rng.shuffle(questions)
    for question in questions:
        shuffled = list(question["parsed_options"])
        rng.shuffle(shuffled)
        question["shown_options"] = [(shown, value, original) for shown, (original, value)
                                     in zip(ANSWER_LETTERS, shuffled)]
    state = {"test_id": test["id"], "student_id": student_id, "topic": test["topic"],
             "ends_at": get_student_deadline(test, student_id), "questions": questions, "current": 0,
             "score": 0, "earned_points": 0, "question_sent": False}
    sync_test_progress(state)
    return state


def get_teacher_class_tests(teacher_id: int, class_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT t.* FROM tests t
            JOIN classes c ON c.id = ? AND c.teacher_id = t.teacher_id
            WHERE t.teacher_id = ? AND (t.class_id = c.id OR t.class_id IS NULL)
            ORDER BY t.id DESC
        """, (class_id, teacher_id)).fetchall()
    return [dict(row) for row in rows]


def get_hard_questions(test_id: int, limit: int = 3) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT q.question_text, COUNT(*) AS answers, SUM(a.is_correct) AS correct
            FROM student_answers a JOIN questions q ON q.id = a.question_id
            WHERE a.test_id = ? GROUP BY q.id
        """, (test_id,)).fetchall()
    hard = [{**dict(row), "percent": round(row["correct"] * 100 / row["answers"])} for row in rows
            if row["correct"] * 100 / row["answers"] < HARD_QUESTION_PERCENT]
    return sorted(hard, key=lambda row: row["percent"])[:limit]


def format_test_analytics(test_id: int, students: list[dict]) -> list[str]:
    weak = [s["full_name"] for s in students if s.get("result_id") is not None
            and s["score"] * 100 < HELP_NEEDED_PERCENT * s["total"]]
    lines = []
    if weak:
        lines.append(f"Нужна помощь (меньше {HELP_NEEDED_PERCENT}%): " + ", ".join(short_report_text(n, 60) for n in weak[:15]) + ".")
    hard = get_hard_questions(test_id)
    if hard:
        lines.append("Трудные вопросы:")
        lines.extend(f"— {short_report_text(q['question_text'], 90)} ({q['percent']}% верно)" for q in hard)
    return lines


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
            SELECT u.id, u.full_name, r.id AS result_id, r.score, r.total, r.finished_at,
                   COALESCE(class_balance.points, 0) AS class_points
            FROM users u
            LEFT JOIN class_members class_balance ON class_balance.student_id = u.id AND class_balance.class_id = ?
            LEFT JOIN test_results r ON r.student_id = u.id AND r.test_id = ?
                AND r.finished_at IS NOT NULL AND r.score IS NOT NULL AND r.total > 0
            WHERE EXISTS (
                SELECT 1 FROM class_members cm
                WHERE cm.student_id = u.id AND cm.class_id = ? AND cm.status = 'accepted'
            ) OR (? = 1 AND r.id IS NOT NULL)
            ORDER BY u.full_name COLLATE NOCASE, u.id
        """, (class_id, test_id, class_id, int(test["class_id"] == class_id))).fetchall()
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


def prepare_test_questions(topic: str, *, template_id: int | None = None, teacher_id: int | None = None) -> list[dict]:
    if template_id is not None:
        template = get_test_template(template_id, teacher_id) if teacher_id is not None else None
        if not template:
            raise ValueError("Созданный тест не найден или принадлежит другому учителю.")
        questions = get_template_questions(template_id, teacher_id)
    else:
        questions = get_questions_by_topic(topic)
    for question in questions:
        question["difficulty"] = parse_excel_difficulty(question["difficulty"])
        question["parsed_options"] = parse_question_options(question["options"])
        question["correct_answer"] = question["correct_answer"].strip().upper()
        if question["correct_answer"] not in ("А", "Б", "В", "Г"):
            raise ValueError(f"Некорректный правильный ответ в вопросе {question['id']}")
        options_text = "\n".join(f"{letter} — {value}" for letter, value in question["parsed_options"])
        if len(question["question_text"]) + len(options_text) + len(topic) + 150 > 4000:
            raise ValueError(f"Слишком длинный вопрос {question['id']}")
    return questions


def get_user_max_id(user_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT max_user_id FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["max_user_id"] if row else None


def get_unfinished_attempts() -> list[tuple[dict, int]]:
    """Начатые прохождения без результата, у которых вышло время."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT t.*, a.student_id AS attempt_student FROM test_attempts a
            JOIN tests t ON t.id = a.test_id
            LEFT JOIN test_results r ON r.test_id = a.test_id AND r.student_id = a.student_id
            WHERE r.id IS NULL
        """).fetchall()
    result = []
    for row in rows:
        test = dict(row)
        student_id = test.pop("attempt_student")
        if not is_test_active({"ends_at": get_student_deadline(test, student_id)}):
            result.append((test, student_id))
    return result


def get_tests_to_close() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM tests WHERE closed_at IS NULL").fetchall()
    return [dict(row) for row in rows if not is_test_active(dict(row))]
