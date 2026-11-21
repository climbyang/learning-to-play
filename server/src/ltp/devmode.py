"""LG developer-mode session keep-alive.

The dev-installed quiz app disappears when the developer-mode session expires. The session
token lives on the TV; LG's public endpoints report the remaining time and reset it to
999:59:59. The token is a credential: it is never logged, put in events or in alerts.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

from ltp.config import DevModeSettings
from ltp.store import Store
from ltp.tv.base import TVController

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 15.0
TOKEN_RE = re.compile(r"[0-9a-fA-F]{64}")
REMAINING_RE = re.compile(r"(\d+):([0-5]\d):([0-5]\d)")
_TOKEN_QUERY_RE = re.compile(r"(sessionToken=)[^&\s\"']+")
KV_TOKEN = "devmode:token"
KV_TOKEN_WAIT = "devmode:token_wait_since"
TOKEN_WAIT_ALERT = timedelta(hours=24)
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "HostKeyAlgorithms=+ssh-rsa",
               "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa", "-o", "ConnectTimeout=10")

Alert = Callable[[str, str], Awaitable[None]]


class DevModeError(RuntimeError):
    """Developer-mode failure. Messages never contain the session token."""


class _RedactTokenFilter(logging.Filter):
    """httpx logs full request URLs at INFO; blank the sessionToken query value."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging itself
            return True
        if "sessionToken=" in msg:
            record.msg = _TOKEN_QUERY_RE.sub(r"\1<redacted>", msg)
            record.args = ()
        return True


def _install_log_redaction() -> None:
    lg = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactTokenFilter) for f in lg.filters):
        lg.addFilter(_RedactTokenFilter())


