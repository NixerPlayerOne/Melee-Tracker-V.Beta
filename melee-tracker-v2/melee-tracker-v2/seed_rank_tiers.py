"""
seed_rank_tiers.py

Siembra la tabla rank_tiers con los umbrales de elo -> tramo, extraídos
de UMBRALES_RANGO en generar_reporte.py (el original, fino, no el
TRAMOS_GRANDES que estaba desalineado con este).

tramo_grande agrupa los 3 sub-rangos de cada rango (I/II/III) en un solo
bucket para el histograma, y Master I/II/III + Grandmaster se agrupan en
"Master+" -- así el histograma y el detalle por rival siempre coinciden,
porque salen de la misma fila.

Correr después de init_db.py:
    python seed_rank_tiers.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"

# (tramo, tramo_grande, elo_min inclusive, elo_max exclusivo)
# elo_max de Grandmaster es un centinela alto, no un tope real.
RANK_TIERS = [
    ("Bronze I",      "Bronze",   0,    766),
    ("Bronze II",     "Bronze",   766,  914),
    ("Bronze III",    "Bronze",   914,  1055),
    ("Silver I",      "Silver",   1055, 1189),
    ("Silver II",     "Silver",   1189, 1316),
    ("Silver III",    "Silver",   1316, 1437),
    ("Gold I",        "Gold",     1437, 1549),
    ("Gold II",       "Gold",     1549, 1655),
    ("Gold III",      "Gold",     1655, 1752),
    ("Platinum I",    "Platinum", 1752, 1843),
    ("Platinum II",   "Platinum", 1843, 1928),
    ("Platinum III",  "Platinum", 1928, 2004),
    ("Diamond I",     "Diamond",  2004, 2075),
    ("Diamond II",    "Diamond",  2075, 2137),
    ("Diamond III",   "Diamond",  2137, 2192),
    ("Master I",      "Master+",  2192, 2275),
    ("Master II",     "Master+",  2275, 2351),
    ("Master III",    "Master+",  2351, 2426),
    ("Grandmaster",   "Master+",  2426, 99999),
]


def main():
    if not DB_PATH.exists():
        print(f"No se encontró {DB_PATH}. Corré init_db.py primero.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "INSERT OR REPLACE INTO rank_tiers (tramo, tramo_grande, elo_min, elo_max) "
        "VALUES (?, ?, ?, ?)",
        RANK_TIERS,
    )
    conn.commit()

    cur = conn.execute("SELECT COUNT(*) FROM rank_tiers")
    total = cur.fetchone()[0]
    conn.close()

    print(f"rank_tiers sembrada: {total} tramos.")


if __name__ == "__main__":
    main()
