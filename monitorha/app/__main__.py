"""Add-on entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from .manager import Manager
from .server import create_app
from .store import Store
from .supervisor import Supervisor

_LOGGER = logging.getLogger("monitorha")

DATA_DIR = Path(os.environ.get("MONITORHA_DATA", "/data"))
PORT = int(os.environ.get("MONITORHA_PORT", "8099"))


async def async_main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = Store(DATA_DIR / "config.json")
    manager = Manager(store)
    await manager.start()

    app = create_app(store, manager)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    _LOGGER.info("Listening on port %s with %s source(s)", PORT, len(manager.runners))

    supervisor = Supervisor()
    if supervisor.available:
        await supervisor.async_publish_discovery(PORT, store.api_token)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    _LOGGER.info("Shutting down")
    await manager.stop()
    await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
