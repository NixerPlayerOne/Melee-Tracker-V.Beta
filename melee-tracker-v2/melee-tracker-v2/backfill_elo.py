"""
backfill_elo.py

Completa `match_players.elo_at_match` para filas ya procesadas, usando
el `elo_history` de cada connect_code (propio y de rivales). Reutiliza
`cargar_mi_elo_history()` de reglas.py -- es generica por connect_code
pese al nombre, no hace falta reescribirla.

Prioridad de busqueda para cada partida:
  1. El registro de elo_history mas reciente EN O ANTES de la fecha de
     la partida (mismo criterio que Regla 2, via mi_elo_en_fecha) --
     el mas preciso, si existe.
  2. Si no hay ninguno anterior (caso comun: rival nunca consultado
     hasta ahora, o partida mas vieja que el primer registro), se usa
     el registro mas CERCANO en el tiempo sin importar la direccion --
     puede ser posterior a la partida. Sigue siendo un dato REAL
     observado, no un valor inventado (nunca se asume "seguro sigue en
     1100" sin un registro real que lo respalde) -- solo que puede
     estar desactualizado si el jugador se movio mucho de rango desde
     entonces. Se cuenta aparte para que quede claro cuantas filas son
     precisas vs aproximadas.

Requiere haber corrido consultar_rangos.py (y opcionalmente
importar_elo_legacy.py) al menos una vez, o no va a encontrar nada
para completar.

Uso:
    python backfill_elo.py
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from reglas import cargar_mi_elo_history, mi_elo_en_fecha

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"


def elo_mas_cercano(historia: list, fecha_partida: datetime):
    """(elo, fue_exacto). Prioriza el mas reciente <= fecha_partida; si
    no hay ninguno, cae al mas cercano en cualquier direccion."""
    exacto = mi_elo_en_fecha(historia, fecha_partida)
    if exacto is not None:
        return exacto, True
    if not historia:
        return None, False
    _, elo = min(historia, key=lambda par: abs((par[0] - fecha_partida).total_seconds()))
    return elo, False


def backfill_elo_at_match(conn: sqlite3.Connection) -> tuple:
    codigos = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT connect_code FROM match_players WHERE elo_at_match IS NULL"
        ).fetchall()
    ]

    total_exactos = 0
    total_aproximados = 0
    for code in codigos:
        historia = cargar_mi_elo_history(conn, code)
        if not historia:
            continue

        filas = conn.execute(
            "SELECT mp.id, r.played_at FROM match_players mp "
            "JOIN replays r ON r.id = mp.replay_id "
            "WHERE mp.connect_code = ? AND mp.elo_at_match IS NULL AND r.played_at IS NOT NULL",
            (code,),
        ).fetchall()

        for mp_id, played_at in filas:
            fecha = datetime.fromisoformat(played_at)
            elo, fue_exacto = elo_mas_cercano(historia, fecha)
            if elo is not None:
                conn.execute(
                    "UPDATE match_players SET elo_at_match = ?, elo_at_match_exacto = ? WHERE id = ?",
                    (elo, 1 if fue_exacto else 0, mp_id),
                )
                if fue_exacto:
                    total_exactos += 1
                else:
                    total_aproximados += 1

    conn.commit()
    return total_exactos, total_aproximados


def main():
    if not DB_PATH.exists():
        print("No existe melee_tracker.db. Corre primero init_db.py")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    pendientes_antes = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE elo_at_match IS NULL"
    ).fetchone()[0]

    exactos, aproximados = backfill_elo_at_match(conn)

    pendientes_despues = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE elo_at_match IS NULL"
    ).fetchone()[0]
    conn.close()

    print(f"Filas sin elo_at_match antes:   {pendientes_antes}")
    print(f"Completadas con dato exacto (en o antes de la partida): {exactos}")
    print(f"Completadas con dato aproximado (mas cercano disponible): {aproximados}")
    print(f"Filas sin elo_at_match despues: {pendientes_despues}")
    print("(las restantes: ese connect_code nunca fue consultado, no hay ningun elo_history)")


if __name__ == "__main__":
    main()

