"""Botón «Probar» del panel: valida la clave y lista los modelos reales.

Sirve para dos cosas a la vez (y por eso devuelve `models`): confirmar que la
clave funciona, y alimentar el desplegable de modelos de la ficha del agente
(`frontend/src/hooks/useProviderModels.ts`).

Cada familia se prueba con SU protocolo, porque el de OpenAI no vale:

  - OpenAI y pasarelas compatibles: `GET {base}/models` con Bearer. Si el
    endpoint no existe (el plan de coding de Z.AI no lo expone), se valida la
    clave con una chat completion mínima. Es la lógica que ya había, movida
    aquí tal cual.
  - Anthropic: `GET /v1/models` con `x-api-key` + `anthropic-version`, y
    PAGINANDO (`has_more` / `after_id`), que devuelve 20 por página: sin
    paginar, el desplegable enseñaría un recorte arbitrario del catálogo.
  - Gemini: `GET {base}/models` con Bearer sobre su capa compatible.

EL CASO DE GOOGLE (la razón principal de este módulo)
-----------------------------------------------------
Casi todas las APIs contestan 401 o 403 a una clave inválida. Google contesta
**400** con `"Please pass a valid API key"` (comprobado el 11-ago-2026 contra
`generativelanguage.googleapis.com`, tanto en `/models` como en
`/chat/completions`). Un comprobador que solo trate 401/403 como "clave mala"
enseña un «HTTP 400» opaco y el usuario no sabe si se ha equivocado de clave, de
URL o de modelo.

Por eso la clasificación no es solo por código: `_is_auth_error()` mira también
el CUERPO del error, de modo que un 400 que hable de la clave se reporta como
clave inválida, y un 400 por cualquier otro motivo se reporta como tal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from app.core.logging import get_logger
from app.providers.llm.families import ANTHROPIC_BASE_URL, GEMINI_BASE_URL, detect_family
from app.providers.llm.gemini_client import normalize_gemini_model

logger = get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 15.0
ANTHROPIC_VERSION = "2023-06-01"

# Pistas de "la clave no vale" dentro del cuerpo de un error. En minúsculas.
# Cubren Google ("Please pass a valid API key"), y de paso cualquier pasarela
# que también conteste 400 en vez de 401.
_AUTH_HINTS = (
    "api key",
    "api_key",
    "apikey",
    "unauthenticated",
    "unauthorized",
    "invalid authentication",
    "credential",
)

MSG_BAD_KEY = "El proveedor rechazó la clave API. Revísala y vuelve a guardarla."


@dataclass
class ProbeResult:
    ok: bool
    message: str
    models: list[str] = field(default_factory=list)


def _is_auth_error(status_code: int, body: str) -> bool:
    """¿Este error significa «clave inválida»?

    401 y 403 siempre. Un 400 SOLO si el cuerpo habla de la clave: es el caso
    de Google, y hay que distinguirlo de un 400 por petición mal formada.
    """
    if status_code in (401, 403):
        return True
    if status_code == 400:
        low = (body or "").lower()
        return any(h in low for h in _AUTH_HINTS)
    return False


async def probe_provider(*, base_url: str | None, api_key: str) -> ProbeResult:
    """Valida credenciales y devuelve los modelos disponibles del proveedor."""
    if not api_key:
        return ProbeResult(ok=False, message="Sin clave API configurada para este proveedor.")
    family = detect_family(base_url)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            if family == "anthropic":
                return await _probe_anthropic(client, base_url, api_key)
            if family == "gemini":
                return await _probe_gemini(client, base_url, api_key)
            return await _probe_openai(client, base_url, api_key)
    except Exception as e:  # noqa: BLE001 — el panel nunca debe ver un 500
        logger.warning("llm_provider.probe_failed", family=family, error=str(e))
        return ProbeResult(ok=False, message="No se pudo conectar con el proveedor.")


# ------------------------------ OpenAI-compatible ------------------------------


async def _probe_openai(
    client: httpx.AsyncClient, base_url: str | None, api_key: str
) -> ProbeResult:
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    r = await client.get(f"{base}/models", headers=headers)
    if _is_auth_error(r.status_code, r.text):
        return ProbeResult(ok=False, message=MSG_BAD_KEY)
    if r.status_code >= 400:
        # Algunos endpoints (p. ej. el plan coding de Z.AI) NO exponen /models.
        # Validamos la CLAVE con una chat completion mínima de modelo
        # inexistente: si la auth pasa, el proveedor responde "modelo
        # desconocido" (4xx != auth) → la clave es válida.
        ping = await client.post(
            f"{base}/chat/completions",
            headers=headers,
            json={
                "model": "__probe_ping__",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
        )
        if _is_auth_error(ping.status_code, ping.text):
            return ProbeResult(ok=False, message=MSG_BAD_KEY)
        return ProbeResult(
            ok=True,
            message=(
                "Clave válida. Este endpoint no expone la lista de modelos: "
                "escribe el id a mano en el agente (p. ej. glm-4.6)."
            ),
        )
    models = _ids_from_openai_payload(r.json())
    return ProbeResult(
        ok=True,
        message=f"Conexión correcta. {len(models)} modelo(s) disponibles.",
        models=models,
    )


def _ids_from_openai_payload(data: object) -> list[str]:
    items = data.get("data") if isinstance(data, dict) else data
    return sorted(
        str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")
    )


# ---------------------------------- Anthropic ----------------------------------


async def _probe_anthropic(
    client: httpx.AsyncClient, base_url: str | None, api_key: str
) -> ProbeResult:
    """`GET /v1/models` de Anthropic: `x-api-key`, versión, y paginado."""
    base = (base_url or ANTHROPIC_BASE_URL).rstrip("/")
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    models: list[str] = []
    after_id: str | None = None
    # Tope de páginas: el catálogo son decenas de modelos, no miles. Evita un
    # bucle infinito si `has_more` viniera mal.
    for _ in range(10):
        params: dict[str, str] = {"limit": "100"}
        if after_id:
            params["after_id"] = after_id
        r = await client.get(f"{base}/models", headers=headers, params=params)
        if _is_auth_error(r.status_code, r.text):
            return ProbeResult(ok=False, message=MSG_BAD_KEY)
        if r.status_code >= 400:
            return ProbeResult(
                ok=False,
                message=f"Anthropic respondió {r.status_code} al listar modelos.",
            )
        data = r.json()
        page = data.get("data") or []
        models.extend(
            str(m.get("id")) for m in page if isinstance(m, dict) and m.get("id")
        )
        if not data.get("has_more") or not page:
            break
        after_id = data.get("last_id") or (
            page[-1].get("id") if isinstance(page[-1], dict) else None
        )
        if not after_id:
            break
    models = sorted(set(models))
    return ProbeResult(
        ok=True,
        message=f"Conexión correcta. {len(models)} modelo(s) disponibles.",
        models=models,
    )


# ----------------------------------- Gemini -----------------------------------


async def _probe_gemini(
    client: httpx.AsyncClient, base_url: str | None, api_key: str
) -> ProbeResult:
    """Capa OpenAI de Gemini. El 400 de clave inválida se traduce bien."""
    base = (base_url or GEMINI_BASE_URL).rstrip("/")
    r = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
    if _is_auth_error(r.status_code, r.text):
        return ProbeResult(ok=False, message=MSG_BAD_KEY)
    if r.status_code >= 400:
        return ProbeResult(
            ok=False, message=f"Google respondió {r.status_code} al listar modelos."
        )
    # Google devuelve los ids como `models/gemini-…`; se guardan sin el prefijo
    # para que el id elegido en el desplegable sea el mismo que se tarifa.
    models = sorted(
        {
            normalize_gemini_model(m) or ""
            for m in _ids_from_openai_payload(r.json())
        }
        - {""}
    )
    return ProbeResult(
        ok=True,
        message=f"Conexión correcta. {len(models)} modelo(s) disponibles.",
        models=models,
    )
