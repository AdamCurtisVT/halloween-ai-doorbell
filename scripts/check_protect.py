import argparse
import asyncio
from dataclasses import asdict
from pathlib import Path
from app.config import Settings
from app.protect.client import connect
from scripts.common import emit, execute


async def main():
    parser = argparse.ArgumentParser(description="Protect connectivity/capability diagnostic")
    parser.add_argument("--list-ids", action="store_true", help="PRIVATE: reveal camera IDs locally")
    parser.add_argument("--snapshot", type=Path, help="Explicit opt-in JPEG destination")
    parser.add_argument("--events-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.events_seconds <= 300:
        parser.error("events-seconds must be 0–300")
    async with connect(Settings()) as adapter:
        emit(await adapter.report())
        if args.list_ids:
            emit({"private_camera_ids": list(adapter.client.bootstrap.cameras)})
        if args.snapshot:
            async with asyncio.timeout(15):
                data = await adapter.snapshot()
            with args.snapshot.open("xb") as output:
                output.write(data)
            emit({"jpeg_received": True, "bytes": len(data), "visual_verification": "pending"})
        if args.events_seconds:
            count = 0
            async for event in adapter.events(args.events_seconds):
                count += 1
                emit(asdict(event))
            emit({"person_messages": count, "dropped_events": adapter.dropped_events,
                  "result": "messages_received" if count else "no_person_event_observed"})
            if not count:
                raise RuntimeError("No real person event observed")


if __name__ == "__main__":
    execute(main)
