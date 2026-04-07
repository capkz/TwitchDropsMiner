"""WebSocket broadcaster for real-time updates to web clients."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from socketio import AsyncServer


class WebSocketBroadcaster:
    """Manages broadcasting messages to all connected web clients via Socket.IO.

    Each broadcaster is associated with one account (identified by account_id).
    Every emitted event is automatically tagged with account_id so the frontend
    can route events to the correct account view.
    """

    def __init__(self, account_id: int | None = None):
        self._sio: AsyncServer | None = None  # Will be set by webapp
        self._account_id: int | None = account_id

    def set_socketio(self, sio: AsyncServer):
        """Set the Socket.IO server instance for broadcasting."""
        self._sio = sio

    def set_account_id(self, account_id: int) -> None:
        """Update the account_id after successful authentication."""
        self._account_id = account_id

    async def emit(self, event: str, data: Any) -> None:
        """Emit an event to all connected clients, tagged with this account's id."""
        if self._sio:
            if isinstance(data, dict):
                tagged = {"account_id": self._account_id, **data}
            else:
                tagged = {"account_id": self._account_id, "data": data}
            await self._sio.emit(event, tagged)
