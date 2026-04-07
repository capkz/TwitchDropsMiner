from __future__ import annotations

import asyncio
import logging
import signal
import sys
import traceback
import warnings
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import truststore


if __name__ == "__main__":
    truststore.inject_into_ssl()

    from src.config import FILE_FORMATTER
    from src.config.settings import Settings
    from src.core.account_manager import AccountManager
    from src.i18n import _
    from src.version import __version__

    logger = logging.getLogger("TwitchDrops")
    if logger.level < logging.INFO:
        logger.setLevel(logging.INFO)
    # Always add console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(FILE_FORMATTER)
    logger.addHandler(console_handler)

    # Create logs directory if it doesn't exist
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "TDM.log"

    # Add file handler for timestamped log
    file_handler = TimedRotatingFileHandler(log_file, when="midnight", backupCount=5)
    file_handler.setFormatter(FILE_FORMATTER)
    logger.addHandler(file_handler)

    logger.info("Logger initialized")

    warnings.simplefilter("default", ResourceWarning)

    logger.debug("Loading settings")
    try:
        settings = Settings()
    except Exception:
        logger.exception("Error while loading settings")
        print(f"Settings error: {traceback.format_exc()}", file=sys.stderr)
        sys.exit(4)

    async def main():
        # set language
        if settings.language:
            _.set_language(settings.language)

        logger.info("=== TwitchDropsMiner Starting ===")
        logger.info(f"Version: {__version__}")
        logger.info(f"Python version: {sys.version}")
        logger.info(f"Platform: {sys.platform}")
        logger.info(f"Proxy: {settings.proxy}")
        logger.info(f"Language: {settings.language}")
        logger.info(
            f"Minimum refresh interval: {settings.minimum_refresh_interval_minutes} minutes"
        )

        # Initialize web GUI and server
        from src.web import app as webapp

        logger.info("Starting web server on http://0.0.0.0:8080")
        web_server_task = asyncio.create_task(webapp.run_server(host="0.0.0.0", port=8080))
        # Give the server a moment to bind so Socket.IO is ready
        await asyncio.sleep(0.2)

        # Create account manager and load saved accounts
        account_manager = AccountManager(settings)
        webapp.set_account_manager(account_manager)

        # Load saved accounts (wires Socket.IO into each WebGUIManager)
        await account_manager.load_accounts(webapp.sio)

        # If no accounts were loaded, the first account login will be started on demand
        # via the web UI. Start any already-loaded accounts now.
        await account_manager.start_all()

        loop = asyncio.get_running_loop()
        if sys.platform == "linux":
            logger.debug("Setting up signal handlers for SIGINT and SIGTERM")
            loop.add_signal_handler(signal.SIGINT, lambda *_: asyncio.create_task(_shutdown()))
            loop.add_signal_handler(signal.SIGTERM, lambda *_: asyncio.create_task(_shutdown()))

        async def _shutdown():
            logger.info("Shutdown requested")
            await account_manager.shutdown()

        logger.info("Main loop running – waiting for accounts or shutdown")
        try:
            # Keep running until cancelled or all accounts exit
            await web_server_task
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("=== Starting shutdown sequence ===")
            if sys.platform == "linux":
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            await account_manager.shutdown()
            if web_server_task and not web_server_task.done():
                logger.info("Shutting down web server")
                await webapp.shutdown_server()
                try:
                    await asyncio.wait_for(web_server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    web_server_task.cancel()
                    try:
                        await web_server_task
                    except asyncio.CancelledError:
                        pass
            settings.save()
            logger.info("=== Shutdown complete ===")

    asyncio.run(main())
