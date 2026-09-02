"""
Melee Tracker V2 - Paso 2: Configuracion
Guarda y lee ruta de replays y connect code desde la tabla `config` de la base.
Si faltan valores, los detecta automaticamente o los pide por dialogo.

Uso como script (primera vez / reconfigurar):
    python config.py

Uso como modulo (en otros scripts):
    from config import ensure_config
    cfg = ensure_config()
    cfg["replay_path"]      # -> ruta a la carpeta de replays
    cfg["connect_code"]     # -> tu connect code
"""
import platform
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"


def _get_connection():
    return sqlite3.connect(DB_PATH)


def get_config(key, default=None):
    conn = _get_connection()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_config(key, value):
    conn = _get_connection()
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def _default_replay_path():
    system = platform.system()
    if system == "Windows":
        candidate = Path.home() / "Documents" / "Slippi"
    else:  # macOS y Linux
        candidate = Path.home() / "Slippi"
    return candidate if candidate.exists() else None


def _ask_replay_path():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    chosen = filedialog.askdirectory(title="Selecciona tu carpeta de replays de Slippi")
    root.destroy()
    return Path(chosen) if chosen else None


def _ask_connect_code():
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    code = simpledialog.askstring("Connect code", "Ingresa tu connect code (ej: NIXE#677):")
    root.destroy()
    return code


def ensure_config():
    replay_path = get_config("replay_path")
    if not replay_path:
        detected = _default_replay_path()
        replay_path = str(detected) if detected else str(_ask_replay_path())
        set_config("replay_path", replay_path)

    connect_code = get_config("connect_code")
    if not connect_code:
        connect_code = _ask_connect_code()
        set_config("connect_code", connect_code)

    return {"replay_path": replay_path, "connect_code": connect_code}


if __name__ == "__main__":
    if not DB_PATH.exists():
        print("No existe melee_tracker.db. Corre primero init_db.py")
    else:
        cfg = ensure_config()
        print(f"replay_path: {cfg['replay_path']}")
        print(f"connect_code: {cfg['connect_code']}")
