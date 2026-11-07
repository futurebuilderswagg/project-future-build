"""Классы, приглашения, профиль и справка."""
import re
from html import escape

from maxapi import F
from maxapi.types import CallbackButton, Command, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import dp
from impulse.database import get_class_by_id, get_class_students, get_teacher_classes, set_invitation_status
from impulse.keyboards import (
    build_error_keyboard, build_invite_class_selection, build_profile_view,
    build_student_class_message,
)
from impulse.messaging import safe_test_callback
from impulse.session import auth_cache, user_states
from impulse.utils import add_report_pagination, get_report_page, short_report_text

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
    classes_text, builder = build_student_class_message(user["id"])
    text = (
        f"✅ Вы приняли приглашение в класс «{short_report_text(cls['name'], 80)}»!"
        if status == "accepted"
        else f"Вы отклонили приглашение в класс «{short_report_text(cls['name'], 80)}»."
    )
    await event.edit(text=text + "\n\n" + classes_text, attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload.startswith("accept_"))
async def accept_invite(event: MessageCallback):
    await respond_to_invitation(event, "accepted")


@dp.message_callback(F.callback.payload.startswith("decline_"))
async def decline_invite(event: MessageCallback):
    await respond_to_invitation(event, "declined")


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


@dp.message_callback(F.callback.payload.startswith("student_classes_"))
async def student_classes_page(event: MessageCallback):
    user = auth_cache.get(str(event.from_user.user_id))
    if not user or user["role"] != "student":
        await event.answer(notification="Войдите как ученик через /start, затем откройте /my_class.")
        return
    match = re.fullmatch(r"student_classes_([0-9]{1,9})_([0-9]{1,9})", event.callback.payload)
    if not match:
        await event.answer(notification="Некорректная страница. Откройте /my_class.")
        return
    text, builder = build_student_class_message(user["id"], int(match[1]), int(match[2]))
    await event.edit(text=text, attachments=[builder.as_markup()])


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
        builder.row(CallbackButton(text=short_report_text(cls["name"], 128), payload=f"view_class_{cls['id']}"))
    builder.row(CallbackButton(text="Назад", payload="back_to_profile"))
    await event.edit(text=f"Количество ваших классов: {len(classes)}", attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload.startswith("view_class_"))
