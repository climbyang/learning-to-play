"""LG webOS adapter (bscpylgtv 0.5.x, official SSAP WebSocket protocol, no Root).

Contract notes verified against bscpylgtv 0.5.5:
- request()/subscribe() prepend "ssap://" themselves -> pass bare luna paths.
- The library only sends keep-alive pings while it believes the TV is on, which
  needs the "power" (and "current_app") state subscriptions enabled. With
  states=[] the power state stays "Unknown" and dead sockets go unnoticed.
- Its state-update callbacks fire on every change; the connection task ends
  when the socket dies, which we watch to emit OFFLINE.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ltp.tv.base import NotPairedError, StateCallback, TVState
from ltp.tv.wol import broadcast_targets, send_wol

log = logging.getLogger(__name__)

EP_FOREGROUND_APP = "com.webos.applicationManager/getForegroundAppInfo"
EP_POWER_STATE = "com.webos.service.tvpower/power/getPowerState"
EP_LIST_APPS = "com.webos.applicationManager/listLaunchPoints"
EP_TOAST = "system.notifications/createToast"
EP_LAUNCHER_CLOSE = "system.launcher/close"
EP_CLOSE_BY_APPID = "com.webos.applicationManager/closeByAppId"
EP_LAUNCH = "system.launcher/launch"
HOME_APP_ID = "com.webos.app.home"

# Power states the TV reports while the panel is effectively off.
OFF_STATES = {None, "", "Power Off", "Suspend", "Active Standby"}

ClientFactory = Callable[..., Awaitable[Any]]


def power_on_from(power_state: str | None, app_id: str) -> bool:
    """Mirror bscpylgtv's is_on: "Unknown" falls back to 'an app is in front'."""
    if power_state == "Unknown":
        return bool(app_id)
    return power_state not in OFF_STATES


async def _default_factory(ip: str, **kwargs: Any) -> Any:
    from bscpylgtv import WebOsClient
    return await WebOsClient.create(ip, **kwargs)


