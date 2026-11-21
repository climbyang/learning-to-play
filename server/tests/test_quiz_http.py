import asyncio
import json
import socket
from dataclasses import replace

import httpx
import pytest

from ltp import cli
from ltp.quiz.bank import QuizBank
from ltp.quiz.http import build_app, run_quiz_server
from quiz_fixtures import BANK_DIR, DEV, Env

TV_IP = "192.168.1.55"
TOKEN = "tok-tv1-secret"
HDR = {"Authorization": f"Bearer {TOKEN}", "X-LTP-API": "2"}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.qcfg = replace(e.qcfg, allowed_ips=frozenset({TV_IP}), tokens={DEV: TOKEN})
    e.build()
    return e


def client(env, ip=TV_IP):
    app = build_app(env.svc, env.qcfg, env.store, env.mon.notify_once, env.clock)
    transport = httpx.ASGITransport(app=app, client=(ip, 5000))
    return httpx.AsyncClient(transport=transport, base_url="http://tv")


async def act(c, env, attempt_id, kind):
    """POST hint/skip with the attempt's current revision."""
    return await c.post(f"/api/attempt/{attempt_id}/{kind}", headers=HDR,
                        json={"rev": env.rev(attempt_id)})


def events(env, kind):
    return [e for e in env.store.recent_events(50) if e.kind == kind]


async def test_wrong_ip_403_and_logged(env):
    async with client(env, ip="10.9.9.9") as c:
        r = await c.get("/api/status", headers=HDR)
    assert r.status_code == 403
    assert "ip=10.9.9.9" in events(env, "quiz_denied")[0].detail


async def test_bad_token_401_token_not_logged(env):
    async with client(env) as c:
        r = await c.get("/api/status", headers={"Authorization": "Bearer wrong-token-xyz",
                                                "X-LTP-API": "2"})
        r2 = await c.get("/api/status", headers={"X-LTP-API": "2"})
    assert r.status_code == 401 and r2.status_code == 401
    dump = json.dumps([(e.kind, e.detail) for e in env.store.recent_events(50)])
    assert "wrong-token-xyz" not in dump and TOKEN not in dump
    assert "why=token" in events(env, "quiz_denied")[0].detail


async def test_version_mismatch_426_notifies_once(env):
    async with client(env) as c:
        for v in ("1", None, "1"):
            h = {"Authorization": f"Bearer {TOKEN}"}
            if v:
                h["X-LTP-API"] = v
            r = await c.get("/api/status", headers=h)
            assert r.status_code == 426 and r.json() == {"error": "version", "server_api": 2}
    assert [m for m in env.sink.msgs if "版本" in m] == ["⚠️ 答题 App 版本与服务器不一致，请重新安装"]


async def test_write_rate_limit_10(env):
    async with client(env) as c:
        codes = [(await c.post("/api/attempt", headers=HDR)).status_code for _ in range(11)]
        assert codes[:10] == [200] * 10 and codes[10] == 429
        r = await c.post("/api/attempt", headers=HDR)
        assert r.json()["error"] == "rate_limited" and isinstance(r.json()["retry_after_s"], int)
        env.clock.advance(seconds=61)
        assert (await c.post("/api/attempt", headers=HDR)).status_code == 200


