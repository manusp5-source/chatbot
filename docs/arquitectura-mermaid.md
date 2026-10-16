# Arquitectura de eskailet-chatbot (diagramas Mermaid)

Dos vistas del mismo sistema, leídas del código a fecha de 16 de octubre de 2026. El primero es la arquitectura de
componentes; el segundo, el recorrido de un mensaje. Se renderizan en GitHub, en VS Code (extensión de Mermaid) y en
<https://mermaid.live>.

## 1. Componentes

```mermaid
flowchart TB
  subgraph CAN["Canales de entrada"]
    WA["WhatsApp<br/>YCloud / Meta"]
    IG["Instagram DM"]
    GM["Correo<br/>Gmail, solo borrador"]
    WEB["Chat web<br/>widget"]
    VOZ["Voz<br/>Retell"]
  end

  subgraph API["FastAPI (servicio app)"]
    WH["Webhooks<br/>firma verificada"]
    PAN["API del panel<br/>JWT con revocacion"]
    WS["WebSocket<br/>/ws/inbox"]
    WSV["WebSocket de voz<br/>/retell/llm-ws"]
    AA["agent-api<br/>tokens con ambitos"]
  end

  subgraph ASY["Asincrono"]
    RED[("Redis<br/>cola, buffer 8 s,<br/>cerrojos, pub/sub")]
    CEL["Celery<br/>worker y beat"]
  end

  subgraph AG["Agente (orquestador)"]
    G1["Gates<br/>cuarentena, estado,<br/>moderacion, limites"]
    G2["Guardarrail clinico<br/>determinista, antes del LLM"]
    LOOP["Bucle LLM + tools<br/>maximo 5 vueltas"]
    VER["Verificador de importes<br/>despues del LLM"]
    ENV["Envio por canal"]
  end

  subgraph TLS["Herramientas del agente (7)"]
    KB["consultar_kb<br/>RAG hibrido + filtro<br/>de instrucciones"]
    CT["contacto<br/>buscar y guardar"]
    CAL["disponibilidad<br/>y cita"]
    HND["derivar_humano"]
  end

  subgraph DAT["Datos"]
    PG[("Postgres 16<br/>+ pgvector")]
  end

  subgraph EXT["Proveedores externos"]
    LLM["LLM<br/>OpenAI / Anthropic / Gemini"]
    GCAL["Google Calendar<br/>y Gmail"]
    RET["Retell"]
  end

  PANEL["Panel<br/>React + TypeScript + Vite"]
  MCP["Servidor MCP<br/>FastMCP, 13 tools"]
  OP["Asistente del operador<br/>Claude Code, n8n"]

  WA --> WH
  IG --> WH
  GM --> WH
  WEB --> WH
  VOZ --> RET
  RET --> WSV

  WH --> RED
  RED --> CEL
  CEL --> G1
  WSV --> G1
  G1 --> G2
  G2 --> LOOP
  LOOP --> VER
  VER --> ENV

  LOOP <--> LLM
  LOOP --> KB
  LOOP --> CT
  LOOP --> CAL
  LOOP --> HND
  KB --> PG
  CT --> PG
  CAL --> GCAL
  HND --> RED
  ENV --> PG
  ENV -.->|respuesta| WA
  ENV -.->|respuesta| WEB

  PANEL <--> PAN
  PANEL <-.->|eventos en vivo| WS
  RED -.->|pub/sub| WS
  PAN --> PG
  CEL --> PG

  OP --> MCP
  MCP -->|"Bearer del cliente"| AA
  AA --> PG
```

## 2. Recorrido de un mensaje (WhatsApp)

```mermaid
sequenceDiagram
  autonumber
  participant C as Cliente
  participant P as WhatsApp
  participant A as FastAPI
  participant R as Redis
  participant W as Celery worker
  participant L as LLM
  participant D as Postgres

  C->>P: escribe un mensaje
  P->>A: webhook firmado
  A->>A: verifica la firma
  A->>D: guarda el mensaje
  A->>R: encola process_message
  A-->>P: 200 enseguida

  W->>R: buffer de 8 s y cerrojo de la conversacion
  W->>W: gates y guardarrail clinico

  alt el guardarrail salta
    W->>C: texto fijo y derivacion a una persona
  else sigue el agente
    loop hasta 5 vueltas
      W->>L: historial y esquemas de las tools
      L-->>W: texto o tool_call
      W->>D: ejecuta la tool (KB, contacto, cita)
    end
    W->>W: verificador de importes
    W->>R: hay otro mensaje pendiente
    alt si hay otro
      W->>W: descarta esta respuesta
    else no hay otro
      W->>P: envia la respuesta
      P->>C: respuesta
      W->>R: publica el evento para el inbox
    end
  end
```

## Leyenda y límites

- Las flechas continuas son llamadas o flujo de datos; las punteadas son respuesta al cliente o eventos en vivo.
- Es **una instalación por cliente** (DEC-01): no hay nada compartido entre clientes y no es multi-tenant.
- El servidor MCP **no es** el camino del agente que habla con los clientes: sus herramientas son funciones del propio
  proceso. El MCP solo sirve para operar la instalación desde fuera.
- En voz no hay buffer de 8 s ni derivación a una persona en mitad de la llamada: se toman datos y se promete devolverla.
- El correo no envía: el agente deja un borrador en Gmail para que lo revise una persona.
