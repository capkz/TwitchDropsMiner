from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import socketio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


if TYPE_CHECKING:
    import uvicorn

    from src.core.account_manager import AccountManager


logger = logging.getLogger("TwitchDrops")

# Create FastAPI app
app = FastAPI(title="Twitch Drops Miner Web", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi", cors_allowed_origins="*", logger=False, engineio_logger=False
)

# Wrap with ASGI app
socket_app = socketio.ASGIApp(sio, app)

# Global references (set by main.py)
account_manager: AccountManager | None = None
_server_instance: uvicorn.Server | None = None


def set_account_manager(mgr: AccountManager):
    """Called by __main__.py to wire the AccountManager in."""
    global account_manager
    account_manager = mgr

    async def _on_accounts_changed():
        await sio.emit("accounts_update", {"accounts": mgr.get_accounts()})

    mgr.set_on_accounts_changed(_on_accounts_changed)


# ---------------------------------------------------------------------------
# Helper to get account entry safely
# ---------------------------------------------------------------------------

def _require_account_manager():
    if account_manager is None:
        raise HTTPException(status_code=503, detail="Account manager not initialized")
    return account_manager


def _require_entry(user_id: int):
    mgr = _require_account_manager()
    entry = mgr.get_account(user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Account {user_id} not found")
    return entry


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str
    token: str = ""


class ChannelSelectRequest(BaseModel):
    channel_id: int
    account_id: int


class SettingsUpdate(BaseModel):
    games_to_watch: list[str] | None = None
    dark_mode: bool | None = None
    language: str | None = None
    proxy: str | None = None
    connection_quality: int | None = None
    minimum_refresh_interval_minutes: int | None = None
    inventory_filters: dict | None = None
    mining_benefits: dict[str, bool] | None = None


class ProxyVerifyRequest(BaseModel):
    proxy: str


# ==================== REST API Endpoints ====================


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the main web interface"""
    web_dir = Path(__file__).parent.parent.parent / "web"
    index_file = web_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse(
        content="<h1>Twitch Drops Miner</h1><p>Web interface files not found.</p>",
        status_code=500,
    )


# ---------------------------------------------------------------------------
# Account management endpoints
# ---------------------------------------------------------------------------

@app.get("/api/accounts")
async def get_accounts():
    """Return all managed accounts."""
    mgr = _require_account_manager()
    return {"accounts": mgr.get_accounts()}


@app.post("/api/accounts")
async def add_account():
    """Add a new account – starts the OAuth login flow."""
    mgr = _require_account_manager()
    entry = await mgr.add_account(sio)
    return {"success": True, "account": entry.to_dict()}


@app.delete("/api/accounts/{user_id}")
async def remove_account(user_id: int):
    """Permanently remove an account and its stored data."""
    mgr = _require_account_manager()
    ok = await mgr.remove_account(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Account {user_id} not found")
    return {"success": True}


@app.post("/api/accounts/{user_id}/logout")
async def logout_account(user_id: int):
    """Clear credentials for an account (triggers re-login on next run)."""
    mgr = _require_account_manager()
    ok = await mgr.logout_account(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Account {user_id} not found")
    return {"success": True}


# ---------------------------------------------------------------------------
# Per-account status / channels / campaigns
# ---------------------------------------------------------------------------

@app.get("/api/accounts/{user_id}/status")
async def get_account_status(user_id: int):
    entry = _require_entry(user_id)
    return {
        "status": entry.gui.status.get(),
        "login": entry.gui.login.get_status(),
        "manual_mode": entry.client.get_manual_mode_info(),
    }


@app.get("/api/accounts/{user_id}/channels")
async def get_account_channels(user_id: int):
    entry = _require_entry(user_id)
    return {"channels": entry.gui.channels.get_channels()}


@app.get("/api/accounts/{user_id}/campaigns")
async def get_account_campaigns(user_id: int):
    entry = _require_entry(user_id)
    return {"campaigns": entry.gui.inv.get_campaigns()}


@app.post("/api/accounts/{user_id}/reload")
async def reload_account(user_id: int):
    entry = _require_entry(user_id)
    from src.config import State
    entry.client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/accounts/{user_id}/close")
async def close_account(user_id: int):
    entry = _require_entry(user_id)
    entry.client.close()
    return {"success": True}


@app.post("/api/accounts/{user_id}/oauth/confirm")
async def confirm_account_oauth(user_id: int):
    """Signal that the user has entered the OAuth code for a specific account."""
    entry = _require_entry(user_id)
    entry.gui.login._login_event.set()
    return {"success": True}


@app.post("/api/accounts/{user_id}/mode/exit-manual")
async def exit_manual_mode_account(user_id: int):
    entry = _require_entry(user_id)
    if not entry.client.is_manual_mode():
        return {"success": False, "message": "Not in manual mode"}
    entry.client.exit_manual_mode("User requested")
    return {"success": True}


# ---------------------------------------------------------------------------
# OAuth confirm for pending (not-yet-authenticated) accounts
# A pending account has no user_id yet, identified by staging temp path.
# The frontend can confirm via the generic /api/oauth/confirm endpoint below
# which broadcasts to all pending accounts.
# ---------------------------------------------------------------------------

@app.post("/api/oauth/confirm")
async def confirm_oauth():
    """Confirm OAuth code for any pending account (fires all pending login events)."""
    mgr = _require_account_manager()
    # Fire for all pending accounts
    for entry in mgr._pending.values():
        entry.gui.login._login_event.set()
    return {"success": True}


# ---------------------------------------------------------------------------
# Channel select (account-scoped)
# ---------------------------------------------------------------------------

@app.post("/api/channels/select")
async def select_channel(request: ChannelSelectRequest):
    """Select a channel to watch for a given account."""
    entry = _require_entry(request.account_id)
    channel = entry.client.channels.get(request.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel.game:
        raise HTTPException(status_code=400, detail="Channel is not playing any game")

    entry.gui.select_channel(request.channel_id)
    from src.config import State
    entry.client.change_state(State.CHANNEL_SWITCH)
    return {"success": True}


# ---------------------------------------------------------------------------
# Settings (shared across all accounts)
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status():
    """Aggregate status – returns accounts list."""
    mgr = _require_account_manager()
    return {"accounts": mgr.get_accounts()}


def _require_first_entry():
    entry = _require_account_manager().get_first_account()
    if entry is None:
        raise HTTPException(status_code=503, detail="No accounts loaded")
    return entry


@app.get("/api/settings")
async def get_settings():
    return _require_first_entry().gui.settings.get_settings()


@app.get("/api/languages")
async def get_languages():
    return _require_first_entry().gui.settings.get_languages()


@app.get("/api/translations")
async def get_translations():
    from src.i18n.translator import _
    return _.t


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
    entry = _require_first_entry()
    settings_dict = settings.dict(exclude_unset=True)
    entry.gui.settings.update_settings(settings_dict)
    return {"success": True, "settings": entry.gui.settings.get_settings()}


@app.post("/api/settings/verify-proxy")
async def verify_proxy(request: ProxyVerifyRequest):
    import time
    import aiohttp

    proxy_url = request.proxy.strip()
    if not proxy_url:
        return {"success": False, "message": "Proxy URL is empty"}

    try:
        start_time = time.time()
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://www.twitch.tv", proxy=proxy_url, timeout=10) as response,
        ):
            if response.status < 500:
                latency = round((time.time() - start_time) * 1000)
                return {"success": True, "message": f"Connected! ({latency}ms)", "latency": latency}
            else:
                return {"success": False, "message": f"Proxy reachable but returned {response.status}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@app.get("/api/version")
async def get_version():
    import aiohttp
    from src.version import __version__

    current_version = __version__
    latest_version = None
    update_available = False
    download_url = None

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://api.github.com/repos/rangermix/TwitchDropsMiner/releases/latest", timeout=5
            ) as response,
        ):
            if response.status == 200:
                data = await response.json()
                latest_version = data.get("tag_name", "").lstrip("v")
                download_url = data.get("html_url")
                if latest_version and latest_version > current_version:
                    update_available = True
    except Exception as e:
        logger.warning(f"Failed to check for updates: {str(e)}")

    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "download_url": download_url or "https://github.com/rangermix/TwitchDropsMiner/releases",
    }


@app.post("/api/reload")
async def trigger_reload():
    """Reload all accounts."""
    mgr = _require_account_manager()
    from src.config import State
    for entry in mgr._accounts.values():
        entry.client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/close")
async def trigger_close():
    """Close all accounts."""
    mgr = _require_account_manager()
    for entry in mgr._accounts.values():
        entry.client.close()
    return {"success": True}


# ==================== Socket.IO Events ====================


@sio.event
async def connect(sid, environ):
    """Client connected – send initial state for all accounts."""
    logger.info(f"Web client connected: {sid}")
    if account_manager is None:
        return

    accounts = account_manager.get_accounts()

    # Determine the "primary" account to send full initial state for
    primary = None
    for entry in account_manager._accounts.values():
        primary = entry
        break
    if primary is None:
        for entry in account_manager._pending.values():
            primary = entry
            break

    initial: dict = {
        "accounts": accounts,
        "settings": {},
        "console": [],
        "status": "",
        "channels": [],
        "campaigns": [],
        "login": {},
        "manual_mode": {"active": False},
        "current_drop": None,
        "wanted_items": [],
    }

    if primary:
        initial.update({
            "status": primary.gui.status.get(),
            "channels": primary.gui.channels.get_channels(),
            "campaigns": primary.gui.inv.get_campaigns(),
            "console": primary.gui.output.get_history(),
            "settings": primary.gui.settings.get_settings(),
            "login": primary.gui.login.get_status(),
            "manual_mode": primary.client.get_manual_mode_info(),
            "current_drop": primary.gui.progress.get_current_drop(),
            "wanted_items": primary.gui.get_wanted_game_tree(),
            "active_account_id": primary.user_id,
        })

    await sio.emit("initial_state", initial, room=sid)


@sio.event
async def disconnect(sid):
    logger.info(f"Web client disconnected: {sid}")


@sio.event
async def request_account_state(sid, data):
    """Client requested full state for a specific account."""
    if account_manager is None:
        return
    user_id = data.get("user_id")
    entry = account_manager.get_account(user_id)
    if entry is None:
        return
    await sio.emit(
        "account_state",
        {
            "account_id": user_id,
            "status": entry.gui.status.get(),
            "channels": entry.gui.channels.get_channels(),
            "campaigns": entry.gui.inv.get_campaigns(),
            "console": entry.gui.output.get_history(),
            "login": entry.gui.login.get_status(),
            "manual_mode": entry.client.get_manual_mode_info(),
            "current_drop": entry.gui.progress.get_current_drop(),
            "wanted_items": entry.gui.get_wanted_game_tree(),
        },
        room=sid,
    )


@sio.event
async def request_reload(sid):
    if account_manager:
        from src.config import State
        for entry in account_manager._accounts.values():
            entry.client.change_state(State.INVENTORY_FETCH)


@sio.event
async def get_wanted_items(sid):
    if account_manager:
        for entry in account_manager._accounts.values():
            await sio.emit(
                "wanted_items_update",
                {"account_id": entry.user_id, **{"data": entry.gui.get_wanted_game_tree()}},
                to=sid,
            )


# Mount static files
web_dir = Path(__file__).parent.parent.parent / "web"
if web_dir.exists():
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")


async def run_server(host: str = "0.0.0.0", port: int = 8080):
    global _server_instance
    import uvicorn

    config = uvicorn.Config(socket_app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    _server_instance = server
    try:
        await server.serve()
    finally:
        _server_instance = None


async def shutdown_server():
    if _server_instance:
        logger.info("Setting server.should_exit = True")
        _server_instance.should_exit = True
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(run_server())
