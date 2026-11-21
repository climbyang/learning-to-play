"""Parent notifications + commands via Telegram Bot API (long polling, no public server)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import httpx

log = logging.getLogger(__name__)

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)


@dataclass(frozen=True)
class Command:
    chat_id: int
    user_id: int
    name: str          # lower-case, no leading "/" or @bot suffix; "" for plain text
    arg: str


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: Buttons | None = None


Handler = Callable[[Command], Awaitable[Reply | None]]


class Notifier(Protocol):
    async def send(self, text: str, buttons: Buttons | None = None) -> None: ...


class NullNotifier:
    """Used when no Telegram token is configured: log only."""

    async def send(self, text: str, buttons: Buttons | None = None) -> None:
        log.info("[notify] %s", text.replace("\n", " | "))


def _markup(buttons: Buttons | None) -> dict | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row]
                                for row in buttons]}


def parse_text_command(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", text
    head, _, rest = text[1:].partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


class TelegramBot:
    def __init__(self, token: str, allowed_ids: frozenset[int], client: httpx.AsyncClient, *,
                 get_offset: Callable[[], int | None] = lambda: None,
                 set_offset: Callable[[int], None] = lambda _o: None,
                 poll_timeout: int = 30) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._allowed = allowed_ids
        self._http = client
        self._get_offset = get_offset
        self._set_offset = set_offset
        self._poll_timeout = poll_timeout

    async def _call(self, method: str, payload: dict, timeout: float = 15.0) -> dict:
        resp = await self._http.post(f"{self._base}/{method}", json=payload, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data.get('error_code')} "
                               f"{data.get('description')}")
        return data

    async def send_to(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        payload: dict = {"chat_id": chat_id, "text": text}
        markup = _markup(buttons)
        if markup:
            payload["reply_markup"] = markup
        try:
            await self._call("sendMessage", payload)
        except Exception as exc:  # never let a notification failure kill the daemon
            log.warning("telegram send to %s failed: %s", chat_id, exc)

    async def send(self, text: str, buttons: Buttons | None = None) -> None:
        for chat_id in sorted(self._allowed):
            await self.send_to(chat_id, text, buttons)

    def _authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        # Private chats only: chat id == user id, and both must be whitelisted.
        return user_id in self._allowed and chat_id in self._allowed

    async def process_update(self, update: dict, handler: Handler) -> None:
        cmd: Command | None = None
        if "callback_query" in update:
            cq = update["callback_query"]
            user_id = (cq.get("from") or {}).get("id")
            chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
            try:
                await self._call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            except Exception as exc:
                log.debug("answerCallbackQuery failed: %s", exc)
            if self._authorized(user_id, chat_id):
                name, _, arg = str(cq.get("data") or "").partition(":")
                cmd = Command(int(chat_id), int(user_id), name.lower(), arg)
        elif "message" in update:
            msg = update["message"]
            user_id = (msg.get("from") or {}).get("id")
            chat_id = (msg.get("chat") or {}).get("id")
            if self._authorized(user_id, chat_id):
                name, arg = parse_text_command(msg.get("text") or "")
                cmd = Command(int(chat_id), int(user_id), name, arg)
        else:
            return
        if cmd is None:
            log.warning("ignored telegram update %s from unauthorized sender",
                        update.get("update_id"))
            return
        reply = await handler(cmd)
        if reply is not None:
            await self.send_to(cmd.chat_id, reply.text, reply.buttons)

    async def poll_once(self, handler: Handler) -> int:
        offset = self._get_offset()
        payload: dict = {"timeout": self._poll_timeout,
                         "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        data = await self._call("getUpdates", payload, timeout=self._poll_timeout + 10)
        updates = data.get("result") or []
        for upd in updates:
            # Persist before handling: a crash mid-handler must not replay the command.
            self._set_offset(int(upd["update_id"]) + 1)
            try:
                await self.process_update(upd, handler)
            except Exception:
                log.exception("handler failed for update %s", upd.get("update_id"))
        return len(updates)

    async def poll(self, handler: Handler) -> None:
        backoff = 5.0
        while True:
            try:
                await self.poll_once(handler)
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("telegram poll error: %s (retry in %.0fs)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)

