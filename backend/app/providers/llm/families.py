"""A qué FAMILIA de API pertenece un proveedor, deducido de su `base_url`.

Hasta ahora todos los proveedores eran "un cliente de OpenAI con otra URL": el
mismo `POST /chat/completions`, la misma cabecera `Authorization: Bearer`, el
mismo formato de tools. Anthropic NO encaja ahí (cabecera `x-api-key`, endpoint
`/messages`, `system` como parámetro aparte y otro formato de tool use), así que
hace falta saber, ANTES de construir el cliente, con qué API estamos hablando.

Por qué por `base_url` y no por una columna nueva en `llm_providers`:

  - La URL YA es la identidad del proveedor en este código. `_is_openai_official`
    (api/admin.py) decide "¿es OpenAI oficial?" con `not base_url`, y el aviso de
    transferencia internacional del panel se decide por el host. Deducir la
    familia del host es la misma regla, no una nueva.
  - Una columna `kind` obligaría a tocar los esquemas `LLMProviderIn/Out` de
    `api/admin.py` para poder rellenarla desde el panel. Sin eso, la columna
    existiría pero nadie podría darle valor, así que el proveedor quedaría
    inutilizable desde la interfaz.

Limitación conocida y asumida: si alguien pone Anthropic o Gemini DETRÁS de un
proxy con dominio propio, la detección por host no lo reconoce y cae a
"openai" (que es lo correcto para una pasarela compatible, y lo que hacen casi
todos los proxies). Para el caso raro de un proxy que hable el protocolo NATIVO
de Anthropic hay `LLM_ANTHROPIC_EXTRA_HOSTS` / `LLM_GEMINI_EXTRA_HOSTS`
(hosts separados por comas) — así se resuelve sin migración ni redespliegue del
panel. La alternativa definitiva (columna `kind`) queda documentada en el
informe de entrega.
"""
from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlparse

# "openai" = la API de OpenAI y CUALQUIER pasarela compatible con ella
# (OpenRouter, DeepSeek, Z.AI, Together, Azure…). Es el comportamiento de
# siempre y el que está en producción: ante la duda, se cae aquí.
LLMFamily = Literal["openai", "anthropic", "gemini"]

# API nativa de Anthropic (Messages API). El `/v1` va incluido porque el resto
# de base_url del panel también lo llevan.
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"

# Gemini expuesto por su CAPA COMPATIBLE CON OPENAI. Documentado por Google en
# https://ai.google.dev/gemini-api/docs/openai — soporta chat completions,
# function calling (tools), streaming y structured outputs, que es exactamente
# lo que usa este chatbot. Por eso Gemini NO necesita un cliente nativo: reusa
# el `OpenAIProvider` de siempre, ya probado en producción.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# Host oficial de cada familia. Se compara por sufijo de dominio para aceptar
# subdominios (p. ej. un `eu.api.anthropic.com` futuro) sin que "notapi
# anthropic.com.evil.net" cuele: el sufijo debe empezar en un punto.
_ANTHROPIC_HOSTS = ("api.anthropic.com",)
_GEMINI_HOSTS = ("generativelanguage.googleapis.com",)


def _extra_hosts(var: str) -> tuple[str, ...]:
    """Hosts adicionales que fuerzan una familia (escape hatch por entorno)."""
    raw = os.getenv(var) or ""
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    """True si `host` es exactamente uno de `patterns` o subdominio suyo."""
    return any(host == p or host.endswith("." + p) for p in patterns)


def detect_family(base_url: str | None) -> LLMFamily:
    """Familia de API a la que apunta `base_url`.

    `None` / vacío = OpenAI oficial (retrocompatibilidad: es como está guardado
    el proveedor "OpenAI" sembrado). Cualquier host desconocido = "openai",
    porque una pasarela compatible es el caso habitual y es el comportamiento
    que ya había.
    """
    url = (base_url or "").strip()
    if not url:
        return "openai"
    # urlparse necesita esquema para poblar `netloc`; si falta, lo suponemos.
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "openai"
    if _host_matches(host, _ANTHROPIC_HOSTS + _extra_hosts("LLM_ANTHROPIC_EXTRA_HOSTS")):
        return "anthropic"
    if _host_matches(host, _GEMINI_HOSTS + _extra_hosts("LLM_GEMINI_EXTRA_HOSTS")):
        return "gemini"
    return "openai"
