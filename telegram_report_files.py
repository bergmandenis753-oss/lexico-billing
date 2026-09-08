import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class TextDocument:
    filename: str
    text: str
    caption: str


def send_document(bot, chat_id, document, reply_markup=None):
    token = bot._token()
    if not token:
        raise RuntimeError("Telegram token is not configured")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.txt", document.filename):
        raise ValueError("Invalid report filename")
    content = document.text.encode("utf-8-sig")
    if len(content) > 49_000_000:
        raise ValueError("Report exceeds the Telegram document size limit")
    boundary = "lexico_" + uuid.uuid4().hex
    fields = {"chat_id": str(chat_id), "caption": document.caption[:1000]}
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    parts = []
    for name, value in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                      f'{value}\r\n').encode("utf-8"))
    parts.extend([
        (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
         f'filename="{document.filename}"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n').encode("ascii"),
        content,
        f'\r\n--{boundary}--\r\n'.encode("ascii"),
    ])
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    # Do not propagate urllib exceptions containing the bot token in the URL.
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram upload failed (HTTP {exc.code})") from None
    except (OSError, ValueError):
        raise RuntimeError("Telegram upload could not be confirmed") from None
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the document")
    return result


def install(bot):
    if getattr(bot, "_report_files_installed", False):
        return
    original_send = bot._send_message

    def send_message(chat_id, text, reply_markup=None):
        if not isinstance(text, TextDocument):
            return original_send(chat_id, text, reply_markup)
        try:
            return send_document(bot, chat_id, text, reply_markup)
        except Exception:
            return original_send(
                chat_id,
                "Не удалось подтвердить отправку файла. Если файл не пришёл, повтори команду позже.",
                reply_markup,
            )

    bot._send_message = send_message
    bot._report_files_installed = True
