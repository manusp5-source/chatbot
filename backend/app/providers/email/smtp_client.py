"""Envío de email por SMTP (p.ej. Google Workspace).

Usa la librería estándar `smtplib` en un hilo (sin dependencias nuevas ni
bloquear el event loop). Lee las credenciales del panel: smtp_host, smtp_port,
smtp_user, smtp_password (App Password de Google), smtp_from.
"""
import asyncio
import smtplib
from email.message import EmailMessage

from app.core.logging import get_logger
from app.services.credentials import get_credential

logger = get_logger(__name__)


async def smtp_configured() -> bool:
    host = await get_credential("smtp_host")
    user = await get_credential("smtp_user")
    pwd = await get_credential("smtp_password")
    return bool(host and user and pwd)


def _send_sync(
    host: str,
    port: int,
    user: str,
    pwd: str,
    from_email: str,
    to: str,
    subject: str,
    html: str,
    text: str | None,
) -> None:
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or "Tu cliente de correo no soporta HTML.")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.login(user, pwd)
        server.send_message(msg)


async def send_email_smtp(to: str, subject: str, body_html: str, body_text: str | None = None) -> bool:
    host = await get_credential("smtp_host")
    user = await get_credential("smtp_user")
    pwd = await get_credential("smtp_password")
    from_email = (await get_credential("smtp_from")) or user
    port_raw = await get_credential("smtp_port")
    if not (host and user and pwd):
        return False
    try:
        port = int(port_raw) if port_raw else 587
    except ValueError:
        port = 587
    try:
        await asyncio.to_thread(_send_sync, host, port, user, pwd, from_email or user, to, subject, body_html, body_text)
        return True
    except Exception as e:
        logger.error("smtp.error", error=str(e))
        return False
