# KNOWLEDGE — Chatbot

Lecciones y trampas que no se deducen leyendo el código. El hook
`pre-compact-state.ps1` añade aquí el estado antes de cada compactación de
contexto; lo de abajo es lo que ya se sabe.

Las decisiones **con alternativa descartada** van en [`DECISIONS.md`](DECISIONS.md).
Aquí va lo que hace perder una tarde.

---

## Migraciones

**`alembic --autogenerate` no sirve en este repositorio.** Comprobado contra una
base exactamente al día: propone borrar cuatro tablas (`classifier_config`,
`agent_prompt_history`, `llm_model_price`, `llm_usage_log`) y catorce índices que
están perfectamente. Dos motivos: hay modelos que no se importan en
`app/models/__init__.py` y Alembic no los ve, y muchos índices se crean con SQL a
mano (parciales, GIN de texto completo, IVFFlat de pgvector, sobre expresiones).
Se escriben `upgrade()` y `downgrade()` a mano.

**`ENCRYPTION_KEY` hace falta para migrar aunque tu migración no cifre nada**,
porque por el camino se ejecutan las que sí. Sin ella:
`Fernet key must be 32 url-safe base64-encoded bytes`.

**El nombre del fichero es el identificador de la revisión.** `--rev-id` no es
opcional: `alembic revision -m "resumen" --rev-id 0055_mi_cambio`. Comprueba el
`down_revision` que rellena Alembic antes de escribir nada.

**Las cuatro primeras migraciones tienen nombre antiguo con fecha delante.** No
las renombres: ya están aplicadas en instalaciones reales.

---

## Campos cifrados

Tres columnas de `contacts` usan `EncryptedText`: `nif`, `direccion` y
`notas_internas`. En Postgres son `bytea`.

- **Nada de `WHERE`, `LIKE`, `ORDER BY`, `GROUP BY`, `UNIQUE` ni índices** sobre
  ellas. No dan error de sintaxis: simplemente no encuentran nada.
- Filtrar por ellas obliga a traerse las filas y comparar en Python. En una lista
  de contactos eso no escala. Piénsalo dos veces antes de cifrar un campo por el
  que la gente va a querer buscar.
- Si se pierde o cambia `ENCRYPTION_KEY`, quedan ilegibles **para siempre**, y el
  tipo devuelve `None` en vez de romper: el dato desaparece en silencio.
- `telefono`, `email` y `nombre` están en claro **a propósito**: la app los
  necesita para buscar y para la clave única.

---

## Comportamientos que parecen bugs y no lo son

| Qué se ve | Por qué está así |
|---|---|
| `/health` devuelve 200 con el worker caído | Para que el contenedor no entre en bucle de reinicios. Mira el campo `worker` del cuerpo |
| El clasificador deja pasar el mensaje cuando falla | Perder un cliente real es peor que tragarse un spam |
| El panel de desarrollo se sirve compilado en el 5173 | Para recarga en caliente, `cd frontend && npm run dev` aparte |
| Postgres y Redis no publican puerto al host | Solo se ven desde dentro de la red de Compose. Para pruebas, levanta un Postgres aparte |

---

## Pruebas

- Viven en **`backend/tests/`** y se lanzan **desde `backend/`**.
- Sin base de datos se saltan unas 290 y fallan cuatro que van a buscar una
  credencial a la base.
- **La imagen de producción no lleva pytest dentro.** `docker compose exec app
  pytest` no va a funcionar.
- Si levantas la base de pruebas en un puerto ya ocupado, Docker no siempre avisa
  y acabas hablando con el Postgres equivocado. El síntoma es
  `FATAL: database "test" does not exist`.

---

## La CI tiene una historia

Del 1 de julio al 11 de agosto de 2026 el workflow **nunca** pasó en verde: 55
ejecuciones, 55 fallos. El paso `alembic upgrade head` corre fuera de pytest, así
que no recibía los valores por defecto de `tests/conftest.py` y moría con
`RuntimeError: ENCRYPTION_KEY no configurada` antes de ejecutar una sola prueba.
Por eso las variables de test se definen en el bloque `env` **del job**: las ven
todos los pasos, no solo pytest.

**Y aun en verde, la CI no es una barrera.** Se dispara con el push a `main` y
EasyPanel despliega ese mismo push. Hacen falta dos cosas que no se pueden
configurar desde el fichero: protección de rama en GitHub y despliegue por
webhook en EasyPanel.

---

## Al añadir un campo al contacto

Son **trece** sitios, no dos. La lista completa y en orden está en
[`CLAUDE.md`](CLAUDE.md), sección "Cómo añadir un campo a la ficha de contacto".
Los que más se olvidan: `_CSV_COLUMNS` (la misma lista sirve para importar y
exportar), `CAMPOS_RELLENABLES` de `contact_merge.py`, y la exportación RGPD.

---

## Sesión del 17 de agosto de 2026

Se reconstruyeron los artefactos de FactorIA a partir del código (Oleada 2 del
plan del paraguas). Hallazgos al hacerlo:

- `implementation/user_journeys.md` describe **la intención de diseño**, no el
  árbol de hoy. Seis de los 27 journeys acabaron en otro sitio del que dice el
  documento — todo el admin en un único `api/admin.py`, el dashboard del lado
  admin en vez del cliente, el websocket como hook en vez de servicio. Están
  marcados `[≠]` en el tracker.
- Aproximadamente **la mitad de lo que hace la aplicación hoy no figura en
  ningún documento de planificación**: clasificador, difusiones, agente interno,
  autoaprendizaje, voz, Instagram, Gmail, copias, tope de gasto, traza, RGPD,
  MCP y push. Está inventariado al final del tracker.
