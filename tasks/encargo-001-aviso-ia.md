# Encargo 001 — Aviso de IA en el primer mensaje de texto

**Para:** Codex · **Revisa:** Claude (revisor y crítico) · **Rama:** `feat/gaps-entrevista`
**Intent:** `planning/intent-002.md` (léelo primero). Codex no ve ningún otro contexto: todo lo
que necesitas está aquí.

## Contexto mínimo
Chatbot de atención al cliente (FastAPI + Celery + Postgres). El agente responde por
WhatsApp, Instagram, chat web, correo y voz. Los comentarios y los nombres de test van **en
español** y explican el porqué. Commits en español, describiendo el efecto para quien usa la
app. No hagas commit: lo hace Claude tras revisar.

## Alcance de ficheros (lo que toques fuera de esta lista es un hallazgo aunque funcione)
- `backend/app/services/conversation.py` — único punto de producción a modificar.
- `backend/tests/test_arnes_evals_limites.py` — quitar el `xfail(strict=True)` de L9 y ajustar.
- `backend/tests/test_aviso_ia_primera_interaccion.py` — fichero NUEVO con las pruebas.
- `arnes/evals.json` — el caso L9: `passes: true` y evidencia.
- `arnes/PROGRESO.md` — una línea de estado de L9.
Nada más. Sin migración, sin tocar prompts, ni `guardarrail_clinico.py`, ni `runtime_config.py`.

## Qué hacer
1. **Rojo primero.** Escribe las pruebas nuevas y la modificación de L9 y **ejecútalas viéndolas
   fallar** antes de implementar. Pega la salida roja en tu informe.
2. En `conversation.py` define, a nivel de módulo, lo siguiente (el nombre exacto lo busca
   el test L9: `aviso_ia_primera_interaccion`):
   - `AVISO_IA_PRIMERA_INTERACCION`: constante de texto. Valor:
     `"Soy el asistente virtual del negocio, no una persona."`
   - `aviso_ia_primera_interaccion(texto: str, *, es_primera: bool) -> str`: función pura que
     devuelve `AVISO + "\n\n" + texto` si `es_primera` y el texto con contenido; si
     `es_primera` es falso o el texto va vacío, lo devuelve igual. No debe duplicar el aviso
     si el texto ya empieza por él.
3. Engánchala en el envío normal del drain, **justo antes** de
   `parts = _split_response(response_text, agent_cfg.response_split_max_parts)`
   (alrededor de la línea 2424), solo en la rama `else` final (la que llama a
   `deliver_response_parts`). No en la rama de email, ni en la de `is_training`, ni en la de
   handoff durante el run.
4. `es_primera`: no existe ningún `Message` de esa conversación con `rol == MessageRole.assistant`
   y sin `extra["is_draft"]` verdadero. Calcúlalo con una consulta en la sesión `db` que ya
   está abierta ahí. Solo canales `whatsapp`, `instagram` y `webchat` de `ConversationCanal`
   (comprueba los nombres reales del enum; no inventes valores). Voz y email: `False`.
5. Si la respuesta es el texto fijo de derivación clínica/urgencia, que ya empieza por «Soy el
   asistente virtual del centro, no una persona.», no dupliques el aviso (basta con comprobar
   que el texto no contenga ya «asistente virtual»).
6. Pruebas nuevas, nombres en español y en frase, que **ejecutan el comportamiento**
   (dobles de BD y de proveedor; ver `test_arnes_evals_limites.py` como referencia de estilo):
   - primera respuesta en WhatsApp lleva el aviso al principio;
   - segunda respuesta (ya hay mensaje previo del agente) no lo lleva;
   - un borrador previo (`is_draft`) no cuenta como mensaje del agente;
   - email y modo entrenamiento no lo llevan;
   - texto vacío no genera un mensaje con solo el aviso;
   - texto que ya dice «asistente virtual» no se duplica.
7. L9: quita el `@pytest.mark.xfail(strict=True, ...)` y deja que el test pase. No estreches
   la aserción para que pase: la mitad A sigue comprobando `MENSAJE_CLINICO` y
   `MENSAJE_URGENCIA`, y la mitad B comprueba que existe `aviso_ia_primera_interaccion` y,
   si puedes, que `AVISO_IA_PRIMERA_INTERACCION` contiene «asistente virtual».
8. `arnes/evals.json` (caso L9) y `arnes/PROGRESO.md`: refleja que pasa. Conserva el formato
   existente; si dudas, copia el de otro caso cerrado.

## Cómo ejecutar (importante)
El Python del sistema es 3.14 y `psycopg2-binary`/`pandas` no compilan. Usa
`uv venv --python 3.12` en `backend/` (o un venv 3.12 existente) y
`pip install -r requirements-dev.txt`.
```
cd backend
.venv/bin/python -m pytest tests/test_aviso_ia_primera_interaccion.py tests/test_arnes_evals_limites.py -q
.venv/bin/python -m pytest -q          # suite completa; sin Postgres se saltan ~290 y fallan 4 conocidas
```
Anota cuántas pasan/fallan/saltan **antes** de tocar nada (línea base) y después.

## Criterio de hecho
- Pruebas nuevas rojas antes y verdes después; L9 pasa sin `xfail`.
- Suite completa sin regresiones frente a la línea base.
- El diff solo toca los ficheros del alcance.

## Informe final (obligatorio)
Lista de ficheros tocados, salida de las ejecuciones roja y verde, línea base y resultado de
la suite completa, y cualquier decisión que hayas tomado que no esté en este encargo.
Si algo no se puede cerrar, dilo: no lo des por hecho.
