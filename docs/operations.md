# Обслуживание бота

### Обновление исходников или зависимостей

Соберите новый образ, затем остановите и удалите прежний контейнер:

```bash
docker build -f Dockerfile -t max-school-bot:local .
docker stop max-school-bot
docker rm max-school-bot
```

После этого повторите команду `docker run` для своей системы из раздела выше. Используйте тот же том `max-school-bot-data`, чтобы сохранить базу. Не удаляйте этот том, пока нужны пользователи и результаты.

Перезапуск процесса очищает авторизацию и шаги диалогов. Принятые ответы и начисленные баллы сохраняются в базе: после повторного входа ученик может открыть тест и продолжить, если его срок ещё не истёк. Для одного токена и одной базы должен работать один экземпляр бота.

## Перенос существующей базы в Docker

Эта процедура предназначена для **первого переноса в ещё пустой Docker-том**. Проводите её после завершения тестирования: авторизация и шаги диалогов не переносятся, а старые сроки тестов могли быть записаны в другом часовом поясе. Сохранённые ответы, результаты и баллы переносятся вместе с базой. Для создания копии на шаге 2 нужен Python: этот шаг можно выполнить на машине прежней установки, затем перенести `bot-transfer.db` на Docker-хост.

1. Остановите прежний локальный процесс бота. Поместите его `bot.db` и свой `token.env` в корень подготовленного проекта.
2. Создайте согласованную копию базы `bot-transfer.db` через SQLite. Команда откажется перезаписывать файл, если он уже существует:

```bash
python -c "import sqlite3; from pathlib import Path; p=Path('bot-transfer.db'); s=sqlite3.connect('file:bot.db?mode=ro', uri=True); p.open('xb').close(); d=sqlite3.connect(p); s.backup(d); d.close(); s.close()"
```

На Windows используйте `.\.venv\Scripts\python.exe` вместо `python`, если окружение не активировано.

3. Соберите образ и импортируйте копию. Следующий блок — для Linux/macOS. Он откажется заменять уже существующую базу в томе:

```bash
docker build -f Dockerfile -t max-school-bot:local .
docker volume create max-school-bot-data
docker run --rm --user 0 \
  --mount type=volume,source=max-school-bot-data,target=/app/data \
  --mount "type=bind,source=$(pwd)/bot-transfer.db,target=/restore/bot-transfer.db,readonly" \
  max-school-bot:local python -c "import os, shutil; from pathlib import Path; s=Path('/restore/bot-transfer.db'); d=Path('/app/data/bot.db'); assert s.is_file(), 'Source database is missing'; assert not d.exists(), 'Target database already exists'; shutil.copyfile(s, d); os.chown(d, 10001, 10001)"
```

Для Windows PowerShell используйте эквивалентный блок:

```powershell
docker build -f Dockerfile -t max-school-bot:local .
docker volume create max-school-bot-data
$dbBackupPath = (Resolve-Path .\bot-transfer.db).Path
docker run --rm --user 0 --mount type=volume,source=max-school-bot-data,target=/app/data --mount "type=bind,source=${dbBackupPath},target=/restore/bot-transfer.db,readonly" max-school-bot:local python -c "import os, shutil; from pathlib import Path; s=Path('/restore/bot-transfer.db'); d=Path('/app/data/bot.db'); assert s.is_file(), 'Source database is missing'; assert not d.exists(), 'Target database already exists'; shutil.copyfile(s, d); os.chown(d, 10001, 10001)"
```

После успешного импорта запустите контейнер командой из раздела [«Быстрый запуск через Docker»](../README.md#быстрый-запуск-через-docker).

Команда импорта выполняется от root только для назначения владельца файла базы; сам бот работает от пользователя `app`. Если появляется `Target database already exists`, остановитесь и проверьте содержимое тома: инструкция не предназначена для объединения двух баз или перезаписи действующей установки.

При первом запуске обновлённого кода база автоматически получит таблицу созданных тестов и необходимые дополнительные поля. Удалять прежнюю базу или регистрировать пользователей заново не требуется. Перед обновлением сохраните резервную копию.

## Резервное копирование

SQLite позволяет получить согласованную копию через [`Connection.backup()`](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup). Для локального бота выполните из рабочего каталога; каждый раз задавайте новое имя файла:

```bash
python -c "import sqlite3; from pathlib import Path; p=Path('bot-backup.db'); s=sqlite3.connect('file:bot.db?mode=ro', uri=True); p.open('xb').close(); d=sqlite3.connect(p); s.backup(d); d.close(); s.close()"
```

Для работающего контейнера:

```bash
docker exec max-school-bot python -c "import sqlite3; from pathlib import Path; p=Path('bot-backup.db'); s=sqlite3.connect('file:bot.db?mode=ro', uri=True); p.open('xb').close(); d=sqlite3.connect(p); s.backup(d); d.close(); s.close()"
docker cp max-school-bot:/app/data/bot-backup.db ./bot-backup.db
```

При повторном копировании замените `bot-backup.db` на новое имя во всех частях команды, например `bot-backup-2026-10-30.db`, в том числе в пути назначения на хосте. Первый шаг не перезапишет существующую копию внутри контейнера; `docker cp` может заменить уже существующий файл на хосте, поэтому выбирайте свободное имя.

Храните копии отдельно от контейнера. Они содержат пользователей, хеши паролей и результаты. Для переноса копии в новый пустой том используйте процедуру импорта выше, подставив имя резервного файла. Не заменяйте файл базы работающего процесса.
