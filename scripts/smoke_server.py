"""Exercise the real HTTP server with isolated, disabled, synthetic configuration."""
import os
import argparse
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dual-listener', action='store_true', help='Exercise the Docker entrypoint on fixed internal ports 8000/8080')
    args = parser.parse_args()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    if args.dual_listener:
        port = 8000
    token = secrets.token_hex(24)
    # Isolate dotenv and environment so the smoke test cannot start real hardware/providers.
    environment = {'PATH': os.environ.get('PATH', ''), 'HOME': '/tmp',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                   'FEATURE_ENABLED': 'false', 'CONTROL_API_TOKEN': token,
                   'PYTHONDONTWRITEBYTECODE': '1'}
    with tempfile.TemporaryDirectory(prefix='doorbell-http-smoke-') as directory:
        command = [sys.executable, '-m', 'app.server'] if args.dual_listener else [
            sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port), '--no-access-log']
        process = subprocess.Popen(command,
                                   cwd=directory, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=1) as client:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        assert client.get('/health').status_code == 200
                        break
                    except httpx.TransportError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('Server failed to start') from None
                        time.sleep(0.1)
                assert client.get('/status').status_code == 401
                client.headers['Authorization'] = 'Bearer ' + token
                assert client.get('/status').json()['enabled'] is False
                assert client.get('/ready').status_code == 503
                assert client.get('/preflight').json()['ok'] is False
                approval = {'approve_camera_audio': True}
                assert client.post('/enable', json=approval).status_code == 409
                assert client.post('/test/session', json=approval).status_code == 409
                assert client.post('/disable').json()['enabled'] is False
                if args.dual_listener:
                    assert client.post('/webhooks/unifi', json={}).status_code == 404
                    assert client.get('http://127.0.0.1:8080/status').status_code == 404
                    assert client.post('http://127.0.0.1:8080/webhooks/unifi', json={}).status_code == 403
            print('HTTP startup, authentication, preflight, disabled controls and shutdown smoke passed; no hardware/provider calls')
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
