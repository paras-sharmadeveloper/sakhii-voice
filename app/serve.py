"""Run one engine instance with graceful call draining.

    python -m app.serve --port 8000

Plain uvicorn closes every open WebSocket (code 1012) as soon as it's asked
to stop, so a deploy would cut live calls. Here, SIGTERM instead:
  1. closes the listening port at once, so the web server sends new calls to
     the other instance (connection refused = instant failover);
  2. waits for this instance's live calls to end, up to DRAIN_TIMEOUT_SECS;
  3. then lets uvicorn shut down as usual.

One process per instance on purpose: with uvicorn's --workers, the parent
process keeps the listening socket open while its workers drain, so the
kernel would keep accepting connections nobody serves. Use more instances
(ports) for more CPU cores.
"""

import argparse
import asyncio
import socket

import uvicorn
from loguru import logger

from app import main as engine
from app.settings import get_settings


class DrainingServer(uvicorn.Server):
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        for server in self.servers:
            server.close()
        for sock in sockets or []:
            sock.close()

        timeout = get_settings().drain_timeout_secs
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if engine.active_calls:
            logger.info("Draining {} live call(s), up to {}s", engine.active_calls, timeout)
        while engine.active_calls and not self.force_exit and loop.time() < deadline:
            await asyncio.sleep(0.5)
        if engine.active_calls:
            logger.warning("Drain timeout: closing {} live call(s)", engine.active_calls)

        await super().shutdown(sockets)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Run one sakhii-voice instance.")
    p.add_argument("--host", default=None, help="default HOST from settings (127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="default PORT from settings (8000)")
    args = p.parse_args(argv)
    s = get_settings()
    config = uvicorn.Config(
        "app.main:app",
        host=args.host or s.host,
        port=args.port or s.port,
        loop="uvloop",
        http="httptools",
        ws="websockets",
        ws_ping_interval=20,
        ws_ping_timeout=20,
        access_log=False,
        log_level=s.log_level.lower(),
    )
    DrainingServer(config).run()


if __name__ == "__main__":
    main()
