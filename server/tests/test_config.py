from datetime import time
from pathlib import Path

import pytest

from ltp.config import load_settings
from ltp.rules import AllowedWhen, AppRule


def _write(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    p = cfg / "settings.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_when_yaml_empty(tmp_path):
    s = load_settings(_write(tmp_path, "{}\n"), {})
    assert s.mode == "observe"
    assert s.free_minutes == 20
    assert s.daily_cap_minutes == 60
    assert s.warn_minutes == (5, 1)
    assert s.tick_seconds == 10
    assert s.curfew_enabled is True
    assert s.curfew_start == time(20, 30)
    assert s.curfew_end == time(7, 0)
    assert s.violation_count == 3
    assert s.violation_window_minutes == 10
    assert s.power_on_debounce_minutes == 5
    assert s.daily_summary_at == time(20, 0)
    assert str(s.timezone) == "Etc/GMT-10"
    assert "" in s.app_rules
    assert s.tv_ip is None and s.telegram_token is None
    assert s.telegram_chat_ids == frozenset()


def test_env_secrets_and_chat_ids(tmp_path):
    env = {"LG_TV_IP": "10.0.0.5", "LG_CLIENT_KEY": "k", "TELEGRAM_BOT_TOKEN": "t",
           "TELEGRAM_CHAT_ID": "1, 2"}
    s = load_settings(_write(tmp_path, "{}\n"), env)
    assert s.tv_ip == "10.0.0.5"
    assert s.tv_client_key == "k"
    assert s.telegram_token == "t"
    assert s.telegram_chat_ids == frozenset({1, 2})


def test_blank_env_values_are_none(tmp_path):
    s = load_settings(_write(tmp_path, "{}\n"), {"LG_CLIENT_KEY": "  ", "TELEGRAM_CHAT_ID": ""})
    assert s.tv_client_key is None
    assert s.telegram_chat_ids == frozenset()


def test_invalid_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_settings(_write(tmp_path, "mode: yolo\n"), {})


def test_db_path_relative_to_server_root(tmp_path):
    s = load_settings(_write(tmp_path, "db_path: data/x.db\n"), {})
    assert s.db_path == tmp_path / "data" / "x.db"


def test_summary_can_be_disabled(tmp_path):
    s = load_settings(_write(tmp_path, "notify:\n  daily_summary_at: null\n"), {})
    assert s.daily_summary_at is None


def test_tv_mac_validated(tmp_path):
    import pytest as _pytest
    from ltp.config import load_settings as _load
    y = tmp_path / "s.yaml"
    y.write_text("{}\n", encoding="utf-8")
    assert _load(y, {"LG_TV_MAC": "00:11:22:33:44:55"}).tv_mac == "00:11:22:33:44:55"
    assert _load(y, {}).tv_mac is None
    with _pytest.raises(ValueError):
        _load(y, {"LG_TV_MAC": "not-a-mac"})


def test_legacy_exempt_apps_become_always(tmp_path):
    s = load_settings(_write(tmp_path, 'exempt_apps: ["", com.webos.app.home]\n'), {})
    always = AppRule(False, AllowedWhen.ALWAYS)
    assert s.app_rules == {"": always, "com.webos.app.home": always}


def test_app_rules_override_exempt(tmp_path):
    text = (
        "exempt_apps: [quiz.app, com.webos.app.home]\n"
        "app_rules:\n"
        "  quiz.app:\n"
        "    counts_toward_quota: false\n"
        "    allowed_when: quota_exhausted\n"
    )
    s = load_settings(_write(tmp_path, text), {})
    assert s.app_rules["quiz.app"] == AppRule(False, AllowedWhen.QUOTA_EXHAUSTED)
    assert s.app_rules["com.webos.app.home"] == AppRule(False, AllowedWhen.ALWAYS)


def test_bad_allowed_when_rejected(tmp_path):
    text = "app_rules:\n  x:\n    counts_toward_quota: true\n    allowed_when: sometimes\n"
    with pytest.raises(ValueError):
        load_settings(_write(tmp_path, text), {})


def test_quiz_defaults(tmp_path):
    s = load_settings(_write(tmp_path, "{}\n"), {"LG_TV_IP": "10.0.0.5"})
    q = s.quiz
    assert q.enabled is False and q.app_id == "com.learningtoplay.quiz"
    assert q.bind == "0.0.0.0" and q.port == 8765 and q.api_version == 2
    assert q.allowed_ips == frozenset({"10.0.0.5"}) and dict(q.tokens) == {}
    assert (q.regular_minutes, q.challenge_minutes) == (10, 25)
    assert (q.min_seconds_regular, q.min_seconds_challenge) == (20, 180)
    assert (q.numeric_tries, q.wrong_cooldown_s, q.skips_per_day) == (3, 60, 3)
    assert (q.wrong_pause_threshold, q.wrong_pause_minutes) == (10, 15)
    assert (q.write_rate_per_min, q.read_rate_per_min, q.dedup_days) == (10, 60, 30)
    assert q.bank_dir == tmp_path / "quiz" / "bank"
    assert load_settings(tmp_path / "config" / "settings.yaml", {}).quiz.allowed_ips == frozenset()


def test_quiz_yaml_and_tokens(tmp_path):
    p = _write(tmp_path, "quiz:\n  enabled: true\n  port: 9000\n  regular_minutes: 8\n"
                         "  allowed_ips: [1.2.3.4, 5.6.7.8]\n"
                         f"  bank_dir: '{(tmp_path / 'abs').as_posix()}'\n")
    s = load_settings(p, {"QUIZ_TOKENS": "tv: abc , tv2:def", "LG_TV_IP": "10.0.0.5"})
    q = s.quiz
    assert q.enabled is True and q.port == 9000 and q.regular_minutes == 8
    assert q.allowed_ips == frozenset({"1.2.3.4", "5.6.7.8"})
    assert dict(q.tokens) == {"tv": "abc", "tv2": "def"}
    assert q.bank_dir == tmp_path / "abs"
    assert dict(load_settings(p, {"QUIZ_TOKENS": " "}).quiz.tokens) == {}
    with pytest.raises(ValueError):
        load_settings(p, {"QUIZ_TOKENS": "notoken"})


@pytest.mark.parametrize("text", [
    "quiz:\n  enabled: 'false'\n",
    "quiz:\n  enabled: 'no'\n",
    "quiz:\n  enabled: 1\n",
    "curfew:\n  enabled: 'false'\n",
    "notify:\n  app_switch: 'false'\n",
    "app_rules:\n  x:\n    counts_toward_quota: 'false'\n",
])
def test_non_boolean_flags_rejected(tmp_path, text):
    with pytest.raises(ValueError, match="must be true or false"):
        load_settings(_write(tmp_path, text), {})


def test_yaml_booleans_accepted(tmp_path):
    s = load_settings(_write(tmp_path, "quiz:\n  enabled: false\ncurfew:\n  enabled: false\n"
                                       "notify:\n  app_switch: true\n"), {})
    assert s.quiz.enabled is False and s.curfew_enabled is False and s.notify_app_switch is True


@pytest.mark.parametrize("key,value", [
    ("write_rate_per_min", 0), ("write_rate_per_min", -1), ("read_rate_per_min", 0),
    ("port", 0), ("port", 65536), ("port", -5), ("api_version", 0),
    ("regular_minutes", 0), ("challenge_minutes", -3), ("numeric_tries", 0),
    ("wrong_pause_threshold", 0), ("wrong_pause_minutes", 0),
    ("min_seconds_regular", -1), ("min_seconds_challenge", -1), ("wrong_cooldown_s", -1),
    ("skips_per_day", -1), ("dedup_days", -1),
    ("port", "abc"), ("port", True), ("regular_minutes", 2.5),
])
def test_quiz_int_out_of_range_rejected(tmp_path, key, value):
    with pytest.raises(ValueError, match=key):
        load_settings(_write(tmp_path, f"quiz:\n  {key}: {value!r}\n".replace("'", "")
                             if not isinstance(value, str) else f"quiz:\n  {key}: {value}\n"), {})


@pytest.mark.parametrize("key", ["skips_per_day", "wrong_cooldown_s", "min_seconds_regular",
                                 "min_seconds_challenge", "dedup_days"])
def test_quiz_zero_allowed_where_meaningful(tmp_path, key):
    s = load_settings(_write(tmp_path, f"quiz:\n  {key}: 0\n"), {})
    assert getattr(s.quiz, key) == 0


def test_quiz_port_bounds_accepted(tmp_path):
    assert load_settings(_write(tmp_path, "quiz:\n  port: 65535\n"), {}).quiz.port == 65535


def test_production_default_timezone_is_utc(tmp_path, monkeypatch):
    # conftest pins DEFAULT_TIMEZONE for every test; undo it to check the shipped default.
    monkeypatch.undo()
    import ltp.config
    assert ltp.config.DEFAULT_TIMEZONE == "UTC"
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("", encoding="utf-8")
    assert str(ltp.config.load_settings(cfg / "settings.yaml", {}).timezone) == "UTC"
