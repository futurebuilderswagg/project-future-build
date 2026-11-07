"""Чтение и проверка тестов из Excel."""
import asyncio
import ipaddress
import json
import re
import socket
from io import BytesIO
from pathlib import PurePath
from urllib.parse import urljoin, urlsplit
from zipfile import ZipFile

import aiohttp
from openpyxl import load_workbook

from impulse.config import (
    MAX_EXCEL_FILE_BYTES, MAX_EXCEL_ROWS, MAX_EXCEL_UNPACKED_BYTES, MAX_IMPORTED_QUESTIONS,
    MAX_TEST_TITLE_LENGTH,
)

class ExcelImportError(ValueError):
    """Ошибка таблицы, которую можно показать учителю."""


def parse_excel_question_number(value) -> int:
    if isinstance(value, bool):
        raise ExcelImportError("В первой колонке нужен положительный целый номер вопроса.")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]+", str(value).strip()):
        raise ExcelImportError("В первой колонке нужен положительный целый номер вопроса.")
    number = int(str(value).strip())
    if not 0 < number <= 2147483647:
        raise ExcelImportError("Номер вопроса должен быть от 1 до 2147483647.")
    return number


def parse_excel_difficulty(value) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, str)) or str(value).strip() not in ("1", "2", "3"):
        raise ExcelImportError("В пятой колонке «Сложность» нужно целое число 1, 2 или 3: 1 — Легкий, 2 — Средний, 3 — Сложный.")
    return int(str(value).strip())


def validate_imported_question(question: dict, title: str = ""):
    parse_excel_difficulty(question.get("difficulty"))
    body = question.get("question_text")
    if not isinstance(body, str) or not body.strip():
        raise ExcelImportError("Во второй колонке нужен текст вопроса и четыре варианта ответа.")
    options = parse_question_options(question.get("options", ""))
    answer = question.get("correct_answer")
    if not isinstance(answer, str) or answer.strip().upper() not in ("А", "Б", "В", "Г"):
        raise ExcelImportError("В третьей колонке нужна одна русская буква: а, б, в или г.")
    explanation = question.get("explanation") or ""
    if not isinstance(explanation, str):
        raise ExcelImportError("Пояснение должно быть текстом.")
    options_text = "\n".join(f"{letter} — {value}" for letter, value in options)
    if len(body) + len(options_text) + max(len(title), MAX_TEST_TITLE_LENGTH) + 150 > 4000:
        raise ExcelImportError("Вопрос с вариантами слишком длинный для одного сообщения MAX.")
    if len(body) + len(explanation) + 200 > 4000:
        raise ExcelImportError("Вопрос с пояснением слишком длинный для одного сообщения MAX.")


