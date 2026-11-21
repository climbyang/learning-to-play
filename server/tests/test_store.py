import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from ltp.store import _SCHEMA, _SCHEMA_V2, Attempt, SchemaTooNewError, Store

UTC = timezone.utc


def t(h, m=0, s=0, day=18):
    return datetime(2026, 11, day, h, m, s, tzinfo=UTC)


def test_midnight_split(tmp_path):
    st = Store(tmp_path / "a.db")
    sid = st.open_session("yt", "YouTube", True, t(23, 55))
    st.close_session(sid, t(0, 5, day=19), "app_switch")
    assert st.used_seconds(t(0, 0, day=18), t(0, 0, day=19)) == 300
    assert st.used_seconds(t(0, 0, day=19), t(0, 0, day=20)) == 300


def test_non_counting_sessions_ignored(tmp_path):
    st = Store(tmp_path / "a.db")
    sid = st.open_session("com.webos.app.home", "Home", False, t(10))
    st.close_session(sid, t(11), "app_switch")
    assert st.used_seconds(t(0), t(23)) == 0


def test_open_session_counts_until_last_heartbeat(tmp_path):
    st = Store(tmp_path / "a.db")
    sid = st.open_session("yt", "YouTube", True, t(10))
    st.heartbeat(sid, t(10, 2))
    assert st.used_seconds(t(0), t(23)) == 120


def test_close_dangling_uses_last_heartbeat_not_now(tmp_path):
    p = tmp_path / "a.db"
    st = Store(p)
    sid = st.open_session("yt", "YouTube", True, t(10))
    st.heartbeat(sid, t(10, 3))
    st2 = Store(p)  # simulate restart hours later
    assert st2.close_dangling() == 1
    assert st2.used_seconds(t(0), t(23)) == 180
    assert st2.open_session_id() is None


def test_kv_persists_across_instances(tmp_path):
    p = tmp_path / "a.db"
    Store(p).set_kv("locked", "1")
    assert Store(p).get_kv("locked") == "1"
    assert Store(p).get_kv("missing", "d") == "d"


def test_credits_filtered_by_day(tmp_path):
    st = Store(tmp_path / "a.db")
    st.add_credit(date(2026, 11, 18), 10, "parent", "by dad", t(9))
    st.add_credit(date(2026, 11, 19), 5, "quiz", "", t(9, day=19), ref="batch-1")
    cs = st.credits(date(2026, 11, 18))
    assert [(c.minutes, c.source) for c in cs] == [(10.0, "parent")]


def test_events_since(tmp_path):
    st = Store(tmp_path / "a.db")
    st.log_event("violation", "yt", t(10))
    st.log_event("violation", "tubi", t(10, 8))
    st.log_event("other", "", t(10, 9))
    assert st.count_events("violation", since=t(10, 5)) == 1
    assert st.count_events("violation", since=t(9)) == 2


def test_schema_version_recorded(tmp_path):
    assert Store(tmp_path / "a.db").get_kv("schema_version") == "3"


D = date(2026, 11, 18)


def mk(id_, device="tv", tier="regular", day=D, state="issued", template="tpl", **kw):
    return Attempt(id=id_, device=device, tier=tier, template_id=template, instance_id="i-" + id_,
                   content_hash="h", snapshot={"q": "2+2? é"}, answer=[1], option_order=[2, 0, 1],
                   day=day, issued_utc=t(10), state=state, **kw)


def test_transaction_rolls_back_on_error(tmp_path):
    st = Store(tmp_path / "a.db")
    with pytest.raises(RuntimeError, match="boom"):
        with st.transaction():
            st.add_credit(D, 5, "quiz", "n", t(10))
            raise RuntimeError("boom")
    assert st.credits(D) == []
    with st.transaction():
        st.add_credit(D, 5, "quiz", "n", t(10))
    assert len(st.credits(D)) == 1


def test_nested_transaction_rejected(tmp_path):
    st = Store(tmp_path / "a.db")
    with st.transaction():
        with pytest.raises(RuntimeError, match="nested transaction"):
            with st.transaction():
                pass
        st.set_kv("k", "v")  # outer still usable
    assert st.get_kv("k") == "v"
    with st.transaction():  # flag reset after completion
        pass


def test_migrates_v1_db_to_v2(tmp_path):
    p = tmp_path / "a.db"
    db = sqlite3.connect(str(p), isolation_level=None)
    db.executescript(_SCHEMA)
    db.execute("INSERT INTO kv VALUES ('schema_version', '1')")
    db.execute("INSERT INTO credits(day, minutes, source, note, ts_utc) VALUES ('2026-11-18', 10, 'free', '', '2026-11-18T00:00:00.000000Z')")
    db.close()
    st = Store(p)
    assert st.get_kv("schema_version") == "3"
    assert len(st.credits(D)) == 1
    st.insert_attempt(mk("a1"))


