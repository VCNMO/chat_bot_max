#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Версия 2: мини-приложение MAX «Портфолио педагога».

Backend на Flask:
  * раздаёт статику из webapp/ (HTML, CSS, JS);
  * принимает фото и анкету документа, хранит всё в SQLite (та же база, что у bot.py);
  * проверяет window.WebApp.initData по HMAC-SHA256 на каждом запросе к API;
  * по флагу --bot запускает long polling и присылает кнопку open_app.

Запуск:
    pip install -r requirements.txt
    python mini_app.py --bot

Для MAX нужен HTTPS. Локально удобно открыть http://127.0.0.1:8080 в браузере
с MINI_APP_DEV_MODE=1 (камера на localhost тоже работает), а для телефона —
туннель (cloudflared/ngrok) или reverse proxy с TLS.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
from flask import Flask, abort, g, jsonify, request, send_file, send_from_directory

# Переиспользуем схему БД, вопросы и клиент MAX API из первой версии.
import bot

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

# bot.py уже читает .env при импорте; вызываем явно, чтобы настройки v2
# не зависели от побочного эффекта импорта.
bot.load_dotenv(BASE_DIR / ".env")

MB = 1024 * 1024
DEFAULT_PORT = 8080
DEFAULT_AUTH_TTL = 3600
DEFAULT_PHOTO_TOKEN_TTL = 2 * 3600
DEFAULT_MAX_PHOTO_MB = 25

log = logging.getLogger("max-mini-app")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: Path
    dev_mode: bool
    auth_ttl: int
    photo_token_ttl: int
    max_photo_bytes: int
    photo_secret: bytes
    host: str
    port: int
    debug: bool
    bot_public_name: str


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Переменная %s=%r не число, использую %s", name, raw, default)
        return default
    return max(minimum, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _resolve_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path)


def photo_secret_for(bot_token: str) -> bytes:
    """Ключ для краткоживущих ссылок на фото.

    Если задан MINI_APP_SECRET — используем его. Иначе выводим из токена бота:
    при смене токена старые ссылки на фото просто перестают работать.
    Без токена (например, ошибка настройки) берём случайный ключ процесса:
    подделать ссылку на чужое фото по константе из исходников нельзя.
    """
    override = os.getenv("MINI_APP_SECRET", "").strip()
    if override:
        if len(override) < 16:
            log.warning(
                "MINI_APP_SECRET короче 16 символов — используйте длинный случайный ключ."
            )
        return override.encode("utf-8")
    if bot_token:
        return hmac.new(
            bot_token.encode("utf-8"), b"max-mini-app-photo-v1", hashlib.sha256
        ).digest()
    return secrets.token_bytes(32)


