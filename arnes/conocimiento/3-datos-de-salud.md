# 3 · Datos de salud — RGPD art. 9 y LOPDGDD 3/2018

## El hecho incómodo

El agente **va a recibir datos de salud** aunque nadie se los pida. "Me duele desde el
jueves", "soy alérgica a la penicilina", "estoy embarazada": eso es categoría especial del
art. 9 del RGPD en cuanto se guarda, y llega por WhatsApp a las once de la noche sin que
nadie lo haya pedido.

Recibirlo no se puede evitar. **Propagarlo sí**, y propagarlo es lo que convierte un mensaje
suelto en un fichero de datos de salud.

## Las tres decisiones de diseño que salen de aquí

**1 · El agente no escribe contenido clínico en la ficha del contacto.**
Ni síntomas, ni medicación, ni antecedentes, ni diagnósticos. La ficha es una ficha
comercial, y en el panel la ve **cualquier persona con rol `cliente`** — que ve toda la base
de contactos, notas internas incluidas (`SECURITY.md` §1). Un síntoma escrito ahí queda a la
vista de todo el equipo, para siempre y sin retención.

**2 · La trazabilidad va donde tiene retención.**
El término exacto que disparó el guardarraíl se guarda en `agent_trace_event`, con su
conversación y su hora, y se purga a los 90 días. Eso es auditoría; la ficha no lo es.

**3 · Lo que ya está cifrado, y lo que no.**
Cifrados con Fernet: `notas_internas`, `nif`, `direccion`, la transcripción de las llamadas y
el resumen de la conversación. **En claro a propósito**: teléfono, email y nombre, porque la
aplicación los necesita para buscar y para la clave única. Es una decisión consciente y
documentada, no un descuido. Y si se pierde `ENCRYPTION_KEY`, lo cifrado no se recupera: el
campo empieza a salir vacío, en silencio.

## Lo que hay que decirle al cliente al instalar

Dos cosas que no son del agente pero condicionan quién ve qué:

- **Dar de alta como `cliente` solo a quien ya podría ver esa información** trabajando en el
  centro. No es un rol para colaboradores externos ni para becarios de un solo canal.
- **Sin `openai_api_key` la moderación de contenido está apagada**, aunque el agente funcione
  con otro proveedor. Nadie filtra acoso, autolesiones ni violencia antes del modelo, y la
  ausencia de alertas parece que todo va bien. Si el centro atiende a menores, esto se
  resuelve antes de abrir.

→ Pieza 06 (regla de escritura) · pieza 05 prohibición 8 · eval `F7`.