async def view_class(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Только учитель может просматривать классы.")
        return
    match = re.fullmatch(r"view_class_([0-9]{1,19})(?:_([0-9]{1,9}))?", event.callback.payload)
    if not match or not 0 < int(match[1]) <= 9223372036854775807:
        builder = build_error_keyboard()
        await event.edit(text="❌ Некорректный запрос.", attachments=[builder.as_markup()])
        return
    class_id = int(match[1])
    cls = get_class_by_id(class_id)
    if not cls or cls["teacher_id"] != user["id"]:
        await event.answer(notification="Класс не найден или принадлежит другому учителю.")
        return
    page = int(match[2] or 0)
    students = get_class_students(class_id)
    items, page, pages, _ = get_report_page(students, page)
    class_name = escape(short_report_text(cls["name"], 60))
    text = f"📚 Класс «{class_name}»\nКод для вступления: {cls['join_code']}\nУчеников: {len(students)}.\n\n"
    if students:
        # Ограничение учитывает и увеличение HTML после экранирования символов.
        names = [short_report_text(" ".join(s["full_name"].split()[:2]), 50) for s in items]
        width = max(len("Фамилия Имя"), *(len(name) for name in names))
        lines = [f"{'Фамилия Имя':<{width}} | Баллы"]
        lines.extend(f"{name:<{width}} | {student['points']}" for name, student in zip(names, items))
        text += "<pre>" + escape("\n".join(lines)) + "</pre>"
        text += "\n\nБаллы за тесты этого учителя, назначенные этому классу."
    else:
        text += "Учеников пока нет."
    text += f"\nСтраница {page + 1}/{pages}."
    builder = InlineKeyboardBuilder()
    add_report_pagination(builder, page, pages, f"view_class_{class_id}")
    builder.row(CallbackButton(text="Обновить", payload=f"view_class_{class_id}_{page}"))
    builder.row(CallbackButton(text="Назад к классам", payload="menu_my_classes"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    await event.edit(text=text, attachments=[builder.as_markup()], format="html")


@dp.message_callback(F.callback.payload == "back_to_profile")
async def back_to_profile(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user_states.pop(user_id, None)
    user = auth_cache.get(user_id)
    if user:
        text, builder = build_profile_view(user)
        await event.edit(
            text=text,
            attachments=[builder.as_markup()]
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.row(
            CallbackButton(text="Учитель", payload="teacher"),
            CallbackButton(text="Ученик", payload="student")
        )
        await event.edit(text="Вы не авторизованы. Выберите роль:", attachments=[builder.as_markup()])


@dp.message_created(Command("profile"))
async def cmd_profile(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    cached_user = auth_cache.get(user_id)
    if cached_user:
        text, builder = build_profile_view(cached_user)
        await event.message.answer(
            text,
            attachments=[builder.as_markup()]
        )
    else:
        await event.message.answer("Сначала войдите: /start → Войти.", attachments=[build_error_keyboard().as_markup()])


@dp.message_callback(F.callback.payload.startswith("profile_page_"))
async def profile_page(event: MessageCallback):
    user = auth_cache.get(str(event.from_user.user_id))
    if not user:
        await event.answer(notification="Сначала войдите через /start.")
        return
    match = re.fullmatch(r"profile_page_([0-9]{1,9})", event.callback.payload)
    if not match:
        await event.answer(notification="Некорректная страница. Откройте /profile.")
        return
    text, builder = build_profile_view(user, int(match[1]))
    await event.edit(text=text, attachments=[builder.as_markup()])


async def ask_join_code(user: dict | None, user_id: str) -> str:
    if not user or user["role"] != "student":
        return "Вступить в класс по коду может ученик. Войдите через /start."
    user_states[user_id] = {"action": "join_class", "step": "code"}
    return "Введите код класса из шести цифр. Его сообщает учитель."


@dp.message_created(Command("join"))
async def cmd_join(event: MessageCreated):
    user_id = str(event.from_user.user_id)
    await event.message.answer(await ask_join_code(auth_cache.get(user_id), user_id))


@dp.message_callback(F.callback.payload == "menu_join_class")
async def menu_join_class(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    await safe_test_callback(event, text=await ask_join_code(auth_cache.get(user_id), user_id),
                             attachments=[build_error_keyboard().as_markup()])


@dp.message_created(Command("help"))
async def cmd_help(event: MessageCreated):
    user = auth_cache.get(str(event.from_user.user_id))
    if user and user["role"] == "teacher":
        text = ("Как работать с ботом:\n1. Создайте класс: /create_class. Бот покажет код из шести цифр.\n"
                "2. Сообщите код ученикам или пригласите их по ФИО: /invite.\n"
                "3. Откройте /tests: «Тесты по темам» — готовые блоки, «Создать свой тест» — загрузка из Excel.\n"
                "4. Когда срок закончится, бот пришлёт отчёт: кому нужна помощь и какие вопросы оказались трудными.\n"
                "5. Подтверждайте бонусы кнопкой «Заявки на бонусы».")
    elif user:
        text = ("Как работать с ботом:\n1. Вступите в класс: /join и код от учителя.\n"
                "2. Проходите тесты: /tests. За верные ответы начисляются баллы, а не оценки.\n"
                "3. Тратьте баллы в «Пространстве достижений»: откройте /profile.\n"
                "4. После закрытия теста откройте разбор ошибок.")
    else:
        text = "Это бот «Академический импульс». Нажмите /start, чтобы зарегистрироваться или войти."
    await event.message.answer(text, attachments=[build_error_keyboard().as_markup()])
