import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from ltp import devmode
from ltp.config import DevModeSettings, load_settings
from ltp.devmode import DevModeClient, DevModeError, DevModeJob, cached_token, read_token_over_ssh
from ltp.monitor import Monitor
from ltp.store import Store
from ltp.tv.fake import FakeTV

TOKEN = "ab12" * 16
OTHER_TOKEN = "cd34" * 16
TZ = ZoneInfo("Etc/GMT-10")
DM_APP = "com.palmdts.devmode"
QUIZ_APP = "com.learningtoplay.quiz"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class Sink:
    def __init__(self):
        self.msgs: list[str] = []

    async def send(self, text, buttons=None):
        self.msgs.append(text)


class FakeLG:
    """MockTransport handler: remaining hours per query, optional failures."""

    def __init__(self, hours=100.0):
        self.hours = hours
        self.checks = 0
        self.resets = 0
        self.reset_fail = False
        self.check_fail = False
        self.reset_restores = True
        self.fail_check_numbers: set[int] = set()
        self.tokens: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.tokens.append(req.url.params["sessionToken"])
        if req.url.path.endswith("CheckDevModeSession.dev"):
            self.checks += 1
            if self.check_fail or self.checks in self.fail_check_numbers:
                return httpx.Response(500, text="boom")
            total = int(self.hours * 3600)
            msg = f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
            return httpx.Response(200, json={"result": "success", "errorCode": "200",
                                             "errorMsg": msg})
        self.resets += 1
        if self.reset_fail:
            return httpx.Response(200, json={"result": "fail", "errorCode": "400",
                                             "errorMsg": "bad"})
        if self.reset_restores:
            self.hours = 999.99
        return httpx.Response(200, json={"result": "success", "errorCode": "200",
                                         "errorMsg": "GNL"})


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "ltp.db")
    lg = FakeLG()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lg))
    tv = FakeTV({DM_APP: "Developer Mode", QUIZ_APP: "Quiz"})
    sink = Sink()
    state = {"token": TOKEN, "read_error": None, "reads": 0}

    async def alert(key, text):
        # mimic Monitor.notify_once: once per day per key
        k = f"notice:{clock().date()}:{key}"
        if store.get_kv(k) is None:
            store.set_kv(k, "1")
            sink.msgs.append(text)

    async def reader():
        state["reads"] += 1
        if state["read_error"]:
            raise DevModeError(state["read_error"])
        return state["token"]

    s = DevModeSettings()
    client = DevModeClient(http, s.base_url, lambda: cached_token(store))
    job = DevModeJob(s, client, store, tv, alert, reader, clock, TZ, lambda: QUIZ_APP)
    return job, lg, tv, sink, clock, store, state


async def run(job):
    job.kick()
    await job.wait()


async def test_extends_when_under_24h(rig):
    job, lg, tv, sink, clock, store, _ = rig
    lg.hours = 20
    await tv.set()
    await run(job)
    assert lg.resets == 1
    assert sink.msgs == []
    assert store.last_event("devmode_extended") is not None


async def test_no_extend_when_plenty(rig):
    job, lg, tv, sink, *_ = rig
    await tv.set()
    await run(job)
    assert lg.checks == 1 and lg.resets == 0


async def test_extend_failure_alerts_once(rig):
    job, lg, tv, sink, clock, *_ = rig
    lg.hours = 20
    lg.reset_fail = True
    await tv.set()
    await run(job)
    clock.advance(minutes=11)
    await run(job)  # retried (day not marked checked) but same alert suppressed
    assert lg.resets == 2
    assert len(sink.msgs) == 1
    assert sink.msgs[0].startswith("⚠️ 开发者模式剩余 20 小时，续期失败：")
    assert "errorCode=400" in sink.msgs[0]


async def test_under_12h_alerts(rig):
    job, lg, tv, sink, *_ = rig
    lg.hours = 5
    lg.reset_restores = False  # renewal "succeeds" but the session stays short
    await tv.set()
    await run(job)
    assert len(sink.msgs) == 1 and "仍只剩 5 小时" in sink.msgs[0]


