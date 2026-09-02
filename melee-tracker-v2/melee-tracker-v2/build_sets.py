"""
build_sets.py

Agrupa los replays ya procesados en `sets` (enfrentamientos consecutivos
contra un mismo rival). Reemplaza por completo a `sets` cada corrida --
es una tabla derivada, siempre se puede reconstruir desde `replays` +
`match_players`, así que no tiene sentido actualizarla incremental.

Criterio de agrupamiento, en orden de confianza:
  1. `match_id_slp` (cuando esta presente en ambos replays consecutivos):
     Slippi comparte este ID entre todas las partidas de una misma
     sesion de matchmaking online -- es la fuente mas confiable, no una
     heuristica.
  2. Si falta `match_id_slp` en cualquiera de los dos (partida offline o
     sin match id detectado): mismo rival + gap <= --gap-minutos entre
     el fin de una partida y el inicio de la siguiente (mismo criterio
     de resumen_ultima_sesion() en reglas.py, pero acotado a un rival
     puntual en vez de toda la sesion de juego).

`resultado_set` se decide por mayoria de victorias dentro del grupo. Si
empatan (ej. un bo2 cortado a la mitad), queda en NULL -- no se inventa
un resultado de set que el propio grupo de partidas no define con
claridad.

Uso:
    python build_sets.py
    python build_sets.py --gap-minutos 20
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import ensure_config

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"


def cargar_partidas_con_rival(conn: sqlite3.Connection, my_code: str):
    """Una fila por replay: (replay_id, match_id_slp, fecha, rival_code, is_winner), ordenadas por fecha."""
    filas = conn.execute(
        """
        SELECT r.id, r.match_id_slp, r.played_at, rival.connect_code, mio.is_winner
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players rival ON rival.replay_id = r.id AND rival.connect_code != ?
        ORDER BY r.played_at
        """,
        (my_code, my_code),
    ).fetchall()

    resultado = []
    for replay_id, match_id_slp, played_at, rival_code, is_winner in filas:
        fecha = datetime.fromisoformat(played_at) if played_at else None
        resultado.append(
            {"replay_id": replay_id, "match_id_slp": match_id_slp, "fecha": fecha,
             "rival_code": rival_code, "is_winner": is_winner}
        )
    return resultado


def agrupar_en_sets(partidas: list, gap_minutos: int) -> list:
    """Devuelve una lista de grupos (cada grupo = lista de partidas de un mismo set)."""
    grupos = []
    grupo_actual = []
    prev = None

    for p in partidas:
        nuevo_grupo = True

        if grupo_actual:
            mismo_match_id = (
                p["match_id_slp"] is not None
                and prev["match_id_slp"] is not None
                and p["match_id_slp"] == prev["match_id_slp"]
            )
            ninguno_tiene_match_id = p["match_id_slp"] is None and prev["match_id_slp"] is None

            if mismo_match_id:
                nuevo_grupo = False
            elif ninguno_tiene_match_id:
                mismo_rival = p["rival_code"] == prev["rival_code"]
                if mismo_rival and p["fecha"] and prev["fecha"]:
                    gap = (p["fecha"] - prev["fecha"]).total_seconds() / 60
                    nuevo_grupo = gap > gap_minutos
                else:
                    nuevo_grupo = True
            # Si uno tiene match_id_slp y el otro no, o los match_id_slp
            # difieren: son sesiones distintas, nuevo grupo (ya en True).

        if nuevo_grupo and grupo_actual:
            grupos.append(grupo_actual)
            grupo_actual = []

        grupo_actual.append(p)
        prev = p

    if grupo_actual:
        grupos.append(grupo_actual)

    return grupos


def resultado_del_grupo(grupo: list):
    victorias = sum(1 for p in grupo if p["is_winner"] is True)
    derrotas = sum(1 for p in grupo if p["is_winner"] is False)
    if victorias > derrotas:
        return "win"
    if derrotas > victorias:
        return "loss"
    return None  # empate o sin datos suficientes -- no se inventa


def reconstruir_sets(conn: sqlite3.Connection, my_code: str, gap_minutos: int):
    conn.execute("UPDATE replays SET set_id = NULL")
    conn.execute("DELETE FROM sets")

    partidas = cargar_partidas_con_rival(conn, my_code)
    grupos = agrupar_en_sets(partidas, gap_minutos)

    for grupo in grupos:
        rival_code = grupo[0]["rival_code"]
        fechas = [p["fecha"] for p in grupo if p["fecha"] is not None]
        started_at = min(fechas).isoformat() if fechas else None
        ended_at = max(fechas).isoformat() if fechas else None
        resultado_set = resultado_del_grupo(grupo)

        cur = conn.execute(
            "INSERT INTO sets (opponent_connect_code, resultado_set, started_at, ended_at) VALUES (?, ?, ?, ?)",
            (rival_code, resultado_set, started_at, ended_at),
        )
        set_id = cur.lastrowid

        conn.executemany(
            "UPDATE replays SET set_id = ? WHERE id = ?",
            [(set_id, p["replay_id"]) for p in grupo],
        )

    conn.commit()
    return len(grupos), len(partidas)


def main():
    parser = argparse.ArgumentParser(description="Agrupa replays procesados en sets por rival")
    parser.add_argument("--gap-minutos", type=int, default=30, help="Gap maximo entre partidas sin match_id_slp para seguir en el mismo set (default 30)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("No existe melee_tracker.db. Corre primero init_db.py")
        sys.exit(1)

    cfg = ensure_config()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    total_sets, total_partidas = reconstruir_sets(conn, cfg["connect_code"], args.gap_minutos)
    conn.close()

    print(f"Partidas agrupadas: {total_partidas}")
    print(f"Sets construidos:   {total_sets}")
    if total_sets:
        print(f"Promedio de partidas por set: {total_partidas / total_sets:.1f}")


if __name__ == "__main__":
    main()
