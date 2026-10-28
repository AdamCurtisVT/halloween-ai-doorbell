import asyncio
import json
import logging
import sys


def emit(value):
    print(json.dumps(value, default=str))


def execute(main):
    # Third-party errors/logs may contain hosts, tokens or media URLs.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        print("Diagnostic failed. Check credentials, permissions, network, capabilities and local setup. No hardware success recorded.", file=sys.stderr)
        sys.exit(1)
