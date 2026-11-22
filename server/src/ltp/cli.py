"""Command line: `ltp run | pair | smoke | status`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import random
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from ltp.config import Settings, load_settings
from ltp.policy import is_counting
from ltp.quiz.bank import BankError, QuizBank
from ltp.rules import AllowedWhen, AppRule
from ltp.store import Store
from ltp.tv.base import TVState
from ltp.tv.lg_webos import LGWebOSController

SERVER_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("ltp")


def _paths() -> tuple[Path, Path]:
    cfg = Path(os.environ.get("LTP_CONFIG", SERVER_ROOT / "config" / "settings.yaml"))
    env = Path(os.environ.get("LTP_ENV", SERVER_ROOT / ".env"))
    return cfg, env


def _settings() -> tuple[Settings, Path]:
    cfg, env_file = _paths()
    if not cfg.is_file():
        raise SystemExit(f"{cfg} not found: copy config/settings.example.yaml to config/settings.yaml first")
    if env_file.exists():
        load_dotenv(env_file, encoding="utf-8")
    return load_settings(cfg, os.environ), env_file


def _setup_logging(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):  # explicit UTF-8 (emoji in messages)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


# -- run -------------------------------------------------------------------
def _load_bank(s: Settings) -> QuizBank:
    """Shipped bank plus published generated questions. If the generated part is broken,
    fall back to the shipped bank alone so a bad batch never disables the quiz."""
    from ltp.quiz.generate import Paths
    main = (s.quiz.bank_dir, Path(s.quiz.bank_dir).parent / "reviews")
    extra = Paths(s.quiz.extra_bank_dir)
    try:
        return QuizBank.load(*main, extra=((extra.bank, extra.reviews),))
    except BankError as exc:
        log.error("generated questions invalid, using the shipped bank only: %s", exc)
        return QuizBank.load(*main)


def _prepare_quiz(s: Settings) -> tuple[Settings, QuizBank | None, list[str]]:
    """Load the quiz bank when enabled; on success register the quiz app's rule.

    Returns (settings, bank, problems). A bad bank disables only the quiz (bank None,
    problems set); monitoring is unaffected.
    """
    if not s.quiz.enabled:
        return s, None, []
    try:
        bank = _load_bank(s)
    except BankError as exc:
        log.error("quiz bank invalid, quiz disabled: %s", exc)
        return s, None, exc.problems
    except Exception as exc:  # any other load failure must disable only the quiz, never the daemon
        log.exception("quiz bank load crashed, quiz disabled")
        return s, None, [f"题库加载异常：{type(exc).__name__}: {exc}"]
    rules = {**s.app_rules, s.quiz.app_id: AppRule(False, AllowedWhen.QUOTA_EXHAUSTED)}
    return replace(s, app_rules=rules), bank, []


async def _run(s: Settings) -> None:
    import httpx

    from ltp.monitor import Monitor
    from ltp.notify import NullNotifier, TelegramBot
    from ltp.quiz.http import run_quiz_server
    from ltp.quiz.service import QuizService

    user_quiz_rule = s.app_rules.get(s.quiz.app_id)  # before the quiz adds its own
    s, bank, bank_problems = _prepare_quiz(s)
    store = Store(s.db_path)
    tv = LGWebOSController(s.tv_ip or "", s.tv_client_key, mac=s.tv_mac)
    async with httpx.AsyncClient() as http:
        bot = None
        if s.telegram_token and s.telegram_chat_ids:
            def get_offset() -> int | None:
                v = store.get_kv("tg_offset")
                return int(v) if v else None

            bot = TelegramBot(s.telegram_token, s.telegram_chat_ids, http,
                              get_offset=get_offset,
                              set_offset=lambda o: store.set_kv("tg_offset", str(o)))
            notifier = bot
        else:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: notifications go to log only")
            notifier = NullNotifier()
        monitor = Monitor(tv, store, notifier, s)
        tasks = [monitor.run()]
        if s.devmode.enabled and s.tv_ip:
            from ltp.devmode import DevModeClient, DevModeJob, cached_token, read_token_over_ssh

            dm = s.devmode

            async def read_token() -> str:
                return await read_token_over_ssh(
                    s.tv_ip or "", str(dm.ssh_key), port=dm.ssh_port, user=dm.ssh_user,
                    token_path=dm.token_path, timeout=dm.ssh_timeout_s)

            client = DevModeClient(http, dm.base_url, lambda: cached_token(store))
            monitor.devmode = DevModeJob(dm, client, store, tv, monitor.notify_once, read_token,
                                         lambda: datetime.now(timezone.utc), s.timezone,
                                         lambda: monitor.quiz_app_id)
        elif s.devmode.enabled:
            log.warning("devmode check disabled: LG_TV_IP not set")
        if bank is not None:
            service = QuizService(bank, monitor, store, s.quiz, random.Random())
            service.bank_loader = lambda: _load_bank(s)
            monitor.enable_quiz(s.quiz.app_id, user_quiz_rule, service)
            tasks.append(run_quiz_server(service, monitor, s.quiz, store, monitor.notify_once,
                                         lambda: datetime.now(timezone.utc)))
        elif bank_problems:
            tasks.append(monitor.notify_once(
                "quiz_bank_error", "⚠️ 题库校验失败，答题已停用：" + "；".join(bank_problems[:3])))
        if bot is not None:
            tasks.append(bot.poll(monitor.handle))
        log.info("starting: mode=%s tv=%s paired=%s db=%s", monitor.mode, s.tv_ip or "-",
                 bool(s.tv_client_key), s.db_path)
        await asyncio.gather(*tasks)


# -- pair ------------------------------------------------------------------
def _write_env_key(env_file: Path, key: str) -> None:
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    line = f"LG_CLIENT_KEY={key}"
    if re.search(r"^LG_CLIENT_KEY=.*$", text, flags=re.M):
        text = re.sub(r"^LG_CLIENT_KEY=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    env_file.write_text(text, encoding="utf-8")
    try:
        env_file.chmod(0o600)
    except OSError:
        pass


async def _pair(s: Settings, env_file: Path, write_env: bool) -> None:
    if not s.tv_ip:
        raise SystemExit("Set LG_TV_IP in .env first")
    print(f"connecting to {s.tv_ip} … accept the prompt on the TV with the remote")
    tv = LGWebOSController(s.tv_ip, s.tv_client_key, allow_pairing=True)
    await tv.connect()
    key = tv.client_key
    st = await tv.refresh()
    await tv.disconnect()
    print(f"paired ✅  foreground app: {st.app_id or '(none)'}")
    if write_env and key:
        _write_env_key(env_file, key)
        print(f"LG_CLIENT_KEY written to {env_file}")
    else:
        print(f"LG_CLIENT_KEY={key}")


# -- smoke (phase-1 PoC, real TV) ----------------------------------------
async def _smoke(s: Settings, power_off: bool, watch_seconds: int,
                 close_after: int | None = None) -> None:
    if not (s.tv_ip and s.tv_client_key):
        raise SystemExit("Need LG_TV_IP and LG_CLIENT_KEY (run `ltp pair --write-env` first)")
    tv = LGWebOSController(s.tv_ip, s.tv_client_key)
    seen: list[TVState] = []
    pending: set[asyncio.Task] = set()

    async def close_later(app_id: str) -> None:
        await asyncio.sleep(close_after)
        if tv.state.app_id != app_id:
            return
        print(f"  [{datetime.now():%H:%M:%S}] closing {app_id} …")
        try:
            await tv.close_app(app_id)
        except Exception as exc:
            print(f"  close_app FAILED: {exc!r}")
            raise

    async def on_state(st: TVState) -> None:
        prev = seen[-1].app_id if seen else None
        seen.append(st)
        print(f"  [{datetime.now():%H:%M:%S}] state → connected={st.connected} "
              f"on={st.power_on} app={tv.app_title(st.app_id)} [{st.app_id}]")
        if (close_after is not None and st.active and st.app_id != prev
                and is_counting(st.app_id, s)):
            t = asyncio.create_task(close_later(st.app_id))
            pending.add(t)
            t.add_done_callback(pending.discard)

    tv.on_state(on_state)
    print("(1) connect"); await tv.connect()
    print("(2) foreground app:", (await tv.refresh()).app_id or "(none)")
    print("(3) toast"); await tv.show_toast("Learning-to-Play 连接成功 ✅")
    hint = f"; counting apps are closed after {close_after}s" if close_after is not None else ""
    print(f"(4) watching app switches for {watch_seconds}s — switch apps on the TV now{hint}")
    await asyncio.sleep(watch_seconds)
    for t in list(pending):
        t.cancel()
    for t in list(pending):
        try:
            await t
        except asyncio.CancelledError:
            pass
    if power_off:
        print("(5) power off in 3s"); await asyncio.sleep(3); await tv.power_off()
    await tv.disconnect()
    print(f"done ✅  ({len(seen)} state events)")


# -- status ----------------------------------------------------------------
def _status(s: Settings) -> None:
    from ltp.monitor import Monitor
    from ltp.notify import NullNotifier
    from ltp.tv.fake import FakeTV

    store = Store(s.db_path)
    mon = Monitor(FakeTV(), store, NullNotifier(), s)
    now = datetime.now(timezone.utc)
    text = mon.status_text(now).splitlines()
    print(text[0])
    print(text[2])
    print("最近事件：")
    for ev in store.recent_events(10):
        print(f"  {ev.ts.astimezone(s.timezone):%m-%d %H:%M:%S}  {ev.kind:<14} {ev.detail}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ltp", description="Learning-to-Play TV monitor")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the monitor daemon")
    pp = sub.add_parser("pair", help="pair with the TV (TV must be on; accept prompt)")
    pp.add_argument("--write-env", action="store_true", help="save LG_CLIENT_KEY into .env")
    sp = sub.add_parser("smoke", help="phase-1 smoke test against the real TV")
    sp.add_argument("--power-off", action="store_true")
    sp.add_argument("--watch", type=int, default=30)
    sp.add_argument("--close-app", type=int, metavar="SECONDS", default=None,
                    help="close any counting app SECONDS after it opens")
    sub.add_parser("status", help="print today's usage and recent events")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)
    s, env_file = _settings()
    if args.cmd == "run":
        asyncio.run(_run(s))
    elif args.cmd == "pair":
        asyncio.run(_pair(s, env_file, args.write_env))
    elif args.cmd == "smoke":
        asyncio.run(_smoke(s, args.power_off, args.watch, args.close_app))
    elif args.cmd == "status":
        _status(s)


if __name__ == "__main__":
    main()
