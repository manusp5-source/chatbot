# Comandos de proyecto apartados — 3 sep 2026

Estos cinco ficheros eran copias de FactorIA v1 (46 lineas frente a las 804 del
global) y **tapaban a los comandos globales** de `~/.claude/commands/`. Un
directorio de comandos propio dentro de un proyecto gana al global y diverge en
silencio: por eso este proyecto seguia sin intent, sin evals y con un `/review`
de un solo revisor.

Apartados, no borrados. Para devolverlos:

```bash
mv .claude/commands.archivados-2026-09-03 .claude/commands
```

Si habia algo genuinamente especifico de este proyecto en ellos, sacalo de aqui y
documenta en el CLAUDE.md del proyecto por que existe. El resto vive en el global.
