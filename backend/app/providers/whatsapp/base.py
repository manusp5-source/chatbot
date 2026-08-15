"""Interfaz abstracta para proveedores de WhatsApp.

Implementaciones concretas: `ycloud` (revendedor) y `meta` (API Cloud oficial de
Meta / WhatsApp Business Platform). Cuál se usa NO es una variable de entorno:
lo elige cada instalación desde el panel y se guarda en el canal de WhatsApp
(`Channel.config["provider"]`). Ver `app/providers/whatsapp/selector.py`.

Aquí viven, además de la interfaz, las piezas que comparten los dos
proveedores: los errores de envío y la normalización del identificador de
destino. Estaban en `ycloud.py` y medio repo los importaba de ahí; se quedan
re-exportados allí para no tocar esos imports.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

MediaKind = Literal["image", "audio", "video", "document", "sticker"]

# Prefijo del identificador de contacto cuando NO tenemos teléfono y solo
# tenemos el BSUID (business-scoped user id) de WhatsApp. Mismo patrón que
# "ig:", "web:" y "email:" que ya usa store_incoming para inferir el canal:
# "wa:" cae en el `else` (canal whatsapp), que es justo lo que queremos.
WA_USER_PREFIX = "wa:"


class WhatsAppNotConfiguredError(RuntimeError):
    """No hay credenciales de WhatsApp: NO se ha llamado a la API.

    Antes esto era un `return "noop"`: el envío no ocurría pero quien llamaba
    lo contaba como éxito, así que una campaña a 300 personas decía "300
    enviados" sin haber mandado un solo mensaje. Ahora es un error explícito y
    quien envía tiene que tratarlo. Vale para los dos proveedores.
    """


class WhatsAppCredentialsUnreadableError(WhatsAppNotConfiguredError):
    """Las credenciales EXISTEN pero no se pueden descifrar.

    Caso típico: se cambió `ENCRYPTION_KEY` y lo guardado ya no se abre. Es
    distinto de "no configuradas" — el arreglo es otro (restaurar la clave, no
    volver a escribir la credencial) y confundirlos manda al operador a
    reintroducir credenciales que en realidad están bien.
    """


class TemplateListError(RuntimeError):
    """No se pudo LISTAR las plantillas (401, 500, JSON roto…).

    Devolver [] en estos casos hacía que la pantalla dijera siempre lo mismo
    ("configura las credenciales o aprueba alguna plantilla") tanto con una
    cuenta sin plantillas como con la clave caducada.
    """


# Formatos de cabecera de plantilla que llevan FICHERO. El texto va aparte
# (lleva variables, no fichero) y "none" significa que no hay cabecera.
HEADER_MEDIA_FORMATS = ("image", "video", "document")


def text_parameters(
    values: list[str], param_format: str, names: list[str] | None = None
) -> list[dict]:
    """Parámetros de texto de un componente, posicionales o CON NOMBRE.

    Meta ofrece hoy los dos formatos: `{{1}}` (posicional) y `{{nombre}}`
    (con nombre). Con nombre, cada parámetro necesita su `parameter_name`; sin
    él Meta rechaza el envío entero.

    El formato de los componentes es de Meta: YCloud lo reenvía tal cual, así
    que esto sirve igual para los dos proveedores.
    """
    out: list[dict] = []
    for i, v in enumerate(values):
        p: dict = {"type": "text", "text": str(v)}
        if param_format == "named":
            name = (names[i] if names and i < len(names) else "") or str(i + 1)
            p["parameter_name"] = str(name)
        out.append(p)
    return out


def build_header_component(header: dict | None, param_format: str) -> dict | None:
    """Componente `header` a partir de la config del envío, o None si no hay.

    Media: `{"format": "image", "media_id": "..."}` o `{"link": "https://..."}`.
    Texto con variables: `{"format": "text", "variables": ["…"]}`.
    """
    if not header:
        return None
    fmt = str(header.get("format") or "").strip().lower()
    if fmt in HEADER_MEDIA_FORMATS:
        media: dict = {}
        if header.get("media_id"):
            media["id"] = str(header["media_id"])
        elif header.get("link"):
            media["link"] = str(header["link"])
        else:
            raise ValueError(
                f"La cabecera de tipo '{fmt}' necesita un fichero subido (media_id) o un enlace."
            )
        if fmt == "document" and header.get("filename"):
            media["filename"] = str(header["filename"])
        return {"type": "header", "parameters": [{"type": fmt, fmt: media}]}
    if fmt == "text":
        values = [str(v) for v in (header.get("variables") or [])]
        if not values:
            return None
        return {
            "type": "header",
            "parameters": text_parameters(
                values, param_format, header.get("variable_names")
            ),
        }
    return None


def normalize_phone(phone: str) -> str:
    """Teléfono a E.164 (`+34600...`), dejando intacto un `wa:<bsuid>`.

    Un identificador "wa:<bsuid>" NO es un teléfono: si se cuela por aquí lo
    devolvemos tal cual en vez de convertirlo en "+wa:ES.123", que no es
    ninguna de las dos cosas y rompería el envío en silencio.
    """
    s = phone.strip().replace(" ", "")
    if s.startswith(WA_USER_PREFIX):
        return s
    if not s.startswith("+"):
        s = "+" + s.lstrip("0")
    return s


@dataclass
class IncomingMessage:
    """Normalización del mensaje entrante, independiente del proveedor."""

    provider_message_id: str          # id externo (para idempotencia)
    from_phone: str                    # E.164 normalizado, o "wa:<bsuid>" si no hay teléfono
    to_phone: str                      # E.164 normalizado
    message_type: Literal[
        "text", "audio", "image", "video", "document", "sticker", "other"
    ]
    text: str | None = None
    audio_url: str | None = None       # URL para descargar
    audio_mime: str | None = None
    # Adjuntos que NO son nota de voz (imagen, vídeo, documento, sticker). El
    # audio sigue con campos propios porque su pipeline es otro (transcripción).
    # `media_url` es la URL del proveedor, que CADUCA: store_incoming la descarga
    # y guarda una copia local, igual que hacemos con las notas de voz.
    media_kind: MediaKind | None = None
    media_url: str | None = None
    media_mime: str | None = None
    media_filename: str | None = None
    customer_name: str | None = None
    # Instagram: el @usuario (handle) separado del nombre real, para mostrar
    # "Nombre" + "@usuario" debajo en la bandeja. Solo lo rellena Instagram.
    customer_handle: str | None = None
    # WhatsApp (desde abril 2026): identificador del cliente ámbito-negocio.
    # Llega SIEMPRE, tenga el cliente nombre de usuario o no; el teléfono, en
    # cambio, solo viaja si hubo contacto en los últimos 30 días o si está en la
    # agenda. Ojo: el BSUID se REGENERA si el cliente cambia de número, así que
    # no sirve como clave inmutable.
    from_user_id: str | None = None
    from_parent_user_id: str | None = None
    raw: dict | None = None


class WhatsAppProvider(ABC):
    """Contrato de un proveedor de WhatsApp Business API."""

    @abstractmethod
    async def verify_webhook_signature(self, headers: dict[str, str], body: bytes) -> bool:
        ...

    @abstractmethod
    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        """Devuelve lista (un webhook puede traer varios eventos)."""
        ...

    @abstractmethod
    async def send_text(self, to_phone: str, body: str) -> str:
        """Devuelve el id externo del mensaje enviado."""
        ...

    @abstractmethod
    async def download_audio(self, audio_url: str) -> tuple[bytes, str]:
        """Descarga el audio. Devuelve (bytes, mime_type)."""
        ...

    @abstractmethod
    async def upload_media(self, file_bytes: bytes, mime: str, filename: str) -> str:
        """Sube el archivo al proveedor y devuelve un media_id reutilizable.

        En YCloud/Meta este id es lo que se referencia luego en
        `send_media` — el archivo binario no viaja en cada send.
        """
        ...

    @abstractmethod
    async def send_media(
        self,
        to_phone: str,
        media_id: str,
        media_kind: MediaKind,
        *,
        caption: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Envía un mensaje multimedia. Devuelve el id externo (wamid)."""
        ...
