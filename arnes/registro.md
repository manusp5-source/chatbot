# Registro — la huella del agente

Semilla del registro (pieza 08). Existe desde el arranque para que los crons del motor de mejora continua tengan siempre de dónde leer: sin registro, `recoger-fallos` no tiene material. Al instalar, este archivo viaja con el arnés (ver tabla de instalación de `INSTALAME.md`, artefacto Memoria).

## Formato de entrada (una línea por tarea relevante)
```
- [YYYY-MM-DD HH:MM] <tarea> | OK / FALLO | qué se hizo | evidencia o nota
```

Reglas:
- Los fallos se marcan con la palabra **FALLO** en mayúsculas: así los localiza la skill `recoger-fallos` sin ambigüedad.
- Cada cron del motor deja aquí constancia de lo que hizo (o de que no tenía material ese ciclo).
- El registro no crece sin control: cuando pase de ~200 entradas, mueve las más antiguas a `registro-archivo.md` en esta misma carpeta.
- La pieza 08 puede redefinir dónde y en qué formato se registra para ESTE agente; si lo hace, este archivo queda como formato por defecto y se anota aquí la nueva ubicación.

## Entradas
- [YYYY-MM-DD HH:MM] montaje | OK | arnés creado desde la plantilla | —
