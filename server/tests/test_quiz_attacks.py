"""Attack suite for the quiz HTTP API.

Everything goes through the HTTP layer against a real SQLite file. Concurrency uses
asyncio.gather; the monitor lock must serialize every outcome.

The default write limit is 10/min, which would mask concurrency results, so the
`attack_app` factory raises the limits far above what any test sends. Only
`test_default_write_limit_still_429` uses the stock limit.
"""
import asyncio
import sqlite3
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from ltp.quiz.http import build_app
from quiz_fixtures import DEV, Env

IP_A = "192.168.1.55"
IP_EVIL = "10.9.9.9"
DEV_B = "tv2"
TOK_A = "tok-tv1-secret"
TOK_B = "tok-tv2-secret"
HA = {"Authorization": f"Bearer {TOK_A}", "X-LTP-API": "2"}
HB = {"Authorization": f"Bearer {TOK_B}", "X-LTP-API": "2"}
HIGH = 100_000


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.qcfg = replace(e.qcfg, allowed_ips=frozenset({IP_A}),
                     tokens={DEV: TOK_A, DEV_B: TOK_B})
    e.build()
    return e


def attack_app(env, *, high_limits=True, ip=IP_A):
    cfg = env.qcfg
    if high_limits:
        cfg = replace(cfg, write_rate_per_min=HIGH, read_rate_per_min=HIGH)
    app = build_app(env.svc, cfg, env.store, env.mon.notify_once, env.clock)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)),
                             base_url="http://tv")


def quiz_credit_rows(env):
    """Raw (minutes, ref) rows read through a separate connection to the real file."""
    con = sqlite3.connect(env.db)
    try:
        return con.execute("SELECT minutes, ref FROM credits WHERE source='quiz' ORDER BY id"
                           ).fetchall()
    finally:
        con.close()


