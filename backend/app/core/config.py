from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_DEFAULTS = {"", "change-me-in-prod"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # Application
    # Nombre visible del producto (título de la API, emails transaccionales,
    # páginas de OAuth…). Cada instalación pone el suyo; el default es neutro.
    # El frontend tiene su equivalente en runtime (APP_NAME → config.js).
    APP_NAME: str = "Chatbot"
    APP_ENV: str = "development"
    APP_PORT: int = 8000
    APP_BASE_URL: str = "http://localhost:8000"
    FRONTEND_BASE_URL: str = "http://localhost:5173"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://chatbot:chatbot@db:5432/chatbot"

    # Redis / Celery
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # Security
    JWT_SECRET: str = "change-me-in-prod"
    JWT_EXPIRATION_HOURS: int = 24
    JWT_ALGORITHM: str = "HS256"
    ENCRYPTION_KEY: str = ""
    # Nº de proxies de confianza por delante (EasyPanel/nginx suele ser 1). Se usa
    # para derivar la IP real del cliente de X-Forwarded-For tomando la entrada
    # añadida por NUESTRO proxy (la de la derecha), no la del principio que el
    # cliente puede falsificar. 0 = ignorar XFF y usar la IP de socket.
    TRUSTED_PROXY_COUNT: int = 1

    # Initial admin
    INITIAL_ADMIN_EMAIL: str = "admin@local"
    INITIAL_ADMIN_PASSWORD: str = "ChangeMe123!"

    # Web Push (VAPID): contacto del operador que exigen los push services.
    # Vacío = se deriva del email del admin (mailto:INITIAL_ADMIN_EMAIL).
    # Acepta "mailto:..." o un email a secas.
    VAPID_SUBJECT: str = ""

    # Storage
    AUDIO_STORAGE_PATH: str = "/data/audios"
    # Uploads salientes del operador (audios, archivos, imágenes que él envía).
    # Comparte volumen con AUDIO_STORAGE_PATH para no exigir un mount nuevo
    # en EasyPanel — basta crear la subcarpeta `uploads/`.
    UPLOADS_PATH: str = "/data/audios/uploads"
    # Backups diarios de Postgres (tarea Celery backup_db, beat 05:00).
    # BACKUP_DIR comparte el volumen de audios igual que UPLOADS_PATH: es el
    # ÚNICO volumen persistente montado en app/worker; cualquier ruta fuera de
    # /data/audios se PIERDE al recrear el contenedor.
    BACKUP_DIR: str = "/data/audios/backups"
    # Días que se conserva cada dump antes de borrarlo (retención local).
    BACKUP_RETENTION_DAYS: int = 7
    # Tope de dumps locales conservados, ADEMÁS de la retención por días. Sin
    # él, una frecuencia horaria acumula 168 ficheros antes de que el primero
    # cumpla 7 días: el disco se llena garantizado y la retención por edad no
    # libera un solo byte justo cuando más falta hace. 0 = sin tope.
    BACKUP_RETENTION_MAX_FILES: int = 14
    # Prefijo de los ficheros de dump (<prefijo>_<fecha>.dump). Vacío = se
    # deriva del nombre de la BD (DATABASE_URL). La retención sigue limpiando
    # también los dumps con el patrón histórico chatbot_*.dump.
    BACKUP_FILE_PREFIX: str = ""
    # Si quedan menos GB libres que esto en el disco de BACKUP_DIR, NO se hace
    # dump (evita llenar el disco y tumbar la BD) y se alerta por seguridad.
    # OJO: este guardia solo bloquea la ESCRITURA LOCAL. Con destino bucket la
    # copia se hace igual, en streaming y sin fichero local (backup_db).
    BACKUP_MIN_FREE_GB: float = 2.0
    # Tamaño de bloque (MB) del cifrado por trozos y de la subida multiparte
    # cuando la copia va directa al bucket sin fichero local. 8 MB es el
    # equilibrio de boto3: pico de RAM acotado y pocas partes.
    BACKUP_STREAM_CHUNK_MB: int = 8
    # Chequeos diarios de las copias (los dispara backup_tick, no el beat):
    # prueba de conexión al bucket + verificación real de la última copia
    # (descarga + descifrado + cabecera de dump de PostgreSQL).
    BACKUP_DAILY_CHECKS: bool = True
    # Límites en bytes (Whatsapp Business API).
    MEDIA_MAX_IMAGE_BYTES: int = 5 * 1024 * 1024
    MEDIA_MAX_AUDIO_BYTES: int = 16 * 1024 * 1024
    MEDIA_MAX_VIDEO_BYTES: int = 16 * 1024 * 1024
    MEDIA_MAX_DOCUMENT_BYTES: int = 100 * 1024 * 1024
    MEDIA_MAX_STICKER_BYTES: int = 1 * 1024 * 1024  # webp pequeño

    # Defaults agente
    DEFAULT_LLM_MODEL: str = "gpt-5.4-mini"
    # Temperature y max_tokens YA NO se configuran a nivel global:
    # los modelos GPT-5.x son "reasoning" y no aceptan ajuste de temperature;
    # max_completion_tokens lo gestiona OpenAI automaticamente. Mantenemos
    # estos atributos como propiedad para compatibilidad con codigo legacy
    # (provider, agent_config) pero ya no se leen del env.
    @property
    def DEFAULT_LLM_TEMPERATURE(self) -> float:
        return 1.0

    @property
    def DEFAULT_LLM_MAX_TOKENS(self) -> int:
        return 0  # 0 = no enviar parametro, dejar default OpenAI

    # Segundos que el agente espera tras el último mensaje para agrupar ráfagas
    # (el cliente suele escribir varios mensajes seguidos). Default para nuevas
    # configuraciones; las existentes se editan por panel. 0 lo desactivaría, por
    # eso el panel fuerza un mínimo de 1.
    MESSAGE_BUFFER_SECONDS: int = 8
    RESPONSE_SPLIT_MAX_PARTS: int = 3
    # Suelo del buffer para canales de TEXTO (WhatsApp/Instagram/web/email). El
    # agrupado de ráfagas solo funciona si el buffer supera el hueco entre
    # mensajes seguidos de una persona (~1-2s); por debajo, el agente contesta a
    # cada mensaje por separado. La VOZ no aplica este suelo (necesita latencia
    # baja). Sube el buffer efectivo aunque el canal resuelva a un agente mal
    # configurado o al de voz por fallback.
    MIN_TEXT_BUFFER_SECONDS: int = 6
    DEFAULT_CONTEXT_WINDOW: int = 20
    # Si False, conversaciones nuevas se crean en estado "humano" (no responde el agente).
    # Útil en modo pruebas hasta que el agente esté entrenado.
    AGENT_AUTORESPONSE_DEFAULT: bool = True

    # Zona horaria del negocio para los agregados del dashboard/Home (buckets
    # por hora, "hoy" vs "ayer"). Configurable por despliegue para que las
    # métricas se lean en hora local, no UTC.
    DASHBOARD_TIMEZONE: str = "Europe/Madrid"

    # Retención del canal Email (minimización de datos). A los N meses se purga
    # el CONTENIDO del correo de la BD (Gmail es el ARCHIVO, la BD una CACHÉ):
    # se vacía Message.contenido y se elimina extra.html_body, conservando solo
    # una entrada ligera (id de Gmail, asunto, cabeceras) para el histórico del
    # CRM y para poder recuperarlo bajo demanda desde Gmail. Solo afecta a email.
    EMAIL_RETENTION_MONTHS: int = 6
    # 0 (o negativo) significa NO PURGAR, que es lo que cualquiera entiende al
    # escribir 0. Antes 0 se traducía en "corte = ahora" y vaciaba el contenido
    # de TODOS los correos en la primera pasada. Y por debajo de este mínimo la
    # purga se sube a él: una retención de días no da margen ni para revisar un
    # hilo reciente, y el cuerpo solo se recupera si sigue vivo en Gmail.
    EMAIL_RETENTION_MIN_MONTHS: int = 1

    # Firma que se añade al final de TODO correo saliente (borradores del agente
    # y respuestas manuales desde el panel). La API de Gmail NO añade la firma de
    # la cuenta — eso solo lo hace la interfaz web al redactar —, así que sin
    # esto los correos salían sin nombre ni empresa. Se puede sobreescribir por
    # canal en Channel.config["email_signature"]. Vacío = sin firma.
    EMAIL_SIGNATURE: str = ""

    # Ingesta de Gmail: intentos máximos de un correo que falla al procesarse
    # antes de darlo por perdido. Los fallidos van a una cola de reintentos
    # persistente (Channel.config["ingest_retry_queue"]); al agotar los intentos
    # se avisa en Monitorización, porque Gmail no vuelve a ofrecer ese correo.
    GMAIL_INGEST_MAX_ATTEMPTS: int = 5

    # Cerrojo por conversación del runtime del agente (segundos). Impide que dos
    # ejecuciones respondan a la vez a la misma conversación (el agente puede
    # tardar 20-40 s con varias vueltas de herramientas). Con caducidad, para que
    # un proceso muerto no deje la conversación bloqueada para siempre.
    AGENT_CONV_LOCK_TTL_S: int = 180

    # Retención de las NOTAS DE VOZ en disco (RGPD 5.1.e). Pasados estos días,
    # una tarea diaria borra el fichero de audio (lo más pesado y sensible) y
    # conserva la transcripción. 0 = desactivado. Por defecto 9 meses.
    AUDIO_RETENTION_DAYS: int = 270

    # Retención de los ADJUNTOS en disco (imágenes, vídeos, documentos: los que
    # manda el cliente y los que envía el operador desde el panel). Sin esto la
    # carpeta `uploads/` crecía para siempre — el audio sí se purgaba, el resto
    # no. La fila del mensaje se conserva: en el histórico queda "imagen
    # recibida" con su fecha, solo desaparece el fichero. 0 = desactivado.
    MEDIA_RETENTION_DAYS: int = 180

    # Retenciones internas (limpieza diaria, tasks/purge_internal_logs).
    # Trazas del agente: contienen previews de mensajes de clientes EN CLARO
    # (RGPD) — retención corta: sirven para depurar lo reciente, no de archivo.
    TRACE_RETENTION_DAYS: int = 30
    LLM_USAGE_RETENTION_DAYS: int = 90
    AUDIT_RETENTION_DAYS: int = 365
    # Envíos masivos terminados (job + destinatarios con teléfono).
    OUTBOUND_RETENTION_DAYS: int = 180
    # Huecos de conocimiento YA resueltos (aprobado/descartado); los pendientes
    # no caducan.
    RESOLVED_GAP_RETENTION_DAYS: int = 180

    # YCloud
    YCLOUD_API_KEY: str = ""
    YCLOUD_WEBHOOK_SECRET: str = ""
    YCLOUD_PHONE_NUMBER: str = ""
    YCLOUD_BASE_URL: str = "https://api.ycloud.com/v2"
    # Proveedor de WhatsApp por DEFECTO: "ycloud" o "meta". Ya no es quien
    # manda — la elección vive en el canal y se cambia desde Conexiones (ver
    # providers/whatsapp/selector.py) — pero se respeta como punto de partida
    # en instalaciones cuyo canal todavía no tiene el campo.
    WHATSAPP_PROVIDER: str = "ycloud"
    # Versión de la Graph API con la que se habla con Meta. Se deja por entorno
    # porque Meta retira cada versión a los ~2 años y ese día el canal se
    # quedaría mudo: poder subirla sin desplegar código es la diferencia entre
    # cambiar una variable y una noche mala.
    META_WA_GRAPH_VERSION: str = "v25.0"
    # Validar el call_id del WebSocket de voz (Retell) contra su API antes de
    # atender la conexión. Es la ÚNICA autenticación del WebSocket de voz: el
    # protocolo del Custom LLM de Retell no firma los mensajes.
    #
    # NO LO APAGUES. Ponerlo en false deja el WebSocket abierto a internet sin
    # ninguna otra comprobación: cualquiera que conozca la URL puede abrir una
    # conexión, inyectar turnos, gastar tokens del LLM y EJECUTAR LAS TOOLS DEL
    # AGENTE (incluido agendar citas reales en el calendario del cliente) y
    # escribir en la base de datos.
    #
    # La validación es FAIL-CLOSED (providers/voice/retell.py): si Retell no
    # responde, reintenta una vez y, si sigue sin poder validar, RECHAZA la
    # conexión. El comentario histórico que decía "ponlo en false si diera
    # problemas — fail-open ya cubre los fallos transitorios" YA NO ES CIERTO.
    RETELL_WS_VALIDATE: bool = True
    # Intentos de conexión al WebSocket de voz por IP y minuto. Cada conexión
    # ilegítima cuesta 1-2 peticiones a la API de Retell (contra la cuota del
    # cliente) más un segundo de espera reteniendo la conexión: sin tope es
    # amplificación barata. Al superarse se cierra SIN llamar a Retell.
    # 0 = sin límite (no recomendado).
    RETELL_WS_MAX_ATTEMPTS_PER_MIN: int = 20
    # Tope de llamadas de voz simultáneas atendidas por este proceso. Cada
    # llamada mantiene un WebSocket vivo y dispara el agente en cada turno; sin
    # tope, un pico (o un abuso) agota el event loop y el pool de BD.
    RETELL_MAX_CONCURRENT_CALLS: int = 20
    # Segundos máximos que esperamos al agente en un turno de voz antes de
    # contestar con una disculpa. Sin tope, un turno colgado deja a quien llama
    # en silencio hasta que Retell corta la llamada.
    RETELL_TURN_TIMEOUT_SECONDS: int = 25
    # Tarifa de telefonía de Retell por minuto en USD (sin el LLM). Solo se usa
    # para ESTIMAR y mostrar el coste de cada llamada en la sección "Llamadas":
    # esos minutos se facturan en Retell, no aquí. 0 = no mostrar estimación.
    RETELL_PRICE_PER_MINUTE_USD: float = 0.0
    # Diagnóstico TEMPORAL del webhook de Instagram (estructura de mensajes en
    # "Logs en vivo"). Apagado por defecto: solo se enciende para depurar el
    # parser de notas de voz de IG. No vuelca contenido, solo tipos/campos.
    IG_WEBHOOK_DEBUG: bool = False

    # RAG (consultar_kb). Antes hardcodeados en la tool; configurables por env
    # para poder ajustar sin tocar código. El umbral 0.3 (similitud coseno con
    # text-embedding-3-small) es deliberadamente laxo: súbelo si el agente
    # mete chunks poco relacionados en sus respuestas.
    KB_MATCH_THRESHOLD: float = 0.3
    KB_TOP_K_DEFAULT: int = 5

    # OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Modelo de transcripción (notas de voz). Por defecto el más reciente y
    # económico: gpt-4o-mini-transcribe (~$0.003/min, mejor WER y muchas menos
    # alucinaciones que el antiguo whisper-1, que costaba $0.006/min). Usa el
    # mismo endpoint audio.transcriptions y respeta `language=es`; solo cambia
    # el nombre del modelo. Alternativas: "gpt-4o-transcribe" (más preciso,
    # $0.006/min) o "whisper-1" (legacy). La variable mantiene el nombre
    # OPENAI_WHISPER_MODEL por compatibilidad con despliegues existentes.
    OPENAI_WHISPER_MODEL: str = "gpt-4o-mini-transcribe"

    # Resend
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Slack
    SLACK_WEBHOOK_URL: str = ""

    # Google OAuth (Calendar + Gmail + Drive). Configurar UNA app de Google
    # Cloud Console — sus credenciales valen para todos los productos Google
    # que conectes después. El redirect URI debe coincidir EXACTAMENTE con el
    # autorizado en la app.
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    # Si no se define, se calcula `${APP_BASE_URL}/api/v1/oauth/google/callback`.
    GOOGLE_OAUTH_REDIRECT_URI: str = ""
    # Redirect URI del LOGIN con Google (distinta de la de servicios de arriba).
    # Si no se define, se calcula `${APP_BASE_URL}/api/v1/auth/google/callback`.
    GOOGLE_LOGIN_REDIRECT_URI: str = ""

    # Instagram Business Login (la API moderna de IG sin Facebook Page).
    # CLIENT_ID y CLIENT_SECRET son los de la app de Meta — los mismos que
    # FACEBOOK_APP_ID/APP_SECRET en realidad. Los duplicamos como vars por
    # claridad; cuando rotes, cámbialos en los dos sitios.
    INSTAGRAM_OAUTH_CLIENT_ID: str = ""
    INSTAGRAM_OAUTH_CLIENT_SECRET: str = ""
    INSTAGRAM_OAUTH_REDIRECT_URI: str = ""

    # CORS
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        # rstrip("/"): un origen con barra final ("https://panel.tudominio.com/")
        # no casa nunca con la cabecera Origin del navegador, que llega sin ella.
        # El navegador se limita a bloquear la petición, así que el síntoma es
        # "el panel no carga nada" sin ningún error que apunte a esto.
        return [o.strip().rstrip("/") for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        # strip(): un "production " con espacio NO debe colar como no-producción.
        return self.APP_ENV.strip().lower() == "production"

    def validate_production(self) -> None:
        """Aborta el arranque si en producción quedan defaults peligrosos."""
        if not self.is_production:
            return
        errors: list[str] = []
        if self.JWT_SECRET in INSECURE_JWT_DEFAULTS or len(self.JWT_SECRET) < 32:
            errors.append("JWT_SECRET vacío, por defecto o más corto de 32 caracteres")
        if not self.ENCRYPTION_KEY:
            errors.append("ENCRYPTION_KEY no configurada (obligatoria en producción)")
        else:
            # Se comprueba el FORMATO al arrancar, no la primera vez que se
            # guarda una credencial. Si no, una clave mal generada deja el
            # despliegue en verde y revienta semanas después, al conectar el
            # primer proveedor, con un error que no menciona la variable.
            try:
                Fernet(self.ENCRYPTION_KEY.encode())
            except Exception:
                errors.append(
                    "ENCRYPTION_KEY con formato inválido: tiene que ser una clave Fernet "
                    "(32 bytes en base64 urlsafe). Genérala con: "
                    "openssl rand -base64 32 | tr '+/' '-_'"
                )
        # Se rechazan tanto el default de fábrica como el histórico: ningún
        # despliegue en producción debe arrancar con un admin de ejemplo.
        if not self.INITIAL_ADMIN_EMAIL or self.INITIAL_ADMIN_EMAIL in {
            "admin@local",
            "admin@example.com",
        }:
            errors.append("INITIAL_ADMIN_EMAIL no configurado o usa el default")
        if not self.INITIAL_ADMIN_PASSWORD or self.INITIAL_ADMIN_PASSWORD == "ChangeMe123!":
            errors.append("INITIAL_ADMIN_PASSWORD no configurado o usa el default")
        if "localhost" in self.APP_BASE_URL or "localhost" in self.FRONTEND_BASE_URL:
            errors.append("APP_BASE_URL/FRONTEND_BASE_URL apuntan a localhost en producción")
        if errors:
            joined = "\n  - ".join(errors)
            raise RuntimeError(
                f"Configuración insegura en APP_ENV=production:\n  - {joined}\n"
                "Define estas variables correctamente antes de arrancar."
            )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.validate_production()
    # Fuera de producción, si el JWT_SECRET sigue siendo el default público,
    # generamos uno ALEATORIO por proceso en vez de firmar tokens con un secreto
    # conocido (staging/demo). En producción esto no se alcanza: validate_production
    # ya aborta el arranque si el secreto es inseguro.
    if not s.is_production and s.JWT_SECRET in INSECURE_JWT_DEFAULTS:
        import secrets as _secrets

        from app.core.logging import get_logger

        s.JWT_SECRET = _secrets.token_urlsafe(48)
        get_logger(__name__).warning(
            "jwt_secret.ephemeral",
            msg="JWT_SECRET por defecto en entorno no-producción: generado uno aleatorio "
            "para esta sesión (los tokens no sobreviven a un reinicio).",
        )
    return s


def avisos_de_configuracion_insegura(s: "Settings") -> list[str]:
    """Defaults peligrosos que siguen puestos, en cualquier entorno.

    `validate_production()` ya aborta el arranque si esto pasa en producción,
    pero solo mira `APP_ENV`. Y la forma más fácil de acabar con una instalación
    abierta a internet con la contraseña de ejemplo es justamente esa: seguir el
    camino de desarrollo (`.env.desarrollo.example` trae `APP_ENV=development`) y
    desplegarlo tal cual. Ahí no saltaba absolutamente nada.

    Esto no bloquea nada: son avisos, y salen en Monitorización para que se vean
    desde el panel. Función pura, sin red ni base de datos.
    """
    if s.is_production:
        return []
    avisos: list[str] = []
    if s.INITIAL_ADMIN_EMAIL in {"admin@local", "admin@example.com"} or (
        s.INITIAL_ADMIN_PASSWORD == "ChangeMe123!"
    ):
        avisos.append(
            "El usuario administrador es el de ejemplo (está escrito en el "
            "repositorio, así que lo conoce cualquiera). Cámbialo antes de abrir "
            "esto a internet."
        )
    if s.JWT_SECRET in INSECURE_JWT_DEFAULTS:
        avisos.append(
            "No hay JWT_SECRET propio: se genera uno al azar en cada arranque, así "
            "que cada reinicio echa del panel a todo el mundo."
        )
    if avisos:
        avisos.append(
            "Esta instalación corre en modo desarrollo (APP_ENV distinto de "
            "«production»), y en ese modo NO se comprueba nada de esto al arrancar. "
            "Para un cliente: .env.example + docker-compose.easypanel.yml."
        )
    return avisos


settings = get_settings()
