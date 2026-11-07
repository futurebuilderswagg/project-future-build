"""Отправка сообщений с защитой от ошибок MAX."""
import logging

from maxapi.types import InputMedia, MessageCallback

from impulse import config
from impulse.config import bot
from impulse.session import auth_cache

async def require_teacher_for_tests(event: MessageCallback) -> dict | None:
    user = auth_cache.get(str(event.from_user.user_id))
    if not user:
        await safe_test_callback(event, notification="Сессия входа истекла. Войдите через /start.")
        return None
    if user["role"] != "teacher":
        await safe_test_callback(event, notification="Меню тестов и результаты классов доступны только учителю.")
        return None
    return user


async def safe_test_callback(event: MessageCallback, *, text=None, notification=None, attachments=None) -> bool:
    """Ошибка обновления старого сообщения не должна останавливать тест."""
    try:
        if text is None:
            result = await event.answer(notification=notification)
        else:
            result = await event.edit(
                text=text, attachments=attachments if attachments is not None else [],
                notification=notification
            )
        if result is None or result is False or getattr(result, "success", True) is False:
            logging.warning("MAX не подтвердил обработку callback теста")
            return False
        return True
    except Exception:
        logging.exception("Не удалось ответить на callback теста")
        return False


async def deliver_test_message(max_user_id, text: str, attachments=None) -> bool:
    try:
        sent = await bot.send_message(
            user_id=int(max_user_id), text=text,
            attachments=attachments if attachments is not None else []
        )
        if sent is None:
            logging.warning("MAX не подтвердил доставку сообщения теста пользователю %s", max_user_id)
            return False
        return True
    except Exception:
        logging.exception("Не удалось доставить сообщение теста пользователю %s", max_user_id)
        return False


async def send_with_picture(max_user_id, picture: str, text: str, attachments=None) -> bool:
    """Картинка из папки assets необязательна: без неё уходит обычный текст."""
    path = config.ASSETS_DIR / picture
    attachments = attachments if attachments is not None else []
    if path.is_file() and await deliver_test_message(max_user_id, text, [InputMedia(path=str(path)), *attachments]):
        return True
    return await deliver_test_message(max_user_id, text, attachments)
