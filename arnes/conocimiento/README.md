# Base de conocimiento del agente

El saber de dominio que hace competente al agente en su nicho. Vacía en el molde; se llena por agente durante la construcción, a partir de la investigación del nicho.

## Qué va aquí
- Lo que hay que saber del sector para hacer el trabajo a nivel profesional.
- Estándares, buenas prácticas y referencias del nicho.
- Datos estables del negocio (lo que cambia poco). Lo que cambia mucho → no aquí, se consulta en vivo.

## Qué NO va aquí
- El alma del agente (eso es el soul, piezas 01-02).
- Datos que caducan o que el agente debe citar actualizados (eso se recupera en vivo, no se congela).

## Cómo crece
El motor de mejora continua añade aprendizajes aquí cuando son seguros (regla B: lo seguro y reversible se auto-aplica; lo de fondo va a propuesta — ver `../motor-mejora-continua/regla-B.md`). Cada archivo, un tema. Nombres claros y cortos, sin espacios ni acentos.

## Estructura sugerida
```
conocimiento/
├── README.md           ← este archivo
├── 1-<tema>.md
├── 2-<tema>.md
└── ...
```
