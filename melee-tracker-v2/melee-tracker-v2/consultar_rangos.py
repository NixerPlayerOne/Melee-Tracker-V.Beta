"""
consultar_rangos.py

Version SQLite. Reemplaza a la version original (rivales.json + API +
rangos_cache.json). La logica de consulta a la API de Slippi es
VERBATIM del original -- no se toco. Lo que cambia:

  - La lista de "a quien consultar" sale de la tabla `players` (se puebla
    sola al correr process_replays.py) en vez de rivales.json. Esto
    incluye TU PROPIO connect_code (players.is_self=1), no solo rivales
    -- el original nunca lo trackeaba, pero Regla 2 (contexto_rival, en
    reglas.py) necesita tu propio historial de elo para funcionar.
  - Cada consulta exitosa se guarda como fila NUEVA en `elo_history`
    (nunca se sobreescribe, a diferencia del cache[code] = resultado
    original), y ademas actualiza `players.elo/rank_tier/rank_updated_at`
    como cache del ultimo valor.
  - Timestamps guardados naive-local (sin tzinfo), igual que
    replays.played_at -- si se guardaran en UTC, las comparaciones de
    fecha en reglas.py (Regla 2) romperian con TypeError al mezclar
    naive y aware.
  - El tramo (Bronze I, Silver II, etc.) se calcula al toque contra
    `rank_tiers`, no hay que mapearlo aparte como hacia generar_reporte.py.

Uso:
    python consultar_rangos.py
    python consultar_rangos.py --forzar            # ignora el TTL, re-consulta todo
    python consultar_rangos.py --dias-cache 3       # cambia el TTL (default 7)
    python consultar_rangos.py --limite 5           # solo los primeros N pendientes (para probar)
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Fix para consolas de Windows que no usan UTF-8 por defecto (crashea con
# nombres de rival que tengan caracteres especiales/emojis)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"

API_URL = "https://internal.slippi.gg/graphql"
DELAY_ENTRE_REQUESTS = 0.4  # segundos, para no golpear la API muy rapido

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "apollographql-client-name": "slippi-web",
    "content-type": "application/json",
    "origin": "https://slippi.gg",
    "referer": "https://slippi.gg/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}

QUERY = """
query Q($cc: String!) {
  getUser(connectCode: $cc) {
    displayName
    connectCode {
      code
    }
    rankedNetplayProfile {
      ratingOrdinal
      ratingUpdateCount
      wins
      losses
      dailyGlobalPlacement
      dailyRegionalPlacement
      characters {
        character
        gameCount
      }
    }
  }
}
"""


def consultar_connect_code(session, connect_code: str) -> dict:
    """
    Hace la consulta a la API de Slippi para un connect code. VERBATIM
    del original. Devuelve un dict normalizado con el resultado o el
    motivo de fallo.
    """
    payload = {
        "operationName": "Q",
        "variables": {"cc": connect_code},
        "query": QUERY,
    }

    try:
        resp = session.post(API_URL, headers=HEADERS, json=payload, timeout=10)
    except Exception as e:
        return {"estado": "error_red", "detalle": str(e)}

    if resp.status_code != 200:
        return {
            "estado": "error_http",
            "detalle": f"status {resp.status_code} | body: {resp.text[:600]!r}",
        }

    try:
        data = resp.json()
    except Exception:
        return {
            "estado": "error_json",
            "detalle": (
                f"status {resp.status_code} | "
                f"content-type: {resp.headers.get('content-type')} | "
                f"body: {resp.text[:300]!r}"
            ),
        }

    if data.get("errors"):
        return {"estado": "error_api", "detalle": str(data["errors"])[:500]}

    user = data.get("data", {}).get("getUser")
    if not user:
        return {"estado": "no_encontrado", "detalle": "connect code inexistente"}

    ranked = user.get("rankedNetplayProfile")

    if not ranked or ranked.get("ratingOrdinal") is None:
        return {
            "estado": "sin_ranked",
            "detalle": "usuario existe pero sin perfil ranked",
            "display_name": user.get("displayName"),
        }

    return {
        "estado": "ok",
        "display_name": user.get("displayName"),
        "elo": round(ranked["ratingOrdinal"], 2),
        "wins": ranked.get("wins"),
        "losses": ranked.get("losses"),
        "rating_update_count": ranked.get("ratingUpdateCount"),
        "daily_global_placement": ranked.get("dailyGlobalPlacement"),
        "daily_regional_placement": ranked.get("dailyRegionalPlacement"),
    }


# ==========================================================================
# NUEVO: SQLite en vez de rivales.json + rangos_cache.json
# ==========================================================================
def obtener_pendientes(conn: sqlite3.Connection, dias_cache: int, forzar: bool):
    """
    Incluye TU PROPIO connect_code (is_self=1), no solo rivales -- a
    diferencia del rivales.json original, que nunca te incluia a vos.
    """
    if forzar:
        rows = conn.execute("SELECT connect_code FROM players ORDER BY connect_code").fetchall()
        return [r[0] for r in rows]

    limite = (datetime.now() - timedelta(days=dias_cache)).isoformat()
    rows = conn.execute(
        "SELECT connect_code FROM players "
        "WHERE rank_updated_at IS NULL OR rank_updated_at < ? "
        "ORDER BY connect_code",
        (limite,),
    ).fetchall()
    return [r[0] for r in rows]


def elo_a_tramo(conn: sqlite3.Connection, elo: float):
    row = conn.execute(
        "SELECT tramo FROM rank_tiers WHERE elo_min <= ? AND ? < elo_max",
        (elo, elo),
    ).fetchone()
    return row[0] if row else None


def guardar_resultado(conn: sqlite3.Connection, connect_code: str, resultado: dict, queried_at: str):
    estado = resultado["estado"]

    if estado == "ok":
        elo = resultado["elo"]
        conn.execute(
            "INSERT INTO elo_history (connect_code, elo, queried_at) VALUES (?, ?, ?)",
            (connect_code, elo, queried_at),
        )
        tramo = elo_a_tramo(conn, elo)
        conn.execute(
            "UPDATE players SET elo = ?, rank_tier = ?, rank_updated_at = ?, "
            "ranked_wins = ?, ranked_losses = ? WHERE connect_code = ?",
            (elo, tramo, queried_at, resultado.get("wins"), resultado.get("losses"), connect_code),
        )
    elif estado == "sin_ranked":
        # Legitimamente no tiene perfil ranked -- se respeta el TTL igual,
        # no tiene sentido re-consultar en cada corrida.
        conn.execute(
            "UPDATE players SET rank_updated_at = ? WHERE connect_code = ?",
            (queried_at, connect_code),
        )
    # error_red/error_http/error_json/error_api/no_encontrado: no se toca
    # rank_updated_at, para que se reintente la proxima corrida sin
    # esperar el TTL completo por un problema transitorio.


def consultar_pendientes(conn: sqlite3.Connection, dias_cache: int = 7, forzar: bool = False,
                          limite: int = None, on_progress=None) -> dict:
    """
    Nucleo reusable: consulta a la API los connect_codes pendientes y
    guarda resultados. Usado tanto por main() (CLI) como por app.py
    (parte del pipeline del boton "Procesar nuevos") -- una sola
    implementacion, no una copia.

    on_progress(evento, connect_code, detalle) se llama:
      - una vez al principio con evento="inicio", connect_code=None,
        detalle=<total de pendientes a consultar>
      - por cada consulta despues, con evento en
        {"ok", "sin_ranked", "error"}

    Devuelve un dict con los conteos finales. Si falta 'requests',
    devuelve {"error": "sin_requests"} sin tocar la base.
    """
    try:
        import requests
    except ImportError:
        return {"error": "sin_requests"}

    pendientes = obtener_pendientes(conn, dias_cache, forzar)
    total_jugadores = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]

    if limite is not None:
        pendientes = pendientes[:limite]

    if on_progress:
        on_progress("inicio", None, len(pendientes))

    if not pendientes:
        return {
            "total_jugadores": total_jugadores, "pendientes": 0,
            "ok": 0, "sin_ranked": 0, "errores": 0,
        }

    session = requests.Session()
    contador_ok = contador_sin_ranked = contador_error = 0

    for code in pendientes:
        resultado = consultar_connect_code(session, code)
        queried_at = datetime.now().isoformat()

        guardar_resultado(conn, code, resultado, queried_at)
        conn.commit()

        estado = resultado["estado"]
        if estado == "ok":
            contador_ok += 1
            if on_progress:
                on_progress("ok", code, resultado.get("elo"))
        elif estado == "sin_ranked":
            contador_sin_ranked += 1
            if on_progress:
                on_progress("sin_ranked", code, None)
        else:
            contador_error += 1
            if on_progress:
                on_progress("error", code, resultado.get("detalle"))

        time.sleep(DELAY_ENTRE_REQUESTS)

    return {
        "total_jugadores": total_jugadores, "pendientes": len(pendientes),
        "ok": contador_ok, "sin_ranked": contador_sin_ranked, "errores": contador_error,
    }


def main():
    parser = argparse.ArgumentParser(description="Consulta rangos de Slippi para los connect codes de la base")
    parser.add_argument("--forzar", action="store_true", help="Ignora el TTL y re-consulta todo")
    parser.add_argument("--dias-cache", type=int, default=7, help="Dias antes de refrescar una entrada (default 7)")
    parser.add_argument("--limite", type=int, default=None, help="Solo procesa los primeros N pendientes (para pruebas)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("No existe melee_tracker.db. Corre primero init_db.py")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    total_antes = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    pendientes_preview = obtener_pendientes(conn, args.dias_cache, args.forzar)
    print(f"Jugadores en la base: {total_antes}")
    print(f"Ya al dia (TTL {args.dias_cache}d vigente): {total_antes - len(pendientes_preview)}")
    print(f"Por consultar: {len(pendientes_preview)}")
    if args.limite is not None:
        print(f"(Limitado a los primeros {args.limite} para esta corrida)")
    print()

    def imprimir_progreso(evento, code, detalle):
        if evento == "inicio":
            return
        if evento == "ok":
            print(f"  {code:<12} -> ELO {detalle:.0f}" if detalle is not None else f"  {code:<12} -> OK")
        elif evento == "sin_ranked":
            print(f"  {code:<12} -> sin perfil ranked")
        else:
            print(f"  {code:<12} -> error: {detalle}")

    resultado = consultar_pendientes(
        conn, dias_cache=args.dias_cache, forzar=args.forzar, limite=args.limite,
        on_progress=imprimir_progreso,
    )
    conn.close()

    if resultado.get("error") == "sin_requests":
        print("Falta la libreria 'requests'. Instalala con: pip install requests")
        sys.exit(1)

    if resultado["pendientes"] == 0:
        print("Nada que consultar, todo esta al dia. Usa --forzar para re-consultar todo.")
        return

    print()
    print("=" * 50)
    print("Resumen de la consulta")
    print("=" * 50)
    print(f"OK (con ELO):        {resultado['ok']}")
    print(f"Sin perfil ranked:   {resultado['sin_ranked']}")
    print(f"Errores/no hallados: {resultado['errores']}")


if __name__ == "__main__":
    main()
