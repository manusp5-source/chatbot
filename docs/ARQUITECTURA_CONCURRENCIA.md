# Arquitectura de concurrencia del chatbot

Cómo se procesan los mensajes entrantes y qué volumen aguanta el sistema.
Para gente no técnica (el administrador / cliente final).

---

## El recorrido de un mensaje, paso a paso

```
1. Cliente escribe en WhatsApp
2. YCloud (proveedor WhatsApp) recibe y reenvía el mensaje al webhook
3. Container `app` (FastAPI):
     - Verifica firma de YCloud
     - Guarda el mensaje en BD (PostgreSQL)
     - Encola tarea "procesar mensaje X" en Redis
     - Responde 200 OK a YCloud (~1s)
4. Container `worker` (Celery):
     - 16 trabajadores (threads) en paralelo escuchan la cola
     - El primer libre coge la tarea
     - Lo encola en el buffer (agrupar ráfagas) y agenda drenado a +5s
     - Termina inmediatamente, queda libre
5. 5s después (Celery countdown):
     - Una nueva tarea drena el buffer y procesa SI fue el último mensaje
     - Llama Moderation (OpenAI) → Chat (LLM) → quizá KB search → YCloud
6. Cliente recibe respuesta por WhatsApp (~8-15s total)
```

---

## Por qué hay buffer (5 segundos)

Sin buffer, si el cliente escribe rápido:

```
"Hola"          → bot: "Hola, ¿en qué te ayudo?"
"oye"           → bot: "Dime"
"una pregunta"  → bot: "Claro, pregunta"
"del horario"   → bot: "Sí, dime"
```

Cuatro respuestas descontextualizadas. Pésimo.

Con buffer de 5s: el bot espera 5 segundos desde el último mensaje, junta
todo el texto ("Hola oye una pregunta del horario") y responde **una sola
vez** entendiendo el contexto completo.

---

## Por qué pool=threads, no prefork

Celery tiene varios "pools" (formas de ejecutar tareas en paralelo):

| Pool | Cómo funciona | Memoria | Bueno para |
|------|---------------|---------|------------|
| `prefork` | N procesos OS independientes | Alta (N × bundle) | CPU-bound (cálculo intensivo) |
| `threads` | N hilos dentro de un proceso | Baja (heap compartido) | **I/O-bound** (esperar HTTP, BD) |
| `gevent`/`eventlet` | N coroutines en 1 proceso | Mínima | Miles de conexiones simultáneas |

Nuestro caso es **I/O-bound**: la mayoría del tiempo esperamos a OpenAI, BD
o Redis. Por eso `threads` con 16 trabajadores nos da concurrencia real con
poca RAM. Si fuera CPU-bound (cálculo pesado), prefork sería mejor.

---

## Buffer no bloqueante

**Antes:**

```
Trabajador 1 recibe msg de Juan → push al buffer → DORMIR 5s
                                                   ↑ thread bloqueado
                                                     no puede hacer nada
Trabajador 2 recibe msg de María → push al buffer → DORMIR 5s
...
```

Con 16 trabajadores y ráfaga de 16 personas, los 16 quedaban dormidos.
La persona 17 esperaba en cola.

**Ahora:**

```
Trabajador 1 recibe msg de Juan → push al buffer → agendar drain a +5s → LIBRE
Trabajador 2 recibe msg de María → push al buffer → agendar drain a +5s → LIBRE
...
5s después, Celery dispara drain_buffer(juan), drain_buffer(maria)...
Esos sí ejecutan el procesamiento real (Moderation + LLM + send), uno por
ráfaga finalizada. Cada uno tarda 5-8s reales.
```

Los 16 trabajadores nunca quedan dormidos esperando — siempre están
procesando o libres para coger lo siguiente.

---

## Volumen que aguantamos

Asumiendo que cada procesamiento real (Moderation + LLM + KB + send) dura
~5 segundos:

| Config | Trabajadores | Cuello de botella | Mensajes/min |
|--------|--------------|-------------------|--------------|
| Anterior (4 prefork) | 4 | concurrency baja | ~50 |
| Actual (16 threads) | 16 | OpenAI rate limit | ~150 (limite OpenAI) |
| Subiendo a 30 threads | 30 | OpenAI rate limit (mismo) | ~150 (no mejora hasta subir plan OpenAI) |
| 30 threads + plan superior OpenAI | 30 | Memoria del worker | ~300+ |

**Cuello de botella REAL para escalar más allá**: el rate limit de OpenAI.
Plan Tier 1 (default) permite ~500 requests/minuto en chat. Cada conversación
hace típicamente 3 calls (moderation + chat + a veces RAG). Eso son ~166
conversaciones/min máximo. Subir el plan de OpenAI sube el rate limit.

---

## Componentes del sistema

```
┌─────────────┐    HTTPS POST    ┌─────────────┐
│   YCloud    │ ───────────────> │  app (Web)  │
│ (WhatsApp)  │                  │   FastAPI   │
└─────────────┘                  └──────┬──────┘
                                        │ encola tarea
                                        ▼
                                 ┌─────────────┐
                                 │   Redis     │ ◄─── pub/sub eventos
                                 │  (broker)   │      tiempo real
                                 └──────┬──────┘
                                        │ workers escuchan
                                        ▼
                                 ┌─────────────┐
                                 │   worker    │ ── 16 threads
                                 │   Celery    │    paralelo
                                 └──────┬──────┘
                                        │ guarda + lee
                                        ▼
                                 ┌─────────────┐
                                 │ PostgreSQL  │
                                 │  + pgvector │
                                 └─────────────┘
```

Además existe el container `beat` (cron interno) y `frontend` (UI React).

---

## Qué pasa si falla algo

| Falla | Qué ocurre | Recuperación |
|-------|------------|--------------|
| Worker se cae | Tareas en cola Redis quedan guardadas | Easypanel reinicia worker (~30s), retoma desde la cola |
| `app` se cae | YCloud no recibe 200, reintenta el webhook | Easypanel reinicia, próximo webhook entra |
| OpenAI 500 | Celery reintenta automáticamente 3 veces con backoff | Se recupera solo, si no eventualmente se loguea |
| BD se cae | Health check falla, Easypanel reinicia containers | Cuando BD vuelve, todo retoma |
| Redis se cae | Cola y eventos en tiempo real caen | Easypanel reinicia, mensajes nuevos llegan; los que estaban en cola se pierden (poco común) |

---

## Próximos pasos de escala

Cuando el negocio crezca, en este orden:

1. **Replicas app=2, worker=2** en Easypanel: sin downtime en deploys + tolerancia a fallos.
2. **Plan OpenAI Tier 2+**: sube el rate limit a 5000 RPM. Permite ~1600 conversaciones/min.
3. **PgBouncer o read replicas** para BD si el volumen llega a miles de mensajes/min.
4. **CDN delante del frontend** si los operadores conectan desde muchos sitios.

Para el volumen actual (decenas-cientos de mensajes/día) no se necesita
nada de esto. La config 16 threads + buffer no bloqueante aguanta de sobra.
