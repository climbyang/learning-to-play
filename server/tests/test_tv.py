import asyncio

import pytest

from ltp.tv import NotPairedError, TVState
from ltp.tv.fake import FakeTV
from ltp.tv.lg_webos import LGWebOSController, power_on_from


class StubClient:
    """Mimics the parts of bscpylgtv.WebOsClient the adapter touches."""

    def __init__(self, ip, **kwargs):
        self.ip = ip
        self.kwargs = kwargs
        self.client_key = kwargs.get("client_key") or "new-key"
        self.requests: list[tuple[str, dict | None]] = []
        self.power_state = {"state": "Active"}
        self.current_appId = "youtube.leanback.v4"
        self.cbs = []
        self.connect_task = None
        self.fail_uris: set[str] = set()
        self.powered_off = False
        self.launch_points = [{"id": "youtube.leanback.v4", "title": "YouTube"}]

    async def connect(self):
        self.connect_task = asyncio.get_running_loop().create_future()

    async def disconnect(self):
        if self.connect_task and not self.connect_task.done():
            self.connect_task.set_result(None)

    async def request(self, uri, payload=None):
        self.requests.append((uri, payload))
        if uri in self.fail_uris:
            raise RuntimeError("boom")
        if uri.endswith("listLaunchPoints"):
            return {"launchPoints": list(self.launch_points)}
        if uri.endswith("getForegroundAppInfo"):
            return {"appId": self.current_appId}
        if uri.endswith("getPowerState"):
            return dict(self.power_state)
        return {"returnValue": True}

    async def register_state_update_callback(self, cb):
        self.cbs.append(cb)
        await cb(self)

    async def power_off(self):
        self.powered_off = True


def _make(key="k", **kw):
    holder = {}

    async def factory(ip, **kwargs):
        holder["c"] = StubClient(ip, **kwargs)
        return holder["c"]

    return LGWebOSController("10.0.0.9", key, client_factory=factory, **kw), holder


async def test_no_key_refuses_to_connect_without_creating_client():
    tv, holder = _make(key=None)
    with pytest.raises(NotPairedError):
        await tv.connect()
    assert "c" not in holder


async def test_connect_enables_power_and_app_states_and_reports_state():
    tv, holder = _make()
    seen = []

    async def cb(s):
        seen.append(s)

    tv.on_state(cb)
    await tv.connect()
    c = holder["c"]
    assert set(c.kwargs["states"]) >= {"power", "current_app"}
    assert c.kwargs["client_key"] == "k"
    assert seen[-1] == TVState(True, True, "youtube.leanback.v4")
    assert tv.app_title("youtube.leanback.v4") == "YouTube"


async def test_requests_use_bare_luna_paths():
    tv, holder = _make()
    await tv.connect()
    await tv.show_toast("hi")
    await tv.refresh()
    uris = [u for u, _ in holder["c"].requests]
    assert ("system.notifications/createToast", {"message": "hi"}) in holder["c"].requests
    assert all(not u.startswith("ssap://") for u in uris)


@pytest.mark.parametrize("ps,app,on", [
    ("Active", "x", True), ("Screen Off", "x", True), ("Active Standby", "x", False),
    ("Suspend", "x", False), ("Power Off", "x", False), ("Unknown", "", False),
    ("Unknown", "x", True), (None, "x", False),
])
def test_power_mapping(ps, app, on):
    assert power_on_from(ps, app) is on


async def test_socket_close_emits_offline():
    tv, holder = _make()
    seen = []

    async def cb(s):
        seen.append(s)

    tv.on_state(cb)
    await tv.connect()
    holder["c"].connect_task.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen[-1] == TVState.OFFLINE
    assert tv.state == TVState.OFFLINE


async def test_refresh_detects_standby():
    tv, holder = _make()
    await tv.connect()
    holder["c"].power_state = {"state": "Active Standby"}
    s = await tv.refresh()
    assert s.power_on is False


