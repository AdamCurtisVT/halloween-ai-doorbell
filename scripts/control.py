"""Local operational CLI; reads bearer token from Settings rather than command arguments."""
import argparse
import ipaddress
import logging
import sys
from urllib.parse import urlsplit
import httpx
from app.config import Settings


ENDPOINTS = {'status': ('GET', '/status'), 'ready': ('GET', '/ready'),
             'preflight': ('GET', '/preflight'), 'enable': ('POST', '/enable'),
             'disable': ('POST', '/disable'), 'cancel': ('POST', '/cancel'),
             'test-session': ('POST', '/test/session')}


def loopback_url(value):
    parsed = urlsplit(value)
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
    except ValueError:
        is_loopback = False
    if (parsed.scheme != 'http' or not is_loopback or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('Use an http loopback IP URL without credentials, path or query')
    return value.rstrip('/')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=ENDPOINTS)
    parser.add_argument('--url', default='http://127.0.0.1:8000', type=loopback_url)
    parser.add_argument('--approve-camera-audio', action='store_true')
    args = parser.parse_args()
    if args.command in ('enable', 'test-session') and not args.approve_camera_audio:
        parser.error('This command may capture camera/microphone media, call providers and play audio; supply --approve-camera-audio')
    logging.disable(logging.CRITICAL)
    try:
        from scripts.common import emit
        token = Settings().control_api_token.get_secret_value()
        if not token:
            raise ValueError('Control token required')
        method, endpoint = ENDPOINTS[args.command]
        payload = {'approve_camera_audio': True} if args.command in ('enable', 'test-session') else None
        with httpx.Client(timeout=45, trust_env=False, follow_redirects=False) as client:
            response = client.request(method, args.url + endpoint,
                                      headers={'Authorization': 'Bearer ' + token}, json=payload)
        # Only our API's JSON is expected; avoid printing arbitrary proxy/error bodies.
        if response.status_code >= 400:
            emit({'ok': False, 'http_status': response.status_code, 'hint': 'Inspect status/preflight or verify the token'})
            return 1
        emit(response.json())
        return 0
    except Exception:
        print('Control request failed; check the local service and token configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