async def test_ineffective_renewal_keeps_retrying_one_alert(rig):
    job, lg, tv, sink, clock, store, _ = rig
    lg.hours = 5
    lg.reset_restores = False
    await tv.set()
    await run(job)
    day = clock().astimezone(TZ).date()
    assert store.get_kv(f"devmode:checked:{day}") is None
    clock.advance(minutes=11)
    await run(job)
    assert lg.resets == 2  # retried on a later tick
    assert len(sink.msgs) == 1
    lg.reset_restores = True
    clock.advance(minutes=11)
    await run(job)
    assert store.get_kv(f"devmode:checked:{day}") == "1"


async def test_unconfirmed_renewal_retries_without_extending_again(rig):
    job, lg, tv, sink, clock, store, _ = rig
    lg.hours = 5
    lg.fail_check_numbers = {2}  # the re-query right after extend fails
    await tv.set()
    await run(job)
    day = clock().astimezone(TZ).date()
    assert lg.resets == 1
    assert store.get_kv(f"devmode:checked:{day}") is None
    assert len(sink.msgs) == 1 and sink.msgs[0].startswith("⚠️ 开发者模式已续期但无法确认结果：")
    clock.advance(minutes=11)
    await run(job)  # query first: 999h now, no second extend
    assert lg.resets == 1
    assert store.get_kv(f"devmode:checked:{day}") == "1"
    assert len(sink.msgs) == 1


async def test_checks_once_per_day(rig):
    job, lg, tv, sink, clock, *_ = rig
    await tv.set()
    await run(job)
    clock.advance(hours=1)
    await run(job)
    assert lg.checks == 1
    clock.advance(days=1)
    await run(job)
    assert lg.checks == 2


async def test_query_failure_alerts_and_retries(rig):
    job, lg, tv, sink, clock, store, _ = rig
    lg.check_fail = True
    await tv.set()
    await run(job)
    assert "无法查询开发者模式剩余时间：LG 返回 HTTP 500" in sink.msgs[0]
    await run(job)  # inside the retry backoff: nothing happens
    assert lg.checks == 1
    clock.advance(minutes=11)
    lg.check_fail = False
    await run(job)
    assert lg.checks == 2 and len(sink.msgs) == 1


async def test_cached_token_used_while_tv_off(rig):
    job, lg, tv, sink, clock, store, state = rig
    await tv.set()
    await run(job)
    assert store.get_kv("devmode:token") == TOKEN
    await tv.set(connected=False)
    clock.advance(days=1)
    state["token"] = OTHER_TOKEN
    await run(job)
    assert state["reads"] == 1  # TV off: no SSH read
    assert lg.checks == 2 and set(lg.tokens) == {TOKEN}


async def test_token_refreshed_when_tv_connected(rig):
    job, lg, tv, sink, clock, store, state = rig
    await tv.set()
    await run(job)
    clock.advance(days=1)
    state["token"] = OTHER_TOKEN
    await run(job)
    assert store.get_kv("devmode:token") == OTHER_TOKEN


async def test_no_token_waits_then_alerts_after_24h(rig):
    job, lg, tv, sink, clock, store, state = rig
    await run(job)  # TV never connected, no cache
    assert lg.checks == 0 and sink.msgs == []
    clock.advance(hours=23)
    await run(job)
    assert sink.msgs == []
    clock.advance(hours=2)
    await run(job)
    assert sink.msgs == ["⚠️ 无法读取开发者模式令牌：电视未连接"]
    clock.advance(minutes=11)
    await run(job)
    assert len(sink.msgs) == 1


async def test_ssh_read_failure_reason_in_alert(rig):
    job, lg, tv, sink, clock, store, state = rig
    state["read_error"] = "ssh 退出码 255"
    await tv.set()
    await run(job)
    clock.advance(hours=25)
    await run(job)
    assert sink.msgs == ["⚠️ 无法读取开发者模式令牌：ssh 退出码 255"]


async def test_token_never_in_logs_or_alerts(rig, caplog):
    job, lg, tv, sink, clock, store, _ = rig
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="httpx")  # other tests may have raised its level
    lg.hours = 20
    await tv.set()
    await run(job)
    lg.check_fail = True
    clock.advance(days=1)
    await run(job)
    assert lg.tokens  # token really was sent to LG
    assert "sessionToken=<redacted>" in caplog.text  # httpx's own URL log is scrubbed
    assert TOKEN not in caplog.text
    assert all(TOKEN not in m for m in sink.msgs)
    assert all(TOKEN not in (e.detail or "") for e in store.recent_events(50))


