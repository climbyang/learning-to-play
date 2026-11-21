"""In-memory TVController for tests and local demos."""
from __future__ import annotations

from ltp.tv.base import StateCallback, TVState


class FakeTV:
    def __init__(self, titles: dict[str, str] | None = None) -> None:
        self._state = TVState.OFFLINE
        self._cb: StateCallback | None = None
        self._titles = titles or {}
        self.reachable = True
        self.refresh_fails = False
        self.toasts: list[str] = []
        self.closed: list[str] = []
        self.launched: list[str] = []
        self.launch_fails = False
        self.powered_off = 0
        self.power_off_fails = False
        self.powered_on = 0
        self.power_on_error: Exception | None = None
        self.connects = 0
        self.list_fails = False

    @property
    def state(self) -> TVState:
        return self._state

    @property
    def client_key(self) -> str | None:
        return "fake-key"

    def on_state(self, callback: StateCallback) -> None:
        self._cb = callback

    async def _emit(self) -> None:
        if self._cb is not None:
            await self._cb(self._state)

    async def set(self, *, connected: bool = True, power_on: bool = True,
                  app_id: str = "") -> None:
        """Test helper: change state and fire the callback."""
        self._state = TVState(connected=connected, power_on=power_on and connected,
                              app_id=app_id if connected else "")
        await self._emit()

    async def connect(self) -> None:
        if not self.reachable:
            raise ConnectionError("fake TV unreachable")
        self.connects += 1
        self._state = TVState(connected=True, power_on=True, app_id=self._state.app_id)
        await self._emit()

    async def disconnect(self) -> None:
        self._state = TVState.OFFLINE
        await self._emit()

    async def refresh(self) -> TVState:
        if self.refresh_fails or not self._state.connected:
            raise ConnectionError("fake refresh failed")
        return self._state

    def app_title(self, app_id: str) -> str:
        return self._titles.get(app_id, app_id)

    async def list_app_ids(self) -> set[str] | None:
        if self.list_fails:
            return None
        return set(self._titles)

    async def show_toast(self, text: str) -> None:
        self.toasts.append(text)

    async def close_app(self, app_id: str) -> None:
        self.closed.append(app_id)
        self._state = TVState(connected=True, power_on=True, app_id="com.webos.app.home")
        await self._emit()

    async def launch_app(self, app_id: str) -> None:
        if self.launch_fails:
            raise RuntimeError("fake launch_app failed")
        self.launched.append(app_id)
        self._state = TVState(connected=True, power_on=True, app_id=app_id)
        await self._emit()

    async def power_off(self) -> None:
        self.powered_off += 1
        if self.power_off_fails:
            raise ConnectionError("fake power_off failed")
        self._state = TVState.OFFLINE
        await self._emit()

    async def power_on(self) -> None:
        if self.power_on_error is not None:
            raise self.power_on_error
        self.powered_on += 1
