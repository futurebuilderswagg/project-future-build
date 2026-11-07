"""Тесты учителя: результаты, создание из Excel, выдача."""
import asyncio
import logging

from maxapi import F
from maxapi.types import CallbackButton, InputMedia, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import (
    MAX_IMPORTED_QUESTIONS, MAX_TEST_TITLE_LENGTH, TEMPLATE_PATH, TEST_ATTEMPT_MINUTES,
    TEST_DURATION_CHOICES, bot, dp,
)
from impulse.database import (
    create_test, create_test_template, format_test_analytics, get_class_by_id, get_class_students,
    get_class_test_report, get_teacher_class_tests, get_teacher_classes, get_teacher_test_templates,
    get_test, get_test_template, get_topics, prepare_test_questions, student_started_test,
)
from impulse.excel import ExcelImportError, download_excel_attachment, parse_test_excel
from impulse.keyboards import (
    build_create_test_keyboard, build_error_keyboard, build_teacher_tests_back_keyboard,
    build_teacher_tests_menu,
)
from impulse.messaging import deliver_test_message, require_teacher_for_tests, safe_test_callback
from impulse.session import auth_cache, user_states
from impulse.utils import (
    add_report_pagination, format_average_stats, format_report_date, get_report_page,
    parse_report_callback, short_report_text,
)

