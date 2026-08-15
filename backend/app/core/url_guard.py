"""Guardas anti-SSRF para URLs salientes controladas por configuración o por
payloads de terceros. Centraliza la lógica que antes vivía en el provider de
WhatsApp para reutilizarla (validación de base_url del fallback LLM, descarga de
adjuntos de webhooks, etc.).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


async def resolves_to_private(host: str) -> bool:
    """True si el host resuelve a una IP interna/privada/loopback/link-local, o
    si no resuelve (se trata como inseguro). `getaddrinfo` es síncrono → executor.
    """
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


async def validate_public_https_url(url: str) -> tuple[bool, str]:
    """Valida que `url` sea https y NO apunte a un host interno/privado.

    Devuelve (ok, motivo). Pensado para validar URLs que un admin configura
    (p. ej. el gateway del fallback LLM) antes de hacer peticiones server-side.
    """
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return False, "URL inválida"
    if p.scheme != "https":
        return False, "La URL debe empezar por https://"
    if not p.hostname:
        return False, "La URL no tiene host"
    if await resolves_to_private(p.hostname):
        return False, "El host apunta a una IP interna/privada (no permitido)"
    return True, ""
