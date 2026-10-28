import secrets
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from typing import Literal
from app.preflight import local_checks, report
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.config import Settings
from app.runtime import Runtime


class CameraAudioApproval(BaseModel):
    approve_camera_audio: Literal[True]


def create_app(settings: Settings | None = None, runtime=None, *, manage_runtime=True) -> FastAPI:
    settings = settings or Settings()
    runtime = runtime or Runtime(settings)

    @asynccontextmanager
    async def lifespan(api):
        if manage_runtime:
            await runtime.start()
        try:
            yield
        finally:
            if manage_runtime:
                await runtime.close()

    api = FastAPI(title="Halloween AI Doorbell", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    api.state.runtime = runtime
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        token = settings.control_api_token.get_secret_value()
        if not token or not credentials or not secrets.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="Authentication required")

    @api.get("/health")
    def health():
        return {"status": "ok", "phase": "development", "build": "conversation-development"}

    @api.get("/status", dependencies=[Depends(authorize)])
    async def status():
        return runtime.status()

    @api.get("/ready", dependencies=[Depends(authorize)])
    async def ready():
        state = runtime.status()
        if state.get('trigger_source') != 'websocket' and not state.get('webhook_listener_ready', True):
            raise HTTPException(status_code=503, detail='Webhook listener unavailable')
        if not state['enabled'] or not state['protect_connected'] or (
                state.get('trigger_source', 'websocket') == 'websocket' and not state['event_subscription_connected']):
            raise HTTPException(status_code=503, detail="Disabled or Protect unavailable")
        if state['mode'] == 'interactive' and not state['microphone_healthy']:
            raise HTTPException(status_code=503, detail="Microphone degraded; greeting-only fallback")
        return {"ready": True, "hardware_verified": False}

    @api.get("/preflight", dependencies=[Depends(authorize)])
    def preflight():
        return report(local_checks(settings))

    @api.post("/enable", dependencies=[Depends(authorize)])
    async def enable(approval: CameraAudioApproval):
        try:
            await runtime.enable()
        except ValueError:
            raise HTTPException(status_code=409, detail="Activation failed; inspect /preflight") from None
        return {"enabled": True, "hardware_verified": False}

    @api.post("/test/session", status_code=202, dependencies=[Depends(authorize)])
    async def test_session(approval: CameraAudioApproval):
        if not runtime.test_session():
            raise HTTPException(status_code=409, detail="Disabled, disconnected, busy, occupied or cooling down")
        return {"accepted": True, "hardware_verified": False}

    @api.post("/disable", dependencies=[Depends(authorize)])
    async def disable():
        await runtime.disable()
        return {"enabled": False}

    @api.post("/cancel", dependencies=[Depends(authorize)])
    async def cancel():
        if runtime.manager:
            await runtime.manager.cancel()
        return {"cancelled": True}

    return api


app = create_app()
