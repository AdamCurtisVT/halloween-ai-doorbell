import argparse
import logging
from app.config import Settings
from app.preflight import preflight
from scripts.common import emit, execute


async def main():
    parser = argparse.ArgumentParser(description='Offline configuration checks; optional authenticated read-only checks')
    parser.add_argument('--live-protect', action='store_true', help='Authenticate to Protect and select the camera; no media')
    parser.add_argument('--provider', action='store_true', help='Check configured model catalog access; no generation or media upload')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    result = await preflight(Settings(), live=args.live_protect, provider=args.provider)
    emit(result)
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    execute(main)
