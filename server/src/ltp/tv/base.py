"""Brand-agnostic TV contract. Business logic depends only on this module."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, ClassVar, Protocol


@dataclass(frozen=True)
class TVState:
    """Snapshot of what the TV is doing. app_id is "" when nothing is in front."""
    connected: bool
    power_on: bool
    app_id: str

    OFFLINE: ClassVar["TVState"]

    @property
    def active(self) -> bool:
        """TV reachable and switched on (screen may show anything)."""
        return self.connected and self.power_on


TVState.OFFLINE = TVState(connected=False, power_on=False, app_id="")

StateCallback = Callable[[TVState], Awaitable[None]]


class NotPairedError(RuntimeError):
    """No client key and pairing not explicitly allowed. Pairing pops a prompt
    on the TV, so only the parent may trigger it (`ltp pair`)."""


class TVController(Protocol):
    @property
    def state(self) -> TVState: ...

    @property
    def client_key(self) -> str | None: ...

    def on_state(self, callback: StateCallback) -> None:
        """Register the single state-change callback (also fired on disconnect)."""
        ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def refresh(self) -> TVState:
        """Actively re-read power + foreground app (liveness probe). Raises on
        failure; callers should then disconnect and reconnect."""
        ...

    def app_title(self, app_id: str) -> str: ...

    async def list_app_ids(self) -> set[str] | None:
        """Re-list launch points now. None (or empty) means the listing is unavailable:
        callers must treat that as "unknown", not "nothing installed"."""
        ...

    async def show_toast(self, text: str) -> None: ...

    async def close_app(self, app_id: str) -> None: ...

    async def launch_app(self, app_id: str) -> None: ...

    async def power_off(self) -> None: ...

    async def power_on(self) -> None: ...
