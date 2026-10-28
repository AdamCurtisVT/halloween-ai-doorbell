"""Standalone authenticated webhook receiver diagnostic: never starts AI, lights or audio."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace
import uvicorn
from app.config import Settings
from app.protect.webhook import WebhookInbox, create_webhook_app
from app.server import Listener
from scripts.common import emit, execute


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True, help='Specific LAN interface IP, not a public interface')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--seconds', type=int, choices=range(1, 301), default=60)
    args = parser.parse_args()
    settings = Settings()
    metrics = Counter()
    inbox = WebhookInbox(settings, metrics)
    runtime = SimpleNamespace(enabled=True, connected=True, manager=object(), metrics=metrics, inbox=inbox)
    server = Listener(uvicorn.Config(create_webhook_app(settings, runtime), host=args.bind,
                                     port=args.port, access_log=False, proxy_headers=False))
    task = asyncio.create_task(server.serve())
    observed = 0
    try:
        async with asyncio.timeout(args.seconds):
            while True:
                event = await inbox.queue.get()
                observed += 1
                emit({'normalized_event': asdict(event), 'source_ref': getattr(runtime, 'last_webhook_source_ref', None)})
    except TimeoutError:
        pass
    finally:
        server.should_exit = True
        await task
    emit({'observed_events': observed, 'metrics': dict(metrics), 'hardware_verified': False})
    if not observed:
        raise RuntimeError('No accepted real event')


if __name__ == '__main__':
    execute(main)
