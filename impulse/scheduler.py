"""Фоновое закрытие тестов и отчёты по сроку."""
import asyncio
import logging

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse import config
from impulse.database import (
    format_test_analytics, get_class_test_report, get_connection, get_teacher_classes,
    get_tests_to_close, get_unfinished_attempts, get_user_max_id, restore_test_state, save_result,
)
from impulse.handlers.student_tests import finish_student_test
from impulse.messaging import deliver_test_message
from impulse.session import active_tests, get_test_lock
from impulse.utils import format_average_stats, short_report_text, utc_text

async def finalize_attempt(test: dict, student_id: int):
    max_user_id = get_user_max_id(student_id)
    if max_user_id:
        async with get_test_lock(max_user_id):
            state = active_tests.get(max_user_id)
            if state and state["test_id"] == test["id"] and state["student_id"] == student_id:
                await finish_student_test(max_user_id, max_user_id, expired=True)
                return
    state = restore_test_state(test, student_id)
    save_result(test["id"], student_id, state["score"], len(state["questions"]))
    if max_user_id:
        await deliver_test_message(max_user_id, f"⏰ Время на тест «{test['topic']}» истекло.\n"
                                   f"Ваш результат: {state['score']}/{len(state['questions'])}.")


async def close_test(test: dict):
    """Сохраняем брошенные прохождения, шлём разбор ученикам и отчёт учителю."""
    with get_connection() as conn:
        pending = conn.execute("""
            SELECT DISTINCT a.student_id FROM student_answers a
            LEFT JOIN test_results r ON r.test_id = a.test_id AND r.student_id = a.student_id
            WHERE a.test_id = ? AND r.id IS NULL
        """, (test["id"],)).fetchall()
        finished = conn.execute("SELECT student_id FROM test_results WHERE test_id = ?", (test["id"],)).fetchall()
        conn.execute("UPDATE tests SET closed_at = ? WHERE id = ?", (utc_text(), test["id"]))
        conn.commit()
    for row in pending:
        await finalize_attempt(test, row["student_id"])
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Разбор ошибок", payload=f"review_{test['id']}"))
    for student_id in {row["student_id"] for row in [*pending, *finished]}:
        max_user_id = get_user_max_id(student_id)
        if max_user_id:
            await deliver_test_message(max_user_id, f"📖 Тест «{test['topic']}» закрыт. Открыт разбор ошибок.",
                                       [builder.as_markup()])
    teacher_max_id = get_user_max_id(test["teacher_id"])
    if not teacher_max_id:
        return
    menu = InlineKeyboardBuilder()
    class_ids = [test["class_id"]] if test["class_id"] else [c["id"] for c in get_teacher_classes(test["teacher_id"])]
    for class_id in class_ids[:5]:
        report = get_class_test_report(test["teacher_id"], class_id, test["id"])
        if not report:
            continue
        stats = report["class_stats"]
        lines = [f"📊 Тест закрыт: «{test['topic']}»", f"Класс: {short_report_text(report['class']['name'], 80)}",
                 f"Прошли: {stats['completed_count']} из {len(report['students'])}.",
                 f"Средний результат: {format_average_stats(stats)}."]
        lines.extend(format_test_analytics(test["id"], report["students"]))
        menu = InlineKeyboardBuilder()
        menu.row(CallbackButton(text="Подробный отчёт", payload=f"results_view_{class_id}_{test['id']}_0"))
        await deliver_test_message(teacher_max_id, "\n".join(lines), [menu.as_markup()])


async def sweep_tests():
    while True:
        await asyncio.sleep(config.SWEEP_INTERVAL_SECONDS)
        # Ошибка в одном тесте не должна мешать закрытию остальных.
        for test, student_id in get_unfinished_attempts():
            try:
                await finalize_attempt(test, student_id)
            except Exception:
                logging.exception("Не удалось завершить тест %s ученика %s", test["id"], student_id)
        for test in get_tests_to_close():
            try:
                await close_test(test)
            except Exception:
                logging.exception("Не удалось закрыть тест %s", test["id"])
