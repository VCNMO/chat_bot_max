#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Минимальный чат-бот для мессенджера MAX: портфолио педагога.

Сценарий:
    /start -> ФИО -> меню (грамота / курс ПК) -> фото документа ->
    вопросы -> сводка за последний год и снова меню.

Запуск:
    pip install -r requirements.txt
    MAX_BOT_TOKEN=... python bot.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "documents.sqlite3"
MARKER_PATH = DATA_DIR / "marker.json"
CA_CERT_PATH = DATA_DIR / "russian_trusted_root_ca.pem"
CA_BUNDLE_PATH = DATA_DIR / "ca_bundle.pem"
CA_CERT_URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"

POLL_TIMEOUT = 30
ONE_YEAR = timedelta(days=365)
MAX_PHOTO_BYTES = 25 * 1024 * 1024
SUMMARY_LIMIT = 6
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

LEVELS = (
    "Организации",
    "Муниципальный",
    "Региональный",
    "Всероссийский",
    "Международный",
)
RESULTS = (
    "Грамота",
    "Призер",
    "Победитель",
    "Стипендиат",
    "Диплом 3 степени",
    "Диплом 2 степени",
    "Диплом 1 степени",
)

PHOTO_PROMPT = (
    "📷 Сфотографируйте документ.\n\n"
    "Нажмите скрепку 📎 рядом с полем ввода → «Камера», сделайте снимок "
    "и отправьте его сюда. Можно выбрать и готовое фото из галереи.\n\n"
    "Чтобы прервать, отправьте /cancel."
)

log = logging.getLogger("max-docs-bot")


def load_dotenv(path: Path) -> None:
    """Крошечная замена python-dotenv: читает KEY=VALUE из .env."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")

MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").strip().rstrip("/")
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# Схема анкеты
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    """Один вопрос по документу."""

    key: str
    label: str
    kind: str = "text"  # text | enum | year | hours
    hint: str = ""
    options: Tuple[str, ...] = ()


FIELDS: Dict[str, Tuple[Question, ...]] = {
    "competition": (
        Question("title", "Название конкурса", hint="«Педагог года — 2025»"),
        Question("organizer", "Организатор", hint="Департамент образования г. Москвы"),
        Question("level", "Уровень конкурса", kind="enum", options=LEVELS),
        Question("dates", "Даты проведения", hint="12.05.2025 – 15.05.2025"),
        Question("year", "Год проведения", kind="year", hint="2025"),
        Question("result", "Результат", kind="enum", options=RESULTS),
    ),
    "course": (
        Question("title", "Название курса", hint="«Цифровые инструменты в работе педагога»"),
        Question("organizer", "Организатор", hint="Академия Минпросвещения России"),
        Question("dates", "Даты проведения", hint="03.02.2025 – 14.02.2025"),
        Question("year", "Год проведения", kind="year", hint="2025"),
        Question("hours", "Количество часов", kind="hours", hint="72"),
    ),
}

KIND_LABELS = {
    "competition": "грамоту за конкурс",
    "course": "курс повышения квалификации",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower().replace("ё", "е"))


def inline_keyboard(rows: List[List[Dict[str, str]]]) -> Dict[str, Any]:
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def callback_button(text: str, payload: str) -> Dict[str, str]:
    return {"type": "callback", "text": text, "payload": payload}


def menu_keyboard() -> List[List[Dict[str, str]]]:
    return [
        [callback_button("🏅 Добавить грамоту", "add:competition")],
        [callback_button("🎓 Добавить курс ПК", "add:course")],
    ]


def options_keyboard(question: Question) -> List[List[Dict[str, str]]]:
    rows: List[List[Dict[str, str]]] = []
    row: List[Dict[str, str]] = []
    for option in question.options:
        row.append(callback_button(option, f"{question.key}:{option}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    fio          TEXT,
    state        TEXT    NOT NULL DEFAULT 'new',
    pending_kind TEXT,
    draft_id     INTEGER,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,
    photo       BLOB    NOT NULL,
    photo_mime  TEXT,
    title       TEXT,
    organizer   TEXT,
    level       TEXT,
    dates       TEXT,
    year        INTEGER,
    result      TEXT,
    hours       INTEGER,
    completed   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_user_created
    ON documents (user_id, completed, created_at);
"""

