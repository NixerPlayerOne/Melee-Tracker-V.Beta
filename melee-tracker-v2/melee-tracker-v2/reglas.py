#!/usr/bin/env python3
"""
reglas.py

Capa 2 adaptada a SQLite. Las 5 reglas (Regla 1..5) y las estadisticas
generales de abajo son COPIA VERBATIM de capa2_reglas.py -- no se toco
ni una formula. Lo unico que cambia es de donde salen los datos:

  ANTES: cargar_partidas() leia partidas_canonico.json (un JOIN manual
         de hasta 4 JSONs, regenerado entero por capa1_partidas.py cada
         vez que algo cambiaba)
  AHORA: cargar_partidas_propias() hace ese mismo JOIN con SQL directo
         sobre melee_tracker.db -- siempre al dia, sin paso intermedio

Los dicts que devuelve cargar_partidas_propias() tienen las MISMAS
claves que esperaban las reglas (l_cancel_rate, apm, digital_apm,
damage_done, etc.) para que el resto del archivo no tuviera que
cambiar una sola linea.

Sigue la misma regla de oro que el original: ningun valor ausente se
reemplaza por 0 o un promedio generico. Si un dato no esta, se
devuelve None y quien llama decide que mostrar.
"""

import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"

# ==========================================================================
# Umbrales (Regla 1 y Regla 2) -- sin cambios respecto al original
# ==========================================================================
MUESTRA_MINIMA_TENDENCIA = 3
MUESTRA_MINIMA_PESO = 20

ELO_UMBRAL_PAREJO = 100
ELO_UMBRAL_FAVORITO = 300
ELO_UMBRAL_EXTREMO = 500

PESO_5B = 0.7
PESO_5A = 0.3

METRICAS_EJECUCION = {
    "l_cancel_rate": 1,
    "apm": 1,
    "digital_apm": 1,
    "damage_done": 1,
    "damage_per_opening": 1,
    "openings_per_kill": -1,
    "wavedash": 1,
    "waveland": 1,
    "dash_dance": 1,
    "roll": -1,
}


# ==========================================================================
# NUEVO: carga de datos desde SQLite (reemplaza cargar_partidas() y
# cargar_elo_history() del original)
# ==========================================================================
def _ratio(numerador, denominador):
    """None si no se puede calcular -- nunca 0 por defecto."""
    if numerador is None or not denominador:
        return None
    return numerador / denominador