def split_excel_question(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ExcelImportError("Во второй колонке нужен текст вопроса и варианты А, Б, В, Г.")
    raw = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    matches = list(re.finditer(r"(?:^|(?<=[\n,;?.]))[ \t]*([АБВГ])\s*[—–\-).:]\s*", raw, re.IGNORECASE))
    if len(matches) != 4 or tuple(m[1].upper() for m in matches) != ("А", "Б", "В", "Г"):
        raise ExcelImportError("Во второй колонке после вопроса запишите четыре варианта по строкам: А) ..., Б) ..., В) ..., Г) ...")
    body = raw[:matches[0].start()].strip()
    values = [raw[m.end():matches[i + 1].start() if i < 3 else len(raw)].strip().rstrip(",;").strip()
              for i, m in enumerate(matches)]
    if not body or any(not value for value in values):
        raise ExcelImportError("Текст вопроса и все четыре варианта должны быть заполнены.")
    return body, json.dumps(values, ensure_ascii=False)


def parse_test_excel(data: bytes) -> list[dict]:
    if not data or len(data) > MAX_EXCEL_FILE_BYTES:
        raise ExcelImportError("Отправьте непустой файл .xlsx размером не более 5 МБ.")
    try:
        with ZipFile(BytesIO(data)) as archive:
            parts = archive.infolist()
            if len(parts) > 256 or sum(part.file_size for part in parts) > MAX_EXCEL_UNPACKED_BYTES:
                raise ExcelImportError("Таблица слишком большая. Удалите лишние листы и форматирование.")
            names = {part.filename for part in parts}
            if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
                raise ExcelImportError("Файл не является книгой Excel .xlsx.")
            # XLSX не требует DTD. Проверяем XML до загрузки sharedStrings и листов.
            for part in parts:
                if part.flag_bits & 1:
                    raise ExcelImportError("Снимите пароль с файла Excel перед загрузкой.")
                if part.filename.lower().endswith((".xml", ".rels")):
                    xml = archive.read(part).replace(b"\x00", b"").upper()
                    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                        raise ExcelImportError("Файл содержит неподдерживаемую XML-структуру. Сохраните его заново как .xlsx.")
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
    except ExcelImportError:
        raise
    except Exception as exc:
        raise ExcelImportError("Не удалось открыть Excel. Сохраните таблицу как .xlsx без пароля.") from exc

    questions = []
    numbers = set()
    rows_seen = 0
    try:
        for sheet in workbook.worksheets:
            sheet.reset_dimensions()
            first_nonempty = True
            for row_number, cells in enumerate(sheet.iter_rows(), 1):
                rows_seen += 1
                if rows_seen > MAX_EXCEL_ROWS:
                    raise ExcelImportError(f"В книге слишком много строк: допустимо до {MAX_EXCEL_ROWS}, включая пустые.")
                values = [cell.value for cell in cells]
                if not any(value is not None and str(value).strip() for value in values):
                    continue
                location = f"Лист «{sheet.title}», строка {row_number}: "
                if any(value is not None and str(value).strip() for value in values[5:]):
                    raise ExcelImportError(location + "нужны ровно пять колонок; уберите данные правее пятой.")
                values = (values + [None] * 5)[:5]
                if any(cell.data_type in ("f", "e") for cell in cells[:5]):
                    raise ExcelImportError(location + "используйте обычные значения, без формул и ошибок Excel.")
                # Названия заголовков не используются; порядок колонок фиксирован.
                if first_nonempty:
                    first_nonempty = False
                    first = values[0]
                    if first is None or (isinstance(first, str) and not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", first.strip())):
                        try:
                            split_excel_question(values[1])
                        except ExcelImportError:
                            continue
                        # Строку с готовым вопросом нельзя молча принять за шапку.
                try:
                    number = parse_excel_question_number(values[0])
                    if number in numbers:
                        raise ExcelImportError(f"повторяется номер вопроса {number}; номера в книге должны быть уникальными.")
                    body, options = split_excel_question(values[1])
                    answer = values[2].strip().upper() if isinstance(values[2], str) else ""
                    explanation = str(values[3]).strip() if values[3] is not None else ""
                    question = {"question_number": number, "question_text": body, "options": options,
                                "correct_answer": answer, "explanation": explanation,
                                "difficulty": parse_excel_difficulty(values[4])}
                    validate_imported_question(question)
                except ValueError as exc:
                    raise ExcelImportError(location + str(exc)) from exc
                questions.append(question)
                numbers.add(number)
                if len(questions) > MAX_IMPORTED_QUESTIONS:
                    raise ExcelImportError(f"В одном тесте допускается до {MAX_IMPORTED_QUESTIONS} вопросов.")
    except ExcelImportError:
        raise
    except Exception as exc:
        raise ExcelImportError("Не удалось прочитать строки Excel. Проверьте формат файла.") from exc
    finally:
        workbook.close()
    if not questions:
        raise ExcelImportError("В таблице нет вопросов. Добавьте строки под заголовками.")
    return sorted(questions, key=lambda question: question["question_number"])


def validate_excel_download_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or (getattr(address, "ipv4_mapped", None) and not address.ipv4_mapped.is_global)):
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise ExcelImportError("Не удалось получить ссылку на файл. Прикрепите Excel заново как файл.") from exc
    return url


class PublicFileResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self._resolver.resolve(host, port, family)
        for record in records:
            address = ipaddress.ip_address(record["host"])
            if not address.is_global or (getattr(address, "ipv4_mapped", None) and not address.ipv4_mapped.is_global):
                raise OSError("Недопустимый адрес сервера файла")
        return records

    async def close(self):
        await self._resolver.close()


async def download_excel_attachment(attachment) -> bytes:
    filename = getattr(attachment, "filename", None)
    if not isinstance(filename, str) or PurePath(filename).suffix.lower() != ".xlsx":
        raise ExcelImportError("Отправьте файл .xlsx. Старый формат .xls нужно пересохранить в Excel как .xlsx.")
    size = getattr(attachment, "size", None)
    if isinstance(size, int) and size > MAX_EXCEL_FILE_BYTES:
        raise ExcelImportError("Размер файла не должен превышать 5 МБ.")
    url = validate_excel_download_url(getattr(getattr(attachment, "payload", None), "url", None))
    resolver = PublicFileResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30),
                                         trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                                         headers={"Accept-Encoding": "identity"}) as session:
            for redirect in range(4):
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location or redirect == 3:
                            raise ExcelImportError("Не удалось скачать вложение. Прикрепите Excel заново.")
                        url = validate_excel_download_url(urljoin(url, location))
                        continue
                    response.raise_for_status()
                    if response.content_length and response.content_length > MAX_EXCEL_FILE_BYTES:
                        raise ExcelImportError("Размер файла не должен превышать 5 МБ.")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > MAX_EXCEL_FILE_BYTES:
                            raise ExcelImportError("Размер файла не должен превышать 5 МБ.")
                    return bytes(data)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise ExcelImportError("Не удалось скачать вложение. Попробуйте отправить файл ещё раз.") from exc
    finally:
        await resolver.close()
    raise ExcelImportError("Не удалось скачать вложение.")


