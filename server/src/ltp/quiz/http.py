"""LAN HTTP API for the TV quiz app.

Access control, in order: source-IP allowlist -> per-device bearer token -> API version
header -> per-device rate limit (reads and writes separate). The app runs from file://
(Origin "null"), so CORS preflights are answered for that origin only; the IP allowlist
and token still gate every real request. The server is embedded in the
daemon process; a failure to start must never stop monitoring (`run_quiz_server`).
"""
from __future__ import annotations

import contextlib
import hmac
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict
from datetime import datetime

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ltp.config import QuizSettings
from ltp.monitor import Monitor
from ltp.quiz.service import (SKIP_LIMIT_EVENT, QuizRejected, QuizService,
                              QuizUnavailable)
from ltp.store import Store

log = logging.getLogger("ltp.quiz.http")

NotifyOnce = Callable[[str, str], Awaitable[None]]
RATE_WINDOW_S = 60
VERSION_MISMATCH_TEXT = "⚠️ 答题 App 版本与服务器不一致，请重新安装"
# Origins a file:// web app may send; never an http(s) web origin.
CORS_ORIGINS = frozenset({"null", "file://"})
PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, X-LTP-API, Content-Type",
    "Access-Control-Max-Age": "600",
}


class _BadRequest(Exception):
    pass


class RateLimiter:
    """In-memory sliding window per (device, class), driven by the injected clock."""

    def __init__(self, now: Callable[[], datetime], window_s: int = RATE_WINDOW_S) -> None:
        self._now = now
        self._window_s = window_s
        self._hits: dict[tuple[str, str], deque[datetime]] = {}

    def check(self, device: str, klass: str, limit: int) -> int | None:
        """Record a hit and return None, or return retry_after_s when over the limit."""
        now = self._now()
        q = self._hits.setdefault((device, klass), deque())
        while q and (now - q[0]).total_seconds() >= self._window_s:
            q.popleft()
        if len(q) >= limit:
            return max(1, math.ceil(self._window_s - (now - q[0]).total_seconds()))
        q.append(now)
        return None


def _error(status: int, error: str, **extra: object) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status)


def _with_cors(request: Request, response: Response) -> Response:
    """Echo an allowed app origin; other origins get no CORS header (the browser blocks)."""
    origin = request.headers.get("origin")
    if origin in CORS_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"
    return response


