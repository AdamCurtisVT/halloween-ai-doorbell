"""Allowlisted structured state logs; never serialize arbitrary extra fields/errors."""
import json
import logging


class StateFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({'event': 'session_state',
                           'session_id': getattr(record, 'session_id', None),
                           'state': getattr(record, 'state', None)})


def configure():
    logger = logging.getLogger('doorbell.session')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StateFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