def parse_question_options(raw: str) -> list[tuple[str, str]]:
    """Разбираем метки вариантов, не разрезая числа с десятичной запятой."""
    letters = ("А", "Б", "В", "Г")
    raw = raw.strip()
    values = None
    if raw.startswith(("[", "{")):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректный JSON вариантов ответа") from exc
        if isinstance(decoded, list) and len(decoded) == 4:
            values = decoded
        elif isinstance(decoded, dict) and set(decoded) == set(letters):
            values = [decoded[letter] for letter in letters]
    else:
        # Метки начинаются с новой строки или после разделителя списка.
        # Единица измерения «(А)» внутри варианта не является новой меткой.
        matches = list(re.finditer(r"(?:^|(?<=[,;\n]))[ \t]*([АБВГ])\s*[—–\-).:]\s*", raw, re.IGNORECASE))
        if len(matches) == 4 and tuple(m[1].upper() for m in matches) == letters:
            values = [
                raw[match.end():matches[i + 1].start() if i + 1 < 4 else len(raw)]
                .strip().rstrip(",;").strip()
                for i, match in enumerate(matches)
            ]
            # Варианты перемешиваются, поэтому единицы из последнего варианта нужны всем.
            values[3] = values[3].rstrip(".")
            unit = re.fullmatch(r"(.*?)\s*\((.+)\)", values[3])
            if unit and all(re.fullmatch(r"[0-9]+(?:,[0-9]+)?", value) for value in (*values[:3], unit[1])):
                values = [f"{value} {unit[2]}" for value in (*values[:3], unit[1])]
        elif not matches:
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(lines) == 4:
                values = lines
    if values is None or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Нужны четыре непустых варианта А, Б, В, Г")
    return list(zip(letters, (value.strip() for value in values)))
