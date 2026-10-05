"""Local-only editor and SMTP sender for the OpenFactory email template."""

from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone
import json
import os
import re
import secrets
import smtplib
import ssl
import subprocess
import sys
import threading
import webbrowser

import certifi

from local_mailer import build_message


ROOT = Path(__file__).resolve().parent
ORIGINAL = ROOT / "specmash_email_final.html"
DRAFT = ROOT / "specmash_email_draft.html"
SETTINGS = ROOT / "local_service_settings.json"
HISTORY = ROOT / "local_history"
HISTORY_INDEX = HISTORY / "index.json"
KEYCHAIN_SERVICE = "openfactory-local-smtp-editor"
SENDER = "news@open-factory.ru"
HISTORY_LOCK = threading.Lock()
PORT = 8765
TOKEN = secrets.token_urlsafe(32)
DEFAULT_SUBJECT = "OpenFactory для «Спецмаш»: склад в 3D и движение каждой детали"
MAX_BODY = 15 * 1024 * 1024


def keychain_password():
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", SENDER, "-w"],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def save_keychain_password(password):
    if not password or not password.isascii() or "\n" in password or "\r" in password:
        raise ValueError("Пароль должен содержать только латинские символы без переноса строки")
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
         "-a", SENDER, "-l", "OpenFactory — пароль приложения Яндекс Почты", "-w"],
        input=password + "\n" + password + "\n", capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError("Не удалось сохранить пароль в Связке ключей macOS")


def delete_keychain_password():
    result = subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", SENDER],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode not in (0, 44):
        raise RuntimeError("Не удалось удалить пароль из Связки ключей macOS")


def history_entries():
    return json.loads(HISTORY_INDEX.read_text(encoding="utf-8")) if HISTORY_INDEX.exists() else []


