from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

import pytest

from ltp.config import load_settings
from ltp.policy import (Action, AllowedWhen, AppRule, allowance_minutes, evaluate, in_curfew,
                        is_counting, rule_for)
from ltp.store import Credit

D = date(2026, 11, 18)


@pytest.fixture
def s(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("{}\n", encoding="utf-8")
    return load_settings(cfg / "settings.yaml", {})


def c(minutes, source):
    return Credit(minutes, source, "", datetime(2026, 11, 18, 9))


def ev(s, used_min, allowance=20.0, counting=True, locked=False, curfew=False, warned=()):
    return evaluate(used_s=used_min * 60, allowance_min=allowance, counting=counting,
                    locked=locked, curfew=curfew, warned=frozenset(warned), settings=s, day=D)


def test_allowance_free_only(s):
    assert allowance_minutes([], s) == 20


def test_quiz_capped_parent_not(s):
    credits = [c(30, "quiz"), c(30, "quiz"), c(15, "parent")]
    assert allowance_minutes(credits, s) == 60 + 15


def test_counting_rules(s):
    assert is_counting("youtube.leanback.v4", s)
    assert is_counting("com.webos.app.livetv", s)
    assert is_counting("com.webos.app.settings", s)  # settings intentionally counts
    assert not is_counting("", s)
    assert not is_counting("com.webos.app.home", s)


def test_non_counting_never_acts(s):
    assert ev(s, 100, counting=False, locked=True, curfew=True).action is Action.NONE


def test_plenty_left_no_action(s):
    d = ev(s, 10)
    assert d.action is Action.NONE
    assert d.remaining_s == 600


def test_warn_five_then_silent_once_warned(s):
    d = ev(s, 15 + 1 / 60)  # 4:59 left
    assert d.action is Action.WARN and d.warn_tier == 5
    assert ev(s, 15 + 1 / 60, warned={d.warn_key}).action is Action.NONE


def test_warn_one_minute_tier(s):
    d = ev(s, 19.5, warned={ev(s, 15.5).warn_key})
    assert d.action is Action.WARN and d.warn_tier == 1


def test_warn_rearms_after_parent_adds_time(s):
    first = ev(s, 16, allowance=20)
    again = ev(s, 26, allowance=30, warned={first.warn_key})
    assert again.action is Action.WARN and again.warn_key != first.warn_key


def test_quota_exhausted(s):
    d = ev(s, 20)
    assert d.action is Action.ENFORCE and d.reason == "quota"


def test_locked_and_curfew_enforce(s):
    assert ev(s, 0, locked=True).reason == "locked"
    assert ev(s, 0, curfew=True).reason == "curfew"


@pytest.mark.parametrize("hh,mm,expected", [
    (23, 0, True), (6, 59, True), (7, 0, False), (20, 29, False), (20, 30, True), (12, 0, False),
])
def test_curfew_window_wraps_midnight(s, hh, mm, expected):
    assert in_curfew(datetime(2026, 11, 18, hh, mm, tzinfo=s.timezone), s) is expected


def test_curfew_same_day_window_and_disabled(s):
    day = replace(s, curfew_start=time(13, 0), curfew_end=time(15, 0))
    assert in_curfew(datetime(2026, 11, 18, 14, 0, tzinfo=s.timezone), day)
    assert not in_curfew(datetime(2026, 11, 18, 16, 0, tzinfo=s.timezone), day)
    off = replace(s, curfew_enabled=False)
    assert not in_curfew(datetime(2026, 11, 18, 23, 0, tzinfo=s.timezone), off)


A, Q, R = AllowedWhen.ALWAYS, AllowedWhen.QUOTA_EXHAUSTED, AllowedWhen.RESTRICTED
N, E = Action.NONE, Action.ENFORCE


@pytest.mark.parametrize("allowed_when,counts,locked,curfew,remaining_s,expected", [
    (A, False, True, False, 600, (N, "")),
    (A, False, False, True, 600, (N, "")),
    (A, False, False, False, -1, (N, "")),
    (A, False, False, False, 600, (N, "")),
    (Q, False, True, False, 600, (E, "locked")),
    (Q, False, False, True, 600, (E, "curfew")),
    (Q, False, False, False, -1, (N, "")),
    (Q, False, False, False, 600, (N, "")),
    (R, True, True, False, 600, (E, "locked")),
    (R, True, False, True, 600, (E, "curfew")),
    (R, True, False, False, -1, (E, "quota")),
    (R, True, False, False, 3600, (N, "")),
    (R, False, True, False, 600, (E, "locked")),
    (R, False, False, True, 600, (E, "curfew")),
    (R, False, False, False, -1, (E, "quota")),
    (R, False, False, False, 3600, (N, "")),
])
def test_allowed_when_matrix(s, allowed_when, counts, locked, curfew, remaining_s, expected):
    d = evaluate(used_s=0, allowance_min=remaining_s / 60, counting=counts, locked=locked,
                 curfew=curfew, warned=frozenset(), settings=s, day=D,
                 allowed_when=allowed_when)
    assert (d.action, d.reason) == expected


def test_rule_for_defaults(s):
    assert rule_for("youtube.leanback.v4", s) == AppRule(True, R)
    assert rule_for("com.webos.app.home", s) == AppRule(False, A)
    assert rule_for("", s) == AppRule(False, A)


def test_restricted_non_counting_gets_quota_warnings(s):
    # remaining is the shared quota, so warnings apply even though the app's own time is free
    d = evaluate(used_s=15 * 60, allowance_min=20, counting=False, locked=False, curfew=False,
                 warned=frozenset(), settings=s, day=D, allowed_when=R)
    assert d.action is Action.WARN and d.warn_tier == 5