def test_migrate_v2_to_v3_keeps_rows(tmp_path):
    p = tmp_path / "a.db"
    db = sqlite3.connect(str(p), isolation_level=None)
    db.executescript(_SCHEMA)
    db.executescript(_SCHEMA_V2)
    db.execute("INSERT INTO kv VALUES ('schema_version', '2')")
    ins = ("INSERT INTO quiz_attempts(id, device, tier, template_id, instance_id, content_hash, "
           "snapshot, answer, day, issued_utc, state, finished_utc) "
           "VALUES (?, 'tv', ?, 'tpl', 'i', 'h', '{}', '[1]', '2026-11-18', "
           "'2026-11-18T10:00:00.000000Z', ?, ?)")
    db.execute(ins, ("old-issued", "regular", "issued", None))
    db.execute(ins, ("old-done", "challenge", "correct", "2026-11-18T10:05:00.000000Z"))
    db.close()
    st = Store(p)
    assert st.get_kv("schema_version") == "3"
    for aid in ("old-issued", "old-done"):
        a = st.get_attempt(aid)
        assert a is not None
        assert a.progress is None
        assert a.rev == 0


def test_update_attempt_bumps_rev(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("a1"))
    st.update_attempt("a1", wrong_count=1)
    st.update_attempt("a1", hints_used=1)
    assert st.get_attempt("a1").rev == 2


def test_update_attempt_rejects_rev(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("a1"))
    with pytest.raises(ValueError):
        st.update_attempt("a1", rev=5)


def test_progress_roundtrip(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("a1"))
    assert st.get_attempt("a1").progress is None
    prog = {"step": 1, "done": [True, False], "note": "é"}
    st.update_attempt("a1", progress=prog)
    assert st.get_attempt("a1").progress == prog
    st.update_attempt("a1", progress=None)
    assert st.get_attempt("a1").progress is None


def test_v4_db_rejected_before_ddl(tmp_path):
    p = tmp_path / "a.db"
    db = sqlite3.connect(str(p), isolation_level=None)
    db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO kv VALUES ('schema_version', '4')")
    db.close()
    with pytest.raises(SchemaTooNewError):
        Store(p)
    db = sqlite3.connect(str(p))
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    db.close()
    assert names == ["kv"]


def test_one_issued_attempt_per_device_and_tier(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("a1"))
    with pytest.raises(sqlite3.IntegrityError):
        st.insert_attempt(mk("a2"))
    st.insert_attempt(mk("a3", tier="challenge"))
    st.insert_attempt(mk("a4", state="correct"))  # non-issued rows are not limited
    st.insert_attempt(mk("a5", device="other"))


def test_one_challenge_per_day(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("c1", tier="challenge", state="correct"))
    with pytest.raises(sqlite3.IntegrityError):
        st.insert_attempt(mk("c2", tier="challenge", state="skipped"))
    st.insert_attempt(mk("c3", tier="challenge", day=D + timedelta(days=1)))


def test_credit_ref_unique(tmp_path):
    st = Store(tmp_path / "a.db")
    st.add_credit(D, 5, "quiz", "n", t(10), ref="r1")
    with pytest.raises(sqlite3.IntegrityError):
        st.add_credit(D, 5, "quiz", "n", t(11), ref="r1")


def test_attempt_roundtrip_and_update(tmp_path):
    st = Store(tmp_path / "a.db")
    a = mk("a1", nominal_minutes=5.0)
    st.insert_attempt(a)
    assert st.get_attempt("a1") == a
    assert st.get_attempt("nope") is None
    assert st.open_attempt("tv", "regular") == a
    assert st.open_attempt("tv", "challenge") is None
    st.update_attempt("a1", state="correct", finished_utc=t(10, 1), wrong_count=2,
                      last_wrong_utc=t(10, 0, 30), credited_minutes=5.0, outcome="ok")
    got = st.get_attempt("a1")
    assert got.state == "correct" and got.finished_utc == t(10, 1) and got.wrong_count == 2
    assert got.last_wrong_utc == t(10, 0, 30)
    assert st.open_attempt("tv", "regular") is None
    with pytest.raises(ValueError):
        st.update_attempt("a1", **{"state='x' --": 1})
    with pytest.raises(ValueError):
        st.update_attempt("a1", nonexistent=1)


