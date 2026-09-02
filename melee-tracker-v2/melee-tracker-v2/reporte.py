"""
reporte.py

Reporte de texto con tus stats reales. No calcula nada nuevo -- todo
sale de funciones que ya existen en reglas.py, mas un par de queries
de horas jugadas (triviales: suma de duration_frames, ya guardado
desde el paso 3, no requiere trackear nada aparte).

Uso:
    python reporte.py
"""

import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from config import ensure_config
from reglas import (
    cargar_partidas_propias, cargar_mi_elo_history, stats_historicas,
    stats_quincena_reciente, zscore_partida, zscore_detalle, peso_resultado,
    autodestrucciones, dias_jugados, racha_actual_dias, resumen_dia,
    resumen_ultima_sesion, tiempo_total_seg, duracion_promedio_seg,
    contexto_rival_partida, METRICAS_EJECUCION,
)

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"


def fmt_horas(segundos):
    if not segundos:
        return "0h"
    h = segundos / 3600
    return f"{h:.1f}h"


def top_rivales(conn: sqlite3.Connection, my_code: str, n=10):
    filas = conn.execute(
        """
        SELECT rival.connect_code, COUNT(*) enfrentamientos,
               SUM(CASE WHEN mio.is_winner = 1 THEN 1 ELSE 0 END) victorias,
               SUM(CASE WHEN mio.is_winner = 0 THEN 1 ELSE 0 END) derrotas
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players rival ON rival.replay_id = r.id AND rival.connect_code != ?
        GROUP BY rival.connect_code
        ORDER BY enfrentamientos DESC
        LIMIT ?
        """,
        (my_code, my_code, n),
    ).fetchall()
    return filas


def main():
    if not DB_PATH.exists():
        print("No existe melee_tracker.db. Corre primero init_db.py")
        sys.exit(1)

    cfg = ensure_config()
    my_code = cfg["connect_code"]
    conn = sqlite3.connect(DB_PATH)

    partidas = cargar_partidas_propias(conn, my_code)
    mi_elo_history = cargar_mi_elo_history(conn, my_code)

    print("=" * 60)
    print(f"REPORTE -- {my_code}")
    print("=" * 60)
    print()

    # --- General ---
    print(f"Partidas totales:  {len(partidas)}")
    print(f"Dias jugados:      {dias_jugados(partidas)}")
    print(f"Racha actual:      {racha_actual_dias(partidas)} dias")
    print(f"Tiempo total:      {fmt_horas(tiempo_total_seg(partidas))}")
    dur_prom = duracion_promedio_seg(partidas)
    print(f"Duracion promedio: {dur_prom:.0f}s" if dur_prom else "Duracion promedio: N/D")

    hoy = resumen_dia(partidas)
    if hoy["partidas"]:
        print(f"Hoy:               {hoy['victorias']}-{hoy['derrotas']} ({hoy['winrate_pct']}% WR)")

    sesion = resumen_ultima_sesion(partidas)
    if sesion:
        print(f"Ultima sesion:     {sesion['fecha']} -- {sesion['partidas']} partidas, {fmt_horas(sesion['duracion_seg'])}")

    print()

    # --- Resultado general (solo partidas con resultado conocido) ---
    con_resultado = [p for p in partidas.values() if p.get("resultado")]
    victorias = sum(1 for p in con_resultado if p["resultado"] == "victoria")
    derrotas = len(con_resultado) - victorias
    if con_resultado:
        print(f"Record: {victorias}-{derrotas} ({100 * victorias / len(con_resultado):.1f}% WR, sobre {len(con_resultado)} partidas con resultado determinado)")
    sin_resultado = len(partidas) - len(con_resultado)
    if sin_resultado:
        print(f"({sin_resultado} partidas sin resultado determinable -- stocks y kills empatados)")
    print()

    # --- Top rivales ---
    print("-" * 60)
    print("TOP 10 RIVALES (por enfrentamientos)")
    print("-" * 60)
    for code, enf, v, d in top_rivales(conn, my_code):
        wr = f"{100 * v / (v + d):.0f}%" if (v + d) else "N/D"
        print(f"  {code:<14} {enf:>3} partidas   {v}-{d}  ({wr} WR)")
    print()

    # --- Ejecucion: historico vs ultima quincena completa ---
    print("-" * 60)
    print("EJECUCION -- historico vs ultima quincena completa")
    print("-" * 60)
    hist = stats_historicas(partidas)
    quincena = stats_quincena_reciente(partidas)
    for campo in METRICAS_EJECUCION:
        m_h, s_h, n_h = hist.get(campo, (None, None, 0))
        m_q, s_q, n_q = quincena.get(campo, (None, None, 0))
        val_h = f"{m_h:.2f}" if m_h is not None else "N/D"
        val_q = f"{m_q:.2f}" if m_q is not None else "N/D"
        print(f"  {campo:<20} historico: {val_h:>8} (n={n_h:<4})   quincena: {val_q:>8} (n={n_q})")
    print()

    # --- Autodestrucciones (ultimas 20 partidas) ---
    print("-" * 60)
    print("AUTODESTRUCCIONES -- ultimas 20 partidas con dato")
    print("-" * 60)
    con_fecha = sorted(
        (p["fecha"], p) for p in partidas.values() if p.get("fecha") and p.get("my_stocks_lost") is not None
    )
    ultimas_20 = [p for _, p in con_fecha[-20:]]
    mias_total = rival_total = 0
    for p in ultimas_20:
        auto = autodestrucciones(p)
        if auto["propias"] is not None:
            mias_total += auto["propias"]
        if auto["rival"] is not None:
            rival_total += auto["rival"]
    print(f"  Tuyas:  {mias_total}")
    print(f"  Rival:  {rival_total}")
    print()

    # --- Contexto de ELO (requiere mi_elo_history) ---
    if mi_elo_history:
        print("-" * 60)
        print("ENFRENTAMIENTOS POR CONTEXTO DE ELO")
        print("-" * 60)
        contextos = Counter()
        for p in partidas.values():
            ctx = contexto_rival_partida(p, mi_elo_history)
            if ctx:
                contextos[ctx] += 1
        if contextos:
            for ctx, cant in contextos.most_common():
                print(f"  {ctx:<35} {cant}")
        else:
            print("  (sin datos suficientes -- elo_rival o mi elo en esa fecha no disponibles)")
    else:
        print("(Sin tu propio elo_history -- corre importar_elo_legacy.py o esperá a que consultar_rangos.py acumule mas datos)")
    print()

    conn.close()


if __name__ == "__main__":
    main()
