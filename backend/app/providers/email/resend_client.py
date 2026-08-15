import httpx

from app.core.logging import get_logger
from app.services.credentials import get_credential

logger = get_logger(__name__)


async def send_email(to: str, subject: str, body_html: str, body_text: str | None = None) -> bool:
    # Preferimos SMTP (Google Workspace) si está configurado: consolida el email
    # en un solo proveedor. Si no hay SMTP, caemos a Resend.
    from app.providers.email.smtp_client import send_email_smtp, smtp_configured

    if await smtp_configured():
        return await send_email_smtp(to, subject, body_html, body_text)
    api_key = await get_credential("resend_api_key")
    from_email = await get_credential("resend_from_email")
    if not api_key or not from_email:
        logger.info("resend.skip.not_configured")
        return False
    payload = {
        "from": from_email,
        "to": [to],
        "subject": subject,
        "html": body_html,
    }
    if body_text:
        payload["text"] = body_text
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
        return True
    except Exception as e:
        logger.error("resend.error", error=str(e))
        return False
