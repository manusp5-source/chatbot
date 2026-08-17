# Buzón de feedback

Semilla del buzón (señal 2 del motor, ver `motor-mejora-continua/senales.md`). Aquí deja correcciones el responsable del arnés (definido en `00-INTAKE.md`, sección 0). Si el blueprint define otro canal (Telegram, chat, etc.), lo que llegue por ahí se vuelca aquí antes de procesarse: el buzón es la única cola que lee el cron-feedback.

## Formato de entrada
```
- [YYYY-MM-DD] PENDIENTE | quién lo dice | la corrección o el feedback, tal cual
```

Reglas:
- Las entradas nuevas entran como **PENDIENTE**.
- La skill `revisar-feedback` procesa las PENDIENTE y las marca como **PROCESADO**, añadiendo qué hizo con cada una (aplicado / propuesto / descartado y por qué).
- No se borran entradas procesadas hasta la purga del registro: son la traza de qué feedback dio lugar a qué cambio.

## Entradas
(vacío al arranque — la primera entrada la deja el responsable del arnés o su canal)
