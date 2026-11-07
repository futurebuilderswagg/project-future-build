"""Вход пользователей и шаги диалогов в памяти."""
import asyncio
import logging
import time

from maxapi.types import MessageCreated

from impulse.config import CACHE_TTL, LOGIN_LOCK_SECONDS, MAX_LOGIN_ATTEMPTS
from impulse.database import find_user_by_max_id

class AuthCache:
    def __init__(self, ttl_seconds: int = CACHE_TTL):
        self._cache = {}
        self.ttl = ttl_seconds

    def set(self, user_id: str, user_data: dict):
        self._cache[user_id] = (user_data, time.time() + self.ttl)

    def get(self, user_id: str) -> dict | None:
        if user_id in self._cache:
            user_data, expires_at = self._cache[user_id]
            if time.time() <= expires_at:
                return user_data
            del self._cache[user_id]
        # Вход не слетает после перезапуска: аккаунт MAX уже привязан к пользователю.
        user_data = find_user_by_max_id(user_id)
        if user_data:
            self.set(user_id, user_data)
        return user_data

    def clear(self, user_id: str):
        self._cache.pop(user_id, None)


auth_cache = AuthCache()


user_states = {}       # состояния диалога (регистрация, вход, создание класса, выдача теста)


active_tests = {}      # current — индекс вопроса, ожидающего ответа; student_id хранится до сохранения результата


test_locks = {}        # блокировки защищают от двойных и одновременных нажатий ученика


failed_attempts = {}   # неудачные вводы пароля и кодов: user_id -> (число, время разблокировки)


def get_test_lock(user_id: str) -> asyncio.Lock:
    if user_id not in test_locks:
        test_locks[user_id] = asyncio.Lock()
    return test_locks[user_id]


def attempts_locked_minutes(user_id: str) -> int:
    _, until = failed_attempts.get(user_id, (0, 0))
    return int((until - time.time()) // 60) + 1 if until > time.time() else 0


def register_failed_attempt(user_id: str):
    count = failed_attempts.get(user_id, (0, 0))[0] + 1
    if count >= MAX_LOGIN_ATTEMPTS:
        failed_attempts[user_id] = (0, time.time() + LOGIN_LOCK_SECONDS)
    else:
        failed_attempts[user_id] = (count, 0)


async def delete_password_message(event: MessageCreated):
    try:
        await event.message.delete()
    except Exception:
        logging.warning("Не удалось удалить сообщение с паролем")