async def read_token_over_ssh(host: str, key_path: str, *, port: int, user: str,
                              token_path: str, timeout: float) -> str:
    """Read the session token from the TV. No shell: argv only. Output must be 64 hex chars."""
    argv = ["ssh", "-i", str(key_path), "-p", str(port), *SSH_OPTIONS,
            f"{user}@{host}", "cat", token_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        raise DevModeError(f"无法运行 ssh：{exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DevModeError(f"ssh 读取令牌超时（{timeout:.0f} 秒）") from None
    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip().splitlines()
        raise DevModeError(f"ssh 退出码 {proc.returncode}"
                           + (f"：{detail[-1][:120]}" if detail else ""))
    token = out.decode("utf-8", "replace").strip()
    if not TOKEN_RE.fullmatch(token):  # deliberately not echoing the output
        raise DevModeError("电视返回的令牌格式不对（应为 64 位十六进制）")
    return token


def parse_remaining(text: str) -> timedelta:
    m = REMAINING_RE.fullmatch((text or "").strip())
    if not m:
        raise DevModeError("无法解析剩余时间")
    h, mi, s = (int(g) for g in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=s)


def cached_token(store: Store) -> str:
    token = store.get_kv(KV_TOKEN)
    if not token:
        raise DevModeError("没有缓存的令牌")
    return token


class DevModeClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str,
                 token_provider: Callable[[], str], timeout: float = HTTP_TIMEOUT_S) -> None:
        self._http = http
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        self._token = token_provider
        self._timeout = timeout
        _install_log_redaction()

    async def _call(self, endpoint: str) -> str:
        try:
            resp = await self._http.get(self._base + endpoint,
                                        params={"sessionToken": self._token()},
                                        timeout=self._timeout)
        except httpx.HTTPError as exc:  # message may embed the URL: keep only the type
            raise DevModeError(f"请求 LG 失败（{type(exc).__name__}）") from None
        if resp.status_code != 200:
            raise DevModeError(f"LG 返回 HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise DevModeError("LG 返回的不是 JSON") from None
        if not isinstance(body, dict) or body.get("result") != "success":
            code = body.get("errorCode") if isinstance(body, dict) else None
            raise DevModeError(f"LG 返回失败（errorCode={code}）")
        return str(body.get("errorMsg", ""))

    async def remaining(self) -> timedelta:
        return parse_remaining(await self._call("CheckDevModeSession.dev"))

    async def extend(self) -> None:
        await self._call("ResetDevModeSession.dev")


class DevModeJob:
    """Once per business day: refresh/cached token -> query -> renew when short, plus a
    check that the developer-mode app (and the quiz app) are still installed."""

    def __init__(self, s: DevModeSettings, client: DevModeClient, store: Store,
                 tv: TVController, alert: Alert, token_reader: Callable[[], Awaitable[str]],
                 now: Callable[[], datetime], tz: ZoneInfo,
                 quiz_app_id: Callable[[], str | None] = lambda: None) -> None:
        self.s = s
        self.client = client
        self.store = store
        self.tv = tv
        self._alert = alert
        self._read_token = token_reader
        self._now = now
        self._tz = tz
        self._quiz_app_id = quiz_app_id
        self._task: asyncio.Task | None = None
        self._next_try: datetime | None = None

    def _day(self, now: datetime) -> date:
        return now.astimezone(self._tz).date()

    def _checked_key(self, day: date) -> str:
        return f"devmode:checked:{day}"

    def _presence_key(self, day: date) -> str:
        return f"devmode:presence:{day}"

    def _due(self, now: datetime) -> bool:
        if not self.s.enabled:
            return False
        if self._next_try is not None and now < self._next_try:
            return False
        day = self._day(now)
        if self.store.get_kv(self._checked_key(day)) is None:
            return True
        return self.tv.state.connected and self.store.get_kv(self._presence_key(day)) is None

    def kick(self) -> None:
        """Start a background check if one is due and none is running. Never raises."""
        try:
            if self._task is not None and not self._task.done():
                return
            if not self._due(self._now()):
                return
            self._task = asyncio.create_task(self._guarded())
        except Exception:
            log.exception("devmode kick failed")

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def _guarded(self) -> None:
        try:
            await self.run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("devmode check crashed")
            self._backoff()

    def _backoff(self) -> None:
        self._next_try = self._now() + timedelta(minutes=self.s.retry_minutes)

    async def _fail(self, text: str) -> None:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        await self._alert(f"devmode:{digest}", text)

    async def run_once(self) -> None:
        now = self._now()
        if not self._due(now):
            return
        day = self._day(now)
        if self.tv.state.connected and self.store.get_kv(self._presence_key(day)) is None:
            await self._check_presence(day)
        if self.store.get_kv(self._checked_key(day)) is not None:
            return
        if not await self._ensure_token(now):
            self._backoff()
            return
        if await self._renew(now):
            self.store.set_kv(self._checked_key(day), "1")
            self._next_try = None
        else:
            self._backoff()

    async def _check_presence(self, day: date) -> None:
        try:
            known = await self.tv.list_app_ids()
        except Exception as exc:
            log.warning("launch point listing failed: %s", exc)
            return
        if not known:  # listing unavailable: unknown, not "deleted"
            return
        wanted = [("开发者模式应用", self.s.app_id)]
        quiz = self._quiz_app_id()
        if quiz:
            wanted.append(("答题应用", quiz))
        self.store.set_kv(self._presence_key(day), "1")
        for name, app_id in wanted:
            if app_id not in known:
                log.warning("app %s (%s) not found on the TV", name, app_id)
                await self._alert(f"devmode_app:{app_id}",
                                  f"⚠️ 电视上找不到 {name}（{app_id}），可能已被删除")

    async def _ensure_token(self, now: datetime) -> bool:
        reason = "电视未连接"
        if self.tv.state.connected:
            try:
                token = await self._read_token()
            except Exception as exc:
                log.warning("devmode token read failed: %s", exc)
                reason = str(exc)
            else:
                if self.store.get_kv(KV_TOKEN) != token:
                    self.store.set_kv(KV_TOKEN, token)
                self.store.del_kv(KV_TOKEN_WAIT)
                return True
        if self.store.get_kv(KV_TOKEN):
            return True  # cached token: LG's endpoint does not need the TV
        since_raw = self.store.get_kv(KV_TOKEN_WAIT)
        if since_raw is None:
            self.store.set_kv(KV_TOKEN_WAIT, now.isoformat())
        elif now - datetime.fromisoformat(since_raw) >= TOKEN_WAIT_ALERT:
            await self._fail(f"⚠️ 无法读取开发者模式令牌：{reason}")
        return False

    async def _renew(self, now: datetime) -> bool:
        """True when the day's check is complete."""
        try:
            remaining = await self.client.remaining()
        except Exception as exc:
            log.warning("devmode query failed: %s", exc)
            await self._fail(f"⚠️ 无法查询开发者模式剩余时间：{exc}")
            return False
        hours = int(remaining.total_seconds() // 3600)
        if remaining >= timedelta(hours=self.s.renew_below_hours):
            return True
        try:
            await self.client.extend()
        except Exception as exc:
            log.warning("devmode renewal failed: %s", exc)
            await self._fail(f"⚠️ 开发者模式剩余 {hours} 小时，续期失败：{exc}")
            return False
        self.store.log_event("devmode_extended", f"remaining_before={hours}h", now)
        log.info("developer-mode session renewed (was %dh left)", hours)
        try:
            after = await self.client.remaining()
        except Exception as exc:
            log.warning("devmode re-query after renewal failed: %s", exc)
            await self._fail(f"⚠️ 开发者模式已续期但无法确认结果：{exc}")
            return False  # retry under the backoff; the next run queries before extending
        if after < timedelta(hours=self.s.alert_below_hours):
            await self._fail(f"⚠️ 开发者模式剩余 {hours} 小时，续期失败：续期后仍只剩 "
                             f"{int(after.total_seconds() // 3600)} 小时")
            return False  # day stays unchecked: retry under the backoff (alert is deduped)
        return True