async def test_close_app_falls_back_to_home():
    tv, holder = _make()
    await tv.connect()
    holder["c"].fail_uris = {"system.launcher/close", "com.webos.applicationManager/closeByAppId"}
    await tv.close_app("youtube.leanback.v4")
    assert holder["c"].requests[-1] == ("system.launcher/launch", {"id": "com.webos.app.home"})


async def test_fake_tv_set_fires_callback():
    tv = FakeTV()
    seen = []

    async def cb(s):
        seen.append(s)

    tv.on_state(cb)
    await tv.set(app_id="tubi")
    assert seen == [TVState(True, True, "tubi")]
    await tv.set(connected=False)
    assert tv.state == TVState.OFFLINE


async def test_late_callback_after_disconnect_ignored():
    tv, holder = _make()
    seen = []

    async def cb(s):
        seen.append(s)

    tv.on_state(cb)
    await tv.connect()
    c = holder["c"]
    await tv.disconnect()
    c.current_appId = "com.tubitv.ott"
    await c.cbs[0](c)               # library fires its closeout callback late
    assert tv.state == TVState.OFFLINE
    assert seen[-1] == TVState.OFFLINE


async def test_refresh_does_not_overwrite_newer_push():
    tv, holder = _make()
    await tv.connect()
    c = holder["c"]
    orig = c.request

    async def slow_request(uri, payload=None):
        res = await orig(uri, payload)
        if uri.endswith("getForegroundAppInfo"):
            c.current_appId = "com.tubitv.ott"   # push arrives mid-refresh
            await c.cbs[0](c)
        return res

    c.request = slow_request
    await tv.refresh()
    assert tv.state.app_id == "com.tubitv.ott"


async def test_close_app_tries_close_by_appid_first():
    tv, holder = _make()
    await tv.connect()
    await tv.close_app("youtube.leanback.v4")
    assert holder["c"].requests[-1] == (
        "com.webos.applicationManager/closeByAppId", {"id": "youtube.leanback.v4"})


async def test_stale_watcher_does_not_clear_new_connection():
    tv, holder = _make()
    seen = []

    async def cb(s):
        seen.append(s)

    tv.on_state(cb)
    await tv.connect()
    old = holder["c"]
    old_watcher = tv._watch_task
    tv._watch_task = None              # watcher outlives disconnect (cancel raced)

    async def no_close():
        pass

    old.disconnect = no_close          # old socket closes only later
    await tv.disconnect()
    await tv.connect()
    new = holder["c"]
    old.connect_task.set_result(None)  # old socket finally closes
    await old_watcher
    assert tv.state.connected
    assert seen[-1].connected
    await tv.refresh()                 # new client still installed
    assert new.requests[-1][0].endswith("getPowerState")


def test_magic_packet_and_targets():
    from ltp.tv.wol import broadcast_targets, magic_packet
    pkt = magic_packet("00:11:22:33:44:55")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("001122334455")
    assert magic_packet("00-11-22-33-44-55") == magic_packet("001122334455") == pkt
    assert broadcast_targets("192.168.1.55") == ["255.255.255.255", "192.168.1.255"]
    with pytest.raises(ValueError):
        magic_packet("00:11:22:33:44")


async def test_lg_power_on_sends_wol_and_requires_mac():
    sent = []
    tv = LGWebOSController("192.168.1.55", "k", mac="00:11:22:33:44:55",
                           wol_sender=lambda mac, targets: sent.append((mac, targets)))
    await tv.power_on()
    assert sent == [("00:11:22:33:44:55", ["255.255.255.255", "192.168.1.255"])]
    with pytest.raises(ValueError):
        await LGWebOSController("192.168.1.55", "k").power_on()


async def test_lg_launch_app_request():
    tv, holder = _make()
    await tv.connect()
    await tv.launch_app("quiz.app")
    assert holder["c"].requests[-1] == ("system.launcher/launch", {"id": "quiz.app"})