def record_attempt(html, recipient, subject, result, detail, message_id=None):
    entry_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
    entry = {
        "id": entry_id, "date": datetime.now(timezone.utc).isoformat(),
        "recipient": recipient, "subject": subject, "result": result,
        "detail": detail, "messageId": message_id,
    }
    with HISTORY_LOCK:
        HISTORY.mkdir(mode=0o700, exist_ok=True)
        path = HISTORY / f"{entry_id}.html"
        path.write_text(html, encoding="utf-8")
        os.chmod(path, 0o600)
        entries = history_entries()
        entries.insert(0, entry)
        temporary = HISTORY / "index.tmp"
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(HISTORY_INDEX)
    return entry


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Never log request bodies or the app password.
        if self.path in {"/api/send", "/api/password", "/api/password/delete"}:
            return
        super().log_message(format, *args)

    def _json(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _valid_host(self):
        return self.headers.get("Host", "") in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def _valid_write(self):
        return (
            self.headers.get("Origin") in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
            and self.headers.get("X-Local-Token") == TOKEN
            and self.headers.get("Content-Type", "").startswith("application/json")
        )

    def do_GET(self):
        if not self._valid_host():
            return self._json(403, {"error": "Доступ только с этого компьютера"})
        if self.path == "/":
            page = (ROOT / "local_service.html").read_text(encoding="utf-8")
            page = page.replace("__LOCAL_TOKEN__", TOKEN)
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/template":
            settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
            self._json(200, {
                "html": (DRAFT if DRAFT.exists() else ORIGINAL).read_text(encoding="utf-8"),
                "subject": settings.get("subject", DEFAULT_SUBJECT),
                "isDraft": DRAFT.exists(),
            })
        elif self.path == "/api/state":
            self._json(200, {"passwordSaved": keychain_password() is not None, "history": history_entries()})
        elif self.path.startswith("/api/history/"):
            entry_id = self.path.removeprefix("/api/history/")
            entry = next((item for item in history_entries() if item["id"] == entry_id), None)
            if not entry:
                return self._json(404, {"error": "Запись истории не найдена"})
            self._json(200, {**entry, "html": (HISTORY / f"{entry_id}.html").read_text(encoding="utf-8")})
        else:
            self._json(404, {"error": "Не найдено"})

    def do_POST(self):
        if not self._valid_host() or not self._valid_write():
            return self._json(403, {"error": "Недопустимый запрос"})
        try:
            if self.path in {"/api/password", "/api/password/delete"}:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    return self._json(413, {"error": "Недопустимый размер запроса"})
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/password/delete":
                    delete_keychain_password()
                    return self._json(200, {"passwordSaved": False})
                password = data.get("password")
                if not isinstance(password, str):
                    raise ValueError("Введите пароль приложения Яндекса")
                save_keychain_password(password)
                return self._json(200, {"passwordSaved": True})
            if self.path == "/api/reset":
                DRAFT.unlink(missing_ok=True)
                SETTINGS.unlink(missing_ok=True)
                return self._json(200, {"ok": True})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self._json(413, {"error": "Шаблон слишком большой (предел 15 МБ)"})
            data = json.loads(self.rfile.read(length))
            html = data.get("html", "")
            subject = data.get("subject", "")
            if not isinstance(html, str) or not isinstance(subject, str):
                raise ValueError("Неверный формат шаблона")
            if not 1 <= len(subject.strip()) <= 200 or "\n" in subject or "\r" in subject:
                raise ValueError("Укажите тему письма (до 200 символов)")
            if "<html" not in html.lower() or len(html) > MAX_BODY:
                raise ValueError("Нужен полный HTML-шаблон")

            if self.path == "/api/save":
                build_message(html, "preview@example.com", subject)
                temporary = DRAFT.with_suffix(".tmp")
                temporary.write_text(html, encoding="utf-8")
                temporary.replace(DRAFT)
                SETTINGS.write_text(json.dumps({"subject": subject}, ensure_ascii=False), encoding="utf-8")
                return self._json(200, {"ok": True})

            if self.path == "/api/send":
                recipient = data.get("recipient", "")
                if not isinstance(recipient, str) or len(recipient) > 254 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", recipient):
                    raise ValueError("Укажите один корректный адрес получателя")
                if parseaddr(recipient)[1] != recipient:
                    raise ValueError("Некорректный адрес получателя")
                password = keychain_password()
                if not password:
                    raise ValueError("Сначала сохраните пароль приложения Яндекса")
                message = build_message(html, recipient, subject)
                context = ssl.create_default_context(cafile=certifi.where())
                try:
                    with smtplib.SMTP_SSL("smtp.yandex.ru", 465, context=context, timeout=45) as smtp:
                        smtp.login(SENDER, password)
                        refused = smtp.send_message(message, from_addr=SENDER, to_addrs=[recipient])
                        if refused:
                            raise RuntimeError("Сервер отклонил адрес получателя")
                except smtplib.SMTPAuthenticationError:
                    error = "Яндекс отклонил пароль приложения"
                    try:
                        record_attempt(html, recipient, subject, "error", error)
                    except OSError:
                        pass
                    return self._json(401, {"error": error})
                except smtplib.SMTPDataError as error:
                    detail = f"Яндекс не принял письмо: {error.smtp_code} {error.smtp_error.decode('utf-8', 'replace')[:180]}"
                    try:
                        record_attempt(html, recipient, subject, "error", detail)
                    except OSError:
                        pass
                    return self._json(502, {"error": detail})
                except (smtplib.SMTPException, OSError, RuntimeError) as error:
                    detail = f"Ошибка SMTP: {type(error).__name__}. Проверьте сеть и попробуйте позже."
                    try:
                        record_attempt(html, recipient, subject, "error", detail)
                    except OSError:
                        pass
                    return self._json(502, {"error": detail})
                try:
                    entry = record_attempt(html, recipient, subject, "accepted", "Яндекс принял письмо", str(message["Message-ID"]))
                    history_id = entry["id"]
                except OSError:
                    history_id = None
                return self._json(200, {"ok": True, "recipient": recipient, "messageId": str(message["Message-ID"]), "historyId": history_id})

            return self._json(404, {"error": "Не найдено"})
        except (ValueError, json.JSONDecodeError) as error:
            return self._json(400, {"error": str(error)})
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return self._json(500, {"error": "Не удалось открыть Связку ключей или сохранить историю"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Локальный редактор: http://127.0.0.1:{PORT}/", flush=True)
    if "--no-open" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Остановлено")
    finally:
        server.server_close()
