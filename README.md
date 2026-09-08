# Melee Tracker

A desktop stats tracker for Super Smash Bros. Melee (Slippi replays). Parses your `.slp` files, tracks your rank, matchups, execution metrics, and more — all locally, no account needed.

🇪🇸 [Versión en español más abajo](#melee-tracker-español)

---

## Download

Go to the [Releases](../../releases) page and download the latest `MeleeTracker.exe`. That's it — no installation, no Python, nothing else to set up.

**Windows only**, for now.

## First run

1. Double-click `MeleeTracker.exe`.
2. **Windows will probably show a "Windows protected your PC" warning.** This is expected — the app isn't code-signed (that costs money and isn't worth it for a free hobby project). Click **"More info"** → **"Run anyway"**.
3. The app will ask you two things the first time: the folder where your Slippi replays are saved, and your connect code (e.g. `ABCD#123`). It only asks once; after that it remembers.
4. A `melee_tracker.db` file will be created next to the `.exe` — this is your local database. Don't delete it unless you want to start fresh.

## Requirements

- Windows 10 or 11.
- [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) — Microsoft's component for embedded browser UIs. Most up-to-date Windows installs already have it. If it's missing, the app will detect it and show you a direct download link instead of crashing.

## What it does

- Parses your `.slp` replays and stores match data locally (no cloud, no external server besides querying your own Slippi rank).
- Tracks ELO/rank history, matchup win rates by character, stage stats, execution metrics (L-cancel %, APM, wavedashes, etc.) over time.
- Groups games into sets, detects kill moves/directions, and more — see the in-app tabs (General, Personajes, Progreso, Historial, Rendimiento, Mi Estadística, Rivales).

## Your data stays yours

`melee_tracker.db` (your match history) and your replay files never leave your computer. The only network call the app makes is to Slippi's public API to fetch rank info for connect codes you've played against.

## Building from source

If you want to build the `.exe` yourself instead of using a Release:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pyinstaller melee_tracker.spec
```

The resulting `MeleeTracker.exe` will be in `dist\`.

## Issues / feedback

Found a bug or have a feature request? Open an [issue](../../issues).

---

<a id="melee-tracker-español"></a>
## Melee Tracker (Español)

Un tracker de estadísticas de escritorio para Super Smash Bros. Melee (replays de Slippi). Analiza tus archivos `.slp`, sigue tu rango, tus matchups, tus métricas de ejecución y más — todo local, sin necesidad de cuenta.

### Descarga

Andá a la página de [Releases](../../releases) y descargá el `MeleeTracker.exe` más reciente. Eso es todo — no hace falta instalar nada, ni Python, ni ninguna otra dependencia.

**Solo Windows**, por ahora.

### Primer uso

1. Hacé doble clic en `MeleeTracker.exe`.
2. **Es probable que Windows muestre un aviso de "Windows protegió su PC".** Esto es esperado — la app no está firmada digitalmente (eso tiene un costo económico que no se justifica para un proyecto hobby gratuito). Hacé clic en **"Más información"** → **"Ejecutar de todas formas"**.
3. La app te va a pedir dos cosas la primera vez: la carpeta donde guardás tus replays de Slippi, y tu connect code (ej. `ABCD#123`). Solo lo pregunta una vez; después lo recuerda.
4. Se va a crear un archivo `melee_tracker.db` al lado del `.exe` — es tu base de datos local. No lo borres a menos que quieras empezar de cero.

### Requisitos

- Windows 10 u 11.
- [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) — un componente de Microsoft para interfaces con navegador embebido. La mayoría de instalaciones de Windows actualizadas ya lo tienen. Si falta, la app lo detecta y te muestra un link de descarga directo en vez de romperse.

### Qué hace

- Analiza tus replays `.slp` y guarda los datos de las partidas localmente (sin nube, sin servidor externo salvo para consultar tu propio rango en la API de Slippi).
- Sigue el historial de ELO/rango, winrate por matchup de personaje, estadísticas por stage, métricas de ejecución (L-cancel %, APM, wavedashes, etc.) a lo largo del tiempo.
- Agrupa partidas en sets, detecta movimientos y direcciones de kill, y más — mirá las pestañas de la app (General, Personajes, Progreso, Historial, Rendimiento, Mi Estadística, Rivales).

### Tus datos son tuyos

`melee_tracker.db` (tu historial de partidas) y tus archivos de replay nunca salen de tu computadora. La única conexión a internet que hace la app es a la API pública de Slippi, para consultar el rango de los connect codes contra los que jugaste.

### Compilar desde el código fuente

Si preferís compilar el `.exe` vos mismo en vez de usar un Release:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pyinstaller melee_tracker.spec
```

El `MeleeTracker.exe` resultante va a quedar en `dist\`.

### Reportar problemas / sugerencias

¿Encontraste un bug o tenés una idea? Abrí un [issue](../../issues).
