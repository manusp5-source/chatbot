# Pieza 04 · Herramientas

> Compila a: la config de herramientas del runtime, sea cual sea la infraestructura — junto con la pieza 05 forma el artefacto **Permisos y herramientas** de la tabla de instalación de `../INSTALAME.md`. Las tareas repetitivas que usan estas herramientas se empaquetan luego como **Skills** (pieza 11).

## Qué es
Las manos del agente: con qué sistemas habla (web, APIs, archivos, MCPs, bots). Herramientas = manos; skills = oficio. Aquí se decide qué puede tocar el agente del mundo real.

## Preguntas para rellenarla
- ¿Con qué sistemas tiene que hablar? (web, APIs, archivos, MCPs, bots…)
- ¿Dónde vive el agente? (Mac, VPS, contenedor…)
- ¿Qué MCPs o integraciones concretas necesita?
- ¿Qué herramientas son críticas y cuáles opcionales?

## Respuesta

### Dónde vive

Dentro de la propia aplicación, en el Docker del cliente: siete servicios (backend, worker,
beat, Postgres con pgvector, Redis, panel y MCP), unos 4 GB. No tiene shell, no tiene disco
y no sale a internet por su cuenta: **solo alcanza lo que le den estas siete herramientas.**

### Las siete herramientas

Ninguna nueva. El arnés no añade manos; lo que aporta es el **contrato de uso** de cada una.

| Herramienta | Para qué | Contrato de uso | Texto | Voz |
|---|---|---|---|---|
| `consultar_kb` | Buscar en la base de conocimiento del centro por significado | **Antes de afirmar cualquier dato del negocio.** Si no devuelve nada, no se sabe. Topes del sistema: `top_k<=8`, consulta ≤500 chars, contenido recortado a 1200 | ✓ | ✓ |
| `buscar_contacto` | Recuperar la ficha de **quien está hablando** | Antes de crear nada. **No acepta parámetros**: no hay forma de pedir la ficha de otra persona | ✓ | ✓ |
| `crear_actualizar_contacto` | Dar de alta o completar el contacto | Cuando hay interés real. Sin narrarlo. El teléfono lo fuerza el sistema desde el webhook: si el mensaje trae otro, se ignora y se alerta | ✓ | ✓ |
| `consultar_disponibilidad` | Ver huecos libres del calendario | Antes de ofrecer una hora. Nunca se ofrece un hueco de memoria | ✓ | ✓ |
| `agendar_cita` | Crear la cita | **Solo tras confirmación explícita**, habiendo repetido día y hora | ✓ | ✓ |
| `derivar_humano` | Pasar la conversación a una persona | Cuando la KB no llega, lo piden, hay compromiso de dinero o plazo, o asoma lo clínico. Cooldown de 10 min por conversación, idempotente si ya está derivada | ✓ | **✗** |
| `schedule_config` | Consultar el horario configurado del centro | Antes de decir cuándo abren o cuándo contestarán | ✓ | ✓ |

### Críticas y opcionales

**Críticas** — sin ellas el agente no hace su trabajo: `consultar_kb` (todo lo que dice se
apoya en ella) y `derivar_humano` (es la salida de emergencia de todo lo demás).

**Necesarias pero no críticas**: las de contacto y las de calendario. Si el centro no usa
calendario, se quitan de la lista y el agente sigue sirviendo para informar y captar.

**Opcional**: `schedule_config`. Sin ella el horario tiene que estar en la KB.

### Mínimo privilegio

Lo que **no** tiene, y no se le da:

- Nada que **cobre, cancele o envíe en masa**. Las difusiones son del panel, no suyas.
- Nada que **lea el programa de gestión** de la clínica. No existe la integración.
- Nada que **escriba en la base de conocimiento**. La KB la mantiene el centro; el agente
  la lee. (Los huecos de FAQ los propone una tarea aparte, con aprobación humana.)
- Nada que **consulte fichas ajenas**. `buscar_contacto` sin parámetros es la garantía.

### Por qué la lista importa técnicamente

`tools_enabled` es una **lista blanca efectiva**. Y hay una distinción que se paga cara si
se ignora: `None` significa "sin configurar" y da acceso a **todas** las herramientas
registradas; `[]` significa "ninguna, a propósito". Un agente al que se le desmarcan todas
las casillas en el panel no puede acabar con `None`. Además, aunque el modelo alucine el
nombre de una herramienta que no tiene, `_run_tool` la rechaza: la lista no es una
sugerencia al modelo, es una puerta cerrada.

## Cuándo está bien
- Cada herramienta que necesita está listada y justificada.
- No tiene acceso a nada que no necesite (mínimo privilegio).
- Está claro dónde vive y qué puede alcanzar desde ahí.
