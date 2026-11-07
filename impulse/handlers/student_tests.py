"""Прохождение теста учеником и разбор ошибок."""
import logging
import sqlite3
from datetime import datetime

from maxapi import F
from maxapi.types import CallbackButton, Command, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import DIFFICULTY_NAMES, dp
from impulse.database import (
    get_result, get_student_deadline, get_student_test_answers, get_test, is_test_active,
    prepare_test_questions, record_student_answer, restore_test_state, save_result, start_attempt,
    student_can_take_test, student_started_test, sync_test_progress,
)
from impulse.keyboards import (
    build_main_menu, build_teacher_tests_menu, build_test_resume_keyboard, build_tests_message,
)
from impulse.messaging import deliver_test_message, safe_test_callback, send_with_picture
from impulse.rewards import apply_test_rewards
from impulse.session import active_tests, auth_cache, get_test_lock, user_states
from impulse.utils import format_report_date, parse_report_callback, utc_now

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
            "📝 Тесты\nВыберите готовый тест по теме, свой тест или посмотрите результаты учеников.",
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


async def notify_test_delivery_failure(user_id: str, test_id: int):
    builder = build_test_resume_keyboard(test_id)
    await deliver_test_message(
        user_id,
        "⚠️ Не удалось доставить вопрос или результат. Ваши принятые ответы сохранены в базе данных.\n"
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
            menu = build_main_menu("student")
            if not is_test_active(test):
                menu.row(CallbackButton(text="Разбор ошибок", payload=f"review_{test_id}"))
            if not await safe_test_callback(event, text=result_text, attachments=[menu.as_markup()]):
                await deliver_test_message(user_id, result_text)
            return
        if not is_test_active({"ends_at": get_student_deadline(test, user["id"])}):
            if state and state["test_id"] == test_id:
                await finish_student_test(user_id, user_id, expired=True)
            elif student_started_test(test_id, user["id"]) or get_student_test_answers(test_id, user["id"]):
                # После перезапуска завершаем начатое прохождение по сохранённым ответам.
                try:
                    expired_state = restore_test_state(test, user["id"])
                    save_result(test_id, user["id"], expired_state["score"], len(expired_state["questions"]))
                except (ValueError, sqlite3.Error):
                    logging.exception("Не удалось завершить истёкший тест %s", test_id)
                    await safe_test_callback(event, notification="Не удалось сохранить результат. Попробуйте открыть тест ещё раз.")
                    return
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
                start_attempt(test_id, user["id"])
                active_tests[user_id] = restore_test_state(test, user["id"])
            except ValueError:
                logging.exception("Ошибка вопросов теста %s", test_id)
                await safe_test_callback(event, notification="Вопросы теста заполнены некорректно. Сообщите учителю.")
                return

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
    try:
        sync_test_progress(state)
        save_result(state["test_id"], state["student_id"], state["score"], len(state["questions"]))
        result = get_result(state["test_id"], state["student_id"])
    except sqlite3.Error:
        logging.exception("Не удалось сохранить итог теста %s ученика %s", state["test_id"], state["student_id"])
        return False  # Состояние и ответы сохраняются; кнопку продолжения можно нажать снова.
    active_tests.pop(user_id, None)
    title = "⏰ Время на тест истекло." if expired else "🏁 Тест завершён!"
    text = f"{title}\nВаш результат: {result['score']}/{result['total']} правильных ответов."
    text += f"\nБаллы за тест: {state['earned_points']}.\nБаллы можно потратить в «Пространстве достижений»."
    if not expired:
        text += "\n" + "\n".join(apply_test_rewards(get_test(state["test_id"]), state["student_id"], result["score"], result["total"]))
    menu = build_main_menu("student")
    test = get_test(state["test_id"])
    if is_test_active(test):
        # Пока тест открыт для класса, ответы не показываем: так их нельзя переслать друг другу.
        text += f"\nРазбор ошибок откроется после закрытия теста: {format_report_date(test['ends_at'])}."
    else:
        menu.row(CallbackButton(text="Разбор ошибок", payload=f"review_{state['test_id']}"))
    return await send_with_picture(max_user_id, "test_done.png", text, [menu.as_markup()])


async def send_question(max_user_id, user_id: str) -> bool:
    state = active_tests.get(user_id)
    if not state:
        return False
    sync_test_progress(state)
    idx = state["current"]
    questions = state["questions"]
    if idx >= len(questions):
        return await finish_student_test(max_user_id, user_id)
    if not is_test_active(state):
        return await finish_student_test(max_user_id, user_id, expired=True)

    question = questions[idx]
    options = question["shown_options"]
    options_text = "\n\n".join(f"{letter} — {value}" for letter, value, _ in options)
    left = max(1, int((datetime.fromisoformat(state["ends_at"]) - utc_now()).total_seconds() // 60))
    text = (
        f"Вопрос {idx + 1}/{len(questions)} (тема: {state['topic']}):\n"
        f"Сложность: {DIFFICULTY_NAMES[question['difficulty']]}. Осталось минут: {left}.\n\n"
        f"{question['question_text']}\n\n{options_text}\n\nВыберите ответ:"
    )
    builder = InlineKeyboardBuilder()
    builder.row(*(CallbackButton(
        text=letter, payload=f"answer_{state['test_id']}_{idx}_{letter}"
    ) for letter, _, _ in options))
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
        # На кнопках буквы своего порядка; в базу пишем исходную букву варианта.
        original = next((source for shown, _, source in question["shown_options"] if shown == letter), letter)
        try:
            saved = record_student_answer(test_id, state["student_id"], question["id"], original)
        except ValueError as exc:
            await safe_test_callback(event, notification=str(exc)[:200])
            return
        except sqlite3.Error:
            logging.exception("Не удалось сохранить ответ ученика %s на вопрос %s", state["student_id"], question["id"])
            await safe_test_callback(event, notification="Не удалось сохранить ответ. Нажмите выбранный вариант ещё раз.")
            return
        # Другой вход того же ученика мог уже сохранить ответ. Первый выбор окончательный.
        sync_test_progress(state)
        state["question_sent"] = False
        # Во время теста не сообщаем, верен ли ответ: так его нельзя передать другим.
        answer_text = "пропущен" if saved["answer"] == "X" else letter

        # Ответ и накопительные баллы уже зафиксированы в одной транзакции.
        await safe_test_callback(
            event,
            text=f"Вопрос {idx + 1}/{len(questions)}:\nСложность: {DIFFICULTY_NAMES[question['difficulty']]}\n\n"
                 f"{question['question_text']}\n\nВаш ответ: {answer_text}.\nОтвет принят.",
            attachments=[]
        )
        delivered = await send_question(event.from_user.user_id, user_id)
        if not delivered:
            await notify_test_delivery_failure(user_id, test_id)


@dp.message_callback(F.callback.payload.startswith("review_"))
async def review_test(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    values = parse_report_callback(event.callback.payload, "review_", 1)
    test = get_test(values[0]) if values else None
    if not user or not test or user["role"] != "student" or not get_result(test["id"], user["id"]):
        await safe_test_callback(event, notification="Разбор недоступен. Откройте /tests.")
        return
    if is_test_active(test):
        await safe_test_callback(event, notification="Разбор откроется после закрытия теста.")
        return
    answers = get_student_test_answers(test["id"], user["id"])
    blocks = []
    for question in prepare_test_questions(test["topic"], template_id=test.get("template_id"), teacher_id=test["teacher_id"]):
        saved = answers.get(question["id"])
        if saved and saved["is_correct"]:
            continue
        options = dict(question["parsed_options"])
        given = options.get(saved["answer"], "пропущен") if saved else "нет ответа"
        block = f"❓ {question['question_text']}\nВаш ответ: {given}\nПравильный ответ: {options[question['correct_answer']]}"
        if question.get("explanation"):
            block += f"\n💡 {question['explanation']}"
        blocks.append(block)
    await safe_test_callback(event, notification="Отправляю разбор.")
    if not blocks:
        await deliver_test_message(user_id, "🎉 Ошибок нет: все ответы верные!")
        return
    # Длинный разбор делим на несколько сообщений MAX.
    chunk = f"📖 Разбор ошибок: {test['topic']}"
    for block in blocks:
        if len(chunk) + len(block) + 2 > 3800:
            await deliver_test_message(user_id, chunk)
            chunk = block
        else:
            chunk += "\n\n" + block
    await deliver_test_message(user_id, chunk)