async def test_presence_missing_alerts_once(rig):
    job, lg, tv, sink, clock, store, _ = rig
    tv._titles.pop(DM_APP)
    tv._titles.pop(QUIZ_APP)
    tv._titles["other"] = "Other"
    await tv.set()
    await run(job)
    assert f"⚠️ 电视上找不到 开发者模式应用（{DM_APP}），可能已被删除" in sink.msgs
    assert f"⚠️ 电视上找不到 答题应用（{QUIZ_APP}），可能已被删除" in sink.msgs
    n = len(sink.msgs)
    await run(job)
    assert len(sink.msgs) == n


async def test_presence_present_no_alert(rig):
    job, lg, tv, sink, *_ = rig
    await tv.set()
    await run(job)
    assert sink.msgs == []


async def test_presence_detects_app_removed_after_connect(rig):
    job, lg, tv, sink, clock, store, _ = rig
    await tv.set()
    await run(job)
    assert sink.msgs == []
    tv._titles.pop(DM_APP)  # deleted while the TV stays connected
    clock.advance(days=1)
    await run(job)
    assert sink.msgs == [f"⚠️ 电视上找不到 开发者模式应用（{DM_APP}），可能已被删除"]


async def test_presence_listing_exception_no_alert(rig):
    job, lg, tv, sink, *_ = rig

    async def boom():
        raise ConnectionError("gone")

    tv.list_app_ids = boom
    await tv.set()
    await run(job)
    assert sink.msgs == [] and lg.checks == 1


async def test_presence_skipped_when_listing_unknown(rig):
    job, lg, tv, sink, *_ = rig
    tv.list_fails = True
    await tv.set()
    await run(job)
    assert sink.msgs == []


async def test_no_overlap(rig):
    job, lg, tv, sink, clock, store, state = rig
    gate = asyncio.Event()
    orig = job._read_token

    async def slow():
        await gate.wait()
        return await orig()

    job._read_token = slow
    await tv.set()
    job.kick()
    first = job._task
    await asyncio.sleep(0)
    job.kick()
    job.kick()
    assert job._task is first
    gate.set()
    await job.wait()
    assert lg.checks == 1 and state["reads"] == 1


async def test_crash_is_contained(rig, caplog):
    job, lg, tv, sink, clock, store, _ = rig

    async def boom(*a):
        raise ValueError("kaput")

    job._alert = boom
    lg.check_fail = True
    await tv.set()
    await run(job)  # must not raise
    assert "devmode check crashed" in caplog.text
    assert job._next_try is not None


async def test_monitor_tick_kicks_job(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("mode: observe\n", encoding="utf-8")
    s = load_settings(cfg / "settings.yaml", {})
    clock = Clock()
    store = Store(tmp_path / "ltp.db")
    tv = FakeTV({DM_APP: "x"})
    lg = FakeLG()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lg))
    sink = Sink()
    mon = Monitor(tv, store, sink, s, now=clock)

    async def reader():
        return TOKEN

    client = DevModeClient(http, s.devmode.base_url, lambda: cached_token(store))
    mon.devmode = DevModeJob(s.devmode, client, store, tv, mon.notify_once, reader, clock,
                             s.timezone)
    await tv.set()
    await mon.tick()
    await mon.devmode.wait()
    await mon.tick()
    await mon.devmode.wait()
    assert lg.checks == 1


# -- client / parsing -------------------------------------------------------
@pytest.mark.parametrize("text,hours", [("987:38:28", 987), ("0:00:05", 0), ("999:59:59", 999)])
def test_parse_remaining(text, hours):
    assert devmode.parse_remaining(text).total_seconds() // 3600 == hours


@pytest.mark.parametrize("text", ["", "GNL", "1:2:3", "12:61:00", "x:00:00"])
def test_parse_remaining_rejects(text):
    with pytest.raises(DevModeError):
        devmode.parse_remaining(text)


@pytest.mark.parametrize("resp", [
    httpx.Response(503, text="x"),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"result": "fail", "errorCode": "1", "errorMsg": ""}),
    httpx.Response(200, json={"result": "success", "errorCode": "200", "errorMsg": "GNL"}),
    httpx.Response(200, json=["list"]),
])
async def test_client_errors(resp):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: resp))
    with pytest.raises(DevModeError) as ei:
        await DevModeClient(http, "https://x/secure/", lambda: TOKEN).remaining()
    assert TOKEN not in str(ei.value)