class LGWebOSController:
    def __init__(self, ip: str, client_key: str | None, *, allow_pairing: bool = False,
                 client_factory: ClientFactory | None = None,
                 request_timeout: float = 5.0, ping_interval: int = 10,
                 mac: str | None = None,
                 wol_sender: Callable[[str, list[str]], None] | None = None) -> None:
        self._ip = ip
        self._mac = mac
        self._wol_sender = wol_sender or send_wol
        self._client_key = client_key
        self._allow_pairing = allow_pairing
        self._factory = client_factory or _default_factory
        self._timeout = request_timeout
        self._ping_interval = ping_interval
        self._client: Any = None
        self._state = TVState.OFFLINE
        self._cb: StateCallback | None = None
        self._titles: dict[str, str] = {}
        self._watch_task: asyncio.Task | None = None
        self._push_version = 0  # bumped by library push updates; refresh() yields to them

    # -- properties -------------------------------------------------------
    @property
    def state(self) -> TVState:
        return self._state

    @property
    def client_key(self) -> str | None:
        return self._client_key

    def on_state(self, callback: StateCallback) -> None:
        self._cb = callback

    def app_title(self, app_id: str) -> str:
        return self._titles.get(app_id, app_id)

    async def list_app_ids(self) -> set[str] | None:
        return await self._refresh_launch_points()

    # -- internals --------------------------------------------------------
    async def _set_state(self, new: TVState) -> None:
        if new == self._state:
            return
        self._state = new
        if self._cb is not None:
            await self._cb(new)

    async def _on_lib_state(self, client: Any) -> None:
        if client is not self._client:  # late callback from a client we already dropped
            return
        self._push_version += 1
        app_id = client.current_appId or ""
        power = (client.power_state or {}).get("state")
        await self._set_state(TVState(connected=True,
                                      power_on=power_on_from(power, app_id),
                                      app_id=app_id))

    async def _watch_connection(self, client: Any, task: asyncio.Task) -> None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                return  # watcher cancelled by disconnect(), which handles cleanup
        except BaseException:  # noqa: BLE001 - any end of the task means offline
            pass
        if client is not self._client:
            return  # a newer connection owns the adapter now
        log.info("TV connection closed")
        self._client = None
        await self._set_state(TVState.OFFLINE)

    async def _request(self, uri: str, payload: dict | None = None) -> dict:
        if self._client is None:
            raise ConnectionError("not connected")
        return await asyncio.wait_for(self._client.request(uri, payload), self._timeout)

    async def _refresh_launch_points(self) -> set[str] | None:
        """List launch points; on success replace (not merge) the title map so removed apps
        disappear. Returns the ids, or None when the listing failed or came back empty."""
        try:
            resp = await self._request(EP_LIST_APPS)
        except Exception as exc:  # titles are cosmetic; monitoring works on ids
            log.warning("could not list launch points: %s", exc)
            return None
        titles = {lp["id"]: lp.get("title") or lp["id"]
                  for lp in resp.get("launchPoints", []) or [] if lp.get("id")}
        if not titles:
            return None
        self._titles = titles
        return set(titles)

    # -- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        if not self._ip:
            raise NotPairedError("LG_TV_IP not set in .env")
        if not self._client_key and not self._allow_pairing:
            raise NotPairedError("no LG client key; run `ltp pair` with the TV on")
        # Short connect timeouts: the daemon retries on its own schedule.
        kwargs: dict[str, Any] = dict(states=["power", "current_app"],
                                      ping_interval=self._ping_interval,
                                      timeout_connect=3, connect_retry_attempts=2)
        if self._client_key:
            kwargs["client_key"] = self._client_key
        client = await self._factory(self._ip, **kwargs)
        await client.connect()
        self._client = client
        self._client_key = client.client_key or self._client_key
        await self._refresh_launch_points()
        try:
            await client.register_state_update_callback(self._on_lib_state)
        except BaseException:
            self._client = None
            await client.disconnect()
            raise
        task = getattr(client, "connect_task", None)
        if task is not None:
            self._watch_task = asyncio.create_task(self._watch_connection(client, task))

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                log.warning("disconnect error: %s", exc)
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        await self._set_state(TVState.OFFLINE)

    async def refresh(self) -> TVState:
        version = self._push_version
        app = await self._request(EP_FOREGROUND_APP)
        app_id = app.get("appId", "") or ""
        try:
            power = (await self._request(EP_POWER_STATE)).get("state")
        except asyncio.TimeoutError:
            raise
        except Exception:  # some firmwares lack tvpower; fall back to app presence
            power = "Unknown"
        if version != self._push_version:  # a newer push arrived while we were polling
            return self._state
        await self._set_state(TVState(connected=True,
                                      power_on=power_on_from(power, app_id),
                                      app_id=app_id))
        return self._state

    # -- actions ----------------------------------------------------------
    async def show_toast(self, text: str) -> None:
        await self._request(EP_TOAST, {"message": text})

    async def close_app(self, app_id: str) -> None:
        """Close the app; fall back to another close API, then to launching Home."""
        # closeByAppId first: launcher/close returns 403 on webOS 6
        for uri in (EP_CLOSE_BY_APPID, EP_LAUNCHER_CLOSE):
            try:
                await self._request(uri, {"id": app_id})
                log.info("closed %s via %s", app_id, uri)
                return
            except Exception as exc:
                log.warning("%s failed for %s: %s", uri, app_id, exc)
        await self._request(EP_LAUNCH, {"id": HOME_APP_ID})
        log.info("left %s by launching %s", app_id, HOME_APP_ID)

    async def launch_app(self, app_id: str) -> None:
        # Standard SSAP launch; not yet verified on the real TV (see docs/compatibility.md).
        await self._request(EP_LAUNCH, {"id": app_id})

    async def power_on(self) -> None:
        """Wake-on-LAN. Verified on webOS 6: port opens ~40 s later, SSAP ~15 s after."""
        if not self._mac:
            raise ValueError("LG_TV_MAC not set in .env")
        self._wol_sender(self._mac, broadcast_targets(self._ip))

    async def power_off(self) -> None:
        if self._client is None:
            raise ConnectionError("not connected")
        await asyncio.wait_for(self._client.power_off(), self._timeout)
