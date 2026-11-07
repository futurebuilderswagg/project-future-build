"""Запуск бота «Академический импульс»."""
import asyncio
import logging

from impulse.config import bot, dp
from impulse.database import init_db, seed_catalog, seed_questions
from impulse.handlers import achievements, auth, classes, student_tests, teacher_tests  # noqa: F401
from impulse.handlers.auth import set_bot_commands, set_bot_description
from impulse.scheduler import sweep_tests
# Общий обработчик текста должен подключаться последним.
from impulse.handlers import text  # noqa: F401,E402


MAX_RETRY_DELAY_SECONDS = 60


async def wait_for_max():
    """Без связи с MAX бот не падает, а ждёт и пробует снова, увеличивая паузу."""
    delay = 5
    while True:
        try:
            await set_bot_commands()
            return
        except Exception as exc:
            logging.warning("Нет связи с MAX: %s. Повтор через %s с.", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)


async def main():
    init_db()
    seed_questions()
    seed_catalog()
    await wait_for_max()
    await set_bot_description()
    sweeper = asyncio.create_task(sweep_tests())
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break
            except Exception:
                # Обрывы связи во время работы maxapi переживает сам, сюда попадают редкие сбои запуска.
                logging.exception("Получение сообщений остановилось, перезапуск после восстановления связи")
                await asyncio.sleep(10)
                await wait_for_max()
    finally:
        sweeper.cancel()


if __name__ == "__main__":
    asyncio.run(main())