def cargar_partidas_propias(conn: sqlite3.Connection, my_code: str) -> dict:
    """
    Reemplaza a cargar_partidas(). Hace el JOIN que antes hacia
    capa1_partidas.py a mano (mecanico + resultado + rival, los 3 en un
    solo SELECT) y arma un dict {file_path: partida} con las mismas
    claves que ya esperaban las reglas de abajo.

    "origen" se recalcula al vuelo en vez de venir grabado: una fila en
    match_players siempre implica "mecanica" (si no se pudo parsear, no
    hay fila); "resultado" esta si is_winner no es NULL; "rival" esta
    si elo_at_match (de la fila del rival) no es NULL.
    """
    filas = conn.execute(
        """
        SELECT
            r.file_path, r.played_at, r.duration_frames,
            mio.is_winner, mio.stocks_lost AS my_stocks_lost,
            rival.stocks_lost AS opp_stocks_lost,
            mio.kills AS kill_count, rival.kills AS opp_kill_count,
            mio.damage_dealt AS damage_done, mio.damage_taken AS damage_received,
            mio.openings_created, mio.neutral_wins AS my_neutral,
            rival.neutral_wins AS opp_neutral,
            mio.counter_hits AS my_counter, rival.counter_hits AS opp_counter,
            mio.trades AS my_trades, rival.trades AS opp_trades,
            mio.trades_benefited,
            mio.l_cancel_success, mio.l_cancel_attempts,
            mio.wavedashes AS wavedash, mio.wavelands AS waveland,
            mio.dash_dances AS dash_dance, mio.rolls AS roll,
            mio.spot_dodges AS spot_dodge, mio.air_dodges AS air_dodge,
            mio.ledge_grabs AS ledgegrab,
            mio.inputs_total, mio.digital_inputs_total, mio.ipm,
            rival.elo_at_match AS elo_rival
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players rival ON rival.replay_id = r.id AND rival.connect_code != ?
        """,
        (my_code, my_code),
    ).fetchall()

    nombres = [
        "file_path", "played_at", "duration_frames", "is_winner", "my_stocks_lost",
        "opp_stocks_lost", "kill_count", "opp_kill_count", "damage_done", "damage_received",
        "openings_created", "my_neutral", "opp_neutral", "my_counter", "opp_counter",
        "my_trades", "opp_trades", "trades_benefited", "l_cancel_success", "l_cancel_attempts",
        "wavedash", "waveland", "dash_dance", "roll", "spot_dodge", "air_dodge", "ledgegrab",
        "inputs_total", "digital_inputs_total", "ipm", "elo_rival",
    ]

    partidas = {}
    for fila in filas:
        f = dict(zip(nombres, fila))

        game_minutes = (f["inputs_total"] / f["ipm"]) if f["ipm"] else None
        digital_apm = _ratio(f["digital_inputs_total"], game_minutes)

        origen_partes = ["mecanica"]
        resultado = None
        if f["is_winner"] is not None:
            resultado = "victoria" if f["is_winner"] else "derrota"
            origen_partes.append("resultado")
        if f["elo_rival"] is not None:
            origen_partes.append("rival")

        partida = {
            "archivo": f["file_path"],
            "fecha": f["played_at"],
            "duracion_seg": (f["duration_frames"] / 60.0) if f["duration_frames"] else None,
            "resultado": resultado,
            "elo_rival": f["elo_rival"],
            "my_stocks_lost": f["my_stocks_lost"],
            "opp_stocks_lost": f["opp_stocks_lost"],
            "kill_count": f["kill_count"],
            "opp_kill_count": f["opp_kill_count"],
            "damage_done": f["damage_done"],
            "damage_received": f["damage_received"],
            "openings_per_kill": _ratio(f["openings_created"], f["kill_count"]),
            "damage_per_opening": _ratio(f["damage_done"], f["openings_created"]),
            "my_neutral": f["my_neutral"], "opp_neutral": f["opp_neutral"],
            "my_counter": f["my_counter"], "opp_counter": f["opp_counter"],
            "my_trades": f["my_trades"], "opp_trades": f["opp_trades"],
            "beneficial_trade_ratio": _ratio(f["trades_benefited"], f["my_trades"]),
            "l_cancel_rate": _ratio(f["l_cancel_success"], f["l_cancel_attempts"]),
            "l_cancel_attempts": f["l_cancel_attempts"],
            "wavedash": f["wavedash"], "waveland": f["waveland"],
            "dash_dance": f["dash_dance"], "roll": f["roll"],
            "spot_dodge": f["spot_dodge"], "air_dodge": f["air_dodge"],
            "ledgegrab": f["ledgegrab"],
            "apm": f["ipm"], "digital_apm": digital_apm,
            "origen": "+".join(origen_partes),
        }
        partidas[f["file_path"]] = partida

    return partidas


def cargar_mi_elo_history(conn: sqlite3.Connection, my_code: str) -> list:
    """
    Reemplaza a cargar_elo_history(). Requiere que consultar_rangos.py
    (version SQLite, todavia no migrada) tambien registre TU PROPIO
    connect_code en elo_history -- el original solo consultaba rivales
    (rivales.json nunca incluia tu propio codigo), asi que esta funcion
    va a devolver [] hasta que eso se implemente. Es una laguna real que
    la Regla 2 (contexto_rival) necesita para funcionar -- ver nota en
    memoria del proyecto.
    """
    filas = conn.execute(
        "SELECT queried_at, elo FROM elo_history WHERE connect_code = ? ORDER BY queried_at",
        (my_code,),
    ).fetchall()
    return [(datetime.fromisoformat(qa), elo) for qa, elo in filas]


def mi_elo_en_fecha(elo_history: list, fecha_partida: datetime):
    """Igual al original: mi ELO mas reciente registrado antes o en el
    momento de una partida. None si la partida es anterior al primer
    registro (no se inventa un ELO retroactivo)."""
    elo_valido = None
    for fecha, elo in elo_history:
        if fecha <= fecha_partida:
            elo_valido = elo
        else:
            break
    return elo_valido


# ==========================================================================
# A partir de aca: TODO verbatim de capa2_reglas.py, sin cambios
# ==========================================================================
def tiene_muestra_minima(n: int, minimo: int = MUESTRA_MINIMA_TENDENCIA) -> bool:
    return n >= minimo


def filtrar_por_origen(partidas: dict, nivel_minimo: str) -> dict:
    requeridos = set(nivel_minimo.split("+"))
    return {
        ruta: p for ruta, p in partidas.items()
        if requeridos.issubset(set((p.get("origen") or "").split("+")))
    }


