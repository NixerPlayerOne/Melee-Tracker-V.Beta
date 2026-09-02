# Melee Tracker V2

Tracker de estadísticas personales para Super Smash Bros. Melee, basado en
replays de [Slippi](https://slippi.gg). Corre en tu propia máquina, procesa
tus `.slp` locales y guarda todo en una base SQLite propia — no depende de
ningún servicio externo salvo la API pública de Slippi para consultar el
rango de tus rivales.

Aplicación de escritorio hecha con Python + SQLite + [pywebview](https://pywebview.flowrl.com/)
(la interfaz es HTML/CSS/JS corriendo en una ventana nativa, sin necesidad de
un navegador aparte).

## Qué muestra

- **General** — tu perfil, ELO y rango actual, última partida jugada con
  contexto (rival favorito/parejo/underdog), insignias de perfil
- **Personajes** — con quién ganás/perdés más, a quién dominás más, con qué
  movimiento matás y con qué movimiento te matan a vos, por personaje
- **Progreso** — historial de ELO, análisis de stocks (grade S–E por partida)
- **Historial** — rachas, matchups difíciles, rendimiento por turno/día/mes
- **Rendimiento** — tu ejecución mecánica (L-cancel, APM, wavedash...) vs. tu
  propio histórico, y si el resultado de cada partida fue "merecido"
- **Mi Estadística** — nivel real de tus rivales, ELO ponderado por partida
- **Rivales** — detalle completo, uno por uno, con varios criterios de orden

## Requisitos

- Python 3.10+
- Windows (probado ahí; `pywebview` es multiplataforma, pero parte del
  manejo de hilos para no bloquear la UI está pensado para WebView2)
- Tus replays de Slippi en algún lado del disco

## Instalación

```bash
git clone https://github.com/TU-USUARIO/melee-tracker-v2.git
cd melee-tracker-v2
pip install -r requirements.txt

python init_db.py          # crea melee_tracker.db
python seed_rank_tiers.py  # siembra los tramos de rango (Bronze..Master+)
```

La primera vez que corras `app.py` te va a pedir tu connect code y la
carpeta donde Slippi guarda tus replays.

## Uso

```bash
python app.py
```

Desde la ventana: **"Procesar replays nuevos"** corre el pipeline completo
(parsea `.slp` nuevos → consulta rangos de rivales → completa ELO histórico
→ arma sets). Se puede volver a correr las veces que haga falta, es seguro.

Si preferís la terminal, cada paso del pipeline también es un script aparte:

```bash
python process_replays.py     # parsea .slp nuevos
python consultar_rangos.py    # consulta ELO de rivales nuevos/vencidos
python backfill_elo.py        # completa el ELO histórico por partida
python build_sets.py          # agrupa partidas consecutivas en sets
python diagnostico.py         # chequeo de salud de la base
python reporte.py             # reporte de texto por consola
```

## Íconos de personajes (opcional)

La app puede mostrar el stock icon de cada personaje si existe el archivo
correspondiente en `assets/chars/`. Esa carpeta va vacía en el repo a
propósito — son sprites del juego, no se redistribuyen acá. Ver
[`assets/chars/README.md`](assets/chars/README.md) para cómo conseguirlos.
Sin ellos, la app funciona igual (cae a texto/medallas).

## Si actualizás el código y cambia el schema de la base

`init_db.py` usa `CREATE TABLE/INDEX IF NOT EXISTS` en todo, así que
correrlo de nuevo sobre una base existente es seguro — solo agrega lo que
falte, nunca borra datos. Además, `config.py` lo corre automáticamente
cada vez que arranca cualquier script, así que en general no hace falta
acordarse de correrlo a mano.

Si el cambio de schema agrega una columna que se llena al parsear el
replay (por ejemplo, la tabla `kills`), los replays ya procesados no la
van a tener hasta que los reproceses:

```bash
python process_replays.py --forzar
```

## Estructura del proyecto

```
app.py               UI de escritorio (pywebview) -- las 7 pestañas
config.py             Configuración (connect code, carpeta de replays)
init_db.py             Esquema de la base SQLite
process_replays.py    Parser de .slp (mecánica, conversiones, kills)
consultar_rangos.py   Consulta de ELO/rango a la API de Slippi
backfill_elo.py       Completa el ELO histórico por partida
build_sets.py          Agrupa partidas consecutivas en sets
reglas.py               Lógica de negocio (Reglas 1-5, stats históricas)
formato.py             Constantes visuales y formateo compartido
insignias.py           Sistema de insignias de perfil y de partida
diagnostico.py         Chequeo de integridad de la base
reporte.py              Reporte de texto por consola
seed_rank_tiers.py     Siembra los tramos de ELO
```

## Licencia

[MIT](LICENSE) — usalo, modificalo, lo que quieras.

## Créditos

- [Project Slippi](https://slippi.gg) por el formato de replay y la API de rango
- [py-slippi](https://github.com/hohav/py-slippi) por el parser de `.slp`
