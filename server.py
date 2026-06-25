import asyncio
import os
import signal
import threading

import uvicorn

from api import app
from main import load_settings, run_forever


def run_api(stop_event: threading.Event) -> None:
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    def watch_stop() -> None:
        stop_event.wait()
        server.should_exit = True

    threading.Thread(target=watch_stop, daemon=True).start()
    server.run()


async def run_scraper(stop_event: threading.Event) -> None:
    settings = load_settings()
    try:
        await run_forever(settings)
    finally:
        stop_event.set()


def main() -> None:
    stop_event = threading.Event()

    def request_stop(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    api_thread = threading.Thread(target=run_api, args=(stop_event,), daemon=True)
    api_thread.start()

    try:
        asyncio.run(run_scraper(stop_event))
    finally:
        stop_event.set()
        api_thread.join(timeout=10)


if __name__ == "__main__":
    main()