async def issue(c, headers=HA, tier="regular"):
    r = await c.post(f"/api/attempt?tier={tier}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def answer_of(env, attempt_id):
    return env.store.get_attempt(attempt_id).answer


async def current_rev(c, attempt_id, headers=HA):
    """Latest revision as the client would read it; 0 when the attempt is not visible."""
    r = await c.get(f"/api/attempt/{attempt_id}", headers=headers)
    return r.json()["rev"] if r.status_code == 200 else 0


async def act(c, attempt_id, kind, headers=HA, rev=None, **extra):
    """POST hint/skip/submit with a revision (fresh unless given)."""
    if rev is None:
        rev = await current_rev(c, attempt_id, headers)
    return await c.post(f"/api/attempt/{attempt_id}/{kind}", headers=headers,
                        json={"rev": rev, **extra})


async def submit(c, attempt_id, answer, headers=HA, rev=None):
    return await act(c, attempt_id, "submit", headers, rev, answer=answer)


async def test_concurrent_same_rev_submit_applies_once(env):
    """20 submits carrying the same rev: the rev guard lets exactly one write through."""
    async with attack_app(env) as c:
        v = await issue(c)
        env.clock.advance(seconds=60)
        correct = answer_of(env, v["id"])
        rs = await asyncio.gather(*[submit(c, v["id"], correct, rev=v["rev"])
                                    for _ in range(20)])
    assert [r.status_code for r in rs] == [200] * 20
    assert {r.json()["state"] for r in rs} == {"correct"}
    assert env.store.get_attempt(v["id"]).rev == v["rev"] + 1  # exactly one write applied
    assert quiz_credit_rows(env) == [(10.0, f"quiz:{v['id']}")]
    assert sum(1 for m in env.sink.msgs if "答对" in m) == 1


async def test_concurrent_double_submit_credits_once(env):
    """20 submits each reading the then-current rev: terminal-state idempotence under the lock."""
    async with attack_app(env) as c:
        v = await issue(c)
        env.clock.advance(seconds=60)
        correct = answer_of(env, v["id"])
        rs = await asyncio.gather(*[submit(c, v["id"], correct) for _ in range(20)])
    assert [r.status_code for r in rs] == [200] * 20
    assert {r.json()["state"] for r in rs} == {"correct"}
    assert {r.json()["credited_minutes"] for r in rs} == {10.0}
    assert quiz_credit_rows(env) == [(10.0, f"quiz:{v['id']}")]
    assert sum(1 for m in env.sink.msgs if "答对" in m) == 1


async def test_replay_terminal_submit_no_side_effect(env):
    async with attack_app(env) as c:
        v = await issue(c)
        env.clock.advance(seconds=60)
        correct = answer_of(env, v["id"])
        first = (await submit(c, v["id"], correct)).json()
        credits_before = quiz_credit_rows(env)
        msgs_before = list(env.sink.msgs)
        attempt_before = env.store.get_attempt(v["id"])
        env.clock.advance(seconds=300)
        replays = [await submit(c, v["id"], correct),
                   await submit(c, v["id"], "definitely-wrong"),
                   await act(c, v['id'], "hint", HA),
                   await act(c, v['id'], "skip", HA)]
    assert [r.status_code for r in replays] == [200] * 4
    assert all(r.json() == first for r in replays)
    assert quiz_credit_rows(env) == credits_before
    assert env.sink.msgs == msgs_before
    assert env.store.get_attempt(v["id"]) == attempt_before


async def test_forged_attempt_id_404(env):
    async with attack_app(env) as c:
        await issue(c)
        calls = [await c.get("/api/attempt/forged-id", headers=HA),
                 await act(c, "forged-id", "hint", HA),
                 await act(c, "forged-id", "skip", HA),
                 await submit(c, "forged-id", "x"),
                 await submit(c, "..%2F..%2Fetc%2Fpasswd", "x"),
                 await submit(c, "%27%20OR%201%3D1%20--", "x")]
    assert [r.status_code for r in calls] == [404] * 6
    # Odd ids may be refused by the router itself (plain 404); plain forged ids reach the service.
    assert all(r.json()["code"] == "not_found" for r in calls[:4])
    assert quiz_credit_rows(env) == []


async def test_other_device_attempt_404(env):
    async with attack_app(env) as c:
        v = await issue(c, HA)
        env.clock.advance(seconds=60)
        before = env.store.get_attempt(v["id"])
        calls = [await c.get(f"/api/attempt/{v['id']}", headers=HB),
                 await act(c, v['id'], "hint", HB),
                 await act(c, v['id'], "skip", HB),
                 await submit(c, v["id"], answer_of(env, v["id"]), HB)]
        # Device B's own attempt is a different one, so A's is never handed to B.
        vb = await issue(c, HB)
        assert [r.status_code for r in calls] == [404] * 4
        assert vb["id"] != v["id"]
        assert env.store.get_attempt(v["id"]) == before
        assert quiz_credit_rows(env) == []
        # The owner can still finish it.
        done = (await submit(c, v["id"], answer_of(env, v["id"]))).json()
    assert done["state"] == "correct"


async def test_hint_and_submit_same_rev_race_one_wins(env):
    """Both calls carry the rev read before the race: exactly one applies, the other no-ops."""
    async with attack_app(env) as c:
        for hint_first in (True, False, True, False):
            v = await issue(c)
            env.clock.advance(seconds=60)
            hint = act(c, v["id"], "hint", rev=v["rev"])
            sub = submit(c, v["id"], answer_of(env, v["id"]), rev=v["rev"])
            rs = await asyncio.gather(*([hint, sub] if hint_first else [sub, hint]))
            assert [r.status_code for r in rs] == [200, 200]
            a = env.store.get_attempt(v["id"])
            assert a.rev == v["rev"] + 1
            # Hint won: hint applied, submit stale. Submit won: answer credited, hint stale.
            assert (a.state, a.hints_used) in {("issued", 1), ("correct", 0)}, (a.state, a.hints_used)
            if a.state == "issued":
                done = await submit(c, v["id"], answer_of(env, v["id"]))
                assert done.json()["state"] == "correct"
                assert env.store.get_attempt(v["id"]).credited_minutes == 5.0
    rows = quiz_credit_rows(env)
    assert len(rows) == 4 and len({r[1] for r in rows}) == 4
    assert all(m in (5.0, 10.0) for m, _ in rows)


async def test_hint_and_submit_race_halves_reward(env):
    """Each call reads a fresh rev; submit retries while the attempt is still open. The lock
    serializes them, so hint-first (5 min) or submit-first (10 min) are the only outcomes."""
    async def submit_retry(c, attempt_id, answer):
        for _ in range(3):
            r = await submit(c, attempt_id, answer)
            if r.json()["state"] != "issued":
                return r
        return r

    async with attack_app(env) as c:
        for hint_first in (True, False, True, False):
            v = await issue(c)
            env.clock.advance(seconds=60)
            hint = act(c, v["id"], "hint")
            sub = submit_retry(c, v["id"], answer_of(env, v["id"]))
            await asyncio.gather(*([hint, sub] if hint_first else [sub, hint]))
            a = env.store.get_attempt(v["id"])
            assert a.state == "correct"
            assert (a.hints_used, a.credited_minutes) in {(1, 5.0), (0, 10.0)},                 (a.hints_used, a.credited_minutes)
            # A hint arriving after the answer cannot change anything.
            late = await act(c, v["id"], "hint")
            assert late.status_code == 200
            assert env.store.get_attempt(v["id"]).hints_used == a.hints_used
    rows = quiz_credit_rows(env)
    assert len(rows) == 4 and len({r[1] for r in rows}) == 4
    assert all(m in (5.0, 10.0) for m, _ in rows)


async def test_skip_limit_under_concurrency(env):
    async def worker(c):
        out = []
        for _ in range(3):
            v = await issue(c)
            out.append(await act(c, v['id'], "skip", HA))
        return out

    async with attack_app(env) as c:
        results = await asyncio.gather(*[worker(c) for _ in range(10)])
    skipped = [a for a in env.store.attempts_on(env.mon.business_day(env.clock()))
               if a.state == "skipped"]
    assert len(skipped) == env.qcfg.skips_per_day == 3
    flat = [r for rs in results for r in rs]
    assert any(r.status_code == 409 and r.json()["code"] == "skip_limit" for r in flat)
    assert env.store.count_events("quiz_skip_limit", env.clock() - timedelta(days=1)) >= 1


async def test_partial_cap_credit(env):
    day = env.mon.business_day(env.clock())
    # cap 60 - free 20 = 40 minutes of quiz room; leave exactly 3.
    env.store.add_credit(day, 37, "quiz", "seed", env.clock(), ref="seed")
    async with attack_app(env) as c:
        v = await issue(c)
        env.clock.advance(seconds=60)
        r = (await submit(c, v["id"], answer_of(env, v["id"]))).json()
    assert r["outcome"] == "cap" and r["credited_minutes"] == 3.0 and r["nominal_minutes"] == 10.0
    assert quiz_credit_rows(env)[-1] == (3.0, f"quiz:{v['id']}")
    assert sum(m for m, _ in quiz_credit_rows(env)) == 40.0
    assert any("只计入 3/10" in m for m in env.sink.msgs)
    assert not any("+10 分钟" in m for m in env.sink.msgs)


async def test_cross_midnight_submit_no_credit(tmp_path):
    # Curfew off so the only thing between issue and submit is the business-day boundary.
    e = Env(tmp_path, "mode: enforce\ncurfew:\n  enabled: false\n")
    e.qcfg = replace(e.qcfg, allowed_ips=frozenset({IP_A}), tokens={DEV: TOK_A})
    e.build()
    e.clock.advance(hours=13, minutes=50)  # 23:50 local (UTC+10)
    async with attack_app(e) as c:
        v = await issue(c)
        issued_day = e.store.get_attempt(v["id"]).day
        e.clock.advance(minutes=11)  # 00:01 local, next business day, past min seconds
        assert e.mon.business_day(e.clock()) > issued_day
        r = await submit(c, v["id"], answer_of(e, v["id"]))
    assert r.status_code == 200 and r.json()["state"] == "expired"
    assert r.json()["credited_minutes"] == 0
    assert quiz_credit_rows(e) == []
    assert e.store.get_attempt(v["id"]).state == "expired"


async def test_lock_and_curfew_reject_over_http(env):
    async with attack_app(env) as c:
        v = await issue(c)
        env.clock.advance(seconds=60)
        correct = answer_of(env, v["id"])
        env.store.set_kv("locked", "1")
        locked = [await c.post("/api/attempt", headers=HA),
                  await submit(c, v["id"], correct),
                  await act(c, v['id'], "hint", HA),
                  await act(c, v['id'], "skip", HA)]
        st = await c.get("/api/status", headers=HA)
        env.store.set_kv("locked", "0")
        env.clock.advance(hours=11)  # 21:00 local, inside the 20:30-07:00 curfew
        curfew = [await c.post("/api/attempt", headers=HA),
                  await submit(c, v["id"], correct),
                  await act(c, v['id'], "hint", HA),
                  await act(c, v['id'], "skip", HA)]
    assert [r.status_code for r in locked] == [503] * 4
    assert all(r.json()["reason"] == "locked" for r in locked)
    assert st.status_code == 200 and st.json()["available"] is False
    assert [r.status_code for r in curfew] == [503] * 4
    assert all(r.json()["reason"] == "curfew" for r in curfew)
    assert quiz_credit_rows(env) == []
    assert env.store.get_attempt(v["id"]).state == "issued"


async def test_restart_mid_attempt_over_http(env):
    async with attack_app(env) as c:
        v = await issue(c)
        hinted = (await act(c, v['id'], "hint", HA)).json()
    assert len(hinted["hints"]) == 1
    env.clock.advance(seconds=60)
    env.restart()  # new Store, Monitor, QuizService on the same SQLite file
    async with attack_app(env) as c:
        again = await issue(c)
        assert again["id"] == v["id"] and again["hints"] == hinted["hints"]
        assert again["options"] == v["options"]
        correct = answer_of(env, v["id"])
        rs = await asyncio.gather(*[submit(c, v["id"], correct) for _ in range(5)])
    assert {r.json()["state"] for r in rs} == {"correct"}
    assert quiz_credit_rows(env) == [(5.0, f"quiz:{v['id']}")]
    env.restart()
    async with attack_app(env) as c:
        replay = await submit(c, v["id"], correct)
    assert replay.json()["state"] == "correct"
    assert quiz_credit_rows(env) == [(5.0, f"quiz:{v['id']}")]


async def test_wrong_source_ip_cannot_use_valid_token(env):
    async with attack_app(env) as c:
        v = await issue(c)
        env.clock.advance(seconds=60)
        correct = answer_of(env, v["id"])
    async with attack_app(env, ip=IP_EVIL) as evil:
        calls = [await evil.get("/api/status", headers=HA),
                 await evil.post("/api/attempt", headers=HA),
                 await evil.get(f"/api/attempt/{v['id']}", headers=HA),
                 await submit(evil, v["id"], correct)]
    assert [r.status_code for r in calls] == [403] * 4
    assert all(correct not in r.text for r in calls)
    assert quiz_credit_rows(env) == []
    assert env.store.get_attempt(v["id"]).state == "issued"
    denied = [e for e in env.store.recent_events(50) if e.kind == "quiz_denied"]
    assert len(denied) == 1 and "ip=10.9.9.9" in denied[0].detail  # throttled: one per IP per minute
    assert not any(TOK_A in e.detail for e in denied)


async def test_default_write_limit_still_429(env):
    async with attack_app(env, high_limits=False) as c:
        codes = [(await c.post("/api/attempt", headers=HA)).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]