DOC_COLUMNS = (
    "id, user_id, kind, title, organizer, level, dates, year, result, hours, "
    "completed, created_at"
)


class Storage:
    """Вся работа с SQLite: пользователи, состояния диалога и документы."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- пользователи ---

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

    def ensure_user(self, user_id: int) -> sqlite3.Row:
        user = self.get_user(user_id)
        if user is None:
            self.conn.execute(
                "INSERT INTO users (user_id, state, created_at) VALUES (?, 'new', ?)",
                (user_id, utc_now()),
            )
            self.conn.commit()
            user = self.get_user(user_id)
        assert user is not None
        return user

    def update_user(self, user_id: int, **fields: Any) -> None:
        allowed = {"fio", "state", "pending_kind", "draft_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Неизвестные поля пользователя: {unknown}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE users SET {assignments} WHERE user_id = ?",
            (*fields.values(), user_id),
        )
        self.conn.commit()

    # --- документы ---

    def create_document(self, user_id: int, kind: str, photo: bytes, mime: str) -> int:
        if kind not in FIELDS:
            raise ValueError(f"Неизвестный тип документа: {kind}")
        cur = self.conn.execute(
            """INSERT INTO documents (user_id, kind, photo, photo_mime, completed, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (user_id, kind, sqlite3.Binary(photo), mime, utc_now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_document(self, doc_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT {DOC_COLUMNS} FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()

    def set_document_field(self, doc_id: int, key: str, value: Any) -> None:
        allowed = {question.key for questions in FIELDS.values() for question in questions}
        if key not in allowed:
            raise ValueError(f"Неизвестное поле документа: {key}")
        self.conn.execute(
            f"UPDATE documents SET {key} = ? WHERE id = ?", (value, doc_id)
        )
        self.conn.commit()

    def complete_document(self, doc_id: int) -> None:
        self.conn.execute(
            "UPDATE documents SET completed = 1 WHERE id = ?", (doc_id,)
        )
        self.conn.commit()

    def delete_document(self, doc_id: int) -> None:
        self.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self.conn.commit()

    def recent_documents(self, user_id: int) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - ONE_YEAR).strftime(TS_FORMAT)
        return self.conn.execute(
            f"""SELECT {DOC_COLUMNS} FROM documents
                WHERE user_id = ? AND completed = 1 AND created_at >= ?
                ORDER BY created_at DESC, id DESC""",
            (user_id, cutoff),
        ).fetchall()


# ---------------------------------------------------------------------------
# Клиент MAX Bot API
# ---------------------------------------------------------------------------


def _retry_after(response: requests.Response) -> float:
    try:
        return max(0.5, min(10.0, float(response.headers.get("Retry-After", "1"))))
    except (TypeError, ValueError):
        return 1.0


class MaxApi:
    """Тонкая обёртка над platform-api2.max.ru."""

    def __init__(self, token: str, verify: Any = True) -> None:
        self.token = token
        self.verify = verify
        self.session = requests.Session()
        self.session.headers.update({"Authorization": token})
        self._last_send: Dict[int, float] = {}

    def _throttle(self, key: int) -> None:
        # MAX разрешает не больше 2 сообщений в секунду в один диалог.
        now = time.monotonic()
        wait = 0.55 - (now - self._last_send.get(key, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_send[key] = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 20,
        retries: int = 2,
    ) -> Dict[str, Any]:
        url = f"{MAX_API_URL}{path}"
        last_response: Optional[requests.Response] = None
        for attempt in range(retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    timeout=timeout,
                    verify=self.verify,
                )
            except requests.RequestException:
                # POST не повторяем, чтобы не отправить сообщение дважды.
                if method == "GET" and attempt + 1 < retries:
                    time.sleep(1)
                    continue
                raise
            last_response = response
            if response.status_code == 429:
                delay = _retry_after(response)
                log.warning("Лимит запросов MAX, пауза %.1f с", delay)
                time.sleep(delay)
                continue
            if response.status_code >= 500 and method == "GET" and attempt + 1 < retries:
                time.sleep(2)
                continue
            if not response.ok:
                log.error(
                    "MAX API %s %s -> %s: %s",
                    method,
                    path,
                    response.status_code,
                    response.text[:500],
                )
                response.raise_for_status()
            return response.json() if response.content else {}
        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError(f"Не удалось выполнить {method} {path}")

    def me(self) -> Dict[str, Any]:
        return self._request("GET", "/me")

    def updates(self, marker: Optional[int]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"timeout": POLL_TIMEOUT, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        return self._request(
            "GET", "/updates", params=params, timeout=POLL_TIMEOUT + 15
        )

    def send(
        self,
        user_id: int,
        text: str,
        keyboard: Optional[List[List[Dict[str, str]]]] = None,
    ) -> Dict[str, Any]:
        self._throttle(user_id)
        body: Dict[str, Any] = {"text": text[:4000]}
        if keyboard is not None:
            body["attachments"] = [inline_keyboard(keyboard)]
        return self._request(
            "POST", "/messages", params={"user_id": user_id}, payload=body
        )

    def answer(
        self,
        callback_id: str,
        *,
        text: Optional[str] = None,
        notification: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if text is not None:
            # Пустой attachments очищает клавиатуру у отредактированного сообщения.
            body["message"] = {"text": text[:4000], "attachments": []}
        if notification:
            body["notification"] = notification[:128]
        if not body:
            body["notification"] = "Готово"
        return self._request(
            "POST", "/answers", params={"callback_id": callback_id}, payload=body
        )

    def download(self, url: str) -> requests.Response:
        response = requests.get(url, timeout=60, verify=self.verify)
        if response.status_code in (401, 403):
            response = requests.get(
                url,
                timeout=60,
                verify=self.verify,
                headers={"Authorization": self.token},
            )
        response.raise_for_status()
        return response


# ---------------------------------------------------------------------------
# Логика бота
# ---------------------------------------------------------------------------


class Bot:
    def __init__(self, api: MaxApi, db: Storage) -> None:
        self.api = api
        self.db = db

    # --- маршрутизация событий ---

    def handle_update(self, update: Dict[str, Any]) -> None:
        kind = update.get("update_type")
        if kind == "bot_started":
            user_id = (update.get("user") or {}).get("user_id")
            if user_id:
                self.on_start(int(user_id))
        elif kind == "message_created":
            message = update.get("message") or {}
            if self.chat_type(message) not in (None, "dialog"):
                return
            self.on_message(message)
        elif kind == "message_callback":
            message = update.get("message") or {}
            if message and self.chat_type(message) not in (None, "dialog"):
                return
            self.on_callback(update)

    @staticmethod
    def chat_type(message: Dict[str, Any]) -> Optional[str]:
        return (message.get("recipient") or {}).get("chat_type")

    # --- входящие сообщения ---

    def on_message(self, message: Dict[str, Any]) -> None:
        sender = message.get("sender") or {}
        if sender.get("is_bot"):
            return
        user_id = sender.get("user_id") or (message.get("recipient") or {}).get("user_id")
        if not user_id:
            return
        user_id = int(user_id)

        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        image = next(
            (item for item in (body.get("attachments") or []) if item.get("type") == "image"),
            None,
        )

        command = text.split()[0].lower() if text else ""
        command = command.split("@", 1)[0]

        if command == "/start":
            self.on_start(user_id)
            return
        if command == "/cancel":
            self.on_cancel(user_id)
            return
        if command == "/my":
            self.send_summary(user_id)
            return

        user = self.db.ensure_user(user_id)
        state = user["state"]

        if image is not None:
            if state == "await_photo":
                self.on_photo(user_id, image)
            elif state == "fill":
                self.api.send(user_id, "Фото уже сохранено. Ответьте, пожалуйста, на вопрос выше.")
            else:
                self.api.send(
                    user_id,
                    "Чтобы добавить документ, нажмите кнопку ниже.",
                    menu_keyboard(),
                )
            return

        if not text:
            return

        if state == "new":
            self.on_fio(user_id, text)
        elif state == "await_photo":
            self.api.send(user_id, PHOTO_PROMPT)
        elif state == "fill":
            self.on_answer(user_id, text)
        elif state == "menu":
            self.api.send(
                user_id,
                "Выберите действие на клавиатуре ниже или отправьте /my.",
                menu_keyboard(),
            )
        else:
            self.on_start(user_id)

    # --- команды ---

    def on_start(self, user_id: int) -> None:
        user = self.db.ensure_user(user_id)
        self.drop_draft(user)
        if user["fio"]:
            self.db.update_user(user_id, state="menu", pending_kind=None, draft_id=None)
            self.send_summary(user_id, header=f"Здравствуйте, {user['fio']}!")
        else:
            self.db.update_user(user_id, state="new", pending_kind=None, draft_id=None)
            self.api.send(
                user_id,
                "Здравствуйте! Я помогу собрать портфолио: грамоты за конкурсы "
                "и курсы повышения квалификации.\n\n"
                "Как вас зовут? Введите ФИО полностью, например:\n"
                "Иванова Мария Петровна",
            )

    def on_cancel(self, user_id: int) -> None:
        user = self.db.ensure_user(user_id)
        self.drop_draft(user)
        if user["fio"]:
            self.db.update_user(user_id, state="menu", pending_kind=None, draft_id=None)
            self.api.send(user_id, "Действие отменено. Что добавляем?", menu_keyboard())
        else:
            self.db.update_user(user_id, state="new", pending_kind=None, draft_id=None)
            self.api.send(user_id, "Действие отменено. Введите ФИО, пожалуйста:")

    def drop_draft(self, user: sqlite3.Row) -> None:
        draft_id = user["draft_id"]
        if not draft_id:
            return
        document = self.db.get_document(int(draft_id))
        if document is not None and not document["completed"]:
            self.db.delete_document(int(draft_id))

    # --- ФИО ---

    def on_fio(self, user_id: int, text: str) -> None:
        fio = re.sub(r"\s+", " ", text).strip()
        if not (5 <= len(fio) <= 200) or len(fio.split()) < 2:
            self.api.send(
                user_id,
                "Похоже, это не ФИО. Введите фамилию, имя и отчество через пробел, "
                "например: Иванова Мария Петровна",
            )
            return
        self.db.update_user(user_id, fio=fio, state="menu")
        self.send_summary(user_id, header=f"Спасибо, {fio}!")

    # --- кнопки ---

    def answer(
        self,
        callback_id: str,
        *,
        text: Optional[str] = None,
        notification: Optional[str] = None,
    ) -> None:
        """Ответ на callback не должен ломать сценарий, если сообщение уже удалено."""
        try:
            self.api.answer(callback_id, text=text, notification=notification)
        except requests.RequestException:
            log.warning("Не удалось ответить на callback %s", callback_id, exc_info=True)

    def on_callback(self, update: Dict[str, Any]) -> None:
        callback = update.get("callback") or {}
        user_id = (callback.get("user") or {}).get("user_id")
        callback_id = callback.get("callback_id")
        payload = callback.get("payload") or ""
        if not user_id or not callback_id:
            return
        user_id = int(user_id)
        user = self.db.ensure_user(user_id)

        if payload.startswith("add:"):
            kind = payload[4:]
            if kind not in FIELDS:
                self.answer(callback_id, notification="Неизвестная команда")
                return
            if user["state"] in ("fill", "await_photo"):
                self.answer(
                    callback_id,
                    notification="Сначала завершите текущий документ или отправьте /cancel",
                )
                return
            if not user["fio"]:
                self.answer(
                    callback_id,
                    text="Сначала введите ФИО. Отправьте его одним сообщением.",
                    notification="Сначала введите ФИО",
                )
                self.db.update_user(user_id, state="new", pending_kind=None, draft_id=None)
                return
            self.drop_draft(user)
            self.db.update_user(
                user_id, state="await_photo", pending_kind=kind, draft_id=None
            )
            self.answer(
                callback_id,
                text=f"➕ Добавляем {KIND_LABELS[kind]}",
                notification="Теперь пришлите фото документа",
            )
            self.api.send(user_id, PHOTO_PROMPT)
            return

        if payload.startswith(("level:", "result:")):
            key, _, value = payload.partition(":")
            self.on_enum_answer(user_id, callback_id, key, value)
            return

        self.answer(callback_id, notification="Кнопка устарела. Отправьте /start")

    def on_enum_answer(self, user_id: int, callback_id: str, key: str, value: str) -> None:
        user = self.db.ensure_user(user_id)
        if user["state"] != "fill" or not user["draft_id"]:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        document = self.db.get_document(int(user["draft_id"]))
        if document is None:
            self.answer(callback_id, notification="Черновик не найден")
            self.on_start(user_id)
            return
        question = self.current_question(document)
        if question is None or question.key != key:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        chosen = self.match_option(question, value)
        if chosen is None:
            self.answer(callback_id, notification="Неизвестный вариант")
            self.ask(user_id, document, question)
            return
        self.db.set_document_field(int(document["id"]), key, chosen)
        self.answer(callback_id, text=f"{question.label}: {chosen}")
        self.advance(user_id, int(document["id"]))

    # --- фото и анкета ---

    def on_photo(self, user_id: int, image: Dict[str, Any]) -> None:
        user = self.db.ensure_user(user_id)
        kind = user["pending_kind"]
        if kind not in FIELDS:
            self.db.update_user(user_id, state="menu", pending_kind=None)
            self.api.send(user_id, "Сначала выберите тип документа:", menu_keyboard())
            return
        try:
            photo, mime = self.fetch_photo(image)
        except Exception as exc:  # сеть/битый ответ CDN
            log.warning("Не удалось скачать фото: %s", exc)
            self.api.send(user_id, "Не удалось получить фото 😔 Попробуйте отправить его ещё раз.")
            return
        if not photo:
            self.api.send(user_id, "Фото пришло пустым. Попробуйте отправить его ещё раз.")
            return
        if len(photo) > MAX_PHOTO_BYTES:
            self.api.send(
                user_id,
                "Фото больше 25 МБ. Сфотографируйте документ ещё раз — камера "
                "мессенджера обычно сжимает снимок автоматически.",
            )
            return
        doc_id = self.db.create_document(user_id, kind, photo, mime)
        self.db.update_user(user_id, state="fill", draft_id=doc_id, pending_kind=None)
        self.api.send(
            user_id,
            "Фото документа сохранено в базе ✅\n"
            "Теперь ответьте на несколько вопросов. Отменить — /cancel.",
        )
        self.advance(user_id, doc_id)

    def fetch_photo(self, image: Dict[str, Any]) -> Tuple[bytes, str]:
        payload = image.get("payload") or {}
        url = payload.get("url")
        if not url:
            raise ValueError("во вложении нет url")
        response = self.api.download(url)
        mime = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        return response.content, mime

    def current_question(self, document: sqlite3.Row) -> Optional[Question]:
        for question in FIELDS[document["kind"]]:
            value = document[question.key]
            if value is None or value == "":
                return question
        return None

    def ask(
        self,
        user_id: int,
        document: sqlite3.Row,
        question: Optional[Question] = None,
    ) -> None:
        if question is None:
            question = self.current_question(document)
        if question is None:
            return
        questions = FIELDS[document["kind"]]
        index = next(
            position
            for position, item in enumerate(questions, 1)
            if item.key == question.key
        )
        prefix = f"{index}/{len(questions)}. {question.label}"
        if question.kind == "enum":
            self.api.send(
                user_id,
                f"{prefix} — выберите вариант:",
                options_keyboard(question),
            )
        else:
            hint = f"\nНапример: {question.hint}" if question.hint else ""
            self.api.send(
                user_id,
                f"{prefix} — напишите ответ одним сообщением.{hint}",
            )

    def advance(self, user_id: int, doc_id: int) -> None:
        document = self.db.get_document(doc_id)
        if document is None:
            return
        question = self.current_question(document)
        if question is None:
            self.finish(user_id, document)
        else:
            self.ask(user_id, document, question)

    def on_answer(self, user_id: int, text: str) -> None:
        user = self.db.ensure_user(user_id)
        document = (
            self.db.get_document(int(user["draft_id"])) if user["draft_id"] else None
        )
        if document is None:
            self.db.update_user(user_id, state="menu", draft_id=None, pending_kind=None)
            self.api.send(
                user_id,
                "Черновик не найден. Начнём заново — выберите документ:",
                menu_keyboard(),
            )
            return
        question = self.current_question(document)
        if question is None:
            self.finish(user_id, document)
            return

        raw = re.sub(r"\s+", " ", text).strip()

        if question.kind == "enum":
            chosen = self.match_option(question, raw)
            if chosen is None:
                self.api.send(
                    user_id,
                    "Не понял вариант. Пожалуйста, выберите кнопкой:",
                    options_keyboard(question),
                )
                return
            value: Any = chosen
        elif question.kind == "year":
            if not re.fullmatch(r"\d{4}", raw) or not (1900 <= int(raw) <= 2100):
                self.api.send(
                    user_id,
                    "Год должен быть числом из 4 цифр, например 2025. Попробуйте ещё раз:",
                )
                return
            value = int(raw)
        elif question.kind == "hours":
            if not re.fullmatch(r"\d{1,5}", raw) or not (1 <= int(raw) <= 10000):
                self.api.send(
                    user_id,
                    "Количество часов — целое число от 1 до 10000. Попробуйте ещё раз:",
                )
                return
            value = int(raw)
        else:
            if not (1 <= len(raw) <= 500):
                self.api.send(
                    user_id, "Ответ должен быть текстом до 500 символов. Попробуйте ещё раз:"
                )
                return
            value = raw

        self.db.set_document_field(int(document["id"]), question.key, value)
        self.advance(user_id, int(document["id"]))

    @staticmethod
    def match_option(question: Question, value: str) -> Optional[str]:
        needle = normalize(value)
        for option in question.options:
            if normalize(option) == needle:
                return option
        return None

    def finish(self, user_id: int, document: sqlite3.Row) -> None:
        self.db.complete_document(int(document["id"]))
        self.db.update_user(user_id, state="menu", draft_id=None, pending_kind=None)
        title = document["title"] or "Документ"
        self.send_summary(user_id, header=f"✅ Готово! «{title}» сохранён.")

    # --- сводка ---

    def send_summary(self, user_id: int, header: Optional[str] = None) -> None:
        documents = self.db.recent_documents(user_id)
        competitions = [doc for doc in documents if doc["kind"] == "competition"]
        courses = [doc for doc in documents if doc["kind"] == "course"]
        hours = sum(int(doc["hours"] or 0) for doc in courses)

        lines: List[str] = []
        if header:
            lines.extend([header, ""])
        lines.append("📊 За последние 12 месяцев")
        lines.append(f"• Конкурсы и грамоты: {len(competitions)}")
        lines.append(
            f"• Курсы повышения квалификации: {len(courses)}"
            + (f" ({hours} ч)" if hours else "")
        )
        lines.append("")

        if not documents:
            lines.append("Пока ничего не загружено.")
        else:
            for number, document in enumerate(documents[:SUMMARY_LIMIT], 1):
                if document["kind"] == "competition":
                    lines.append(f"{number}. 🏅 {document['title']}")
                    lines.append(
                        f"   {document['organizer']} · {document['level']}"
                    )
                    lines.append(
                        f"   {document['dates']} · {document['year']} г."
                    )
                    lines.append(f"   Результат: {document['result']}")
                else:
                    lines.append(f"{number}. 🎓 {document['title']}")
                    lines.append(f"   {document['organizer']}")
                    lines.append(
                        f"   {document['dates']} · {document['year']} г. · "
                        f"{document['hours']} ч"
                    )
                lines.append("")
            if len(documents) > SUMMARY_LIMIT:
                lines.append(
                    f"…и ещё {len(documents) - SUMMARY_LIMIT} док. — полный список в базе."
                )
                lines.append("")

        lines.append("Добавить новый документ:")
        text = "\n".join(lines)
        self.api.send(user_id, text[:4000], menu_keyboard())


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def build_ca_bundle() -> Any:
    """Готовит CA-bundle: системные сертификаты + корень Минцифры.

    platform-api2.max.ru подписан Russian Trusted Root CA, которого нет
    в certifi/Mozilla. Сертификат скачивается один раз и кладётся в data/.
    """
    override = os.getenv("MAX_CA_BUNDLE_PATH")
    if override:
        if Path(override).exists():
            return override
        log.warning("Файл MAX_CA_BUNDLE_PATH=%s не найден, собираю bundle заново", override)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CA_CERT_PATH.exists():
        try:
            response = requests.get(CA_CERT_URL, timeout=20)
            response.raise_for_status()
            if b"BEGIN CERTIFICATE" not in response.content:
                raise ValueError("ответ не похож на PEM-сертификат")
            CA_CERT_PATH.write_bytes(response.content)
            log.info("Корневой сертификат Минцифры скачан: %s", CA_CERT_PATH)
        except Exception as exc:
            log.warning(
                "Не удалось скачать корневой сертификат Минцифры (%s).\n"
                "Если MAX API отвечает ошибкой SSL, скачайте %s вручную "
                "и положите в %s, затем перезапустите бота.",
                exc,
                CA_CERT_URL,
                CA_CERT_PATH,
            )
            return True

    try:
        import certifi

        base = Path(certifi.where()).read_text(encoding="utf-8").rstrip()
        root = CA_CERT_PATH.read_text(encoding="utf-8")
        CA_BUNDLE_PATH.write_text(f"{base}\n\n{root}", encoding="utf-8")
        return str(CA_BUNDLE_PATH)
    except Exception as exc:
        log.warning(
            "Не удалось собрать общий CA-bundle (%s), использую стандартные "
            "сертификаты. При ошибке SSL добавьте корень Минцифры в системное "
            "хранилище.",
            exc,
        )
        return True


def load_marker() -> Optional[int]:
    try:
        data = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
        marker = data.get("marker")
        return int(marker) if marker is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def save_marker(marker: Optional[int]) -> None:
    try:
        if marker is None:
            MARKER_PATH.unlink(missing_ok=True)
        else:
            MARKER_PATH.write_text(json.dumps({"marker": marker}), encoding="utf-8")
    except OSError as exc:
        log.warning("Не удалось сохранить marker: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not BOT_TOKEN:
        log.error(
            "Не задан MAX_BOT_TOKEN. Скопируйте .env.example в .env и вставьте "
            "токен бота (business.max.ru → Чат-боты → ⋮ → Настройки)."
        )
        return 1

    api = MaxApi(BOT_TOKEN, build_ca_bundle())
    db = Storage(DB_PATH)

    try:
        info = api.me()
    except requests.RequestException as exc:
        log.error("Не удалось подключиться к MAX API: %s", exc)
        log.error(
            "Проверьте токен и доступ к %s. Если ошибка SSL — см. раздел "
            "«Сертификат Минцифры» в README.",
            MAX_API_URL,
        )
        return 1

    log.info(
        "Бот запущен: %s (@%s), user_id=%s",
        info.get("name"),
        info.get("username"),
        info.get("user_id"),
    )
    log.info("База данных: %s", DB_PATH)
    log.info("Long polling запущен, Ctrl+C — остановить")

    bot = Bot(api, db)
    marker = load_marker()

    try:
        while True:
            try:
                result = api.updates(marker)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    log.error(
                        "MAX API отклонил токен (%s). Проверьте MAX_BOT_TOKEN.", status
                    )
                    return 1
                if status == 400:
                    log.warning("Сервер отклонил marker, начинаю с последнего события")
                    marker = None
                    save_marker(None)
                else:
                    log.warning("Ошибка long polling: %s", exc)
                    time.sleep(3)
                continue
            except requests.RequestException as exc:
                log.warning("Сеть недоступна: %s", exc)
                time.sleep(3)
                continue
            except Exception as exc:  # чтобы один сбой не останавливал бота
                log.exception("Неожиданный сбой long polling: %s", exc)
                time.sleep(3)
                continue

            for update in result.get("updates") or []:
                try:
                    bot.handle_update(update)
                except Exception:
                    log.exception("Ошибка обработки %s", update.get("update_type"))

            new_marker = result.get("marker")
            if new_marker is not None and new_marker != marker:
                marker = new_marker
                save_marker(marker)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    return 0


if __name__ == "__main__":
    sys.exit(main())
