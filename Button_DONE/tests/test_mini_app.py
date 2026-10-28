# -*- coding: utf-8 -*-
"""Офлайн-тесты API мини-приложения: Flask test client + временная SQLite."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mini_app  # noqa: E402

TOKEN = "test-bot-token"
USER_ID = 4242
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 10


def make_init_data(
    token: str = TOKEN,
    user_id: int = USER_ID,
    auth_date: int | None = None,
    first_name: str = "Иван",
) -> str:
    if auth_date is None:
        auth_date = int(time.time())
    fields = {
        "query_id": "test-query",
        "auth_date": str(auth_date),
        "user": json.dumps(
            {
                "id": user_id,
                "first_name": first_name,
                "last_name": "Петров",
                "username": "tester",
                "language_code": "ru",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    pairs = sorted(f"{key}={value}" for key, value in fields.items())
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(
        secret, "\n".join(pairs).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    fields["hash"] = digest
    return urllib.parse.urlencode(fields)


def make_settings(directory: str, **overrides):
    defaults = dict(
        bot_token=TOKEN,
        db_path=Path(directory) / "test.sqlite3",
        dev_mode=False,
        auth_ttl=3600,
        photo_token_ttl=7200,
        max_photo_bytes=25 * 1024 * 1024,
        photo_secret=mini_app.photo_secret_for(TOKEN),
        host="127.0.0.1",
        port=0,
        debug=False,
        bot_public_name="",
    )
    defaults.update(overrides)
    return mini_app.Settings(**defaults)


COMPETITION = {
    "title": "Педагог года — 2025",
    "organizer": "Департамент образования",
    "level": "Региональный",
    "dates": "12.05.2025 – 15.05.2025",
    "year": "2025",
    "result": "Диплом 1 степени",
}

COURSE = {
    "title": "Цифровые инструменты",
    "organizer": "Академия Минпросвещения",
    "dates": "03.02.2025 – 14.02.2025",
    "year": "2025",
    "hours": "72",
}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mini-app-test-")
        self.settings = make_settings(self.tmp.name)
        self.app = mini_app.create_app(self.settings)
        self.client = self.app.test_client()
        self.init_data = make_init_data()

    def tearDown(self):
        self.tmp.cleanup()

    def headers(self, init_data=None):
        return {"X-Max-Init-Data": init_data or self.init_data}

    def bootstrap(self, init_data=None):
        return self.client.get("/api/bootstrap", headers=self.headers(init_data))

    def save_fio(self, fio="Иванова Мария Петровна"):
        return self.client.post(
            "/api/profile",
            headers=self.headers(),
            json={"fio": fio},
        )

    def upload(self, kind, fields, photo=JPEG, filename="document.jpg"):
        data = {
            "kind": kind,
            "fields": json.dumps(fields, ensure_ascii=False),
            "photo": (io.BytesIO(photo), filename),
        }
        return self.client.post(
            "/api/documents",
            headers=self.headers(),
            data=data,
            content_type="multipart/form-data",
        )


class AuthTests(ApiTestCase):
    def test_health_is_open(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_api_requires_init_data(self):
        response = self.client.get("/api/bootstrap")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "unauthorized")

    def test_valid_init_data_opens_bootstrap(self):
        response = self.bootstrap()
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dev_mode"])
        self.assertEqual(payload["profile"]["user_id"], USER_ID)
        self.assertEqual(payload["user"]["fio"], "")
        self.assertEqual(len(payload["fields"]["competition"]), 6)
        self.assertEqual(len(payload["fields"]["course"]), 5)
        self.assertEqual(payload["summary"]["total"], 0)

    def test_tampered_init_data_is_rejected(self):
        bad = self.init_data.replace("query_id=test-query", "query_id=other-query")
        self.assertNotEqual(bad, self.init_data)
        self.assertEqual(self.bootstrap(bad).status_code, 401)

    def test_expired_init_data_is_rejected(self):
        expired = make_init_data(auth_date=int(time.time()) - 7200)
        self.assertEqual(self.bootstrap(expired).status_code, 401)

    def test_future_init_data_is_rejected(self):
        future = make_init_data(auth_date=int(time.time()) + 3600)
        self.assertEqual(self.bootstrap(future).status_code, 401)

    def test_missing_bot_token_rejects(self):
        app = mini_app.create_app(
            make_settings(self.tmp.name, bot_token="", photo_secret=b"x")
        )
        response = app.test_client().get(
            "/api/bootstrap", headers={"X-Max-Init-Data": self.init_data}
        )
        self.assertEqual(response.status_code, 401)

    def test_dev_mode_without_init_data(self):
        app = mini_app.create_app(
            make_settings(self.tmp.name, dev_mode=True, bot_token="")
        )
        response = app.test_client().get("/api/bootstrap")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["dev_mode"])


class ProfileTests(ApiTestCase):
    def test_invalid_fio(self):
        response = self.save_fio("Иван")
        self.assertEqual(response.status_code, 400)
        self.assertIn("fio", response.get_json()["fields"])

    def test_fio_is_saved_and_returned(self):
        response = self.save_fio()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["fio"], "Иванова Мария Петровна")

        bootstrap = self.bootstrap().get_json()
        self.assertEqual(bootstrap["user"]["fio"], "Иванова Мария Петровна")

    def test_fio_normalizes_spaces(self):
        response = self.save_fio("  Иванова   Мария   Петровна ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["fio"], "Иванова Мария Петровна")

    def test_fio_rejects_digits(self):
        response = self.save_fio("Иванов Иван 123")
        self.assertEqual(response.status_code, 400)


class DocumentTests(ApiTestCase):
    def test_competition_upload_and_photo(self):
        saved = self.upload("competition", COMPETITION)
        self.assertEqual(saved.status_code, 201)
        payload = saved.get_json()
        self.assertTrue(payload["ok"])
        document = payload["document"]
        self.assertEqual(document["kind"], "competition")
        self.assertEqual(document["title"], COMPETITION["title"])
        self.assertEqual(document["level"], "Региональный")
        self.assertEqual(document["result"], "Диплом 1 степени")
        self.assertIsNone(document["hours"])
        self.assertEqual(payload["summary"]["competitions"], 1)
        self.assertEqual(payload["summary"]["total"], 1)

        photo = self.client.get(document["photo_url"])
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, "image/jpeg")
        self.assertEqual(photo.data, JPEG)
        self.assertIn("private", photo.headers["Cache-Control"])

    def test_course_upload_counts_hours(self):
        saved = self.upload("course", COURSE, photo=PNG, filename="doc.png")
        self.assertEqual(saved.status_code, 201)
        summary = saved.get_json()["summary"]
        self.assertEqual(summary["courses"], 1)
        self.assertEqual(summary["hours"], 72)
        self.assertEqual(summary["documents"][0]["hours"], 72)

        photo = self.client.get(saved.get_json()["document"]["photo_url"])
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, "image/png")

    def test_documents_are_shared_with_v1_storage(self):
        self.upload("course", COURSE)
        storage = mini_app.bot.Storage(self.settings.db_path)
        try:
            documents = storage.recent_documents(USER_ID)
        finally:
            storage.conn.close()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["title"], COURSE["title"])

    def test_invalid_enum_year_and_missing_title(self):
        fields = dict(COMPETITION)
        fields.update({"title": "", "level": "Неизвестный", "year": "25"})
        response = self.upload("competition", fields)
        self.assertEqual(response.status_code, 400)
        errors = response.get_json()["fields"]
        self.assertIn("title", errors)
        self.assertIn("level", errors)
        self.assertIn("year", errors)

    def test_invalid_hours(self):
        fields = dict(COURSE)
        fields["hours"] = "0"
        response = self.upload("course", fields)
        self.assertEqual(response.status_code, 400)
        self.assertIn("hours", response.get_json()["fields"])

    def test_unknown_kind_is_rejected(self):
        response = self.upload("diploma", COMPETITION)
        self.assertEqual(response.status_code, 400)

    def test_non_image_is_rejected(self):
        response = self.upload(
            "competition", COMPETITION, photo=b"<html>not an image</html>"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("photo", response.get_json()["fields"])

    def test_empty_photo_is_rejected(self):
        response = self.upload("competition", COMPETITION, photo=b"")
        self.assertEqual(response.status_code, 400)

    def test_answers_can_arrive_as_numbers(self):
        fields = dict(COURSE)
        fields["year"] = 2025
        fields["hours"] = 36
        response = self.upload("course", fields)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["document"]["hours"], 36)

    def test_photo_requires_valid_token(self):
        saved = self.upload("competition", COMPETITION).get_json()
        document = saved["document"]
        self.assertEqual(
            self.client.get(f"/api/documents/{document['id']}/photo").status_code, 404
        )
        self.assertEqual(
            self.client.get(
                f"/api/documents/{document['id']}/photo?t=bad.token"
            ).status_code,
            404,
        )

    def test_photo_token_of_another_user_does_not_work(self):
        saved = self.upload("competition", COMPETITION).get_json()
        document = saved["document"]
        foreign = mini_app.make_photo_token(
            self.settings.photo_secret, USER_ID + 1, 3600
        )
        response = self.client.get(
            f"/api/documents/{document['id']}/photo?t={urllib.parse.quote(foreign)}"
        )
        self.assertEqual(response.status_code, 404)

    def test_expired_photo_token_does_not_work(self):
        saved = self.upload("competition", COMPETITION).get_json()
        document = saved["document"]
        expires = int(time.time()) - 1
        payload = f"{USER_ID}:{expires}"
        signature = hmac.new(
            self.settings.photo_secret, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        response = self.client.get(
            f"/api/documents/{document['id']}/photo?t={expires}.{signature}"
        )
        self.assertEqual(response.status_code, 404)

    def test_old_documents_are_excluded_from_summary(self):
        self.bootstrap()
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(self.settings.db_path)
        try:
            conn.execute(
                """INSERT INTO documents
                   (user_id, kind, photo, photo_mime, title, completed, created_at)
                   VALUES (?, 'course', ?, 'image/jpeg', 'Старый курс', 1, ?)""",
                (USER_ID, sqlite3.Binary(JPEG), old_date),
            )
            conn.commit()
        finally:
            conn.close()
        summary = self.bootstrap().get_json()["summary"]
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["courses"], 0)

    def test_second_document_appears_in_summary(self):
        self.upload("competition", COMPETITION)
        response = self.upload("course", COURSE)
        summary = response.get_json()["summary"]
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["competitions"], 1)
        self.assertEqual(summary["courses"], 1)
        self.assertEqual(len(summary["documents"]), 2)


class UnitTests(unittest.TestCase):
    def test_init_data_roundtrip(self):
        parsed = mini_app.validate_init_data(make_init_data(), TOKEN, 3600)
        self.assertEqual(parsed["user_id"], USER_ID)
        self.assertEqual(parsed["user"]["first_name"], "Иван")

    def test_init_data_with_cyrillic_and_ampersand(self):
        raw = make_init_data(first_name="Иван & Марья")
        parsed = mini_app.validate_init_data(raw, TOKEN, 3600)
        self.assertEqual(parsed["user"]["first_name"], "Иван & Марья")

    def test_init_data_requires_token(self):
        with self.assertRaises(mini_app.AuthError):
            mini_app.validate_init_data(make_init_data(), "", 3600)

    def test_fio_validation(self):
        self.assertEqual(
            mini_app.validate_fio("  Иванов   Иван  "), "Иванов Иван"
        )
        for bad in ("", "Иван", "Иванов Иван 42"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    mini_app.validate_fio(bad)

    def test_detect_image_mime(self):
        self.assertEqual(mini_app.detect_image_mime(JPEG), "image/jpeg")
        self.assertEqual(mini_app.detect_image_mime(PNG), "image/png")
        self.assertIsNone(mini_app.detect_image_mime(b"GIF89a"))

    def test_photo_secret_is_derived_from_token(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINI_APP_SECRET", None)
            first = mini_app.photo_secret_for(TOKEN)
            second = mini_app.photo_secret_for(TOKEN)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, mini_app.photo_secret_for(TOKEN + "x"))

    def test_photo_secret_without_token_is_random(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINI_APP_SECRET", None)
            first = mini_app.photo_secret_for("")
            second = mini_app.photo_secret_for("")
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, second)

    def test_photo_secret_override(self):
        with mock.patch.dict(os.environ, {"MINI_APP_SECRET": "a" * 32}):
            self.assertEqual(mini_app.photo_secret_for(TOKEN), b"a" * 32)


if __name__ == "__main__":
    unittest.main()
