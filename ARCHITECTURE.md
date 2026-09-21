# Architecture

## System context

The platform runs as an isolated deployment for one customer. External channels deliver webhooks to the FastAPI application. The application authenticates requests, resolves the customer contact, selects the channel-compatible agent, retrieves relevant knowledge, calls the configured model, and sends the response through the channel provider.

~~~mermaid
flowchart TB
    subgraph External["External systems"]
        WA[WhatsApp / YCloud / Meta]
        IG[Instagram / Meta]
        MAIL[Gmail or SMTP]
        VOICE[Retell]
        CAL[Google Calendar]
        MODELS[OpenAI / Anthropic / Gemini]
        MCPCLIENT[External MCP clients]
    end

    subgraph Platform["Customer deployment"]
        WEB[React admin and operator panels]
        API[FastAPI API]
        AGENT[Agent orchestrator]
        TOOLS[Agent tools - knowledge / CRM / calendar / handoff]
        WORKER[Celery worker]
        BEAT[Celery Beat]
        REDIS[(Redis)]
        DB[(PostgreSQL + pgvector)]
        MCP[MCP server]
        FILES[(Audio and uploaded files)]
    end

    WA --> API
    IG --> API
    MAIL --> API
    VOICE --> API
    WEB <--> API
    API --> AGENT
    AGENT --> TOOLS
    TOOLS <--> DB
    TOOLS --> CAL
    AGENT <--> MODELS
    API <--> REDIS
    API <--> DB
    API <--> FILES
    REDIS --> WORKER
    REDIS --> BEAT
    WORKER --> DB
    WORKER --> MODELS
    API --> MCP
    MCP <--> MCPCLIENT
    API --> WA
    API --> IG
    API --> MAIL
    API --> VOICE
~~~

## Message flow

~~~mermaid
sequenceDiagram
    participant Channel as Channel provider
    participant API as FastAPI API
    participant DB as PostgreSQL
    participant Agent as Agent orchestrator
    participant Search as Knowledge and CRM tools
    participant Model as LLM provider
    participant Worker as Celery worker
    participant Operator as Operator panel

    Channel->>API: Webhook with inbound message
    API->>DB: Resolve contact and conversation
    API->>Agent: Enqueue or process message
    Agent->>Search: Retrieve context and contact data
    Search->>DB: Hybrid vector and text search
    DB-->>Search: Context and CRM records
    Search-->>Agent: Tool results
    Agent->>Model: Prompt with policy and context
    Model-->>Agent: Response or tool call
    Agent->>DB: Persist messages, trace and usage
    Agent-->>API: Outbound response or human handoff
    API-->>Channel: Provider message
    API-->>Operator: WebSocket event and Web Push alert
    API->>Worker: Background job when needed
    Worker->>DB: Index, deliver, back up or learn
~~~

## Runtime responsibilities

- backend/app/api/ exposes authentication, channel webhooks, contacts, conversations, knowledge, administration and the agent API.
- backend/app/agents/ coordinates customer-facing and internal agents.
- backend/app/agents/tools/ provides controlled access to the knowledge base, CRM, scheduling and human handoff.
- backend/app/providers/ isolates model, channel, email, voice, calendar, embedding and transcription integrations.
- backend/app/services/ contains business rules, encryption, auditing, delivery, indexing, backups and usage tracking.
- backend/app/tasks/ contains Celery jobs.
- frontend/ contains the admin and operator panels.
- mcp-server/ exposes selected agent API operations through MCP without holding application credentials.

## Data and security boundaries

PostgreSQL stores application records, encrypted credentials, audit events, conversation data and pgvector embeddings. Redis stores queues and transient coordination state. Audio and uploaded documents use the configured volume or storage provider. Provider secrets are read from encrypted application storage or deployment configuration; they are never sent to the MCP server. Production startup validation rejects unsafe defaults and local URLs.