def contexto_rival(elo_mio: float, elo_rival: float) -> str:
    if elo_mio is None or elo_rival is None:
        return None
    diff = elo_rival - elo_mio
    if abs(diff) < ELO_UMBRAL_PAREJO:
        return "parejo"
    if abs(diff) >= ELO_UMBRAL_EXTREMO:
        return "diferencia_extrema_favor_rival" if diff > 0 else "diferencia_extrema_favor_mio"
    if abs(diff) >= ELO_UMBRAL_FAVORITO:
        return "diferencia_grande_favor_rival" if diff > 0 else "diferencia_grande_favor_mio"
    return "rival_favorito" if diff > 0 else "rival_underdog"


def contexto_rival_partida(partida: dict, elo_history: list) -> str:
    if partida.get("elo_rival") is None or partida.get("fecha") is None:
        return None
    fecha = datetime.fromisoformat(partida["fecha"])
    elo_mio = mi_elo_en_fecha(elo_history, fecha)
    return contexto_rival(elo_mio, partida["elo_rival"])


def autodestrucciones(partida: dict):
    my_lost = partida.get("my_stocks_lost")
    opp_lost = partida.get("opp_stocks_lost")
    kills_mios = partida.get("kill_count")
    kills_rival = partida.get("opp_kill_count")

    propias = (my_lost - kills_rival) if (my_lost is not None and kills_rival is not None) else None
    rival = (opp_lost - kills_mios) if (opp_lost is not None and kills_mios is not None) else None

    return {"propias": propias, "rival": rival}


def _stats_de_campo(partidas: list, campo: str):
    valores = [p[campo] for p in partidas if p.get(campo) is not None]
    if len(valores) < 2:
        return None, None, len(valores)
    return statistics.mean(valores), statistics.stdev(valores), len(valores)


def stats_historicas(partidas: dict) -> dict:
    base = list(filtrar_por_origen(partidas, "mecanica").values())
    return {campo: _stats_de_campo(base, campo) for campo in METRICAS_EJECUCION}


def _quincena_de_fecha(fecha: datetime):
    q = 1 if fecha.day <= 15 else 2
    return (fecha.year, fecha.month, q)


def _ultima_quincena_completa(fecha_referencia: date):
    if fecha_referencia.day <= 15:
        primer_dia_mes = fecha_referencia.replace(day=1)
        ultimo_dia_mes_anterior = primer_dia_mes - timedelta(days=1)
        return (ultimo_dia_mes_anterior.year, ultimo_dia_mes_anterior.month, 2)
    return (fecha_referencia.year, fecha_referencia.month, 1)


def stats_quincena_reciente(partidas: dict, fecha_referencia: date = None) -> dict:
    fecha_referencia = fecha_referencia or date.today()
    quincena_objetivo = _ultima_quincena_completa(fecha_referencia)

    base = list(filtrar_por_origen(partidas, "mecanica").values())
    en_quincena = [
        p for p in base
        if p.get("fecha") and _quincena_de_fecha(datetime.fromisoformat(p["fecha"])) == quincena_objetivo
    ]

    if not tiene_muestra_minima(len(en_quincena), MUESTRA_MINIMA_PESO):
        return {campo: (None, None, len(en_quincena)) for campo in METRICAS_EJECUCION}

    return {campo: _stats_de_campo(en_quincena, campo) for campo in METRICAS_EJECUCION}


def zscore_partida(partida: dict, stats_referencia: dict) -> float:
    zscores = []
    for campo, signo in METRICAS_EJECUCION.items():
        valor = partida.get(campo)
        media, desvio, n = stats_referencia.get(campo, (None, None, 0))
        if valor is None or media is None or desvio in (None, 0):
            continue
        z = signo * (valor - media) / desvio
        zscores.append(z)

    if not zscores:
        return None
    return statistics.mean(zscores)


def zscore_detalle(partida: dict, stats_referencia: dict) -> list:
    detalle = []
    for campo, signo in METRICAS_EJECUCION.items():
        valor = partida.get(campo)
        media, desvio, n = stats_referencia.get(campo, (None, None, 0))
        if valor is None or media is None or desvio in (None, 0):
            continue
        z = signo * (valor - media) / desvio
        detalle.append((campo, z))

    detalle.sort(key=lambda par: abs(par[1]), reverse=True)
    return detalle


