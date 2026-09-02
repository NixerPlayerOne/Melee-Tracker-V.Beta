"""
Melee Tracker V2 - Paso 1: Esquema de base de datos
Crea melee_tracker.db con las tablas base: config, players, sets, replays, match_players.
Correr UNA sola vez (o las veces que quieras, usa IF NOT EXISTS) para inicializar la base.

Uso:
    python init_db.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Reemplaza rangos_cache.json: bucketing de tramo vive en la base,
-- una sola fuente de verdad para histograma y resultados-por-tramo.
-- elo_min inclusive, elo_max EXCLUSIVE (elo_min <= elo < elo_max), igual
-- que la comparación "elo < umbral" del generar_reporte.py original.
-- tramo_grande agrupa los sub-rangos finos (Bronze I/II/III -> Bronze,
-- ... Master I/II/III + Grandmaster -> Master+) para que el histograma
-- nunca pueda desalinearse del detalle por rival, a diferencia del
-- UMBRALES_RANGO vs TRAMOS_GRANDES original que se desalinearon entre sí.
CREATE TABLE IF NOT EXISTS rank_tiers (
    tramo TEXT PRIMARY KEY,
    tramo_grande TEXT NOT NULL,
    elo_min REAL NOT NULL,
    elo_max REAL NOT NULL
);

-- Log append-only: cada consulta a la API queda como fila nueva, nunca se
-- sobreescribe. Permite ver evolución de rango y, al procesar backlog de
-- replays viejos, elegir el registro más cercano en el tiempo a played_at
-- en vez de solo "el elo de hoy".
CREATE TABLE IF NOT EXISTS elo_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connect_code TEXT NOT NULL REFERENCES players(connect_code),
    elo REAL NOT NULL,
    queried_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    connect_code TEXT PRIMARY KEY,
    display_tag TEXT,
    -- Cache del último valor, para no tener que hacer JOIN+MAX en cada
    -- lectura. Se escribe siempre junto con el INSERT a elo_history (mismo
    -- lugar en el código), así que no puede desincronizarse.
    elo REAL,
    -- Record oficial de ranked que devuelve la API de Slippi (distinto
    -- de tu record local, que solo cuenta lo que tenés guardado como
    -- replay -- estos dos numeros pueden no coincidir).
    ranked_wins INTEGER,
    ranked_losses INTEGER,
    rank_tier TEXT REFERENCES rank_tiers(tramo),
    rank_updated_at TEXT,
    is_self INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opponent_connect_code TEXT REFERENCES players(connect_code),
    resultado_set TEXT CHECK (resultado_set IN ('win', 'loss') OR resultado_set IS NULL),
    started_at TEXT,
    ended_at TEXT
);

-- Replays que se determinó que hay que saltar (doubles, sin connect
-- code de rival, etc.) -- sin esto, cada corrida de process_replays.py
-- volvía a abrir y parsear estos archivos completos solo para
-- descubrir de nuevo que había que saltarlos.
CREATE TABLE IF NOT EXISTS omitidos (
    file_path TEXT PRIMARY KEY,
    motivo TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    file_hash TEXT,
    -- ID de sesion/match de Slippi (de extraer_match_id). Comparte valor
    -- entre juegos de un mismo set online -- guardado para poder agrupar
    -- en `sets` mas adelante sin tener que re-parsear los .slp.
    match_id_slp TEXT,
    played_at TEXT,
    stage_id INTEGER,
    duration_frames INTEGER,
    game_mode TEXT,
    -- Dato oficial del evento Game End: como termino (GAME/TIME/NO_CONTEST/...)
    -- y que puerto se fue por combo L+R+A+Start, si alguno. Guardado para
    -- no tener que re-parsear el .slp para saber si una partida se
    -- abandono en vez de jugarse hasta el final.
    end_method TEXT,
    lras_port INTEGER,
    set_id INTEGER REFERENCES sets(id),
    analysis_version INTEGER NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_id INTEGER NOT NULL REFERENCES replays(id) ON DELETE CASCADE,
    connect_code TEXT NOT NULL REFERENCES players(connect_code),
    port INTEGER,
    character_id INTEGER,
    is_winner INTEGER,
    stocks_remaining INTEGER,
    digital_inputs_total INTEGER,
    inputs_total INTEGER,
    wavedashes INTEGER,
    wavelands INTEGER,
    ipm REAL,
    -- Tier 1 (post-frame stats), guardados crudos: nunca promediar % por partida
    l_cancel_success INTEGER,
    l_cancel_attempts INTEGER,
    rolls INTEGER,
    spot_dodges INTEGER,
    air_dodges INTEGER,
    ledge_grabs INTEGER,
    dash_dances INTEGER,
    -- Danio y stocks de ESTA fila (su propio danio hecho/recibido, no "mi"/"rival")
    damage_dealt REAL,
    damage_taken REAL,
    stocks_lost INTEGER,
    -- Conversiones (portado de slippi-js conversions.ts), crudo por partida
    openings_created INTEGER,
    kills INTEGER,
    deaths INTEGER,
    neutral_wins INTEGER,
    counter_hits INTEGER,
    trades INTEGER,
    trades_benefited INTEGER,
    -- Snapshot del elo del rival al momento de PROCESAR el replay (no existe
    -- elo histórico real en la API de Slippi ni en el .slp). Es la mejor
    -- aproximación disponible a "rango al momento de la partida" -- solo es
    -- precisa si el replay se procesa poco después de jugado.
    elo_at_match REAL,
    -- 1 si elo_at_match viene de un registro de elo_history EN O ANTES
    -- de la fecha de esta partida (preciso); 0 si es el mas cercano
    -- disponible pero de otra fecha (aproximado); NULL si no se
    -- completo todavia. Sin esto se perdia la distincion que
    -- backfill_elo.py ya calculaba, y la UI mostraba todo con la
    -- misma confianza sin importar que tan preciso fuera.
    elo_at_match_exacto INTEGER
);

-- Detalle de cada kill de la partida (en cualquier direccion): con que
-- movimiento, a que porcentaje, y hacia que blast zone. Complementa a
-- match_players.kills/deaths (que son solo el conteo) -- de aca sale
-- "con que movimiento matas mas" y su contraparte "con que movimiento
-- te matan mas", cruzado por personaje propio/rival. Ver detectar_kills()
-- en process_replays.py.
CREATE TABLE IF NOT EXISTS kills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_id INTEGER NOT NULL REFERENCES replays(id) ON DELETE CASCADE,
    victim_connect_code TEXT NOT NULL REFERENCES players(connect_code),
    victim_character_id INTEGER,
    victim_percent REAL,
    killer_connect_code TEXT NOT NULL REFERENCES players(connect_code),
    killer_character_id INTEGER,
    -- Attack enum de slippi.id (NAIR=13, FAIR=14, BAIR=15, etc. -- ver
    -- formato.attack_nombre()). NULL si el rival no habia conectado
    -- ningun golpe todavia (ej. autodestruccion desde el arranque).
    killer_move INTEGER,
    -- 'left' | 'right' | 'up' | 'down', del action state real de "muriendo"
    death_direction TEXT,
    frame INTEGER
);

CREATE INDEX IF NOT EXISTS idx_replays_played_at ON replays(played_at);
CREATE INDEX IF NOT EXISTS idx_replays_stage ON replays(stage_id);
CREATE INDEX IF NOT EXISTS idx_match_players_replay ON match_players(replay_id);
CREATE INDEX IF NOT EXISTS idx_match_players_code ON match_players(connect_code);
CREATE INDEX IF NOT EXISTS idx_match_players_char ON match_players(character_id);
CREATE INDEX IF NOT EXISTS idx_players_rank_tier ON players(rank_tier);
CREATE INDEX IF NOT EXISTS idx_elo_history_code_time ON elo_history(connect_code, queried_at);
CREATE INDEX IF NOT EXISTS idx_kills_replay ON kills(replay_id);
CREATE INDEX IF NOT EXISTS idx_kills_killer ON kills(killer_connect_code, killer_character_id);
CREATE INDEX IF NOT EXISTS idx_kills_victim ON kills(victim_connect_code, victim_character_id);
"""


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()

    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tablas = [row[0] for row in cur.fetchall() if row[0] != "sqlite_sequence"]
    conn.close()

    print(f"Base de datos creada en: {DB_PATH}")
    print(f"Tablas: {', '.join(tablas)}")


if __name__ == "__main__":
    main()