@dp.message_callback(F.callback.payload == "menu_teacher_tests")
async def menu_teacher_tests(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    user_states.pop(str(event.from_user.user_id), None)
    await safe_test_callback(
        event, text="📝 Тесты\n«Тесты по темам» — готовые тесты по блокам программы.\n"
                    "«Мои тесты» — тесты, которые вы загрузили сами.\n"
                    "«Создать свой тест» — загрузка новых вопросов из Excel.",
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
    lines.extend(["", "Средний балл — количество правильных ответов среди завершивших тест."])
    analytics = format_test_analytics(test_id, students)
    if analytics:
        lines.extend(["", *analytics])
    lines.extend(["", "Ученики:"])
    for number, student in enumerate(items, start=start + 1):
        name = short_report_text(student["full_name"], 180)
        if student["result_id"] is not None:
            result = f"{student['score']} из {student['total']} правильных"
        elif student_started_test(test_id, student["id"]):
            result = "проходит сейчас"
        else:
            result = "не начат"
        lines.append(f"{number}. {name} — {result}. Баллы в классе: {student['class_points']}.")
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


@dp.message_callback(F.callback.payload == "download_template")
async def download_template(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    await safe_test_callback(event, notification="Отправляю образец.")
    try:
        await bot.send_message(user_id=event.from_user.user_id, text="Образец Excel для создания теста.",
                               attachments=[InputMedia(path=str(TEMPLATE_PATH))])
    except Exception:
        logging.exception("Не удалось отправить образец Excel")
        await deliver_test_message(event.from_user.user_id,
                                   "Не удалось отправить файл. Образец test_template.xlsx лежит в репозитории проекта.")


@dp.message_callback(F.callback.payload == "menu_create_test")
async def menu_create_test(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    user_states[str(event.from_user.user_id)] = {"action": "create_test", "step": "name", "teacher_id": teacher["id"]}
    await safe_test_callback(event, text="Создание своего теста:\n1. Скачайте образец Excel и заполните его.\n"
                             "2. Напишите название теста.\n3. Отправьте файл в этот чат.\n\n"
                             f"Введите название теста (до {MAX_TEST_TITLE_LENGTH} символов):",
                             attachments=[build_create_test_keyboard().as_markup()])


async def handle_test_creation_message(event: MessageCreated, state: dict):
    user_id = str(event.from_user.user_id)
    teacher = auth_cache.get(user_id)
    if not teacher or teacher["role"] != "teacher" or teacher["id"] != state.get("teacher_id"):
        if user_states.get(user_id) is state:
            user_states.pop(user_id, None)
        await event.message.answer("Сессия создания теста истекла. Войдите как учитель и нажмите «Создать тест».",
                                   attachments=[build_error_keyboard().as_markup()])
        return
    body = event.message.body
    text = (getattr(body, "text", None) or "").strip()
    files = [item for item in (getattr(body, "attachments", None) or []) if getattr(item, "type", None) == "file"]
    if state.get("step") == "processing":
        await event.message.answer("Таблица уже обрабатывается. Дождитесь результата.")
        return
    if state.get("step") == "name":
        if text.startswith("/"):
            return
        title = " ".join(text.split())
        if files or not title or len(title) > MAX_TEST_TITLE_LENGTH:
            await event.message.answer(f"Сначала отправьте название отдельным текстовым сообщением: от 1 до {MAX_TEST_TITLE_LENGTH} символов.",
                                       attachments=[build_create_test_keyboard().as_markup()])
            return
        state.update(title=title, step="upload")
        await event.message.answer(
            f"Теперь отправьте Excel-файл .xlsx для теста «{title}».\n\n"
            "Нужны пять колонок в таком порядке:\n"
            "1. Номер вопроса.\n2. Вопрос и четыре варианта ответа.\n3. Правильная буква: а, б, в или г.\n4. Пояснение.\n"
            "5. Сложность: 1 — Легкий, 2 — Средний, 3 — Сложный.\n\n"
            "Названия заголовков могут быть любыми. В одной строке — один вопрос.\n"
            "Варианты перемешиваются, поэтому не пишите «все перечисленные» или «А и Б».\n"
            "Во второй колонке пишите, например:\nСколько будет 2 + 2?\nА) 3\nБ) 4\nВ) 5\nГ) 6\n\n"
            f"До {MAX_IMPORTED_QUESTIONS} вопросов, файл до 5 МБ. Прикрепите файл следующим сообщением.",
            attachments=[build_create_test_keyboard().as_markup()]
        )
        return
    if len(files) != 1:
        await event.message.answer("Прикрепите один Excel-файл .xlsx с пятью колонками.",
                                   attachments=[build_create_test_keyboard().as_markup()])
        return
    # Фиксируем обработку до первого await, чтобы два файла не создали два теста.
    state["step"] = "processing"
    try:
        data = await download_excel_attachment(files[0])
        questions = await asyncio.to_thread(parse_test_excel, data)
        current_teacher = auth_cache.get(user_id)
        if user_states.get(user_id) is not state:
            return  # Учитель отменил создание или открыл другой сценарий.
        if not current_teacher or current_teacher["role"] != "teacher" or current_teacher["id"] != state["teacher_id"]:
            user_states.pop(user_id, None)
            await event.message.answer("Время входа истекло. Войдите снова и создайте тест ещё раз.",
                                       attachments=[build_error_keyboard().as_markup()])
            return
        template_id = create_test_template(teacher["id"], state["title"], questions,
                                           source_message_id=getattr(body, "mid", None))
    except (ExcelImportError, ValueError) as exc:
        if user_states.get(user_id) is state:
            state["step"] = "upload"
            await event.message.answer(f"Не удалось импортировать тест: {exc}\n\nИсправьте таблицу и отправьте её ещё раз.",
                                       attachments=[build_create_test_keyboard().as_markup()])
        return
    except Exception:
        logging.exception("Ошибка импорта теста учителя %s", teacher["id"])
        if user_states.get(user_id) is state:
            state["step"] = "upload"
            await event.message.answer("Не удалось сохранить тест. Попробуйте отправить файл ещё раз.",
                                       attachments=[build_create_test_keyboard().as_markup()])
        return
    user_states.pop(user_id, None)
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Выдать этот тест", payload=f"select_custom_test_{template_id}"))
    builder.row(CallbackButton(text="Мои тесты", payload="menu_created_tests"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    await event.message.answer(f"✅ Тест «{state['title']}» создан.\nВопросов: {len(questions)}.\n"
                               "Он сохранён в разделе «Тесты» → «Мои тесты».",
                               attachments=[builder.as_markup()])


async def show_created_tests(event: MessageCallback, teacher: dict, page: int = 0):
    templates = get_teacher_test_templates(teacher["id"])
    items, page, pages, _ = get_report_page(templates, page)
    builder = InlineKeyboardBuilder()
    for template in items:
        builder.row(CallbackButton(text=short_report_text(f"№{template['id']}: {template['title']} ({template['question_count']})", 128),
                                   payload=f"view_custom_test_{template['id']}"))
    add_report_pagination(builder, page, pages, "created_tests_page")
    builder.row(CallbackButton(text="Создать свой тест", payload="menu_create_test"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    text = f"Мои тесты: {len(templates)}.\nВыберите тест. Страница {page + 1}/{pages}." if templates else "У вас пока нет своих тестов. Нажмите «Создать свой тест», чтобы загрузить Excel."
    await safe_test_callback(event, text=text, attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload == "menu_created_tests")
async def menu_created_tests(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if teacher:
        user_states.pop(str(event.from_user.user_id), None)
        await show_created_tests(event, teacher)


@dp.message_callback(F.callback.payload.startswith("created_tests_page_"))
async def created_tests_page(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    values = parse_report_callback(event.callback.payload, "created_tests_page_", 1)
    if teacher and values:
        await show_created_tests(event, teacher, values[0])
    elif teacher:
        await safe_test_callback(event, notification="Не удалось открыть страницу. Откройте «Созданные тесты».")


@dp.message_callback(F.callback.payload.startswith("view_custom_test_"))
async def view_custom_test(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    values = parse_report_callback(event.callback.payload, "view_custom_test_", 1)
    template = get_test_template(values[0], teacher["id"]) if values else None
    if not template:
        await safe_test_callback(event, notification="Тест не найден или принадлежит другому учителю.")
        return
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Выдать тест", payload=f"select_custom_test_{template['id']}"))
    builder.row(CallbackButton(text="Мои тесты", payload="menu_created_tests"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    await safe_test_callback(event, text=f"📝 {template['title']}\nНомер: {template['id']}\nВопросов: {template['question_count']}\n"
                             f"Создан: {format_report_date(template['created_at'])}\n\nВыберите класс для выдачи кнопкой ниже.",
                             attachments=[builder.as_markup()])


async def show_test_class_selection(event: MessageCallback, teacher: dict, topic: str, page: int = 0):
    classes = get_teacher_classes(teacher["id"])
    if not classes:
        await safe_test_callback(event, text="Сначала создайте класс и пригласите учеников.",
                                 attachments=[build_teacher_tests_back_keyboard().as_markup()])
        return
    items, page, pages, _ = get_report_page(classes, page)
    builder = InlineKeyboardBuilder()
    for cls in items:
        builder.row(CallbackButton(text=short_report_text(cls["name"], 128), payload=f"test_class_{cls['id']}"))
    add_report_pagination(builder, page, pages, "test_targets_page")
    builder.row(CallbackButton(text="Всем классам", payload="test_class_all"))
    own_test = user_states.get(str(event.from_user.user_id), {}).get("template_id") is not None
    builder.row(CallbackButton(text="Назад к тестам", payload="menu_created_tests" if own_test else "menu_give_test"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    await safe_test_callback(event, text=f"Тест: «{topic}». Выберите класс, которому выдать тест:",
                             attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload.startswith("test_targets_page_"))
async def test_targets_page(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    state = user_states.get(str(event.from_user.user_id))
    values = parse_report_callback(event.callback.payload, "test_targets_page_", 1)
    if not values or not state or state.get("action") != "give_test" or not state.get("topic") or state.get("teacher_id") != teacher["id"]:
        await safe_test_callback(event, notification="Выберите тест заново через меню «Тесты».")
        return
    await show_test_class_selection(event, teacher, state["topic"], values[0])


@dp.message_callback(F.callback.payload.startswith("select_custom_test_"))
async def select_custom_test(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    if not teacher:
        return
    values = parse_report_callback(event.callback.payload, "select_custom_test_", 1)
    template = get_test_template(values[0], teacher["id"]) if values else None
    if not template:
        await safe_test_callback(event, notification="Тест не найден или принадлежит другому учителю.")
        return
    user_states[str(event.from_user.user_id)] = {"action": "give_test", "step": "class", "teacher_id": teacher["id"],
                                               "topic": template["title"], "template_id": template["id"], "topics": get_topics()}
    await show_test_class_selection(event, teacher, template["title"])


async def show_give_test_catalog(event: MessageCallback, teacher: dict, page: int = 0):
    topics = get_topics()
    entries = [(topic, f"select_topic_{i}") for i, topic in enumerate(topics)]
    user_states[str(event.from_user.user_id)] = {"action": "give_test", "step": "topic", "teacher_id": teacher["id"], "topics": topics}
    if not entries:
        builder = build_teacher_tests_back_keyboard()
        builder.row(CallbackButton(text="Создать свой тест", payload="menu_create_test"))
        await safe_test_callback(event, text="Готовых тестов пока нет. Создайте свой тест из Excel.", attachments=[builder.as_markup()])
        return
    items, page, pages, _ = get_report_page(entries, page)
    builder = InlineKeyboardBuilder()
    for title, payload in items:
        builder.row(CallbackButton(text=short_report_text(title, 128), payload=payload))
    add_report_pagination(builder, page, pages, "test_catalog_page")
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    await safe_test_callback(event, text=f"Тесты по темам. Выберите блок программы. Страница {page + 1}/{pages}.",
                             attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload == "menu_give_test")
async def menu_give_test(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    user = auth_cache.get(user_id)
    if not user or user["role"] != "teacher":
        await event.answer(notification="Только учитель может выдавать тесты.")
        return
    await show_give_test_catalog(event, user)


@dp.message_callback(F.callback.payload.startswith("test_catalog_page_"))
async def test_catalog_page(event: MessageCallback):
    teacher = await require_teacher_for_tests(event)
    values = parse_report_callback(event.callback.payload, "test_catalog_page_", 1)
    if teacher and values:
        await show_give_test_catalog(event, teacher, values[0])
    elif teacher:
        await safe_test_callback(event, notification="Выберите тест заново через меню «Тесты».")


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
    state["teacher_id"] = teacher["id"]
    state["template_id"] = None
    await show_test_class_selection(event, teacher, topic)


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

    template_id = state.get("template_id")
    if state.get("teacher_id", teacher["id"]) != teacher["id"]:
        await safe_test_callback(event, notification="Сессия выдачи изменилась. Выберите тест заново.")
        return
    try:
        questions = prepare_test_questions(topic, template_id=template_id, teacher_id=teacher["id"])
    except ValueError:
        logging.exception("Некорректные вопросы по теме %s", topic)
        await event.answer(notification="В вопросах этой темы ошибка: проверьте варианты и правильные ответы в базе.")
        return
    if not questions:
        await event.answer(notification="В этой теме пока нет вопросов.")
        return
    if not any(get_class_students(cid) for cid in class_ids):
        await event.answer(notification="В этом классе пока нет учеников. Сначала пригласите их.")
        return

    state.update(step="duration", class_ids=class_ids, target_desc=target_desc, class_id_for_test=class_id_for_test)
    builder = InlineKeyboardBuilder()
    for minutes, label in TEST_DURATION_CHOICES.items():
        builder.row(CallbackButton(text=label, payload=f"test_duration_{minutes}"))
    builder.row(CallbackButton(text="В меню тестов", payload="menu_teacher_tests"))
    await safe_test_callback(event, text=f"Тест: «{topic}».\nСколько тест будет открыт для учеников? "
                             f"На само прохождение у ученика {TEST_ATTEMPT_MINUTES} минут.",
                             attachments=[builder.as_markup()])


@dp.message_callback(F.callback.payload.startswith("test_duration_"))
async def test_duration_selected(event: MessageCallback):
    user_id = str(event.from_user.user_id)
    state = user_states.get(user_id)
    teacher = auth_cache.get(user_id)
    if not teacher or teacher["role"] != "teacher" or not state or state.get("step") != "duration":
        await event.answer(notification="Сессия истекла, начните заново.")
        return
    values = parse_report_callback(event.callback.payload, "test_duration_", 1)
    if not values or values[0] not in TEST_DURATION_CHOICES:
        await event.answer(notification="Ошибка выбора срока.")
        return
    topic, class_ids, target_desc = state["topic"], state["class_ids"], state["target_desc"]

    # Создаём тест
    test_id = create_test(teacher["id"], topic, state["class_id_for_test"], template_id=state.get("template_id"),
                          duration_minutes=values[0])
    user_states.pop(user_id, None)
    closes_at = format_report_date(get_test(test_id)["ends_at"])

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
                         f"Тест открыт до {closes_at}. Пройдите его в удобное время: "
                         f"с момента начала будет {TEST_ATTEMPT_MINUTES} минут.\n"
                         "Нажмите «Пройти тест» или откройте /tests.",
                    attachments=[builder.as_markup()]
                )
                if sent is not None:
                    sent_count += 1
                else:
                    logging.warning("MAX не подтвердил отправку теста %s ученику %s", test_id, s["id"])
            except Exception as e:
                logging.error(f"Не удалось отправить тест ученику {s['full_name']}: {e}")

    result_text = (
        f"✅ Тест по теме «{topic}» выдан {target_desc}.\n"
        f"Открыт до {closes_at}. Приглашение отправлено {sent_count} ученикам.\n"
        "Когда срок закончится, бот пришлёт вам сводный отчёт."
    )
    if sent_count == 0:
        result_text += "\n⚠️ Сообщения не доставлены. Ученики могут открыть тест после входа через «Мои тесты» или /tests."
    builder = build_teacher_tests_back_keyboard()
    await event.edit(text=result_text, attachments=[builder.as_markup()])