def load_settings() -> Settings:
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    db_raw = os.getenv("MINI_APP_DB_PATH", "").strip()
    db_path = _resolve_path(db_raw) if db_raw else bot.DB_PATH
    max_photo_mb = _env_int("MINI_APP_MAX_PHOTO_MB", DEFAULT_MAX_PHOTO_MB, minimum=1)
    return Settings(
        bot_token=token,
        db_path=db_path,
        dev_mode=_env_bool("MINI_APP_DEV_MODE", False),
        auth_ttl=_env_int("MINI_APP_AUTH_TTL", DEFAULT_AUTH_TTL, minimum=60),
        photo_token_ttl=_env_int(
            "MINI_APP_PHOTO_TOKEN_TTL", DEFAULT_PHOTO_TOKEN_TTL, minimum=60
        ),
        max_photo_bytes=max_photo_mb * MB,
        photo_secret=photo_secret_for(token),
        host=os.getenv("MINI_APP_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_env_int("MINI_APP_PORT", DEFAULT_PORT, minimum=1),
        debug=_env_bool("MINI_APP_DEBUG", False),
        bot_public_name=os.getenv("MAX_BOT_PUBLIC_NAME", "").strip().lstrip("@"),
    )


# ---------------------------------------------------------------------------
# Авторизация мини-приложения (MAX Bridge initData)
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """initData отсутствует, просрочен или подпись не совпала."""


def validate_init_data(
    raw: str,
    bot_token: str,
    max_age: int,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Проверяет window.WebApp.initData.

    secretKey = HMAC_SHA256("WebAppData", bot_token)
    hash      = HMAC_SHA256(secretKey, "key=value" через "\\n", hash исключён)
    """
    if not raw:
        raise AuthError("пустой initData")
    if len(raw) > 16 * 1024:
        raise AuthError("слишком длинный initData")
    if not bot_token:
        raise AuthError("на сервере не задан MAX_BOT_TOKEN")

    try:
        values = urllib.parse.parse_qs(raw, keep_blank_values=True)
    except ValueError as exc:
        raise AuthError("не удалось разобрать initData") from exc

    hashes = values.get("hash") or []
    if len(hashes) != 1 or not hashes[0]:
        raise AuthError("в initData нет одного поля hash")
    received_hash = hashes[0].strip().lower()

    auth_dates = values.get("auth_date") or []
    if len(auth_dates) != 1:
        raise AuthError("в initData нет одного поля auth_date")
    try:
        auth_date = int(auth_dates[0])
    except ValueError as exc:
        raise AuthError("auth_date не число") from exc

    current = int(now if now is not None else time.time())
    if auth_date > current + 300:
        raise AuthError("auth_date из будущего")
    if current - auth_date > max_age:
        raise AuthError("initData просрочен")

    pairs: List[str] = []
    for key, items in values.items():
        if key == "hash":
            continue
        for value in items:
            pairs.append(f"{key}={value}")
    pairs.sort()
    data_check_string = "\n".join(pairs)

    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("подпись initData не совпала")

    user: Dict[str, Any] = {}
    raw_user = (values.get("user") or [""])[0]
    if raw_user:
        try:
            parsed = json.loads(raw_user)
        except json.JSONDecodeError as exc:
            raise AuthError("поле user — не JSON") from exc
        if isinstance(parsed, dict):
            user = parsed

    user_id = user.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise AuthError("в initData нет числового user.id")

    return {
        "user_id": user_id,
        "user": user,
        "auth_date": auth_date,
        "query_id": (values.get("query_id") or [""])[0],
        "dev": False,
    }


def make_photo_token(secret: bytes, user_id: int, ttl: int) -> str:
    expires = int(time.time()) + max(60, ttl)
    payload = f"{user_id}:{expires}"
    signature = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def photo_token_user(secret: bytes, token: str, user_id: int) -> bool:
    """Проверяет, что токен выдан именно этому пользователю."""
    if not token or token.count(".") != 1:
        return False
    raw_expires, signature = token.split(".", 1)
    try:
        expires = int(raw_expires)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    payload = f"{user_id}:{expires}"
    expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Валидация ФИО и ответов
# ---------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def validate_fio(raw: Any) -> str:
    fio = clean_text(raw)
    if not (5 <= len(fio) <= 200) or len(fio.split()) < 2:
        raise ValueError(
            "Введите фамилию, имя и отчество через пробел, например: "
            "Иванова Мария Петровна"
        )
    if not re.fullmatch(r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z'\- ]*", fio):
        raise ValueError("В ФИО допустимы только буквы, пробел, дефис и апостроф")
    return fio


def validate_answers(kind: str, raw: Any) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Проверяет ответы по тем же правилам, что и первая версия."""
    if kind not in bot.FIELDS:
        raise ValueError("Неизвестный тип документа")
    if not isinstance(raw, dict):
        raw = {}

    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for question in bot.FIELDS[kind]:
        value = raw.get(question.key)
        if isinstance(value, bool) or value is None:
            text = ""
        elif isinstance(value, (int, float)):
            text = str(int(value))
        else:
            text = clean_text(value)

        if question.kind == "enum":
            needle = bot.normalize(text)
            chosen = next(
                (option for option in question.options if bot.normalize(option) == needle),
                None,
            )
            if chosen is None:
                errors[question.key] = "Выберите вариант из списка"
            else:
                values[question.key] = chosen
        elif question.kind == "year":
            if not re.fullmatch(r"\d{4}", text):
                errors[question.key] = "Укажите год из 4 цифр, например 2025"
            elif not (1900 <= int(text) <= 2100):
                errors[question.key] = "Год должен быть между 1900 и 2100"
            else:
                values[question.key] = int(text)
        elif question.kind == "hours":
            if not re.fullmatch(r"\d{1,5}", text):
                errors[question.key] = "Укажите целое число часов"
            elif not (1 <= int(text) <= 10000):
                errors[question.key] = "Количество часов — от 1 до 10 000"
            else:
                values[question.key] = int(text)
        else:
            if not text:
                errors[question.key] = "Заполните поле"
            elif len(text) > 500:
                errors[question.key] = "Не больше 500 символов"
            else:
                values[question.key] = text

    return values, errors


def detect_image_mime(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------------------------------------------------------------------------
# SQLite: отдельное соединение на запрос (Flask работает в нескольких потоках)
# ---------------------------------------------------------------------------


class MiniStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(bot.SCHEMA)
            conn.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_user(self, user_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO users (user_id, state, created_at)
                   VALUES (?, 'menu', ?)""",
                (user_id, bot.utc_now()),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:  # практически невозможно
            raise RuntimeError("Не удалось создать пользователя")
        return dict(row)

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_fio(self, user_id: int, fio: str) -> None:
        self.ensure_user(user_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET fio = ?, state = 'menu' WHERE user_id = ?",
                (fio, user_id),
            )

    def add_document(
        self,
        user_id: int,
        kind: str,
        photo: bytes,
        photo_mime: str,
        values: Dict[str, Any],
    ) -> int:
        self.ensure_user(user_id)
        columns = ["user_id", "kind", "photo", "photo_mime", "completed", "created_at"]
        params: List[Any] = [
            user_id,
            kind,
            sqlite3.Binary(photo),
            photo_mime,
            1,
            bot.utc_now(),
        ]
        for question in bot.FIELDS[kind]:
            columns.append(question.key)
            params.append(values.get(question.key))
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            cur = conn.execute(
                f"INSERT INTO documents ({', '.join(columns)}) VALUES ({placeholders})",
                params,
            )
            return int(cur.lastrowid)

    def recent_documents(self, user_id: int) -> List[Dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - bot.ONE_YEAR).strftime(bot.TS_FORMAT)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT {bot.DOC_COLUMNS} FROM documents
                    WHERE user_id = ? AND completed = 1 AND created_at >= ?
                    ORDER BY created_at DESC, id DESC""",
                (user_id, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_document(self, user_id: int, doc_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {bot.DOC_COLUMNS} FROM documents WHERE id = ? AND user_id = ?",
                (doc_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def document_owner(self, doc_id: int) -> Optional[int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM documents WHERE id = ? AND completed = 1",
                (doc_id,),
            ).fetchone()
        return int(row["user_id"]) if row else None

    def get_photo(
        self, user_id: int, doc_id: int
    ) -> Optional[Tuple[bytes, str]]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT photo, photo_mime FROM documents
                   WHERE id = ? AND user_id = ? AND completed = 1""",
                (doc_id, user_id),
            ).fetchone()
        if row is None or row["photo"] is None:
            return None
        return bytes(row["photo"]), row["photo_mime"] or "image/jpeg"

    def summary(self, user_id: int, secret: bytes, token_ttl: int) -> Dict[str, Any]:
        documents = self.recent_documents(user_id)
        token = make_photo_token(secret, user_id, token_ttl)
        serialized = [serialize_document(doc, token) for doc in documents]
        competitions = [doc for doc in serialized if doc["kind"] == "competition"]
        courses = [doc for doc in serialized if doc["kind"] == "course"]
        return {
            "period_days": int(bot.ONE_YEAR.total_seconds() // 86400),
            "total": len(serialized),
            "competitions": len(competitions),
            "courses": len(courses),
            "hours": sum(int(doc["hours"] or 0) for doc in courses),
            "documents": serialized,
        }


def serialize_document(document: Dict[str, Any], photo_token: str) -> Dict[str, Any]:
    result = dict(document)
    result["photo_url"] = (
        f"/api/documents/{int(result['id'])}/photo?t={urllib.parse.quote(photo_token)}"
    )
    return result


# ---------------------------------------------------------------------------
# Flask-приложение
# ---------------------------------------------------------------------------


def api_error(
    message: str,
    status: int = 400,
    *,
    code: Optional[str] = None,
    fields: Optional[Dict[str, str]] = None,
):
    payload: Dict[str, Any] = {"ok": False, "error": message}
    if code:
        payload["code"] = code
    if fields:
        payload["fields"] = fields
    return jsonify(payload), status


def create_app(settings: Optional[Settings] = None) -> Flask:
    settings = settings or load_settings()
    store = MiniStorage(settings.db_path)

    app = Flask(__name__, static_folder=str(WEBAPP_DIR), static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = settings.max_photo_bytes + 2 * MB
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.json.ensure_ascii = False
    app.extensions["mini_settings"] = settings
    app.extensions["mini_store"] = store

    if settings.dev_mode:
        log.warning(
            "MINI_APP_DEV_MODE=1: авторизация MAX отключена. "
            "Никогда не включайте этот режим на публичном сервере."
        )
    if not settings.bot_token:
        log.warning(
            "MAX_BOT_TOKEN не задан: запросы мини-приложения будут отклоняться "
            "(кроме dev-режима)."
        )

    # --- авторизация ---

    @app.before_request
    def authenticate():
        if not request.path.startswith("/api/"):
            return None
        if request.endpoint == "health":
            return None
        if request.endpoint == "document_photo":
            return None

        raw = request.headers.get("X-Max-Init-Data", "").strip()
        if not raw:
            if settings.dev_mode:
                dev_id = _env_int("MINI_APP_DEV_USER_ID", 1, minimum=1)
                g.auth = {
                    "user_id": dev_id,
                    "user": {
                        "id": dev_id,
                        "first_name": "Тестовый",
                        "last_name": "Пользователь",
                        "username": "dev",
                    },
                    "auth_date": int(time.time()),
                    "query_id": "dev",
                    "dev": True,
                }
                return None
            return api_error(
                "Приложение работает внутри MAX. Откройте его кнопкой в чате с ботом.",
                401,
                code="unauthorized",
            )

        try:
            g.auth = validate_init_data(
                raw, settings.bot_token, settings.auth_ttl
            )
        except AuthError as exc:
            log.info("initData отклонён: %s", exc)
            return api_error(
                "Не удалось подтвердить вход. Перезапустите приложение из MAX.",
                401,
                code="unauthorized",
            )
        return None

    def current_user_id() -> int:
        auth = getattr(g, "auth", None) or {}
        return int(auth.get("user_id") or 0)

    def current_summary() -> Dict[str, Any]:
        return store.summary(
            current_user_id(), settings.photo_secret, settings.photo_token_ttl
        )

    # --- страницы ---

    @app.get("/")
    def index():
        response = send_from_directory(WEBAPP_DIR, "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "version": "2.0"})

    @app.get("/api/bootstrap")
    def bootstrap():
        user_id = current_user_id()
        user = store.ensure_user(user_id)
        auth_user = g.auth.get("user") or {}
        return jsonify(
            {
                "ok": True,
                "version": "2.0",
                "dev_mode": bool(g.auth.get("dev")),
                "profile": {
                    "user_id": user_id,
                    "first_name": str(auth_user.get("first_name") or ""),
                    "last_name": str(auth_user.get("last_name") or ""),
                    "username": str(auth_user.get("username") or ""),
                },
                "user": {"fio": user.get("fio") or ""},
                "fields": {
                    kind: [asdict(question) for question in questions]
                    for kind, questions in bot.FIELDS.items()
                },
                "limits": {
                    "max_photo_bytes": settings.max_photo_bytes,
                    "max_photo_mb": settings.max_photo_bytes // MB,
                },
                "summary": current_summary(),
            }
        )

    @app.post("/api/profile")
    def save_profile():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return api_error("Ожидается JSON с полем fio")
        try:
            fio = validate_fio(payload.get("fio"))
        except ValueError as exc:
            return api_error(str(exc), fields={"fio": str(exc)})
        user_id = current_user_id()
        store.set_fio(user_id, fio)
        return jsonify({"ok": True, "user": {"fio": fio}})

    @app.post("/api/documents")
    def add_document():
        kind = (request.form.get("kind") or "").strip()
        if kind not in bot.FIELDS:
            return api_error("Выберите тип документа: грамота или курс ПК")

        raw_fields = request.form.get("fields") or "{}"
        try:
            fields = json.loads(raw_fields)
        except json.JSONDecodeError:
            return api_error("Поле fields должно содержать JSON")
        if not isinstance(fields, dict):
            return api_error("Поле fields должно быть объектом JSON")

        values, errors = validate_answers(kind, fields)
        if errors:
            return api_error("Проверьте заполненные поля", fields=errors)

        upload = request.files.get("photo")
        if upload is None or not upload.filename:
            return api_error("Прикрепите фото документа", fields={"photo": "Нет фото"})

        photo = upload.read(settings.max_photo_bytes + 1)
        if not photo:
            return api_error("Фото пустое. Сделайте снимок ещё раз.")
        if len(photo) > settings.max_photo_bytes:
            return api_error(
                f"Фото больше {settings.max_photo_bytes // MB} МБ. Сделайте снимок ещё раз.",
                413,
                fields={"photo": "Слишком большой файл"},
            )

        mime = detect_image_mime(photo)
        if mime is None:
            return api_error(
                "Это не JPEG, PNG или WebP. Сфотографируйте документ ещё раз.",
                fields={"photo": "Неподдерживаемый формат"},
            )

        user_id = current_user_id()
        doc_id = store.add_document(user_id, kind, photo, mime, values)
        document = store.get_document(user_id, doc_id)
        token = make_photo_token(
            settings.photo_secret, user_id, settings.photo_token_ttl
        )
        return (
            jsonify(
                {
                    "ok": True,
                    "document": serialize_document(document, token),
                    "summary": current_summary(),
                }
            ),
            201,
        )

    @app.get("/api/documents/<int:doc_id>/photo")
    def document_photo(doc_id: int):
        token = request.args.get("t", "")
        # Ссылка подписана для конкретного user_id, поэтому чужое фото не откроется.
        user_id = store.document_owner(doc_id)
        if user_id is None:
            abort(404)
        if not photo_token_user(settings.photo_secret, token, user_id):
            abort(404)
        photo = store.get_photo(user_id, doc_id)
        if photo is None:
            abort(404)
        data, mime = photo
        extension = {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
        response = send_file(
            io.BytesIO(data),
            mimetype=mime,
            as_attachment=False,
            download_name=f"document-{doc_id}.{extension}",
            max_age=0,
        )
        response.headers["Cache-Control"] = "private, max-age=3600"
        response.headers["Content-Disposition"] = "inline"
        return response

    # --- ошибки и заголовки ---

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://max.ru https://st.max.ru https://*.max.ru; "
            "style-src 'self'; "
            "img-src 'self' blob: data:; "
            "media-src 'self' blob:; "
            "connect-src 'self' https://max.ru https://*.max.ru; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'self' https://max.ru https://*.max.ru",
        )
        if request.path.startswith("/api/") and request.endpoint != "document_photo":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return api_error(
            f"Фото больше {settings.max_photo_bytes // MB} МБ. "
            "Сделайте снимок ещё раз — приложение сжимает его автоматически.",
            413,
            fields={"photo": "Слишком большой файл"},
        )

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return api_error("Не найдено", 404, code="not_found")
        return (
            "Страница не найдена. Откройте приложение из чата с ботом MAX.",
            404,
        )

    return app


# ---------------------------------------------------------------------------
# Бот: кнопка open_app
# ---------------------------------------------------------------------------

WELCOME = (
    "📚 Портфолио педагога\n\n"
    "Откройте мини-приложение: в нём камера, грамоты, курсы повышения "
    "квалификации и сводка за последние 12 месяцев.\n\n"
    "Нажмите кнопку ниже — приложение откроется прямо в MAX."
)


def open_app_keyboard(public_name: str) -> List[List[Dict[str, str]]]:
    return [
        [
            {
                "type": "open_app",
                "text": "📷 Открыть приложение",
                "web_app": public_name,
            }
        ]
    ]


def _handle_bot_update(
    update: Dict[str, Any], send_welcome
) -> None:
    kind = update.get("update_type")
    if kind == "bot_started":
        user_id = (update.get("user") or {}).get("user_id")
        if user_id:
            send_welcome(int(user_id))
        return
    if kind != "message_created":
        return

    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    if recipient.get("chat_type") not in (None, "dialog"):
        return
    sender = message.get("sender") or {}
    if sender.get("is_bot"):
        return
    user_id = sender.get("user_id") or recipient.get("user_id")
    if user_id:
        send_welcome(int(user_id))


def run_bot(settings: Settings) -> None:
    """Long polling: на любое сообщение присылаем кнопку мини-приложения."""
    api = bot.MaxApi(settings.bot_token, bot.build_ca_bundle())
    info = api.me()
    public_name = settings.bot_public_name or str(info.get("username") or "").lstrip("@")
    if not public_name:
        raise RuntimeError(
            "Не удалось определить публичное имя бота. Задайте MAX_BOT_PUBLIC_NAME "
            "в .env (имя бота в MAX без @)."
        )

    keyboard = open_app_keyboard(public_name)

    def send_welcome(user_id: int) -> None:
        api.send(user_id, WELCOME, keyboard)

    log.info("Бот @%s: кнопка open_app готова", public_name)
    log.info("Long polling запущен (не запускайте одновременно с bot.py)")

    marker = bot.load_marker()
    while True:
        try:
            result = api.updates(marker)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (401, 403):
                log.error("MAX API отклонил токен (%s). Проверьте MAX_BOT_TOKEN.", status)
                return
            if status == 400:
                log.warning("Сервер отклонил marker, начинаю с последнего события")
                marker = None
                bot.save_marker(None)
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
                _handle_bot_update(update, send_welcome)
            except Exception:
                log.exception("Ошибка обработки %s", update.get("update_type"))

        new_marker = result.get("marker")
        if new_marker is not None and new_marker != marker:
            marker = new_marker
            bot.save_marker(marker)


def start_bot_thread(settings: Settings) -> threading.Thread:
    def worker() -> None:
        try:
            run_bot(settings)
        except Exception:
            log.exception("Бот остановлен из-за ошибки")

    thread = threading.Thread(target=worker, name="max-bot", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Мини-приложение MAX «Портфолио педагога» (версия 2)"
    )
    parser.add_argument("--host", help="адрес веб-сервера (по умолчанию из MINI_APP_HOST)")
    parser.add_argument("--port", type=int, help="порт (по умолчанию 8080)")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="включить режим разработки без проверки initData",
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="запустить long polling и присылать кнопку открытия мини-приложения",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    args = parse_args(argv)
    settings = load_settings()
    if args.dev:
        settings = replace(settings, dev_mode=True)
    if args.host:
        settings = replace(settings, host=args.host)
    if args.port:
        settings = replace(settings, port=args.port)

    app = create_app(settings)

    if args.bot:
        if not settings.bot_token:
            log.error("Для --bot нужен MAX_BOT_TOKEN в .env")
            return 2
        start_bot_thread(settings)

    log.info("Мини-приложение: http://%s:%s", settings.host, settings.port)
    log.info("База данных: %s", settings.db_path)
    if settings.dev_mode:
        log.warning("Режим разработки: откройте http://127.0.0.1:%s", settings.port)
    else:
        log.info(
            "Для MAX нужен HTTPS-адрес. Локально: туннель или reverse proxy с TLS."
        )

    try:
        app.run(
            host=settings.host,
            port=settings.port,
            debug=settings.debug,
            threaded=True,
            use_reloader=False,
        )
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    return 0


if __name__ == "__main__":
    sys.exit(main())
