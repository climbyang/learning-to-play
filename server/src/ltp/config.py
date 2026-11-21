"""Settings: structured config from config/settings.yaml + secrets from env (.env)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import yaml

from ltp.rules import AllowedWhen, AppRule
from ltp.tv.wol import parse_mac

MODES = ("observe", "enforce")
DEFAULT_TIMEZONE = "UTC"  # used when settings.yaml has no timezone

DEFAULT_EXEMPT = (
    "",
    "com.webos.app.home",
    "com.webos.app.homeconnect",
    "com.webos.app.launcher",
    "com.webos.app.softwareupdate",
    "com.webos.app.screensaver",
)


@dataclass(frozen=True)
class QuizSettings:
    enabled: bool = False
    app_id: str = "com.learningtoplay.quiz"
    bind: str = "0.0.0.0"
    port: int = 8765
    allowed_ips: frozenset[str] = frozenset()
    tokens: Mapping[str, str] = field(default_factory=dict)  # device -> bearer token
    api_version: int = 2
    regular_minutes: int = 10
    challenge_minutes: int = 25
    min_seconds_regular: int = 20
    min_seconds_challenge: int = 180
    min_seconds_passage_regular: int = 60  # issue -> last part submission
    min_seconds_passage_challenge: int = 300
    vocab_cooldown_s: int = 5  # between vocab check submissions
    numeric_tries: int = 3
    wrong_cooldown_s: int = 60
    skips_per_day: int = 3
    wrong_pause_threshold: int = 10
    wrong_pause_minutes: int = 15
    write_rate_per_min: int = 10
    read_rate_per_min: int = 60
    dedup_days: int = 30
    # Reward for regular questions of a kind family ("vocab" covers vocab_*), instead of
    # regular_minutes: quick questions must not pay as much as a maths problem.
    kind_minutes: Mapping[str, int] = field(default_factory=lambda: {"vocab": 3})
    bank_dir: Path = Path("quiz/bank")  # relative to the server root


_QUIZ_INT_KEYS = ("port", "api_version", "regular_minutes", "challenge_minutes",
                  "min_seconds_regular", "min_seconds_challenge", "min_seconds_passage_regular",
                  "min_seconds_passage_challenge", "vocab_cooldown_s", "numeric_tries",
                  "wrong_cooldown_s", "skips_per_day", "wrong_pause_threshold",
                  "wrong_pause_minutes", "write_rate_per_min", "read_rate_per_min", "dedup_days")


@dataclass(frozen=True)
class DevModeSettings:
    enabled: bool = True
    ssh_key: Path = Path("~/.ssh/ltp_tv_webos")  # expanded at load time
    ssh_port: int = 9922
    ssh_user: str = "prisoner"
    ssh_timeout_s: int = 15
    token_path: str = "/var/luna/preferences/devmode_enabled"  # on the TV
    base_url: str = "https://developer.lge.com/secure/"
    app_id: str = "com.palmdts.devmode"
    renew_below_hours: int = 24
    alert_below_hours: int = 12
    retry_minutes: int = 10


_DEVMODE_INT_KEYS = ("ssh_port", "ssh_timeout_s", "renew_below_hours", "alert_below_hours",
                     "retry_minutes")
_DEVMODE_INT_MAX = {"ssh_port": 65535}


@dataclass(frozen=True)
class Settings:
    timezone: ZoneInfo
    mode: str
    free_minutes: float
    daily_cap_minutes: float
    warn_minutes: tuple[int, ...]
    tick_seconds: float
    app_rules: Mapping[str, AppRule]
    curfew_enabled: bool
    curfew_start: time
    curfew_end: time
    violation_count: int
    violation_window_minutes: float
    curfew_power_off: bool
    notify_app_switch: bool
    daily_summary_at: time | None
    power_on_debounce_minutes: float
    db_path: Path
    tv_ip: str | None
    tv_client_key: str | None
    tv_mac: str | None
    telegram_token: str | None
    telegram_chat_ids: frozenset[int]
    quiz: QuizSettings = QuizSettings()
    devmode: DevModeSettings = DevModeSettings()


def _bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    """Strict YAML boolean: a string such as "false" must not silently become True."""
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false, got {value!r}")
    return value


def _parse_hhmm(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, int):  # YAML 1.1 parses unquoted 20:30 as sexagesimal 1230
        return time(value // 60, value % 60)
    hh, mm = str(value).strip().split(":")
    return time(int(hh), int(mm))


def _env(env: Mapping[str, str], key: str) -> str | None:
    v = env.get(key)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _chat_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(p) for p in raw.split(",") if p.strip())


def _quiz_tokens(raw: str | None) -> dict[str, str]:
    """QUIZ_TOKENS="device:token,device2:token2" -> {device: token}."""
    tokens: dict[str, str] = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        device, sep, token = part.partition(":")
        if not sep or not device.strip() or not token.strip():
            raise ValueError("QUIZ_TOKENS entries must look like device:token")
        tokens[device.strip()] = token.strip()
    return tokens


# Smallest allowed value per quiz int key (default 1). 0 is meaningful for: no minimum
# think time, no cooldown, skips disabled, dedup off.
_QUIZ_INT_MIN = {"min_seconds_regular": 0, "min_seconds_challenge": 0, "wrong_cooldown_s": 0,
                 "min_seconds_passage_regular": 0, "min_seconds_passage_challenge": 0,
                 "vocab_cooldown_s": 0,
                 "skips_per_day": 0, "dedup_days": 0}
_QUIZ_INT_MAX = {"port": 65535}


def _quiz_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"quiz.{key} must be an integer, got {value!r}")
    lo, hi = _QUIZ_INT_MIN.get(key, 1), _QUIZ_INT_MAX.get(key)
    if value < lo or (hi is not None and value > hi):
        bound = f">= {lo}" if hi is None else f"between {lo} and {hi}"
        raise ValueError(f"quiz.{key} must be {bound}, got {value}")
    return value


def _parse_quiz(raw: Mapping[str, Any], env: Mapping[str, str], tv_ip: str | None,
                server_root: Path) -> QuizSettings:
    d = QuizSettings()
    ips = raw.get("allowed_ips")
    bank_dir = Path(raw.get("bank_dir", d.bank_dir))
    if not bank_dir.is_absolute():
        bank_dir = server_root / bank_dir
    ints = {k: _quiz_int(raw, k, getattr(d, k)) for k in _QUIZ_INT_KEYS}
    return QuizSettings(
        enabled=_bool(raw, "enabled", d.enabled),
        app_id=str(raw.get("app_id", d.app_id)),
        bind=str(raw.get("bind", d.bind)),
        allowed_ips=(frozenset(str(ip) for ip in ips) if ips is not None
                     else frozenset({tv_ip}) if tv_ip else frozenset()),
        tokens=_quiz_tokens(_env(env, "QUIZ_TOKENS")),
        bank_dir=bank_dir,
        kind_minutes=_kind_minutes(raw.get("kind_minutes", d.kind_minutes)),
        **ints,
    )


def _kind_minutes(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"quiz.kind_minutes must be a mapping, got {raw!r}")
    out: dict[str, int] = {}
    for kind, minutes in raw.items():
        if (not isinstance(kind, str) or not kind or isinstance(minutes, bool)
                or not isinstance(minutes, int) or not 0 <= minutes <= 60):
            raise ValueError(f"quiz.kind_minutes: {kind!r} needs whole minutes 0-60, got {minutes!r}")
        out[kind] = minutes
    return out


def _devmode_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"devmode.{key} must be an integer, got {value!r}")
    hi = _DEVMODE_INT_MAX.get(key)
    if value < 1 or (hi is not None and value > hi):
        bound = ">= 1" if hi is None else f"between 1 and {hi}"
        raise ValueError(f"devmode.{key} must be {bound}, got {value}")
    return value


def _parse_devmode(raw: Mapping[str, Any], env: Mapping[str, str]) -> DevModeSettings:
    d = DevModeSettings()
    ints = {k: _devmode_int(raw, k, getattr(d, k)) for k in _DEVMODE_INT_KEYS}
    key_path = _env(env, "DEVMODE_SSH_KEY") or str(raw.get("ssh_key", d.ssh_key))
    return DevModeSettings(
        enabled=_bool(raw, "enabled", d.enabled),
        ssh_key=Path(key_path).expanduser(),
        ssh_user=str(raw.get("ssh_user", d.ssh_user)),
        token_path=str(raw.get("token_path", d.token_path)),
        base_url=str(raw.get("base_url", d.base_url)),
        app_id=str(raw.get("app_id", d.app_id)),
        **ints,
    )


def _parse_app_rules(data: Mapping[str, Any]) -> dict[str, AppRule]:
    """Legacy `exempt_apps` entries become (not counted, always allowed);
    explicit `app_rules` entries win for the same app."""
    rules: dict[str, AppRule] = {
        str(a or ""): AppRule(False, AllowedWhen.ALWAYS)
        for a in data.get("exempt_apps", DEFAULT_EXEMPT)
    }
    for app_id, raw in (data.get("app_rules") or {}).items():
        raw = raw or {}
        rules[str(app_id or "")] = AppRule(
            counts=_bool(raw, "counts_toward_quota", True),
            allowed_when=AllowedWhen(str(raw.get("allowed_when", "restricted"))),
        )
    return rules


def load_settings(yaml_path: Path, env: Mapping[str, str]) -> Settings:
    """Load settings. Relative db_path resolves against the server root
    (the parent of the config/ directory holding the yaml)."""
    yaml_path = Path(yaml_path)
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    quota = data.get("quota") or {}
    curfew = data.get("curfew") or {}
    enf = data.get("enforcement") or {}
    notify = data.get("notify") or {}

    mode = str(data.get("mode", "observe"))
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    summary = notify.get("daily_summary_at", "20:00")
    server_root = yaml_path.resolve().parent.parent
    db_path = Path(data.get("db_path", "data/ltp.db"))
    if not db_path.is_absolute():
        db_path = server_root / db_path

    tv_ip = _env(env, "LG_TV_IP")
    tv_mac = _env(env, "LG_TV_MAC")
    if tv_mac:
        parse_mac(tv_mac)  # fail fast on a typo instead of at the first /on
    return Settings(
        timezone=ZoneInfo(str(data.get("timezone", DEFAULT_TIMEZONE))),
        mode=mode,
        free_minutes=float(quota.get("free_minutes", 20)),
        daily_cap_minutes=float(quota.get("daily_cap_minutes", 60)),
        warn_minutes=tuple(sorted((int(m) for m in quota.get("warn_minutes", [5, 1])),
                                  reverse=True)),
        tick_seconds=float(data.get("tick_seconds", 10)),
        app_rules=_parse_app_rules(data),
        curfew_enabled=_bool(curfew, "enabled", True),
        curfew_start=_parse_hhmm(curfew.get("start", "20:30")),
        curfew_end=_parse_hhmm(curfew.get("end", "07:00")),
        violation_count=int(enf.get("violation_count", 3)),
        violation_window_minutes=float(enf.get("violation_window_minutes", 10)),
        curfew_power_off=_bool(enf, "curfew_power_off", False),
        notify_app_switch=_bool(notify, "app_switch", False),
        daily_summary_at=None if summary is None else _parse_hhmm(summary),
        power_on_debounce_minutes=float(notify.get("power_on_debounce_minutes", 5)),
        db_path=db_path,
        tv_ip=tv_ip,
        tv_client_key=_env(env, "LG_CLIENT_KEY"),
        tv_mac=tv_mac,
        telegram_token=_env(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_ids=_chat_ids(_env(env, "TELEGRAM_CHAT_ID")),
        quiz=_parse_quiz(data.get("quiz") or {}, env, tv_ip, server_root),
        devmode=_parse_devmode(data.get("devmode") or {}, env),
    )
