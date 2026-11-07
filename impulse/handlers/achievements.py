"""Пространство достижений и заявки на бонусы."""
import re

from maxapi import F
from maxapi.types import CallbackButton, MessageCallback
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import BONUSES, dp
from impulse.database import get_class_by_id, get_student_classes, get_user_max_id
from impulse.keyboards import build_order_keyboard
from impulse.messaging import (
    deliver_test_message, require_teacher_for_tests, safe_test_callback, send_with_picture,
)
from impulse.rewards import (
    decide_bonus_order, format_level, get_badges, get_bonus, get_earned_points, get_pending_orders,
    get_streak, get_student_orders, order_bonus,
)
from impulse.session import auth_cache
from impulse.utils import format_report_date, parse_report_callback, short_report_text, utc_text

async def show_achievements(event: MessageCallback, user: dict, cls: dict):
    balance = cls["points"]
    streak = get_streak(user["id"])
    text = (f"🏆 Пространство достижений\nКласс: {short_report_text(cls['name'], 80)}\n"
            f"Уровень: {format_level(get_earned_points(cls['id'], user['id']))}\nБаланс: {balance}\n"
            f"🔥 Импульс: дней подряд {streak['current']}, рекорд {streak['best']}.\n")
    badges = get_badges(user["id"])
    if badges:
        text += "\nЗначки:" + "".join(f"\n🏅 {badge}" for badge in badges) + "\n"
    text += "\nБонусы:"
    builder = InlineKeyboardBuilder()
    for bonus in BONUSES:
        text += f"\n• {bonus['name']} — {bonus['cost']}"
        builder.row(CallbackButton(text=short_report_text(f"{bonus['cost']}: {bonus['name']}", 128),
                                   payload=f"ach_pick_{cls['id']}_{bonus['key']}"))
    orders = get_student_orders(cls["id"], user["id"])
    if orders:
        names = {"pending": "ждёт учителя", "approved": "подтверждён", "rejected": "отклонён, баллы возвращены"}
        text += "\n\nМои заявки:" + "".join(f"\n• {o['bonus_name']} — {names[o['status']]}" for o in orders)
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    await safe_test_callback(event, text=text, attachments=[builder.as_markup()])


def get_student_class_by_id(student_id: int, class_id: int) -> dict | None:
    return next((cls for cls in get_student_classes(student_id) if cls["id"] == class_id), None)


async def require_student(event: MessageCallback) -> dict | None:
    user = auth_cache.get(str(event.from_user.user_id))
    if not user or user["role"] != "student":
        await safe_test_callback(event, notification="Войдите как ученик через /start.")
        return None
    return user


