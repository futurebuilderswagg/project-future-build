"""Проверки логики без подключения к MAX. Запуск: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from datetime import date
from io import BytesIO
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["MAX_BOT_TOKEN"] = "token-for-tests"
os.environ.setdefault("TEACHER_CODE", "test-code")
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook  # noqa: E402

from impulse import config, database, excel, rewards  # noqa: E402


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config.DB_PATH = str(Path(self.folder.name) / "test.db")
        database.init_db()
        database.seed_questions()
        database.register_user("Учитель Иван Иванович", "secret12", "teacher")
        database.register_user("Иванова Анна Петровна", "secret12")
        self.teacher = database.find_user_by_name("Учитель Иван Иванович")["id"]
        self.student = database.find_user_by_name("Иванова Анна Петровна")["id"]
        self.class_id = database.create_class("9А", self.teacher)[0]
        self.code = database.get_class_by_id(self.class_id)["join_code"]
        database.join_class_by_code(self.code, self.student)

    def tearDown(self):
        self.folder.cleanup()

    def set_points(self, points):
        with database.get_connection() as conn:
            conn.execute("UPDATE class_members SET points = ?", (points,))

    def balance(self):
        return database.get_student_points(self.student, self.class_id)


class NamesTest(DatabaseTestCase):
    def test_name_ignores_case_spaces_and_yo(self):
        self.assertEqual(database.normalize_name("  Семёнов   ИВАН "), "семенов иван")

    def test_duplicate_name_is_rejected(self):
        self.assertFalse(database.register_user("иванова  анна петровна", "secret34"))

    def test_login_ignores_case(self):
        self.assertIsNotNone(database.authenticate_user("ИВАНОВА анна петровна", "secret12"))
        self.assertIsNone(database.authenticate_user("Иванова Анна Петровна", "wrong-password"))


class AttemptTest(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.test_id = database.create_test(self.teacher, "Механика", self.class_id)
        self.test = database.get_test(self.test_id)

    def test_answers_shuffled_but_stable_for_student(self):
        first = database.restore_test_state(self.test, self.student)
        second = database.restore_test_state(self.test, self.student)
        self.assertEqual([q["id"] for q in first["questions"]], [q["id"] for q in second["questions"]])
        self.assertNotEqual([q["id"] for q in first["questions"]],
                            [q["id"] for q in database.restore_test_state(self.test, self.student + 1)["questions"]])

    def test_points_are_given_once(self):
        question = database.restore_test_state(self.test, self.student)["questions"][0]
        database.record_student_answer(self.test_id, self.student, question["id"], question["correct_answer"])
        again = database.record_student_answer(self.test_id, self.student, question["id"], question["correct_answer"])
        self.assertFalse(again["newly_recorded"])
        self.assertEqual(self.balance(), question["difficulty"] * 10)

    def test_attempt_time_limit(self):
        database.start_attempt(self.test_id, self.student)
        with database.get_connection() as conn:
            conn.execute("UPDATE test_attempts SET started_at = datetime('now', '-25 minutes')")
        question = database.restore_test_state(self.test, self.student)["questions"][0]
        with self.assertRaises(ValueError):
            database.record_student_answer(self.test_id, self.student, question["id"], "А")
        self.assertEqual(len(database.get_unfinished_attempts()), 1)


class BonusTest(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.bonus = rewards.get_bonus("homework")

    def test_not_enough_points(self):
        order, error = rewards.order_bonus(self.class_id, self.student, self.bonus)
        self.assertIsNone(order)
        self.assertIn("Не хватает", error)

    def test_limit_and_refund(self):
        self.set_points(2000)
        order, _ = rewards.order_bonus(self.class_id, self.student, self.bonus)
        self.assertEqual(self.balance(), 500)
        self.assertIsNotNone(rewards.order_bonus(self.class_id, self.student, self.bonus)[1])
        rewards.decide_bonus_order(order, self.teacher, False)
        self.assertEqual(self.balance(), 2000)
        self.assertIsNone(rewards.decide_bonus_order(order, self.teacher, False))
        self.assertEqual(self.balance(), 2000)

    def test_foreign_teacher_cannot_decide(self):
        self.set_points(2000)
        order, _ = rewards.order_bonus(self.class_id, self.student, self.bonus)
        self.assertIsNone(rewards.decide_bonus_order(order, self.teacher + 100, True))

    def test_level_uses_earned_points(self):
        self.set_points(2500)
        rewards.order_bonus(self.class_id, self.student, rewards.get_bonus("regular"))
        self.assertEqual(rewards.get_earned_points(self.class_id, self.student), 2500)
        self.assertTrue(rewards.format_level(2500).startswith("51, звание «Магистр»"))


class StreakTest(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.test = database.get_test(database.create_test(self.teacher, "Механика", self.class_id))

    def day(self, number):
        return date(2026, 11, number)

    def test_streak_grows_once_a_day(self):
        self.assertEqual(rewards.update_streak(self.student, self.day(2)), (1, True))
        self.assertEqual(rewards.update_streak(self.student, self.day(2)), (1, False))
        self.assertEqual(rewards.update_streak(self.student, self.day(3)), (2, True))

    def test_one_missed_day_is_forgiven_once_a_week(self):
        rewards.update_streak(self.student, self.day(2))
        self.assertEqual(rewards.update_streak(self.student, self.day(4))[0], 2)
        self.assertEqual(rewards.update_streak(self.student, self.day(6))[0], 1)

    def test_long_break_resets_streak(self):
        rewards.update_streak(self.student, self.day(2))
        self.assertEqual(rewards.update_streak(self.student, self.day(9))[0], 1)
        self.assertEqual(rewards.get_streak(self.student)["best"], 1)

    def test_perfect_test_gives_bonus_and_badge_once(self):
        self.assertTrue(any("Снайпер" in line for line in rewards.apply_test_rewards(self.test, self.student, 10, 10)))
        self.assertFalse(any("Снайпер" in line for line in rewards.apply_test_rewards(self.test, self.student, 10, 10)))
        self.assertEqual(len(rewards.get_badges(self.student)), 1)
        self.assertEqual(self.balance(), 130)

    def test_good_result_gives_smaller_bonus(self):
        rewards.apply_test_rewards(self.test, self.student, 8, 10)
        self.assertEqual(self.balance(), 50)

    def test_weak_result_gives_no_bonus(self):
        rewards.apply_test_rewards(self.test, self.student, 5, 10)
        self.assertEqual(self.balance(), 0)


class StartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_bot_waits_for_max_instead_of_crashing(self):
        import main
        attempts, pauses = [], []

        async def flaky_commands():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("нет сети")

        async def no_sleep(seconds):
            pauses.append(seconds)

        with patch.object(main, "set_bot_commands", flaky_commands), patch.object(main.asyncio, "sleep", no_sleep):
            await main.wait_for_max()
        self.assertEqual(len(attempts), 3)
        self.assertEqual(pauses, [5, 10])


class ScaleTest(unittest.TestCase):
    def test_old_database_points_are_multiplied_once(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        config.DB_PATH = str(Path(folder.name) / "old.db")
        with database.get_connection() as conn:
            conn.execute(database.STUDENT_ANSWERS_TABLE.format(name="student_answers").replace("0 AND 30", "0 AND 3"))
        database.init_db()
        database.register_user("Иванова Анна Петровна", "secret12")
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET points = 7")
        database.init_db()
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT points FROM users").fetchone()[0], 7)
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'student_answers'").fetchone()[0]
        self.assertIn("0 AND 30", sql)


class CatalogTest(DatabaseTestCase):
    def test_excel_file_becomes_topic(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            config.CATALOG_DIR = Path(folder)
            (Path(folder) / "01 Кинематика.xlsx").write_bytes((ROOT / "test_template.xlsx").read_bytes())
            (Path(folder) / "Сломанный.xlsx").write_bytes(b"not excel")
            database.seed_catalog()
            database.seed_catalog()
        self.assertIn("01 Кинематика", database.get_topics())
        self.assertNotIn("Сломанный", database.get_topics())
        self.assertEqual(len(database.get_questions_by_topic("01 Кинематика")), 2)


class OptionsTest(unittest.TestCase):
    def test_unit_is_shared_by_all_options(self):
        options = excel.parse_question_options("А — 15, Б — 30, В — 45, Г — 90 (м).")
        self.assertEqual([value for _, value in options], ["15 м", "30 м", "45 м", "90 м"])

    def test_text_options_keep_their_words(self):
        options = excel.parse_question_options("А — в 2, Б — в 3, В — в 4, Г — импульсы равны.")
        self.assertEqual(options[3][1], "импульсы равны")


class ExcelTest(unittest.TestCase):
    @staticmethod
    def book(*rows):
        workbook = Workbook()
        for row in rows:
            workbook.active.append(row)
        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def test_sample_file(self):
        self.assertEqual(len(excel.parse_test_excel((ROOT / "test_template.xlsx").read_bytes())), 2)

    def test_formula_is_rejected(self):
        data = self.book(["№", "Вопрос", "Ответ", "Пояснение", "Сложность"],
                         [1, "=1+1", "А", "", 1])
        with self.assertRaises(excel.ExcelImportError):
            excel.parse_test_excel(data)

    def test_extra_column_is_rejected(self):
        data = self.book([1, "Сколько?\nА) 1\nБ) 2\nВ) 3\nГ) 4", "А", "", 1, "лишнее"])
        with self.assertRaises(excel.ExcelImportError):
            excel.parse_test_excel(data)


if __name__ == "__main__":
    unittest.main()
