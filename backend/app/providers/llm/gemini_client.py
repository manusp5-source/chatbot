"""Implementación Google Gemini del LLMProvider, sobre su capa OpenAI.

Gemini tiene su propia API nativa (`generateContent`, con `contents`/`parts`,
`functionDeclarations` y la clave en `?key=` o en la cabecera `x-goog-api-key`),
PERO Google publica además una capa compatible con la API de OpenAI:

    https://generativelanguage.googleapis.com/v1beta/openai/

Según la documentación oficial (https://ai.google.dev/gemini-api/docs/openai)
esa capa cubre chat completions, function calling (tools), streaming y
structured outputs — es decir, TODO lo que este chatbot necesita: el orquestador
solo hace `POST /chat/completions` sin streaming y con tools. Por eso Gemini no
lleva un cliente nativo: reusa el `OpenAIProvider` de siempre, que es el que
está en producción y el que tiene los tests. Escribir un traductor nativo
`contents`/`parts` sería más código y más superficie de fallo para el mismo
resultado.

Lo que esta subclase añade sobre el cliente genérico es lo único que Gemini hace
distinto y que sí nos afecta:

  1. La `base_url` correcta por defecto, para que el panel no dependa de que
     alguien la escriba bien a mano.
  2. Normalizar el id del modelo. El listado de Google devuelve los modelos como
     `models/gemini-3.1-pro-preview` (herencia de su API nativa, donde el
     nombre del recurso es la ruta). Si el admin elige ese id del desplegable,
     acaba guardado tal cual en la ficha del agente — y entonces la tabla de
     precios busca `models/gemini-3.1-pro-preview`, no lo encuentra y el gasto
     cuenta 0. Se recorta el prefijo en el único sitio por el que pasan todas
     las llamadas.

Advertencia declarada: la propia documentación de Google dice que el soporte de
las librerías de OpenAI "sigue en beta" y que los parámetros no soportados se
ignoran EN SILENCIO. Aquí no mandamos nada exótico (model, messages, tools,
temperature, max_tokens), así que el riesgo es bajo, pero conviene saberlo.
"""
from __future__ import annotations

from app.providers.llm.base import LLMCompletion, LLMMessage, LLMToolSchema
from app.providers.llm.families import GEMINI_BASE_URL
from app.providers.llm.openai_client import OpenAIProvider

# Prefijo que Google antepone a los ids en su listado de modelos.
_MODELS_PREFIX = "models/"


def normalize_gemini_model(model: str | None) -> str | None:
    """`models/gemini-3.1-pro-preview` → `gemini-3.1-pro-preview`.

    Se aplica también al buscar precio (services/openrouter_pricing.py), para
    que el id guardado y el tarifado sean el mismo.
    """
    if not model:
        return model
    m = model.strip()
    if m.startswith(_MODELS_PREFIX):
        return m[len(_MODELS_PREFIX) :]
    return m


class GeminiProvider(OpenAIProvider):
    """Gemini a través de su capa compatible con OpenAI."""

    def __init__(
        self,
        *,
        api_key_credential: str = "gemini_api_key",
        default_model: str | None = None,
        label: str = "gemini",
        explicit_api_key: str | None = None,
        explicit_base_url: str | None = None,
        accepts_temperature: bool | None = True,
    ) -> None:
        super().__init__(
            api_key_credential=api_key_credential,
            # Sin `base_url_credential` ni `model_credential`: la URL es fija y
            # el modelo lo elige el agente. Así `is_configured()` heredado pide
            # solo la clave, que es justo lo que hace falta.
            default_base_url=GEMINI_BASE_URL,
            default_model=normalize_gemini_model(default_model),
            label=label,
            explicit_api_key=explicit_api_key,
            explicit_base_url=(explicit_base_url or "").strip() or GEMINI_BASE_URL,
            accepts_temperature=accepts_temperature,
        )

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        source: str = "agent",
    ) -> LLMCompletion:
        # Normalizar ANTES de delegar: el cliente genérico usa el id tal cual
        # para llamar, para apuntar el consumo y para estimar el coste.
        return await super().complete(
            messages,
            tools,
            normalize_gemini_model(model),
            temperature,
            max_tokens,
            source,
        )
