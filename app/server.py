"""Two HTTP listeners, one orchestration runtime. Only webhooks are exposed to the LAN."""
import asyncio
from contextlib import contextmanager
import signal
import uvicorn
from app.config import Settings
from app.main import create_app
from app.protect.webhook import create_webhook_app
from app.runtime import Runtime


class Listener(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield  # The shared parent owns shutdown for both listeners.


async def serve():
    settings = Settings()
    runtime = Runtime(settings)
    listeners = [Listener(uvicorn.Config(create_app(settings, runtime, manage_runtime=False),
                    host='0.0.0.0', port=8000, access_log=False, proxy_headers=False,
                    timeout_graceful_shutdown=10)),
                 Listener(uvicorn.Config(create_webhook_app(settings, runtime),
                    host='0.0.0.0', port=8080, access_log=False, proxy_headers=False,
                    limit_concurrency=64, timeout_keep_alive=5, timeout_graceful_shutdown=10))]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = []
    unexpected = False
    try:
        await runtime.start()
        tasks = [asyncio.create_task(listener.serve()) for listener in listeners]
        async with asyncio.timeout(10):
            while not all(listener.started for listener in listeners):
                if any(task.done() for task in tasks):
                    raise RuntimeError('HTTP listener failed to start')
                await asyncio.sleep(0.05)
        runtime.webhook_listener_ready = True
        waiter = asyncio.create_task(stop.wait())
        await asyncio.wait([*tasks, waiter], return_when=asyncio.FIRST_COMPLETED)
        unexpected = not stop.is_set()
        waiter.cancel()
    finally:
        runtime.webhook_listener_ready = False
        for listener in listeners:
            listener.should_exit = True
        try:
            await runtime.close()  # Cancel media promptly, before waiting for HTTP draining.
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    if unexpected:
        raise RuntimeError('HTTP listener stopped unexpectedly')


if __name__ == '__main__':
    asyncio.run(serve())
