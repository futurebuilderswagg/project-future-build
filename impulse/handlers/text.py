"""Обработка текстовых сообщений. Подключается последним."""
import asyncio
import hmac
import logging

from maxapi.types import CallbackButton, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import MAX_CLASS_NAME_LENGTH, MAX_FULL_NAME_LENGTH, TEACHER_CODE, bot, dp
from impulse.database import (
    add_invitation, authenticate_user, create_class, find_user_by_name, get_class_by_id,
    join_class_by_code, register_user, update_user_max_id,
)
from impulse.handlers.teacher_tests import handle_test_creation_message
from impulse.keyboards import (
    build_error_keyboard, build_invitation_keyboard, build_profile_view, send_start_message,
)
from impulse.session import (
    active_tests, attempts_locked_minutes, auth_cache, delete_password_message, failed_attempts,
    register_failed_attempt, user_states,
)

@dp.message_created()
async def handle_text(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    state = user_states.get(user_id)
    text = (getattr(event.message.body, "text", None) or "").strip()

    if not state:
        if text.startswith("/"):
            await event.message.answer("Такой команды нет. Список возможностей: /help.")
            return
        await event.message.answer("Используйте /start для начала работы.", attachments=[build_error_keyboard().as_markup()])
        return

    if state.get("action") == "create_test":
        await handle_test_creation_message(event, state)
        return

    if not text:
        return

    # Неизвестная команда не должна попасть в название класса или ФИО.
    if text.startswith("/") and state.get("step") != "password":
        await event.message.answer("Сейчас бот ждёт ответ на предыдущий вопрос. Введите его или нажмите «В профиль».",
                                   attachments=[build_error_keyboard().as_markup()])
        return

    # ---- Регистрация ----
    if state["action"] == "register":
        if state["step"] == "teacher_code":
            locked = attempts_locked_minutes(user_id)
            if locked:
                await event.message.answer(f"Слишком много попыток. Повторите через {locked} мин.")
                return
            if not hmac.compare_digest(text.encode(), TEACHER_CODE.encode()):
                register_failed_attempt(user_id)
                await event.message.answer("❌ Неверный код учителя. Попробуйте ещё раз.", attachments=[build_error_keyboard().as_markup()])
                return
            state["step"] = "name"
            await event.message.answer("Введите ваше ФИО (Фамилия Имя Отчество) одним сообщением:")
            return
        if state["step"] == "name":
            if len(text.split()) < 2 or len(text) > MAX_FULL_NAME_LENGTH:
                await event.message.answer("Введите ФИО полностью (Фамилия Имя Отчество).", attachments=[build_error_keyboard().as_markup()])
                return
            state["full_name"] = text
            state["step"] = "password"
            await event.message.answer("Введите пароль (не короче 6 символов):")
            return
        if state["step"] == "password":
            await delete_password_message(event)
            if len(text) < 6:
                await event.message.answer("Пароль слишком короткий. Введите пароль не короче 6 символов:", attachments=[build_error_keyboard().as_markup()])
                return
            # bcrypt считается в отдельном потоке, чтобы бот не замирал для остальных.
            success = await asyncio.to_thread(register_user, state["full_name"], text, state["role"])
            if success:
                user = find_user_by_name(state["full_name"])
                update_user_max_id(user["id"], user_id)
                profile_user = {key: user[key] for key in ("id", "full_name", "role")}
                auth_cache.set(user_id, profile_user)
                profile_text, builder = build_profile_view(profile_user)
                await event.message.answer(f"✅ Регистрация успешна!\n\n{profile_text}", attachments=[builder.as_markup()])
            else:
                await event.message.answer("❌ Пользователь с таким ФИО уже существует. Если это ваше ФИО, обратитесь к учителю.",
                                           attachments=[build_error_keyboard().as_markup()])
            user_states.pop(user_id, None)
            return

    # ---- Вступление в класс по коду ----
    if state["action"] == "join_class":
        user = auth_cache.get(user_id)
        if not user or user["role"] != "student":
            user_states.pop(user_id, None)
            await event.message.answer("Вступить в класс по коду может ученик. Войдите через /start.")
            return
        locked = attempts_locked_minutes(user_id)
        if locked:
            await event.message.answer(f"Слишком много попыток. Повторите через {locked} мин.")
            return
        cls, joined = join_class_by_code(text, user["id"])
        if not cls:
            register_failed_attempt(user_id)
            await event.message.answer("❌ Класс с таким кодом не найден. Проверьте код и введите его ещё раз.",
                                       attachments=[build_error_keyboard().as_markup()])
            return
        user_states.pop(user_id, None)
        note = "✅ Вы вступили в класс" if joined else "ℹ️ Вы уже состоите в классе"
        await event.message.answer(f"{note} «{cls['name']}».", attachments=[build_error_keyboard().as_markup()])
        return

    # ---- Вход ----
    if state["action"] == "login":
        if state["step"] == "name":
            state["full_name"] = text
            state["step"] = "password"
            await event.message.answer("Введите пароль:")
            return
        if state["step"] == "password":
            await delete_password_message(event)
            locked = attempts_locked_minutes(user_id)
            if locked:
                user_states.pop(user_id, None)
                await event.message.answer(f"Слишком много неудачных попыток. Повторите вход через {locked} мин.")
                return
            user = await asyncio.to_thread(authenticate_user, state["full_name"], text)
            if not user:
                register_failed_attempt(user_id)
            if user:
                failed_attempts.pop(user_id, None)
                auth_cache.set(user_id, user)
                update_user_max_id(user["id"], user_id)
                active = active_tests.get(user_id)
                if active and active["student_id"] != user["id"]:
                    active_tests.pop(user_id, None)
                profile_text, builder = build_profile_view(user)
                welcome_text = f"✅ Добро пожаловать!\n\n{profile_text}"
                if user["role"] == "student":
                    resume_test_id = state.get("resume_test_id")
                    if resume_test_id is not None:
                        builder.row(CallbackButton(text="Пройти тест", payload=f"start_test_{resume_test_id}"))
                        welcome_text += "\n\nНажмите «Пройти тест», чтобы вернуться к выбранному заданию."
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
            if len(text) > MAX_CLASS_NAME_LENGTH:
                await event.message.answer(f"Название слишком длинное: до {MAX_CLASS_NAME_LENGTH} символов. Введите короче:",
                                           attachments=[build_error_keyboard().as_markup()])
                return
            class_id, error = create_class(text, teacher["id"])
            if class_id:
                builder = InlineKeyboardBuilder()
                builder.row(CallbackButton(text="Пригласить в этот класс", payload=f"invite_to_class_{class_id}"))
                builder.row(
                    CallbackButton(text="Мои классы", payload="menu_my_classes"),
                    CallbackButton(text="В профиль", payload="back_to_profile"),
                )
                join_code = get_class_by_id(class_id)["join_code"]
                await event.message.answer(f"✅ Класс «{text}» создан!\nКод для вступления: {join_code}\n"
                                           "Сообщите его ученикам: они введут код в боте и сразу попадут в класс.",
                                           attachments=[builder.as_markup()])
            else:
                await event.message.answer(error, attachments=[build_error_keyboard().as_markup()])
            user_states.pop(user_id, None)
            return

    # ---- Приглашение ученика ----
    if state["action"] == "invite":
        if state["step"] == "name":
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
                        "\nУченик сможет принять его после входа через «Мои классы» или /my_class."
                    )
                else:
                    result_text = f"✅ Приглашение отправлено ученику {student['full_name']} в класс «{cls['name']}»."
            else:
                result_text = (
                    f"⚠️ Приглашение для {student['full_name']} сохранено, но его MAX-аккаунт ещё не привязан."
                    "\nПопросите ученика войти в бота через /start и открыть «Мои классы» или /my_class."
                )
            user_states.pop(user_id, None)
            await event.message.answer(result_text, attachments=[build_error_keyboard().as_markup()])
            return