async def test_read_limit_separate(env):
    async with client(env) as c:
        for _ in range(10):
            assert (await c.post("/api/attempt", headers=HDR)).status_code == 200
        codes = [(await c.get("/api/status", headers=HDR)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


async def test_submit_ignores_client_time_fields(env):
    async with client(env) as c:
        v = (await c.post("/api/attempt", headers=HDR)).json()
        correct = env.answer(type("V", (), {"id": v["id"]}))
        r = await c.post(f"/api/attempt/{v['id']}/submit", headers=HDR, json={
            "rev": v["rev"], "answer": correct, "elapsed_s": 9999, "time": 9999, "issued_utc": "2000-01-01"})
    body = r.json()
    assert r.status_code == 200 and body["state"] == "correct"
    assert body["outcome"] == "too_fast" and body["credited_minutes"] == 0


async def test_bad_json_400(env):
    async with client(env) as c:
        v = (await c.post("/api/attempt", headers=HDR)).json()
        url = f"/api/attempt/{v['id']}/submit"
        bad = [
            await c.post(url, headers=HDR, content=b"{not json"),
            await c.post(url, headers=HDR, json={"rev": v["rev"], "answer": 5}),
            await c.post(url, headers=HDR, json={"rev": v["rev"]}),
            await c.post(url, headers=HDR, json=["a"]),
        ]
    assert [r.status_code for r in bad] == [400] * 4


async def test_result_lookup_after_submit(env):
    async with client(env) as c:
        v = (await c.post("/api/attempt", headers=HDR)).json()
        env.clock.advance(seconds=70)
        correct = env.store.get_attempt(v["id"]).answer
        done = (await c.post(f"/api/attempt/{v['id']}/submit", headers=HDR,
                             json={"rev": v["rev"], "answer": correct})).json()
        got = await c.get(f"/api/attempt/{v['id']}", headers=HDR)
        missing = await c.get("/api/attempt/nope", headers=HDR)
    assert got.status_code == 200 and got.json() == done and done["outcome"] == "credited"
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


async def test_rejections_map_to_status(env):
    async with client(env) as c:
        assert (await c.post("/api/attempt?tier=bogus", headers=HDR)).status_code == 400
        v = (await c.post("/api/attempt?tier=challenge", headers=HDR)).json()
        r = await act(c, env, v['id'], "skip")
    assert r.status_code == 409 and r.json()["code"] == "no_skip_challenge"


async def test_unavailable_maps_503(env):
    env.store.set_kv("locked", "1")
    async with client(env) as c:
        r = await c.post("/api/attempt", headers=HDR)
        s = await c.get("/api/status", headers=HDR)
    assert r.status_code == 503
    assert r.json() == {"error": "unavailable", "reason": "locked"}
    assert s.status_code == 200 and s.json()["available"] is False


async def test_stale_rev_retry_while_paused_is_200(tmp_path):
    from test_quiz_passage import make_env, numeric
    env = make_env(tmp_path, [numeric("n1"), numeric("n2")])
    env.qcfg = replace(env.qcfg, allowed_ips=frozenset({TV_IP}), tokens={DEV: TOKEN})
    env.build()
    day = env.clock().astimezone(env.settings.timezone).date()
    async with client(env) as c:
        while True:
            v = (await c.post("/api/attempt", headers=HDR)).json()
            a = env.store.get_attempt(v["id"])
            wrong = (next(o["id"] for o in v["options"] if o["id"] != a.answer)
                     if v["answer_type"] == "choice" else str(int(float(a.answer)) + 1))
            body = {"rev": v["rev"], "answer": wrong}
            r = await c.post(f"/api/attempt/{v['id']}/submit", headers=HDR, json=body)
            assert r.status_code == 200
            if env.store.wrong_total(DEV, day) >= env.qcfg.wrong_pause_threshold:
                break
            env.clock.advance(seconds=61)
        again = await c.post(f"/api/attempt/{v['id']}/submit", headers=HDR, json=body)
        fresh = await c.post(f"/api/attempt/{v['id']}/submit", headers=HDR,
                             json={"rev": r.json()["rev"], "answer": wrong})
        s = await c.get("/api/status", headers=HDR)
    assert again.status_code == 200 and again.json() == r.json()
    assert fresh.status_code == 503 and fresh.json()["reason"] == "paused"
    assert s.json()["reason"] == "paused"
    assert s.json()["retry_after_s"] == fresh.json()["retry_after_s"] > 0


async def test_responses_never_contain_answer_before_terminal(env):
    """Every bank item, every non-terminal response: no answer/explanation keys or text."""
    count = 0
    async with client(env) as c:
        for tier in ("regular", "challenge"):
            for inst in env.bank.instances(tier):
                def issue(ctx, inst=inst, tier=tier):
                    env.store.expire_before(ctx.day, ctx.now)
                    return env.svc._issue(DEV, tier, inst, ctx).id
                # fresh rate-limit window per item; challenge is one per device per day
                env.clock.advance(days=1) if tier == "challenge" else env.clock.advance(seconds=61)
                attempt_id = await env.mon.quiz_txn(issue)
                replies = [await c.get(f"/api/attempt/{attempt_id}", headers=HDR)]
                for _ in range(len(inst.hints)):
                    replies.append(await act(c, env, attempt_id, "hint"))
                for r in replies:
                    body = r.json()
                    assert r.status_code == 200 and body["state"] == "issued"
                    assert "answer" not in body
                    assert body["explanation"] is None and body["correct_answer"] is None
                    if inst.answer_type != "choice":
                        assert inst.answer not in r.text.replace(inst.prompt, "")
                    if inst.explanation:
                        assert inst.explanation not in r.text
                env.store.update_attempt(attempt_id, state="skipped")  # free the open slot
                count += 1
    assert count == len(env.bank.instances("regular")) + len(env.bank.instances("challenge"))


async def test_choice_hints_not_leaking_answer_key(env):
    async with client(env) as c:
        v = (await c.post("/api/attempt", headers=HDR)).json()
        h = await act(c, env, v['id'], "hint")
    assert h.status_code == 200
    assert set(h.json()) == {"id", "rev", "tier", "state", "prompt", "figure", "answer_type",
                             "options", "hints", "hints_total", "tries_left", "skips_left",
                             "outcome", "credited_minutes", "nominal_minutes",
                             "explanation", "correct_answer", "passage", "stage", "step",
                             "parts", "vocab", "parts_right", "parts_total"}
    assert h.json()["correct_answer"] is None and h.json()["passage"] is None


def _free_port_busy():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


async def test_bind_failure_does_not_stop_daemon(env):
    busy, port = _free_port_busy()
    try:
        env.mon.quiz_app_id = env.qcfg.app_id
        cfg = replace(env.qcfg, bind="127.0.0.1", port=port)
        await asyncio.wait_for(
            run_quiz_server(env.svc, env.mon, cfg, env.store, env.mon.notify_once, env.clock), 15)
        assert env.mon.quiz_app_id is None
        assert len([m for m in env.sink.msgs if "答题服务启动失败" in m]) == 1
        # second failure the same day: no second alert
        await asyncio.wait_for(
            run_quiz_server(env.svc, env.mon, cfg, env.store, env.mon.notify_once, env.clock), 15)
        assert len([m for m in env.sink.msgs if "答题服务启动失败" in m]) == 1
    finally:
        busy.close()


async def test_missing_tokens_is_startup_failure_not_crash(env):
    env.mon.quiz_app_id = env.qcfg.app_id
    cfg = replace(env.qcfg, tokens={})
    await run_quiz_server(env.svc, env.mon, cfg, env.store, env.mon.notify_once, env.clock)
    assert env.mon.quiz_app_id is None
    assert any("QUIZ_TOKENS" in m for m in env.sink.msgs)


async def test_server_serves_and_stops_on_cancel(env):
    s, port = _free_port_busy()
    s.close()
    cfg = replace(env.qcfg, bind="127.0.0.1", port=port, allowed_ips=frozenset({"127.0.0.1"}))
    task = asyncio.create_task(
        run_quiz_server(env.svc, env.mon, cfg, env.store, env.mon.notify_once, env.clock))
    try:
        async with httpx.AsyncClient() as c:
            for _ in range(50):
                try:
                    r = await c.get(f"http://127.0.0.1:{port}/api/status", headers=HDR)
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("server did not start")
        assert r.status_code == 200
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_bank_error_disables_quiz_only(env, tmp_path):
    bad = tmp_path / "badbank"
    bad.mkdir()
    (bad / "x.json").write_text("{}", encoding="utf-8")
    s = replace(env.settings, quiz=replace(env.qcfg, bank_dir=bad))
    s2, bank, problems = cli._prepare_quiz(s)
    assert bank is None and problems and s2 is s
    assert s2.quiz.app_id not in s2.app_rules
    # the same code path the daemon uses to alert; monitor stays usable
    await env.mon.notify_once("quiz_bank_error", "⚠️ 题库校验失败，答题已停用：" + "；".join(problems[:3]))
    assert any("题库校验失败" in m for m in env.sink.msgs)


def test_prepare_quiz_registers_rule_and_disabled_is_noop(env):
    from ltp.rules import AllowedWhen
    s2, bank, problems = cli._prepare_quiz(
        replace(env.settings, quiz=replace(env.qcfg, bank_dir=BANK_DIR)))
    assert bank is not None and not problems
    rule = s2.app_rules[env.qcfg.app_id]
    assert rule.counts is False and rule.allowed_when is AllowedWhen.QUOTA_EXHAUSTED
    off, nobank, _ = cli._prepare_quiz(env.settings)
    assert off is env.settings and nobank is None


async def test_skip_limit_is_logged_for_digest(env):
    async with client(env) as c:
        for _ in range(env.qcfg.skips_per_day):
            v = (await c.post("/api/attempt?tier=regular", headers=HDR)).json()
            assert (await act(c, env, v['id'], "skip")).status_code == 200
        v = (await c.post("/api/attempt?tier=regular", headers=HDR)).json()
        r = await act(c, env, v['id'], "skip")
    assert r.status_code == 409 and r.json()["code"] == "skip_limit"
    assert len(events(env, "quiz_skip_limit")) == 1


async def test_forwarded_for_header_cannot_spoof_allowlist(env):
    # Real uvicorn: a request from 127.0.0.1 must stay 127.0.0.1 even with X-Forwarded-For.
    s, port = _free_port_busy()
    s.close()
    cfg = replace(env.qcfg, bind="127.0.0.1", port=port, allowed_ips=frozenset({TV_IP}))
    task = asyncio.create_task(
        run_quiz_server(env.svc, env.mon, cfg, env.store, env.mon.notify_once, env.clock))
    try:
        async with httpx.AsyncClient() as c:
            for _ in range(50):
                try:
                    r = await c.get(f"http://127.0.0.1:{port}/api/status",
                                    headers={**HDR, "X-Forwarded-For": TV_IP})
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("server did not start")
        assert r.status_code == 403
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_prepare_quiz_unexpected_bank_error_disables_quiz_only(env, monkeypatch):
    def boom(_dir, _reviews):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli.QuizBank, "load", staticmethod(boom))
    s = replace(env.settings, quiz=replace(env.qcfg, bank_dir=BANK_DIR))
    s2, bank, problems = cli._prepare_quiz(s)
    assert bank is None and s2 is s
    assert len(problems) == 1 and "RuntimeError" in problems[0] and "kaboom" in problems[0]


async def test_server_failure_revokes_quiz_exemption(env):
    s2, _bank, _ = cli._prepare_quiz(
        replace(env.settings, quiz=replace(env.qcfg, bank_dir=BANK_DIR)))
    env.mon.s = s2
    env.mon.enable_quiz(env.qcfg.app_id, None)
    assert env.qcfg.app_id in env.mon.s.app_rules
    cfg = replace(env.qcfg, tokens={})
    await run_quiz_server(env.svc, env.mon, cfg, env.store, env.mon.notify_once, env.clock)
    assert env.mon.quiz_app_id is None
    assert env.qcfg.app_id not in env.mon.s.app_rules


async def test_quiz_denied_events_throttled_per_ip(env):
    async with client(env, ip="10.9.9.9") as bad, client(env, ip="10.9.9.8") as other:
        for _ in range(20):
            assert (await bad.get("/api/status", headers=HDR)).status_code == 403
        assert (await other.get("/api/status", headers=HDR)).status_code == 403
        assert len(events(env, "quiz_denied")) == 2  # one per IP, not one per request
        env.clock.advance(seconds=61)
        assert (await bad.get("/api/status", headers=HDR)).status_code == 403
        assert len(events(env, "quiz_denied")) == 3


async def test_quiz_denied_token_events_throttled_but_still_401(env):
    async with client(env) as c:
        codes = [(await c.get("/api/status", headers={"X-LTP-API": "2"})).status_code
                 for _ in range(15)]
    assert codes == [401] * 15
    assert len(events(env, "quiz_denied")) == 1


async def test_missing_rev_400(env):
    async with client(env) as c:
        v = (await c.post("/api/attempt", headers=HDR)).json()
        base = f"/api/attempt/{v['id']}"
        calls = [
            await c.post(f"{base}/hint", headers=HDR),
            await c.post(f"{base}/hint", headers=HDR, json={}),
            await c.post(f"{base}/skip", headers=HDR, json={"rev": "1"}),
            await c.post(f"{base}/skip", headers=HDR, json={"rev": True}),
            await c.post(f"{base}/submit", headers=HDR, json={"answer": "1"}),
            await c.post(f"{base}/submit", headers=HDR, json={"answer": "1", "rev": 1.5}),
        ]
    assert [r.status_code for r in calls] == [400] * 6
    assert all(r.json()["code"] == "bad_input" for r in calls)
    assert env.store.get_attempt(v["id"]).rev == v["rev"]


async def test_open_endpoint_read_class(env):
    async with client(env) as c:
        empty = await c.get("/api/attempts/open", headers=HDR)
        v = (await c.post("/api/attempt", headers=HDR)).json()
        got = await c.get("/api/attempts/open", headers=HDR)
        # 10 writes/min would be exhausted by writes; reads have their own larger budget.
        codes = [(await c.get("/api/attempts/open", headers=HDR)).status_code
                 for _ in range(env.qcfg.write_rate_per_min + 5)]
    assert empty.json() == {"attempts": []}
    assert [a["id"] for a in got.json()["attempts"]] == [v["id"]]
    assert got.json()["attempts"][0]["rev"] == v["rev"]
    assert codes == [200] * len(codes)


async def test_version_1_rejected_426(env):
    async with client(env) as c:
        r = await c.get("/api/attempts/open",
                        headers={"Authorization": f"Bearer {TOKEN}", "X-LTP-API": "1"})
    assert r.status_code == 426 and r.json()["server_api"] == 2


# -- CORS (the app runs from file://, Origin "null") -----------------------------

PREFLIGHT = {"Origin": "null", "Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "authorization,x-ltp-api,content-type"}
API_PATHS = ("/api/status", "/api/attempts/open", "/api/attempt", "/api/attempt/x",
             "/api/attempt/x/hint", "/api/attempt/x/submit", "/api/attempt/x/skip",
             "/api/attempt/x/review")


async def test_preflight_from_app_origin_is_204_on_every_route(env):
    async with client(env) as c:
        for path in API_PATHS:
            r = await c.options(path, headers=PREFLIGHT)
            assert r.status_code == 204, path
            assert r.headers["access-control-allow-origin"] == "null"
            assert r.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
            assert r.headers["access-control-allow-headers"] == (
                "Authorization, X-LTP-API, Content-Type")
            assert r.headers["access-control-max-age"] == "600"
        no_origin = await c.options("/api/status")
    assert no_origin.status_code == 204
    assert "access-control-allow-origin" not in no_origin.headers
    # A preflight never reaches the service: nothing is issued.
    assert env.store.attempts_on(env.mon.business_day(env.clock())) == []


async def test_preflight_rejects_web_origin_and_foreign_ip(env):
    async with client(env) as c:
        web = await c.options("/api/status",
                              headers={**PREFLIGHT, "Origin": "http://evil.example"})
    async with client(env, ip="10.9.9.9") as c:
        far = [await c.options("/api/status", headers=PREFLIGHT) for _ in range(3)]
    assert web.status_code == 403 and "access-control-allow-origin" not in web.headers
    assert [r.status_code for r in far] == [403] * 3
    assert all("access-control-allow-origin" not in r.headers for r in far)
    assert len([e for e in events(env, "quiz_denied") if "ip=10.9.9.9" in e.detail]) == 1


async def test_actual_responses_carry_allow_origin_for_app_only(env):
    async with client(env) as c:
        ok = await c.get("/api/status", headers={**HDR, "Origin": "null"})
        err = await c.get("/api/status", headers={"Origin": "null", "X-LTP-API": "2"})
        web = await c.get("/api/status", headers={**HDR, "Origin": "http://evil.example"})
        bare = await c.get("/api/status", headers=HDR)
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "null"
    assert err.status_code == 401 and err.headers["access-control-allow-origin"] == "null"
    assert web.status_code == 200 and "access-control-allow-origin" not in web.headers
    assert "access-control-allow-origin" not in bare.headers