async def test_list_app_ids_relists_and_drops_removed_apps():
    tv, holder = _make()
    await tv.connect()
    c = holder["c"]
    c.launch_points = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
    assert await tv.list_app_ids() == {"a", "b"}
    c.launch_points = [{"id": "a", "title": "A"}]
    assert await tv.list_app_ids() == {"a"}
    assert tv.app_title("b") == "b"  # title map replaced, not merged


async def test_reconnect_clears_stale_ids():
    tv, holder = _make()
    await tv.connect()
    holder["c"].launch_points = [{"id": "old", "title": "Old"}]
    assert await tv.list_app_ids() == {"old"}
    await tv.disconnect()
    await tv.connect()  # fresh stub lists only youtube
    assert await tv.list_app_ids() == {"youtube.leanback.v4"}
    assert tv.app_title("old") == "old"


async def test_list_app_ids_failure_or_empty_is_unknown():
    tv, holder = _make()
    await tv.connect()
    c = holder["c"]
    c.fail_uris.add("com.webos.applicationManager/listLaunchPoints")
    assert await tv.list_app_ids() is None
    c.fail_uris.clear()
    c.launch_points = []
    assert await tv.list_app_ids() is None
    assert tv.app_title("youtube.leanback.v4") == "YouTube"  # old titles kept on failure


async def test_disconnect_returns_and_goes_offline_when_library_disconnect_hangs():
    # bscpylgtv's disconnect awaits its connection task, whose cleanup can wait forever;
    # the daemon's tick must not hang with it (seen live: 4.5 h without ticks).
    tv, holder = _make(lifecycle_timeout=0.05)
    await tv.connect()

    async def stuck():
        await asyncio.Event().wait()

    holder["c"].disconnect = stuck
    await asyncio.wait_for(tv.disconnect(), 2)
    assert tv.state == TVState.OFFLINE


async def test_connect_gives_up_when_library_connect_hangs():
    tv, holder = _make(lifecycle_timeout=0.05)
    orig = StubClient.connect

    async def stuck(self):
        await asyncio.Event().wait()

    StubClient.connect = stuck
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(tv.connect(), 2)
    finally:
        StubClient.connect = orig
    assert tv.state == TVState.OFFLINE
    await tv.connect()  # a later attempt still works
    assert tv.state.connected


async def test_power_off_handler_does_not_deadlock_the_connection():
    # bscpylgtv 0.5.5: on power-off its power-state handler awaits disconnect(), which
    # awaits the connection task, whose cleanup waits (ignoring cancellation) for the
    # very task running that handler. Model that structure on a real client.
    from bscpylgtv import WebOsClient

    from ltp.tv.lg_webos import _avoid_power_off_deadlock

    client = WebOsClient("192.168.1.20", client_key="k", states=["power"])
    _avoid_power_off_deadlock(client)
    client._power_state = {"state": "Active"}
    pushed = asyncio.Event()

    async def callback_handler():
        try:
            await pushed.wait()
            await client.set_power_state({"state": "Suspend"})
            await asyncio.Event().wait()  # keeps reading its queue until cancelled
        except asyncio.CancelledError:
            pass

    async def connect_handler():
        handler = asyncio.create_task(callback_handler())
        try:
            await asyncio.wait({handler})
        finally:
            handler.cancel()
            closeout = asyncio.create_task(asyncio.wait({handler}))
            while not closeout.done():
                try:
                    await asyncio.shield(closeout)
                except asyncio.CancelledError:
                    pass

    client.connect_task = asyncio.create_task(connect_handler())
    await asyncio.sleep(0)
    pushed.set()
    done, _ = await asyncio.wait({client.connect_task}, timeout=2)
    assert done, "connection task deadlocked after power-off"
    assert not client.is_on


async def test_adapter_installs_power_off_fix_before_connecting():
    seen = {}
    orig = StubClient.connect

    async def connect(self):
        seen["patched"] = "set_power_state" in self.__dict__
        await orig(self)

    StubClient.set_power_state = lambda self, payload: None
    StubClient.connect = connect
    try:
        tv, _ = _make()
        await tv.connect()
    finally:
        StubClient.connect = orig
        del StubClient.set_power_state
    assert seen["patched"]
