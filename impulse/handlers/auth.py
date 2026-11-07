"""Старт, регистрация, вход и выход."""
import logging

from maxapi import F
from maxapi.methods.set_commands import SetCommands
from maxapi.types import BotCommand, CallbackButton, Command, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import BOT_DESCRIPTION, ROLE_NAMES, TEACHER_CODE, WELCOME_TEXT, bot, dp
from impulse.database import clear_user_max_id
from impulse.keyboards import build_profile_view, send_start_message
from impulse.session import active_tests, auth_cache, user_states

async def set_bot_commands():
    commands = [
        BotCommand(name="start", description="Запустить бота"),
        BotCommand(name="register", description="Регистрация"),
        BotCommand(name="help", description="Показать справку"),
        BotCommand(name="profile", description="Мой профиль и меню"),
        BotCommand(name="create_class", description="Создать класс (учитель)"),
        BotCommand(name="invite", description="Пригласить ученика (учитель)"),
        BotCommand(name="my_class", description="Мои классы и приглашения (ученик)"),
        BotCommand(name="join", description="Вступить в класс по коду (ученик)"),
        BotCommand(name="tests", description="Тесты и результаты"),
    ]
    await SetCommands(bot=bot, commands=commands).fetch()


async def set_bot_description():
    try:
        await bot.change_info(description=BOT_DESCRIPTION)
    except Exception:
        logging.warning("Не удалось обновить описание бота")


@dp.message_created(Command('start'))
async def hello(event: MessageCreated):
    max_user_id = str(event.from_user.user_id)
    user = auth_cache.get(max_user_id)
    if user:
        await event.message.answer(
            f"Вы вошли как {user['full_name']} ({ROLE_NAMES.get(user['role'], user['role'])}).\n"
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
            f"Вы уже авторизованы как {user['full_name']} ({ROLE_NAMES.get(user['role'], user['role'])}).\n"
            f"Для доступа к меню используйте /profile."
        )
        return
    await send_start_message(event.message, max_user_id)


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
    # Снимаем привязку, иначе вход восстановится сам при следующем нажатии.
    clear_user_max_id(user_id)
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
        text=WELCOME_TEXT,
        attachments=[builder.as_markup()]
    )


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


@dp.message_callback(F.callback.payload.in_(["teacher_reg", "student_reg"]))
async def start_registration(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    role = "teacher" if event.callback.payload == "teacher_reg" else "student"
    if role == "teacher" and TEACHER_CODE:
        user_states[user_id] = {"action": "register", "role": role, "step": "teacher_code"}
        await event.edit(text="Введите код учителя. Его выдаёт администратор бота в школе:", attachments=[])
        return
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
