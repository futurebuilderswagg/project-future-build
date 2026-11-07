"""Даты, короткие подписи и страницы списков."""
from datetime import datetime, timezone

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from impulse.config import DISPLAY_TZ, RESULTS_PAGE_SIZE

# Все даты в базе хранятся в UTC, как и CURRENT_TIMESTAMP в SQLite.
def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).strftime("%Y-%m-%d %H:%M:%S")


def short_report_text(value, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def format_report_date(value) -> str:
    try:
        moment = datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
        return moment.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "дата не указана"


def format_average_stats(stats: dict) -> str:
    average = stats["average_score"]
    if average is None:
        return "— (нет завершённых прохождений)"
    text = format(average, ".2f").replace(".", ",")
    if stats["min_total"] == stats["max_total"]:
        text += f" из {stats['max_total']}"
    return text


def get_report_page(items: list, page: int, *, page_size: int = RESULTS_PAGE_SIZE) -> tuple[list, int, int, int]:
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = min(max(page, 0), total_pages - 1)
    start = page * page_size
    return items[start:start + page_size], page, total_pages, start


def add_report_pagination(builder: InlineKeyboardBuilder, page: int, total_pages: int, prefix: str):
    buttons = []
    if page > 0:
        buttons.append(CallbackButton(text="← Назад", payload=f"{prefix}_{page - 1}"))
    if page + 1 < total_pages:
        buttons.append(CallbackButton(text="Далее →", payload=f"{prefix}_{page + 1}"))
    if buttons:
        builder.row(*buttons)


def parse_report_callback(payload, prefix: str, count: int) -> tuple[int, ...] | None:
    if not isinstance(payload, str) or not payload.startswith(prefix):
        return None
    try:
        values = tuple(int(part) for part in payload[len(prefix):].split("_"))
    except ValueError:
        return None
    if len(values) != count or any(not 0 <= value <= 9223372036854775807 for value in values):
        return None
    return values