def peso_5a(partida: dict):
    my_lost = partida.get("my_stocks_lost")
    opp_lost = partida.get("opp_stocks_lost")
    kills_mios = partida.get("kill_count")
    kills_rival = partida.get("opp_kill_count")

    if not my_lost or not opp_lost or kills_mios is None or kills_rival is None:
        return None
    if my_lost == 0 or opp_lost == 0:
        return None

    merito_propio = kills_mios / opp_lost
    merito_ajeno = kills_rival / my_lost
    return merito_propio - merito_ajeno


def peso_resultado(partida: dict, stats_referencia: dict):
    z = zscore_partida(partida, stats_referencia)
    if z is None:
        return None

    cinco_b = max(-1.0, min(1.0, z / 3.0))
    cinco_a = peso_5a(partida)

    if cinco_a is None:
        return cinco_b

    return PESO_5B * cinco_b + PESO_5A * cinco_a


def tiempo_total_seg(partidas: dict) -> float:
    return sum(p.get("duracion_seg") or 0 for p in partidas.values())


def duracion_promedio_seg(partidas: dict):
    duraciones = [p["duracion_seg"] for p in partidas.values() if p.get("duracion_seg")]
    return statistics.mean(duraciones) if duraciones else None


def partida_extrema(partidas: dict, mas_larga: bool):
    con_dur = [p for p in partidas.values() if p.get("duracion_seg")]
    if not con_dur:
        return None
    return max(con_dur, key=lambda p: p["duracion_seg"]) if mas_larga \
        else min(con_dur, key=lambda p: p["duracion_seg"])


def dias_jugados(partidas: dict) -> int:
    dias = {p["fecha"][:10] for p in partidas.values() if p.get("fecha")}
    return len(dias)


def racha_actual_dias(partidas: dict, fecha_referencia: date = None) -> int:
    fecha_referencia = fecha_referencia or date.today()
    dias = {datetime.fromisoformat(p["fecha"]).date()
            for p in partidas.values() if p.get("fecha")}
    if not dias:
        return 0

    cursor = fecha_referencia
    if cursor not in dias:
        cursor -= timedelta(days=1)
        if cursor not in dias:
            return 0

    racha = 0
    while cursor in dias:
        racha += 1
        cursor -= timedelta(days=1)
    return racha


def resumen_dia(partidas: dict, fecha_referencia: date = None) -> dict:
    fecha_referencia = fecha_referencia or date.today()
    fecha_str = fecha_referencia.isoformat()
    del_dia = [p for p in partidas.values()
               if (p.get("fecha") or "").startswith(fecha_str) and p.get("resultado") is not None]
    total = len(del_dia)
    victorias = sum(1 for p in del_dia if p["resultado"] == "victoria")
    return {
        "partidas": total,
        "victorias": victorias,
        "derrotas": total - victorias,
        "winrate_pct": round(100 * victorias / total, 1) if total else None,
    }


def resumen_ultima_sesion(partidas: dict, gap_minutos: int = 30):
    con_fecha = sorted(
        (datetime.fromisoformat(p["fecha"]), p) for p in partidas.values() if p.get("fecha")
    )
    if not con_fecha:
        return None

    sesiones = []
    sesion_actual = [con_fecha[0]]
    for i in range(1, len(con_fecha)):
        hueco_min = (con_fecha[i][0] - con_fecha[i - 1][0]).total_seconds() / 60
        if hueco_min > gap_minutos:
            sesiones.append(sesion_actual)
            sesion_actual = []
        sesion_actual.append(con_fecha[i])
    sesiones.append(sesion_actual)

    ultima = sesiones[-1]
    inicio, fin = ultima[0][0], ultima[-1][0]
    duracion_ultima_partida = ultima[-1][1].get("duracion_seg") or 0
    duracion_seg = (fin - inicio).total_seconds() + duracion_ultima_partida
    return {"fecha": inicio.date().isoformat(), "duracion_seg": duracion_seg, "partidas": len(ultima)}


if __name__ == "__main__":
    if not DB_PATH.exists():
        print("No existe melee_tracker.db.")
    else:
        from config import ensure_config
        cfg = ensure_config()
        conn = sqlite3.connect(DB_PATH)
        partidas = cargar_partidas_propias(conn, cfg["connect_code"])
        print(f"Partidas cargadas: {len(partidas)}")
        print(f"Racha actual: {racha_actual_dias(partidas)} dias")
        conn.close()
