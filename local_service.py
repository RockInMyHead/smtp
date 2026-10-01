"""Local-only editor and SMTP sender for the OpenFactory email template."""

from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re
import secrets
import smtplib
import ssl
import sys
import webbrowser

import certifi

from local_mailer import build_message


ROOT = Path(__file__).resolve().parent
ORIGINAL = ROOT / "specmash_email_final.html"
DRAFT = ROOT / "specmash_email_draft.html"
SETTINGS = ROOT / "local_service_settings.json"
PORT = 8765
TOKEN = secrets.token_urlsafe(32)
DEFAULT_SUBJECT = "OpenFactory для «Спецмаш»: склад в 3D и движение каждой детали"
MAX_BODY = 15 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Never log request bodies or the app password.
        if self.path == "/api/send":
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
        else:
            self._json(404, {"error": "Не найдено"})

    def do_POST(self):
        if not self._valid_host() or not self._valid_write():
            return self._json(403, {"error": "Недопустимый запрос"})
        try:
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
                password = data.get("password", "")
                if not isinstance(recipient, str) or len(recipient) > 254 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", recipient):
                    raise ValueError("Укажите один корректный адрес получателя")
                if parseaddr(recipient)[1] != recipient:
                    raise ValueError("Некорректный адрес получателя")
                if not isinstance(password, str) or not password:
                    raise ValueError("Введите пароль приложения Яндекса")
                if not password.isascii():
                    raise ValueError("Пароль приложения содержит нелатинские символы. Вставьте его заново латиницей из Яндекса.")
                message = build_message(html, recipient, subject)
                context = ssl.create_default_context(cafile=certifi.where())
                try:
                    with smtplib.SMTP_SSL("smtp.yandex.ru", 465, context=context, timeout=45) as smtp:
                        smtp.login("news@open-factory.ru", password)
                        refused = smtp.send_message(message, from_addr="news@open-factory.ru", to_addrs=[recipient])
                        if refused:
                            raise RuntimeError("Сервер отклонил адрес получателя")
                except smtplib.SMTPAuthenticationError:
                    return self._json(401, {"error": "Яндекс отклонил пароль приложения"})
                except smtplib.SMTPDataError as error:
                    return self._json(502, {"error": f"Яндекс не принял письмо: {error.smtp_code} {error.smtp_error.decode('utf-8', 'replace')[:180]}"})
                except (smtplib.SMTPException, OSError) as error:
                    return self._json(502, {"error": f"Ошибка SMTP: {type(error).__name__}. Проверьте сеть и попробуйте позже."})
                return self._json(200, {"ok": True, "recipient": recipient, "messageId": str(message["Message-ID"])})

            return self._json(404, {"error": "Не найдено"})
        except (ValueError, json.JSONDecodeError) as error:
            return self._json(400, {"error": str(error)})


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
