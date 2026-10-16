"""Tell the person something outside the board: a push (ntfy) or a Telegram message.

Configured under `[notify]`: `push_url` (an ntfy-style topic URL; the text is POSTed to it) and/or
`telegram_token` with `telegram_chat`. Nothing configured means nothing sent. A failed send is never
an error for the caller: the board post it accompanies already happened."""
from __future__ import annotations

import urllib.parse
import urllib.request

TIMEOUT_S = 5


def _post(url: str, data: bytes, headers: dict | None = None) -> bool:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return False
    try:
        req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT_S):   # noqa: S310: scheme checked above
            return True
    except Exception:
        return False


def send(cfg: dict | None, text: str) -> bool:
    """Send `text` over every configured channel; True if at least one accepted it."""
    conf = (cfg or {}).get("notify") or {}
    sent = False
    if conf.get("push_url"):
        sent = _post(str(conf["push_url"]), text.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"}) or sent
    if conf.get("telegram_token") and conf.get("telegram_chat"):
        url = f"https://api.telegram.org/bot{conf['telegram_token']}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": str(conf["telegram_chat"]), "text": text}).encode("ascii")
        sent = _post(url, body) or sent
    return sent
