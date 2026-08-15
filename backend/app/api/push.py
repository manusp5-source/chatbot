"""Endpoints de Web Push: clave pública VAPID + alta/baja de suscripción.

Cualquier usuario autenticado puede suscribir su dispositivo (la PWA del
operador). El envío lo dispara el backend cuando una conversación necesita
atención (ver services/web_push.py + disparadores en conversation/handoff).
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.common import OkResponse
from app.services.web_push import (
    delete_subscription,
    get_vapid_public_key,
    save_subscription,
)

router = APIRouter(prefix="/push", tags=["push"])


class VapidKeyOut(BaseModel):
    public_key: str


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeBody(BaseModel):
    endpoint: str
    keys: SubscriptionKeys


class UnsubscribeBody(BaseModel):
    endpoint: str


@router.get("/vapid-public-key", response_model=VapidKeyOut)
async def vapid_public_key(_: User = Depends(get_current_user)) -> VapidKeyOut:
    """Clave pública VAPID (base64url) para `pushManager.subscribe`."""
    return VapidKeyOut(public_key=await get_vapid_public_key())


@router.post("/subscribe", response_model=OkResponse)
async def subscribe(
    body: SubscribeBody,
    request: Request,
    user: User = Depends(get_current_user),
) -> OkResponse:
    """Registra (o actualiza) la suscripción del dispositivo del operador."""
    await save_subscription(
        user_id=user.id,
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        user_agent=request.headers.get("user-agent"),
    )
    return OkResponse(ok=True)


@router.post("/unsubscribe", response_model=OkResponse)
async def unsubscribe(
    body: UnsubscribeBody,
    _: User = Depends(get_current_user),
) -> OkResponse:
    await delete_subscription(body.endpoint)
    return OkResponse(ok=True)