@dp.message_callback(F.callback.payload == "menu_achievements")
async def menu_achievements(event: MessageCallback):
    user = await require_student(event)
    if not user:
        return
    classes = get_student_classes(user["id"])
    if len(classes) == 1:
        await show_achievements(event, user, classes[0])
        return
    builder = InlineKeyboardBuilder()
    for cls in classes[:20]:
        builder.row(CallbackButton(text=short_report_text(f"{cls['name']}: {cls['points']}", 128), payload=f"ach_class_{cls['id']}"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    text = "Баллы считаются отдельно в каждом классе. Выберите класс:" if classes else "Сначала вступите в класс."
    await safe_test_callback(event, text=text, attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload.startswith("ach_class_"))
async def achievements_class(event: MessageCallback):
    user = await require_student(event)
    values = parse_report_callback(event.callback.payload, "ach_class_", 1)
    cls = get_student_class_by_id(user["id"], values[0]) if user and values else None
    if cls:
        await show_achievements(event, user, cls)
    elif user:
        await safe_test_callback(event, notification="Класс не найден.")


@dp.message_callback(F.callback.payload.startswith("ach_pick_"))
async def achievements_pick(event: MessageCallback):
    user = await require_student(event)
    match = re.fullmatch(r"ach_pick_([0-9]{1,19})_([a-z]+)", event.callback.payload)
    bonus = get_bonus(match[2]) if match else None
    cls = get_student_class_by_id(user["id"], int(match[1])) if user and match else None
    if not user:
        return
    if not cls or not bonus:
        await safe_test_callback(event, notification="Бонус не найден.")
        return
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Да, получить", payload=f"ach_yes_{cls['id']}_{bonus['key']}"),
        CallbackButton(text="Назад", payload=f"ach_class_{cls['id']}"),
    )
    await safe_test_callback(event, text=f"Бонус: {bonus['name']}\nСтоимость: {bonus['cost']}, баланс: {cls['points']}.\n"
                             "Баллы спишутся сразу. Если учитель отклонит заявку, они вернутся.",
                             attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload.startswith("ach_yes_"))
async def achievements_confirm(event: MessageCallback):
    user = await require_student(event)
    match = re.fullmatch(r"ach_yes_([0-9]{1,19})_([a-z]+)", event.callback.payload)
    bonus = get_bonus(match[2]) if match else None
    if not user:
        return
    if not bonus:
        await safe_test_callback(event, notification="Бонус не найден.")
        return
    class_id = int(match[1])
    order_id, error = order_bonus(class_id, user["id"], bonus)
    if error:
        await safe_test_callback(event, notification=error)
        return
    cls = get_class_by_id(class_id)
    teacher = get_user_max_id(cls["teacher_id"])
    if teacher:
        await deliver_test_message(teacher, f"🎁 Заявка на бонус №{order_id}\nУченик: {user['full_name']}\n"
                                   f"Класс: {short_report_text(cls['name'], 80)}\nБонус: {bonus['name']}",
                                   [build_order_keyboard(order_id).as_markup()])
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="К достижениям", payload=f"ach_class_{class_id}"))
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    await safe_test_callback(event, text=f"✅ Заявка отправлена учителю.\nСписано баллов: {bonus['cost']}.",
                             attachments=[builder.as_markup()])


async def show_bonus_requests(event: MessageCallback, teacher: dict):
    orders = get_pending_orders(teacher["id"])
    builder = InlineKeyboardBuilder()
    lines = ["🎁 Заявки на бонусы"]
    for order in orders:
        lines.append(f"№{order['id']}. {short_report_text(order['full_name'], 60)}, "
                     f"класс {short_report_text(order['class_name'], 30)}: {order['bonus_name']}")
        builder.row(
            CallbackButton(text=f"Подтвердить №{order['id']}", payload=f"bonus_ok_{order['id']}"),
            CallbackButton(text=f"Отклонить №{order['id']}", payload=f"bonus_no_{order['id']}"),
        )
    if not orders:
        lines.append("Новых заявок нет.")
    builder.row(CallbackButton(text="В профиль", payload="back_to_profile"))
    await safe_test_callback(event, text="\n".join(lines), attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload == "menu_bonus_requests")
async def menu_bonus_requests(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if teacher:
        await show_bonus_requests(event, teacher)


@dp.message_callback(F.callback.payload.startswith("bonus_ok_"))
async def bonus_approve(event: MessageCallback):
    await decide_bonus(event, True)


@dp.message_callback(F.callback.payload.startswith("bonus_no_"))
async def bonus_reject(event: MessageCallback):
    await decide_bonus(event, False)


async def decide_bonus(event: MessageCallback, approve: bool):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    values = parse_report_callback(event.callback.payload, "bonus_ok_" if approve else "bonus_no_", 1)
    order = decide_bonus_order(values[0], teacher["id"], approve) if values else None
    if not order:
        await safe_test_callback(event, notification="Заявка не найдена или уже обработана.")
        return
    if order["max_user_id"]:
        if not approve:
            text = f"Учитель отклонил заявку «{order['bonus_name']}». Баллы возвращены: {order['cost']}."
        elif order["bonus_key"] == "expert":
            text = (f"🏅 Сертификат «Эксперт темы»\nВыдан: {order['full_name']}\nКласс: {order['class_name']}\n"
                    f"Учитель: {teacher['full_name']}\nДата: {format_report_date(utc_text())}")
        else:
            text = f"✅ Учитель подтвердил бонус: {order['bonus_name']}."
        picture = "certificate.png" if approve and order["bonus_key"] == "expert" else "bonus.png"
        await send_with_picture(order["max_user_id"], picture, text)
    await show_bonus_requests(event, teacher)