def build_app(service: QuizService, cfg: QuizSettings, store: Store,
              notify_once: NotifyOnce, now: Callable[[], datetime]) -> Starlette:
    limiter = RateLimiter(now)
    denied_log = RateLimiter(now)  # caps quiz_denied events: one per source IP per minute
    tokens = list(cfg.tokens.items())

    def identify(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        presented = presented.strip() if scheme.lower() == "bearer" else ""
        match: str | None = None
        for device, token in tokens:  # compare against every token: timing must not reveal which
            same = hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
            if same and match is None:
                match = device
        return match

    def log_denied(ip: str, why: str) -> None:
        if denied_log.check(ip, "denied", 1) is None:
            store.log_event("quiz_denied", f"ip={ip} why={why}", now())

    async def preflight(request: Request) -> Response:
        ip = request.client.host if request.client else ""
        if ip not in cfg.allowed_ips:
            log_denied(ip, "ip")
            return _error(403, "forbidden")
        origin = request.headers.get("origin")
        if origin is not None and origin not in CORS_ORIGINS:
            log_denied(ip, "origin")
            return _error(403, "forbidden")
        return _with_cors(request, Response(status_code=204, headers=PREFLIGHT_HEADERS))

    def guard(handler: Callable[[Request, str], Awaitable[object]], *, write: bool):
        async def endpoint(request: Request) -> Response:
            return _with_cors(request, await checked(request))

        async def checked(request: Request) -> Response:
            ip = request.client.host if request.client else ""
            if ip not in cfg.allowed_ips:
                log_denied(ip, "ip")
                return _error(403, "forbidden")
            device = identify(request)
            if device is None:
                log_denied(ip, "token")
                return _error(401, "unauthorized")
            if request.headers.get("x-ltp-api") != str(cfg.api_version):
                await notify_once("quiz_version_mismatch", VERSION_MISMATCH_TEXT)
                return _error(426, "version", server_api=cfg.api_version)
            retry = limiter.check(device, "write" if write else "read",
                                  cfg.write_rate_per_min if write else cfg.read_rate_per_min)
            if retry is not None:
                return _error(429, "rate_limited", retry_after_s=retry)
            try:
                result = await handler(request, device)
            except QuizUnavailable as exc:
                extra = {} if exc.retry_after_s is None else {"retry_after_s": exc.retry_after_s}
                return _error(503, "unavailable", reason=exc.reason, **extra)
            except QuizRejected as exc:
                if exc.code == "skip_limit":
                    # Recorded outside the rolled-back transaction; feeds the daily digest.
                    store.log_event(SKIP_LIMIT_EVENT, f"device={device}", now())
                status = {"not_found": 404, "bad_input": 400}.get(exc.code, 409)
                extra = {} if exc.retry_after_s is None else {"retry_after_s": exc.retry_after_s}
                return _error(status, exc.code, code=exc.code, **extra)
            except _BadRequest:
                return _error(400, "bad_input", code="bad_input")
            payload = result if isinstance(result, dict) else asdict(result)  # type: ignore[call-overload]
            return JSONResponse(payload)
        return endpoint

    async def status(request: Request, device: str) -> object:
        return await service.status(device)

    async def result(request: Request, device: str) -> object:
        return await service.result(device, request.path_params["id"])

    async def current(request: Request, device: str) -> object:
        return await service.current(device, request.query_params.get("tier", "regular"))

    async def open_attempts(request: Request, device: str) -> object:
        return {"attempts": [asdict(v) for v in await service.open_attempts(device)]}

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
            raise _BadRequest from None
        if not isinstance(body, dict):
            raise _BadRequest
        return body

    def int_field(body: dict, key: str) -> int:
        value = body.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise _BadRequest
        return value

    async def hint(request: Request, device: str) -> object:
        body = await json_body(request)
        return await service.hint(device, request.path_params["id"], int_field(body, "rev"))

    async def skip(request: Request, device: str) -> object:
        body = await json_body(request)
        return await service.skip(device, request.path_params["id"], int_field(body, "rev"))

    async def submit(request: Request, device: str) -> object:
        body = await json_body(request)
        answer = body.get("answer")
        if not isinstance(answer, str):
            raise _BadRequest
        step = body.get("step")
        if step is not None and (not isinstance(step, int) or isinstance(step, bool)):
            raise _BadRequest
        return await service.submit(device, request.path_params["id"], answer,
                                    int_field(body, "rev"), step)

    async def review(request: Request, device: str) -> object:
        body = await json_body(request)
        return await service.review(device, request.path_params["id"], int_field(body, "rev"))

    api = [
        ("/api/status", guard(status, write=False), "GET"),
        ("/api/attempts/open", guard(open_attempts, write=False), "GET"),
        ("/api/attempt/{id}", guard(result, write=False), "GET"),
        ("/api/attempt", guard(current, write=True), "POST"),
        ("/api/attempt/{id}/hint", guard(hint, write=True), "POST"),
        ("/api/attempt/{id}/submit", guard(submit, write=True), "POST"),
        ("/api/attempt/{id}/skip", guard(skip, write=True), "POST"),
        ("/api/attempt/{id}/review", guard(review, write=True), "POST"),
    ]
    routes = [Route(path, ep, methods=[method]) for path, ep, method in api]
    routes += [Route(path, preflight, methods=["OPTIONS"]) for path, _, _ in api]
    return Starlette(routes=routes)


class EmbeddedServer(uvicorn.Server):
    """uvicorn server that leaves the daemon's signal handling alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    def install_signal_handlers(self) -> None:  # older uvicorn versions
        return None


async def run_quiz_server(service: QuizService, monitor: Monitor, cfg: QuizSettings,
                          store: Store, notify_once: NotifyOnce,
                          now: Callable[[], datetime]) -> None:
    """Serve until cancelled. A startup failure alerts the parent and returns, so the
    monitor keeps running without the quiz."""

    async def fail(klass: str, reason: str) -> None:
        log.error("quiz server failed (%s): %s", klass, reason)
        store.log_event("quiz_server_failed", f"{klass}: {reason}", now())
        await monitor.disable_quiz()
        await notify_once(f"quiz_server_failed:{klass}", f"⚠️ 答题服务启动失败：{reason}")

    if not cfg.tokens:
        await fail("no_tokens", "未配置 QUIZ_TOKENS")
        return
    app = build_app(service, cfg, store, notify_once, now)
    server = EmbeddedServer(uvicorn.Config(
        app, host=cfg.bind, port=cfg.port, lifespan="off", log_level="warning",
        proxy_headers=False, forwarded_allow_ips=""))
    try:
        await server.serve()
    except SystemExit:  # uvicorn exits(1) when it cannot bind
        await fail("bind", f"无法监听 {cfg.bind}:{cfg.port}（端口被占用？）")
    except OSError as exc:
        await fail("os_error", f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        log.exception("quiz server crashed")
        await fail("crash", f"{type(exc).__name__}: {exc}")
