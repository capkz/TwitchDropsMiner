"""Multi-account manager for Twitch Drops Miner.

Manages the lifecycle of multiple Twitch client instances, each authenticated
as a different Twitch account. Supports adding, removing, logging in/out, and
running all accounts concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import json
from collections.abc import Awaitable, Callable

from src.config.paths import (
    ACCOUNTS_CONFIG_PATH,
    ACCOUNTS_DIR,
    get_account_cookies_path,
    get_account_dir,
)


if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.core.client import Twitch
    from src.web.gui_manager import WebGUIManager


logger = logging.getLogger("TwitchDrops")


@dataclass
class AccountEntry:
    """Represents one managed Twitch account."""

    user_id: int | None
    username: str | None
    client: Twitch
    gui: WebGUIManager
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Temporary staging directory used before user_id is known
    staging_dir: Path | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a dict safe for JSON and for the web API."""
        login_status = self.gui.login.get_status() if self.gui else {}
        return {
            "user_id": self.user_id,
            "username": self.username,
            "status": login_status.get("status", ""),
            "authenticated": self.is_authenticated,
            "running": self.task is not None and not self.task.done(),
        }


class AccountManager:
    """Manages multiple Twitch client instances for concurrent multi-account mining."""

    def __init__(self, settings: Settings):
        self.settings: Settings = settings
        self._accounts: dict[int, AccountEntry] = {}   # user_id -> entry
        self._pending: dict[str, AccountEntry] = {}   # staging_id -> entry (mid-login)
        self._on_accounts_changed: Callable[[], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def set_on_accounts_changed(self, cb: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine-returning callable to be called on account changes."""
        self._on_accounts_changed = cb

    def get_accounts(self) -> list[dict[str, Any]]:
        """Return serialisable info for all accounts (pending + authenticated)."""
        return [e.to_dict() for e in (*self._accounts.values(), *self._pending.values())]

    def get_account(self, user_id: int) -> AccountEntry | None:
        return self._accounts.get(user_id)

    def get_first_account(self) -> AccountEntry | None:
        """Return the first available account (authenticated, then pending)."""
        for entry in self._accounts.values():
            return entry
        for entry in self._pending.values():
            return entry
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_config(self) -> list[dict[str, Any]]:
        if not ACCOUNTS_CONFIG_PATH.exists():
            return []
        try:
            with open(ACCOUNTS_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_config(self) -> None:
        data = [
            {"user_id": uid, "username": e.username}
            for uid, e in self._accounts.items()
        ]
        with open(ACCOUNTS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _make_client(
        self,
        cookies_path: Path,
        account_id: int | None,
        on_authenticated,
    ) -> tuple[Twitch, WebGUIManager]:
        """Create a (Twitch, WebGUIManager) pair wired together."""
        from src.core.client import Twitch
        from src.web.gui_manager import WebGUIManager

        client = Twitch(
            self.settings,
            account_id=account_id,
            cookies_path=cookies_path,
            on_authenticated=on_authenticated,
        )
        gui = WebGUIManager(client, account_id=account_id)
        client.gui = gui
        return client, gui

    async def load_accounts(self, sio) -> None:
        """Load previously saved accounts and wire them to Socket.IO."""
        accounts_data = self._load_config()
        for record in accounts_data:
            user_id: int = record["user_id"]
            username: str | None = record.get("username")
            cookies_path = get_account_cookies_path(user_id)

            client, gui = self._make_client(
                cookies_path=cookies_path,
                account_id=user_id,
                on_authenticated=self._make_on_authenticated(None),
            )
            gui.set_socketio(sio)
            entry = AccountEntry(
                user_id=user_id,
                username=username,
                client=client,
                gui=gui,
            )
            self._accounts[user_id] = entry
            logger.info(f"Loaded account user_id={user_id} (username={username})")

        # Migrate legacy single-account cookies if present
        await self._migrate_legacy_cookies(sio)

    async def _migrate_legacy_cookies(self, sio) -> None:
        """If data/cookies.jar exists and no accounts are loaded, import it."""
        from src.config import COOKIES_PATH

        if self._accounts or not COOKIES_PATH.exists():
            return
        # Create a temporary client just to find out the user_id
        staging_id = str(uuid.uuid4())
        staging_dir = ACCOUNTS_DIR / f"staging_{staging_id}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_cookies = staging_dir / "cookies.jar"
        shutil.copy2(COOKIES_PATH, staging_cookies)

        client, gui = self._make_client(
            cookies_path=staging_cookies,
            account_id=None,
            on_authenticated=self._make_on_authenticated(staging_id),
        )
        gui.set_socketio(sio)
        entry = AccountEntry(
            user_id=None,
            username=None,
            client=client,
            gui=gui,
            staging_dir=staging_dir,
        )
        self._pending[staging_id] = entry
        logger.info("Migrating legacy cookies.jar to per-account storage")

    async def add_account(self, sio) -> AccountEntry:
        """Add a new account and start its login flow."""
        staging_id = str(uuid.uuid4())
        staging_dir = ACCOUNTS_DIR / f"staging_{staging_id}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_cookies = staging_dir / "cookies.jar"

        client, gui = self._make_client(
            cookies_path=staging_cookies,
            account_id=None,
            on_authenticated=self._make_on_authenticated(staging_id),
        )
        gui.set_socketio(sio)
        entry = AccountEntry(
            user_id=None,
            username=None,
            client=client,
            gui=gui,
            staging_dir=staging_dir,
        )
        self._pending[staging_id] = entry
        entry.task = asyncio.create_task(self._run_account(entry, staging_id))
        await self._notify_changed()
        logger.info(f"Started new account login flow (staging_id={staging_id})")
        return entry

    def _make_on_authenticated(self, staging_id: str | None):
        """Return the on_authenticated callback for a given staging slot."""

        async def _on_authenticated(user_id: int) -> None:
            await self._promote(staging_id, user_id)

        return _on_authenticated

    async def _promote(self, staging_id: str | None, user_id: int) -> None:
        """Move a staging account to a full account after successful login."""
        # Find the entry
        if staging_id and staging_id in self._pending:
            entry = self._pending.pop(staging_id)
        elif user_id in self._accounts:
            # Already promoted (existing account re-authenticated)
            entry = self._accounts[user_id]
            await self._notify_changed()
            return
        else:
            logger.warning(f"_promote called but staging_id={staging_id} not found")
            return

        # Move staging directory to final per-account directory
        if entry.staging_dir is not None:
            final_dir = get_account_dir(user_id)
            final_cookies = final_dir / "cookies.jar"
            staging_cookies = entry.staging_dir / "cookies.jar"
            if staging_cookies.exists():
                shutil.move(str(staging_cookies), final_cookies)
            shutil.rmtree(entry.staging_dir, ignore_errors=True)
            entry.staging_dir = None
            # Update cookies path on all components that hold a reference
            entry.client._cookies_path = final_cookies
            entry.client._auth_state._cookies_path = final_cookies
            if entry.client._http_client is not None:
                entry.client._http_client._cookies_path = final_cookies

        entry.user_id = user_id

        # Update account_id on the GUI broadcaster so events carry the right id
        entry.gui.set_account_id(user_id)

        self._accounts[user_id] = entry
        self._save_config()
        await self._notify_changed()
        logger.info(f"Account promoted: user_id={user_id}")

    async def _run_account(self, entry: AccountEntry, staging_id: str | None = None) -> None:
        """Run a single account's main loop, handling errors."""
        from src.exceptions import CaptchaRequired

        try:
            await entry.client.run()
        except CaptchaRequired:
            logger.error(f"Captcha required for account user_id={entry.user_id}")
            entry.client.print("Captcha required – cannot continue for this account.")
        except Exception as exc:
            logger.exception(f"Fatal error in account user_id={entry.user_id}: {exc}")
        finally:
            entry.task = None
            await self._notify_changed()

    async def start_all(self) -> None:
        """Start all loaded accounts concurrently. Call after load_accounts()."""
        for entry in list(self._accounts.values()):
            if entry.task is None or entry.task.done():
                entry.task = asyncio.create_task(self._run_account(entry))
        for staging_id, entry in list(self._pending.items()):
            if entry.task is None or entry.task.done():
                entry.task = asyncio.create_task(self._run_account(entry, staging_id))

    async def remove_account(self, user_id: int) -> bool:
        """Stop and permanently remove an account."""
        entry = self._accounts.pop(user_id, None)
        if entry is None:
            return False
        await self._stop_entry(entry)
        # Delete per-account data
        shutil.rmtree(get_account_dir(user_id), ignore_errors=True)
        self._save_config()
        await self._notify_changed()
        logger.info(f"Removed account user_id={user_id}")
        return True

    async def logout_account(self, user_id: int) -> bool:
        """Clear credentials for an account (keeps the account slot, re-triggers login)."""
        entry = self._accounts.get(user_id)
        if entry is None:
            return False
        # Stop the running client
        await self._stop_entry(entry)
        # Wipe cookies
        cookies_path = get_account_cookies_path(user_id)
        cookies_path.unlink(missing_ok=True)
        # Clear auth state
        entry.client._auth_state.clear()
        # Restart the client (will trigger OAuth flow again)
        entry.task = asyncio.create_task(self._run_account(entry))
        await self._notify_changed()
        logger.info(f"Logged out account user_id={user_id}")
        return True

    async def shutdown(self) -> None:
        """Gracefully shut down all running accounts."""
        all_entries = list(self._accounts.values()) + list(self._pending.values())
        for entry in all_entries:
            entry.client.close()
        # Give tasks a moment to finish
        tasks = [e.task for e in all_entries if e.task and not e.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Shutdown each client
        for entry in all_entries:
            await entry.client.shutdown()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _stop_entry(self, entry: AccountEntry) -> None:
        """Request a client to stop and wait for its task."""
        entry.client.close()
        if entry.task and not entry.task.done():
            try:
                await asyncio.wait_for(entry.task, timeout=5.0)
            except asyncio.TimeoutError:
                entry.task.cancel()
                try:
                    await entry.task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        await entry.client.shutdown()

    async def _notify_changed(self) -> None:
        """Notify the webapp that the accounts list has changed."""
        if self._on_accounts_changed is not None:
            try:
                await self._on_accounts_changed()
            except Exception:
                pass