async def test_client_network_error_hides_token():
    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with pytest.raises(DevModeError) as ei:
        await DevModeClient(http, "https://x/secure", lambda: TOKEN).extend()
    assert TOKEN not in str(ei.value) and "ConnectError" in str(ei.value)


# -- ssh --------------------------------------------------------------------
class FakeProc:
    def __init__(self, out=b"", err=b"", rc=0, hang=False):
        self._out, self._err, self.returncode, self._hang = out, err, rc, hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(60)
        return self._out, self._err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def patch_exec(monkeypatch, proc):
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return proc

    monkeypatch.setattr(devmode.asyncio, "create_subprocess_exec", fake_exec)
    return seen


async def ssh(timeout=5):
    return await read_token_over_ssh("192.168.1.55", "/k/key", port=9922, user="prisoner",
                                     token_path="/var/luna/preferences/devmode_enabled",
                                     timeout=timeout)


async def test_ssh_reads_token_without_shell(monkeypatch):
    seen = patch_exec(monkeypatch, FakeProc(out=(TOKEN + "\n").encode()))
    assert await ssh() == TOKEN
    argv = seen["argv"]
    assert argv[0] == "ssh" and "prisoner@192.168.1.55" in argv
    assert argv[-2:] == ("cat", "/var/luna/preferences/devmode_enabled")
    assert "9922" in argv and "BatchMode=yes" in argv and "shell" not in seen["kw"]


@pytest.mark.parametrize("out", [b"", b"abc", (TOKEN + "0").encode(), b"g" * 64,
                                 (TOKEN + "\nextra").encode()])
async def test_ssh_rejects_bad_token(monkeypatch, out):
    patch_exec(monkeypatch, FakeProc(out=out))
    with pytest.raises(DevModeError) as ei:
        await ssh()
    assert "64" in str(ei.value)
    assert TOKEN not in str(ei.value)


async def test_ssh_nonzero_exit(monkeypatch):
    patch_exec(monkeypatch, FakeProc(err=b"Permission denied\n", rc=255))
    with pytest.raises(DevModeError, match="255.*Permission denied"):
        await ssh()


async def test_ssh_timeout_kills(monkeypatch):
    proc = FakeProc(hang=True)
    patch_exec(monkeypatch, proc)
    with pytest.raises(DevModeError, match="超时"):
        await ssh(timeout=0.05)
    assert proc.killed


async def test_ssh_missing_binary(monkeypatch):
    async def nope(*a, **k):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(devmode.asyncio, "create_subprocess_exec", nope)
    with pytest.raises(DevModeError, match="ssh"):
        await ssh()


# -- config -------------------------------------------------------------------
def _load(tmp_path, body, env=None):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "settings.yaml").write_text(body, encoding="utf-8")
    return load_settings(cfg / "settings.yaml", env or {}).devmode


def test_config_defaults(tmp_path):
    d = _load(tmp_path, "mode: observe\n")
    assert d.enabled and d.ssh_port == 9922 and d.ssh_user == "prisoner"
    assert d.ssh_key.name == "ltp_tv_webos" and "~" not in str(d.ssh_key)
    assert d.renew_below_hours == 24 and d.alert_below_hours == 12
    assert d.base_url == "https://developer.lge.com/secure/" and d.app_id == DM_APP


def test_config_env_key_and_overrides(tmp_path):
    d = _load(tmp_path, "devmode:\n  enabled: false\n  ssh_port: 22\n",
              {"DEVMODE_SSH_KEY": "/x/key"})
    assert not d.enabled and d.ssh_port == 22 and str(d.ssh_key).replace("\\", "/") == "/x/key"


@pytest.mark.parametrize("body", ["devmode:\n  enabled: 'false'\n",
                                  "devmode:\n  ssh_port: 70000\n",
                                  "devmode:\n  renew_below_hours: 0\n",
                                  "devmode:\n  alert_below_hours: true\n",
                                  "devmode:\n  retry_minutes: x\n"])
def test_config_rejects_bad_values(tmp_path, body):
    with pytest.raises(ValueError):
        _load(tmp_path, body)
