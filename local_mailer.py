"""Build an email from the editable HTML template without changing its layout."""

from base64 import b64decode
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
import re


DATA_IMAGE = re.compile(r'src=["\']data:(image/(?:png|jpeg|gif));base64,([A-Za-z0-9+/=]+)["\']', re.I)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())


def build_message(html: str, recipient: str, subject: str):
    images = []

    def embed(match):
        mime_type = match.group(1).lower()
        payload = b64decode(match.group(2), validate=True)
        cid = f"openfactory-image-{len(images) + 1}-{make_msgid()[1:9]}"
        images.append((cid, mime_type, payload))
        return f'src="cid:{cid}"'

    email_html = DATA_IMAGE.sub(embed, html)
    if re.search(r'<script\b|\son[a-z]+\s*=', email_html, re.I):
        raise ValueError("HTML содержит скрипт или обработчик события")
    if re.search(r'src=["\']data:', email_html, re.I):
        raise ValueError("Неподдерживаемый формат изображения. Используйте PNG, JPEG или GIF")

    plain = TextExtractor()
    plain.feed(html)

    message = MIMEMultipart("related", policy=SMTP)
    message["From"] = "news@open-factory.ru"
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="open-factory.ru")

    alternatives = MIMEMultipart("alternative", policy=SMTP)
    alternatives.attach(MIMEText("\n".join(plain.parts), "plain", "utf-8", policy=SMTP))
    alternatives.attach(MIMEText(email_html, "html", "utf-8", policy=SMTP))
    message.attach(alternatives)

    for cid, mime_type, payload in images:
        image = MIMEImage(payload, _subtype=mime_type.split("/", 1)[1], policy=SMTP)
        image.add_header("Content-ID", f"<{cid}>")
        image.add_header("Content-Disposition", "inline", filename=f"{cid}.{mime_type.split('/')[-1]}")
        message.attach(image)

    return message
