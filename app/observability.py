"""Allowlisted structured logs; never serialize arbitrary extra fields/errors."""
from collections import deque
from datetime import datetime, timezone
import json
import logging

FAILURES = logging.getLogger('doorbell.failure')
# Most recent failures for /status; bounded and in memory only.
RECENT = deque(maxlen=20)


class StateFormatter(logging.Formatter):
    def format(self, record):
        if record.name == FAILURES.name:
            return json.dumps({'event': 'failure', 'stage': getattr(record, 'stage', None),
                               'error': getattr(record, 'error', None)})
        return json.dumps({'event': 'session_state',
                           'session_id': getattr(record, 'session_id', None),
                           'state': getattr(record, 'state', None)})


def describe(error):
    """Exception class and HTTP status only: messages can carry URLs, credentials or transcripts."""
    status = getattr(error, 'status_code', None)
    if status is None:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
    return type(error).__name__ + (f' {status}' if isinstance(status, int) else '')


def failure(stage, error):
    error = describe(error)
    RECENT.append({'stage': stage, 'error': error, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    FAILURES.warning('failure', extra={'stage': stage, 'error': error})


def configure():
    for name, level in (('doorbell.session', logging.INFO), (FAILURES.name, logging.WARNING)):
        logger = logging.getLogger(name)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(StateFormatter())
            logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
