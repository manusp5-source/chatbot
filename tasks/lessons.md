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