def test_attempt_queries(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("a1", state="correct", template="t1", finished_utc=t(9), wrong_count=1,
                         credited_minutes=3.0))
    st.insert_attempt(mk("a2", state="correct", template="t1", finished_utc=t(11), wrong_count=2,
                         credited_minutes=2.0))
    st.insert_attempt(mk("a3", state="exhausted", template="t2", finished_utc=t(12), wrong_count=3))
    st.insert_attempt(mk("a4", device="other", tier="challenge", day=D + timedelta(days=1)))
    assert {a.id for a in st.attempts_on(D)} == {"a1", "a2", "a3"}
    assert st.count_attempts("tv", D) == 3
    assert st.count_attempts("tv", D, state="correct") == 2
    assert st.wrong_total("tv", D) == 6
    assert st.wrong_total("zzz", D) == 0
    st.add_credit(D, 4, "quiz", "", t(10))
    st.add_credit(D, 9, "free", "", t(10))
    assert st.quiz_credit_minutes(D) == 4
    assert st.quiz_credit_minutes(D + timedelta(days=1)) == 0


def test_last_seen_by_template(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("a1", state="correct", template="t1", finished_utc=t(9)))
    st.insert_attempt(mk("a2", state="correct", template="t1", finished_utc=t(11)))
    st.insert_attempt(mk("a3", state="exhausted", template="t2", finished_utc=t(12)))
    st.insert_attempt(mk("a4", state="skipped", template="t3", finished_utc=t(13)))
    st.insert_attempt(mk("a5", day=D - timedelta(days=1), state="expired", template="t7",
                         finished_utc=t(13)))
    st.insert_attempt(mk("a6", device="other", state="correct", template="t4",
                         finished_utc=t(13)))
    st.insert_attempt(mk("a7", tier="challenge", state="correct", template="t5",
                         finished_utc=t(13)))
    st.insert_attempt(mk("a8", day=D + timedelta(days=1), template="t6"))  # still issued
    # every terminal (answer revealed) attempt counts as seen, per device and tier
    assert st.last_seen_by_template("tv", "regular", t(8)) == {
        "t1": t(11), "t2": t(12), "t3": t(13), "t7": t(13)}
    assert st.last_seen_by_template("tv", "regular", t(12)) == {
        "t2": t(12), "t3": t(13), "t7": t(13)}
    assert st.last_seen_by_template("tv", "regular", t(14)) == {}


def test_expire_before(tmp_path):
    st = Store(tmp_path / "a.db")
    st.insert_attempt(mk("old"))
    st.insert_attempt(mk("oldc", tier="challenge"))
    st.insert_attempt(mk("new", device="o2", day=D + timedelta(days=1)))
    assert st.expire_before(D + timedelta(days=1), t(23)) == 2
    a = st.get_attempt("old")
    assert a.state == "expired" and a.finished_utc == t(23)
    assert st.get_attempt("new").state == "issued"


class _FlakyConn:
    """sqlite3.Connection proxy whose COMMIT / ROLLBACK can be made to fail once."""

    def __init__(self, real, fail_commit=False, fail_rollback=False):
        self._real = real
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback

    def execute(self, sql, *args):
        if sql == "COMMIT" and self.fail_commit:
            self.fail_commit = False
            raise sqlite3.OperationalError("disk I/O error")
        if sql == "ROLLBACK" and self.fail_rollback:
            self.fail_rollback = False
            raise sqlite3.OperationalError("rollback failed")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_failed_commit_rolls_back_and_store_stays_usable(tmp_path):
    st = Store(tmp_path / "a.db")
    st._db = _FlakyConn(st._db, fail_commit=True)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        with st.transaction():
            st.set_kv("k", "lost")
    assert not st._db.in_transaction
    st.set_kv("k2", "kept")
    with st.transaction():  # a new explicit txn can start
        st.set_kv("k3", "kept")
    other = Store(tmp_path / "a.db")  # separate connection sees only committed data
    assert other.get_kv("k") is None
    assert other.get_kv("k2") == "kept" and other.get_kv("k3") == "kept"


def test_failed_commit_and_rollback_chains_and_reraises_commit_error(tmp_path):
    st = Store(tmp_path / "a.db")
    st._db = _FlakyConn(st._db, fail_commit=True, fail_rollback=True)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O") as ei:
        with st.transaction():
            st.set_kv("k", "v")
    assert "rollback failed" in str(ei.value.__cause__)
    st._db.execute("ROLLBACK")  # clean up the connection the fake left open


def test_rollback_failure_does_not_mask_original_error(tmp_path):
    st = Store(tmp_path / "a.db")
    st._db = _FlakyConn(st._db, fail_rollback=True)
    with pytest.raises(ValueError, match="boom"):
        with st.transaction():
            raise ValueError("boom")
    st._db.execute("ROLLBACK")
