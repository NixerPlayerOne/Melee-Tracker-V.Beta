"""
diagnostico.py

Chequeo de salud de melee_tracker.db -- no calcula estadisticas de
juego, solo verifica que los datos sean internamente consistentes.
Pensado para correr despues de cualquier batch grande (process_replays,
consultar_rangos, build_sets, backfill_elo) y confirmar que no quedo
nada roto.

Uso:
    python diagnostico.py
"""

import sqlite3
import sys
from pathlib import Path

from config import ensure_config

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"


def check(nombre, ok, detalle=""):
    marca = "OK  " if ok else "FAIL"
    linea = f"[{marca}] {nombre}"
    if detalle:
        linea += f" -- {detalle}"
    print(linea)
    return ok


def main():
    if not DB_PATH.exists():
        print("No existe melee_tracker.db.")
        sys.exit(1)

    cfg = ensure_config()
    my_code = cfg["connect_code"]
    conn = sqlite3.connect(DB_PATH)

    todo_ok = True

    # --- Conteos generales ---
    n_replays = conn.execute("SELECT COUNT(*) FROM replays").fetchone()[0]
    n_match_players = conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
    n_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    n_sets = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    print(f"replays: {n_replays}  match_players: {n_match_players}  players: {n_players}  sets: {n_sets}")
    print()

    # --- Cada replay debe tener EXACTAMENTE 2 filas en match_players ---
    filas = conn.execute(
        "SELECT replay_id, COUNT(*) c FROM match_players GROUP BY replay_id HAVING c != 2"
    ).fetchall()
    todo_ok &= check(
        "Cada replay tiene exactamente 2 jugadores",
        len(filas) == 0,
        f"{len(filas)} replay(s) con != 2 filas" if filas else "",
    )

    # --- No debe haber dos filas del mismo connect_code en el mismo replay ---
    filas = conn.execute(
        "SELECT replay_id, connect_code, COUNT(*) c FROM match_players "
        "GROUP BY replay_id, connect_code HAVING c > 1"
    ).fetchall()
    todo_ok &= check(
        "Sin connect_code duplicado dentro de un mismo replay",
        len(filas) == 0,
        f"{len(filas)} caso(s)" if filas else "",
    )

    # --- is_winner: entre las 2 filas de un mismo replay (cuando ambas
    # estan definidas), nunca deberia sumar 0 (ambos False) ni 2 (ambos True)
    filas = conn.execute(
        """
        SELECT replay_id FROM match_players
        WHERE is_winner IS NOT NULL
        GROUP BY replay_id
        HAVING COUNT(*) = 2 AND SUM(is_winner) IN (0, 2)
        """
    ).fetchall()
    todo_ok &= check(
        "is_winner nunca queda en empate (ambos True o ambos False)",
        len(filas) == 0,
        f"{len(filas)} replay(s)" if filas else "",
    )

    # --- stocks_remaining en rango plausible (0-4) donde no es NULL ---
    filas = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE stocks_remaining IS NOT NULL AND (stocks_remaining < 0 OR stocks_remaining > 4)"
    ).fetchone()[0]
    todo_ok &= check(
        "stocks_remaining en rango 0-4",
        filas == 0,
        f"{filas} fila(s) fuera de rango" if filas else "",
    )

    # --- damage_dealt/damage_taken no negativos ---
    filas = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE damage_dealt < 0 OR damage_taken < 0"
    ).fetchone()[0]
    todo_ok &= check("damage_dealt/damage_taken nunca negativos", filas == 0, f"{filas} fila(s)" if filas else "")

    # --- l_cancel_success nunca mayor que l_cancel_attempts ---
    filas = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE l_cancel_success > l_cancel_attempts"
    ).fetchone()[0]
    todo_ok &= check(
        "l_cancel_success <= l_cancel_attempts siempre",
        filas == 0,
        f"{filas} fila(s)" if filas else "",
    )

    # --- file_path unico (deberia ser imposible por el UNIQUE constraint, chequeo igual) ---
    filas = conn.execute(
        "SELECT COUNT(*) FROM (SELECT file_path FROM replays GROUP BY file_path HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    todo_ok &= check("file_path sin duplicados en replays", filas == 0, f"{filas} caso(s)" if filas else "")

    # --- rank_tiers cubre 0..alto sin huecos ni solapes ---
    filas = conn.execute("SELECT tramo, elo_min, elo_max FROM rank_tiers ORDER BY elo_min").fetchall()
    huecos = 0
    for i in range(1, len(filas)):
        if filas[i - 1][2] != filas[i][1]:
            huecos += 1
    todo_ok &= check(
        f"rank_tiers sin huecos ni solapes ({len(filas)} tramos)",
        len(filas) > 0 and huecos == 0,
        f"{huecos} discontinuidad(es)" if huecos else ("tabla vacia" if not filas else ""),
    )

    # --- Todo connect_code en match_players existe en players (FK deberia garantizarlo, chequeo igual) ---
    filas = conn.execute(
        "SELECT COUNT(DISTINCT mp.connect_code) FROM match_players mp "
        "LEFT JOIN players p ON p.connect_code = mp.connect_code WHERE p.connect_code IS NULL"
    ).fetchone()[0]
    todo_ok &= check("Todo connect_code de match_players existe en players", filas == 0, f"{filas} codigo(s) huerfano(s)" if filas else "")

    # --- Tu propio connect_code aparece marcado is_self=1 ---
    fila = conn.execute("SELECT is_self FROM players WHERE connect_code = ?", (my_code,)).fetchone()
    todo_ok &= check(
        f"Tu connect_code ({my_code}) esta en players con is_self=1",
        fila is not None and fila[0] == 1,
        "no encontrado o is_self != 1" if not (fila and fila[0] == 1) else "",
    )

    # --- Todo replay tiene set_id asignado (si build_sets.py ya corrio) ---
    sin_set = conn.execute("SELECT COUNT(*) FROM replays WHERE set_id IS NULL").fetchone()[0]
    check(
        "Replays sin set_id asignado",
        sin_set == 0,
        f"{sin_set} replay(s) -- normal si todavia no corriste build_sets.py, o si corrio a mitad de un --forzar",
    )

    # --- Cobertura de elo_at_match ---
    total_mp = conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
    sin_elo = conn.execute("SELECT COUNT(*) FROM match_players WHERE elo_at_match IS NULL").fetchone()[0]
    pct = 100 * (total_mp - sin_elo) / total_mp if total_mp else 0
    print(f"[INFO] Cobertura de elo_at_match: {total_mp - sin_elo}/{total_mp} ({pct:.1f}%)")

    conn.close()

    print()
    print("TODO OK" if todo_ok else "HAY PROBLEMAS -- revisar los FAIL de arriba")


if __name__ == "__main__":
    main()
