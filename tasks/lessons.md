# Lecciones — eskailet-chatbot

Reglas extraídas de correcciones reales. Se lee al empezar a trabajar en este proyecto.

---

## 1 · El trabajo sin commitear no está a salvo entre comandos

**17 ago 2026.** El hook `checkpoint.ps1` hizo un `git stash` **incluyendo untracked** y se
llevó una carpeta entera (`arnes/`, 36 ficheros) y un fichero de test nuevo. Los dos estaban
sin commitear. Se recuperó todo —la fuente vivía fuera del repo y el resto estaba en el
stash `claude-checkpoint-*`— pero el susto fue real y se perdió media hora en forense.

**Cómo aplicarla:** en cuanto un bloque de trabajo funcione, commitear en la rama. No
esperar a "tenerlo todo". Y si algo desaparece, mirar `git stash list` antes de reescribirlo.

## 2 · Un test que hace `grep` del código fuente no prueba nada

**17 ago 2026.** Los primeros evals de límites del arnés comprobaban cosas como
`"html.escape" in fuente` o `"8" in fuente`. Un juez externo lo tumbó: con eso, alguien podía
dejar el cooldown de `derivar_humano` sin implementar y la prueba seguía en verde porque la
palabra aparecía en un comentario.

**Cómo aplicarla:** se ejecuta el comportamiento. Si hace falta Postgres o Redis, se
sustituyen por dobles que registran lo que se les pide — así la prueba entra en CI *y* prueba
algo. Ver `backend/tests/test_arnes_evals_limites.py` como referencia. Un eval en verde que
no prueba lo que dice es peor que no tenerlo: da falsa tranquilidad.

## 3 · Cuando un revisor externo contradice lo que das por hecho, compruébalo antes de descartarlo

**17 ago 2026.** El juez avisó de que `arnes/compilado/*.md` no existían en disco. La reacción
fue "imposible, los tests que los leen están en verde". Era cierto (lección 1). Comprobarlo
costó un comando; descartarlo habría dejado el arnés instalado a medias sin que nadie lo viera.

## 4 · La documentación de seguridad se desactualiza en silencio

**17 ago 2026.** `SECURITY.md` §3 dice que `derivar_humano` escapa el motivo como HTML
"porque Telegram usa parse_mode HTML". El producto dejó de notificar a Telegram/Slack por
RGPD y ahora avisa por web push interno: `html.escape` ya no existe en el fichero. Un eval
escrito a partir del documento habría fallado contra el código.

**Cómo aplicarla:** para afirmar un comportamiento de seguridad, la fuente es el código.
`SECURITY.md` se contrasta, no se copia.

**Corregido el 17 ago 2026**: el desfase no era una fila, eran nueve sitios. Telegram y
Slack se retiraron del producto entero (las alertas van a los Logs en vivo del panel) y el
documento seguía diciéndole al operador que configurase `telegram_bot_token` "porque es lo
que te avisa de que alguien está abusando del bot". Quien lo siguiera creería tener un canal
de alertas que no existe.

## 5 · Los pines del repo son de Python 3.12

**17 ago 2026.** `psycopg2-binary` y `pandas` no compilan en el 3.14 del sistema. La salida
limpia es `uv venv --python 3.12`, que baja un CPython propio sin tocar el sistema.

## 6 · Codex entrega sin haber ejecutado nada: quien revisa ejecuta

**14 oct 2026.** El sandbox de Codex no puede cargar el venv del repo (`PermissionError` en
`site-packages`, DLL de `cryptography` denegada) ni acceder a la red. Dos encargos seguidos
volvieron con «implementado, pytest no se pudo ejecutar». Uno traía dos anclas de texto mal
escritas en la prueba nueva (`no empieces` contra `No empieces`), que solo se vio al ejecutar.

**Cómo aplicarla:** un informe de Codex sin salida de pytest no es verificación. El revisor
monta un venv 3.12 **fuera del repo** (`uv venv --python 3.12 <carpeta-de-scratchpad>`), ejecuta
los tests, y devuelve a Codex el diagnóstico exacto si hay rojo. Y se le dice en el encargo
que no intente crear venvs ni instalar nada, que ahí pierde el tiempo.

## 7 · Un informe de exploración se contrasta antes de usarlo

**14 oct 2026.** El informe de un agente de exploración afirmó tres cosas que el código
desmentía: que el repositorio no tenía remoto (`origin` existe), que el dashboard no medía
contención (calcula `ai_resolved_rate`) y que la rama de texto de la búsqueda híbrida no tenía
corte (`plainto_tsquery` exige todas las palabras). Las tres habían entrado ya en un documento
de preparación de entrevista.

**Cómo aplicarla:** lo que se vaya a decir en voz alta o a escribir como hecho se comprueba en
el fichero antes de afirmarlo. Un recuento, una ausencia («no hay X») y una causa son las tres
cosas que más fallan.

## 8 · Tests con base de datos: levantar una real, no dejar que se salten

**14 oct 2026.** Sin Postgres ni Redis, ~290 tests se saltan y ~10 fallan por `redis:6379`, y
«la suite está igual que antes» se queda en una comparación entre dos huecos. Con
`pgvector/pgvector:pg16` en `127.0.0.1:5466` y `redis:7-alpine` en `127.0.0.1:6399`
(`DATABASE_URL`, `REDIS_URL`) corren de verdad, y las consultas SQL nuevas se prueban.

**Cómo aplicarla:** el hook de seguridad bloquea el literal `password=` en la línea de
comandos; para una base desechable en localhost se usa `POSTGRES_HOST_AUTH_METHOD=trust`, que
no lleva contraseña. Comprobar antes en `INFRA-LOCAL.md` que el puerto está libre, y retirar
los contenedores al terminar.
