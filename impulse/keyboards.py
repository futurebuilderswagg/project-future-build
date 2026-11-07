"""Кнопки и готовые экраны бота."""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import ROLE_NAMES, STUDENT_CLASSES_PAGE_SIZE, WELCOME_TEXT
from impulse.database import (
    get_available_tests, get_pending_invitations, get_student_classes, get_teacher_classes,
)
from impulse.messaging import send_with_picture
from impulse.utils import add_report_pagination, get_report_page, short_report_text

def format_student_class_lines(classes: list[dict], start: int = 0) -> list[str]:
    return [
        f"{number}. Класс «{short_report_text(cls['name'], 80)}»\n"
        f"Учитель: {short_report_text(cls['teacher_name'], 120)}\nБаллы: {cls['points']}"
        for number, cls in enumerate(classes, start + 1)
    ]


def build_profile_view(user: dict, page: int = 0) -> tuple[str, InlineKeyboardBuilder]:
    text = f"👤 Профиль:\nФИО: {short_report_text(user['full_name'], 180)}\nРоль: {ROLE_NAMES.get(user['role'], user['role'])}"
    builder = build_main_menu(user["role"])
    if user["role"] == "student":
        classes = get_student_classes(user["id"])
        items, page, pages, start = get_report_page(classes, page)
        text += f"\n\n📚 Мои классы: {len(classes)}."
        if items:
            text += "\n\n" + "\n\n".join(format_student_class_lines(items, start))
            text += f"\n\nСтраница {page + 1}/{pages}. Баллы учитываются отдельно в каждом классе."
            add_report_pagination(builder, page, pages, "profile_page")
        else:
            text += "\nВы пока не вступили ни в один класс."
        pending = len(get_pending_invitations(user["id"]))
        if pending:
            text += f"\n\n📩 Ожидают ответа приглашения: {pending}. Откройте «Мои классы»."
    return text, builder


def build_profile_text(user: dict, page: int = 0) -> str:
    return build_profile_view(user, page)[0]


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
        builder.row(CallbackButton(text="Заявки на бонусы", payload="menu_bonus_requests"))
    else:
        builder.row(
            CallbackButton(text="Мои классы", payload="menu_my_class"),
            CallbackButton(text="Мои тесты", payload="menu_my_tests"),
        )
        builder.row(CallbackButton(text="Пространство достижений", payload="menu_achievements"))
        builder.row(CallbackButton(text="Вступить по коду класса", payload="menu_join_class"))
    builder.row(CallbackButton(text="Выйти", payload="logout"))
    return builder


def build_error_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    return builder


def build_teacher_tests_menu() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Тесты по темам", payload="menu_give_test"))
    builder.row(CallbackButton(text="Мои тесты", payload="menu_created_tests"))
    builder.row(CallbackButton(text="Создать свой тест", payload="menu_create_test"))
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
        builder.row(CallbackButton(text=short_report_text(cls["name"], 128), payload=f"select_class_for_invite_{cls['id']}"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    return "Выберите класс, в который хотите пригласить ученика:", builder


def build_student_class_message(student_id: int, class_page: int = 0,
                                invitation_page: int = 0) -> tuple[str, InlineKeyboardBuilder]:
    classes = get_student_classes(student_id)
    pending = get_pending_invitations(student_id)
    items, class_page, class_pages, start = get_report_page(classes, class_page, page_size=STUDENT_CLASSES_PAGE_SIZE)
    invites, invitation_page, invite_pages, invite_start = get_report_page(pending, invitation_page, page_size=STUDENT_CLASSES_PAGE_SIZE)
    text = f"📚 Мои классы: {len(classes)}."
    if items:
        text += "\n\n" + "\n\n".join(format_student_class_lines(items, start))
        text += f"\nСтраница классов {class_page + 1}/{class_pages}."
    else:
        text += "\nВы пока не вступили ни в один класс."
    builder = InlineKeyboardBuilder()
    if invites:
        text += f"\n\n📩 Приглашения: {len(pending)}. Выберите действие для каждого приглашения."
        for number, invitation in enumerate(invites, invite_start + 1):
            text += (f"\n\n{number}. Класс «{short_report_text(invitation['name'], 80)}»\n"
                     f"Учитель: {short_report_text(invitation['teacher_name'], 120)}")
            builder.row(
                CallbackButton(text=f"Принять №{number}", payload=f"accept_{invitation['id']}"),
                CallbackButton(text=f"Отклонить №{number}", payload=f"decline_{invitation['id']}")
            )
        text += f"\nСтраница приглашений {invitation_page + 1}/{invite_pages}."
    else:
        text += "\n\nНовых приглашений нет."
    class_buttons, invitation_buttons = [], []
    if class_page > 0:
        class_buttons.append(CallbackButton(text="← Классы", payload=f"student_classes_{class_page - 1}_{invitation_page}"))
    if class_page + 1 < class_pages:
        class_buttons.append(CallbackButton(text="Классы →", payload=f"student_classes_{class_page + 1}_{invitation_page}"))
    if invitation_page > 0:
        invitation_buttons.append(CallbackButton(text="← Приглашения", payload=f"student_classes_{class_page}_{invitation_page - 1}"))
    if invitation_page + 1 < invite_pages:
        invitation_buttons.append(CallbackButton(text="Приглашения →", payload=f"student_classes_{class_page}_{invitation_page + 1}"))
    if class_buttons:
        builder.row(*class_buttons)
    if invitation_buttons:
        builder.row(*invitation_buttons)
    builder.row(CallbackButton(text="Обновить", payload=f"student_classes_{class_page}_{invitation_page}"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    return text, builder


async def send_start_message(event_or_message, user_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Учитель", payload="teacher"),
        CallbackButton(text="Ученик", payload="student")
    )
    await send_with_picture(user_id, "welcome.png", WELCOME_TEXT, [builder.as_markup()])


def build_create_test_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Скачать образец Excel", payload="download_template"))
    builder.row(CallbackButton(text="Отмена", payload="back_to_profile"))
    return builder


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


def build_test_resume_keyboard(test_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Продолжить тест", payload=f"start_test_{test_id}"))
    builder.row(CallbackButton(text="Мои тесты", payload="menu_my_tests"))
    return builder


def build_order_keyboard(order_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text=f"Подтвердить №{order_id}", payload=f"bonus_ok_{order_id}"),
        CallbackButton(text=f"Отклонить №{order_id}", payload=f"bonus_no_{order_id}"),
    )
    return builder
