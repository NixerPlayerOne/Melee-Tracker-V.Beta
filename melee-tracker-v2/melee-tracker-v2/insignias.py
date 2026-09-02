"""
insignias.py

Insignias de PERFIL (persistentes, describen un patron de muchas
partidas) e insignias POR PARTIDA (describen que paso en una partida
puntual, como las medallas de fin de partida en un shooter de equipo).

Disenadas con dos principios de gamificacion:
  - El test de "wow, como lo hiciste": si cualquiera la obtiene facil,
    no vale la pena mostrarla.
  - Niveles dentro de UNA insignia (Bronce/Plata/Oro/Platino) en vez
    de una insignia nueva por cada incremento chico.

Dos insignias que se discutieron -- "Kill relampago" (necesita el
percent exacto de cada kill, no solo el conteo) y "Remontada"
(necesita la trayectoria de stocks durante la partida, no solo el
valor final) -- quedan afuera: el parser hoy no extrae esos datos.
Agregarlas requiere extender process_replays.py y correr --forzar de
nuevo.

Cada insignia de perfil devuelve None si no aplica, o un dict
{"nombre", "tier", "color", "tooltip"}. Las de partida devuelven una
lista (puede haber varias en la misma partida).

No importa nada de app.py (evita import circular) -- solo de
formato.py y reglas.py.
"""

import sqlite3
from datetime import datetime

from formato import TIER_COLORES, char_nombre
from reglas import (
    stats_historicas, stats_quincena_reciente, METRICAS_EJECUCION,
    mi_elo_en_fecha, autodestrucciones,
)


def _tier_por_umbral(valor, umbrales):
    """umbrales: [(minimo, nombre_tier), ...] de mayor a menor. Tier mas alto que el valor alcanza, o None."""
    for minimo, nombre in umbrales:
        if valor >= minimo:
            return nombre
    return None


def racha_victorias_maxima(partidas: dict):
    """(mejor, fecha_inicio, fecha_fin) de la racha de victorias consecutivas mas larga."""
    con_fecha = sorted(
        (p["fecha"], p.get("resultado")) for p in partidas.values() if p.get("fecha")
    )
    mejor = actual = 0
    mejor_inicio = mejor_fin = None
    actual_inicio = None
    for fecha, resultado in con_fecha:
        if resultado == "victoria":
            if actual == 0:
                actual_inicio = fecha
            actual += 1
            if actual > mejor:
                mejor = actual
                mejor_inicio = actual_inicio
                mejor_fin = fecha
        elif resultado == "derrota":
            actual = 0
        # resultado None (indeterminado): no corta la racha ni suma, se ignora
    return mejor, mejor_inicio, mejor_fin


def _fecha_umbral_acumulado(partidas: dict, umbral_seg: float):
    """Fecha (ISO) en que la suma acumulada de duracion_seg cruzo el umbral, ordenando por fecha."""
    con_fecha = sorted(
        (p["fecha"], p.get("duracion_seg") or 0) for p in partidas.values() if p.get("fecha")
    )
    acumulado = 0.0
    for fecha, dur in con_fecha:
        acumulado += dur
        if acumulado >= umbral_seg:
            return fecha
    return None


def _fmt_fecha(fecha_iso: str) -> str:
    if not fecha_iso:
        return "fecha desconocida"
    return datetime.fromisoformat(fecha_iso).strftime("%d/%m/%Y")


# ==========================================================================
# Insignias de PERFIL
# ==========================================================================
def insignia_dedicacion(partidas: dict, tiempo_total_seg: float):
    horas = (tiempo_total_seg or 0) / 3600
    umbrales_horas = [(400, "Platino"), (150, "Oro"), (50, "Plata"), (10, "Bronce")]
    tier = _tier_por_umbral(horas, umbrales_horas)
    if not tier:
        return None

    umbral_horas_tier = next(h for h, t in umbrales_horas if t == tier)
    fecha_alcanzado = _fecha_umbral_acumulado(partidas, umbral_horas_tier * 3600)

    return {
        "nombre": "Dedicación", "tier": tier, "color": TIER_COLORES[tier],
        "tooltip": (
            f"{horas:.0f} horas jugadas registradas en total. "
            f"Alcanzaste este nivel el {_fmt_fecha(fecha_alcanzado)}."
        ),
    }


def insignia_identidad(conn: sqlite3.Connection, my_code: str):
    fila = conn.execute(
        "SELECT character_id, COUNT(*) c FROM match_players "
        "WHERE connect_code = ? AND character_id IS NOT NULL "
        "GROUP BY character_id ORDER BY c DESC LIMIT 1",
        (my_code,),
    ).fetchone()
    if not fila:
        return None
    character_id, partidas_con_char = fila

    total = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE connect_code = ?", (my_code,)
    ).fetchone()[0]
    if not total:
        return None

    concentracion = partidas_con_char / total
    if concentracion < 0.9:
        return None

    tier = _tier_por_umbral(partidas_con_char, [(2000, "Platino"), (1000, "Oro"), (500, "Plata"), (100, "Bronce")])
    if not tier:
        return None

    primera_fecha = conn.execute(
        "SELECT MIN(r.played_at) FROM match_players mp JOIN replays r ON r.id = mp.replay_id "
        "WHERE mp.connect_code = ? AND mp.character_id = ?",
        (my_code, character_id),
    ).fetchone()[0]

    return {
        "nombre": "Especialista", "tier": tier, "color": TIER_COLORES[tier],
        "tooltip": (
            f"Jugaste {char_nombre(character_id)} en {partidas_con_char} de tus {total} partidas "
            f"({concentracion*100:.0f}%), desde el {_fmt_fecha(primera_fecha)}."
        ),
    }


def insignia_progreso(partidas: dict):
    hist = stats_historicas(partidas)
    quincena = stats_quincena_reciente(partidas)

    mejor_campo = None
    mejor_z = 0.0
    for campo, signo in METRICAS_EJECUCION.items():
        m_h, s_h, n_h = hist.get(campo, (None, None, 0))
        m_q, s_q, n_q = quincena.get(campo, (None, None, 0))
        if m_h is None or s_h in (None, 0) or m_q is None or n_q < 20:
            continue
        z = signo * (m_q - m_h) / s_h
        if z > mejor_z:
            mejor_z = z
            mejor_campo = campo

    if mejor_campo is None:
        return None
    tier = _tier_por_umbral(mejor_z, [(1.5, "Oro"), (1.0, "Plata"), (0.5, "Bronce")])
    if not tier:
        return None

    from formato import METRICA_LABELS, fmt_metrica
    from reglas import _ultima_quincena_completa
    from datetime import date

    m_h = hist[mejor_campo][0]
    m_q = quincena[mejor_campo][0]
    anio, mes, q = _ultima_quincena_completa(date.today())
    rango = f"1-15/{mes:02d}/{anio}" if q == 1 else f"16-fin/{mes:02d}/{anio}"

    return {
        "nombre": "Progreso real", "tier": tier, "color": TIER_COLORES[tier],
        "tooltip": (
            f"{METRICA_LABELS.get(mejor_campo, mejor_campo)} pasó de "
            f"{fmt_metrica(mejor_campo, m_h, es_promedio=True)} histórico a "
            f"{fmt_metrica(mejor_campo, m_q, es_promedio=True)} en tu quincena del {rango}."
        ),
    }


def insignia_racha(partidas: dict):
    mejor, fecha_inicio, fecha_fin = racha_victorias_maxima(partidas)
    tier = _tier_por_umbral(mejor, [(20, "Platino"), (15, "Oro"), (10, "Plata"), (5, "Bronce")])
    if not tier:
        return None
    return {
        "nombre": "Racha de fuego", "tier": tier, "color": TIER_COLORES[tier],
        "tooltip": (
            f"Tu racha de victorias consecutivas más larga: {mejor}, "
            f"del {_fmt_fecha(fecha_inicio)} al {_fmt_fecha(fecha_fin)}."
        ),
    }


def insignia_caza_gigantes_perfil(partidas: dict, mi_elo_history: list):
    mejor_delta = None
    mejor_rival_elo = None
    mejor_fecha = None
    for p in partidas.values():
        if p.get("resultado") != "victoria" or p.get("elo_rival") is None or not p.get("fecha"):
            continue
        fecha = datetime.fromisoformat(p["fecha"])
        mi_elo = mi_elo_en_fecha(mi_elo_history, fecha)
        if mi_elo is None:
            continue
        delta = p["elo_rival"] - mi_elo
        if mejor_delta is None or delta > mejor_delta:
            mejor_delta = delta
            mejor_rival_elo = p["elo_rival"]
            mejor_fecha = p["fecha"]

    if mejor_delta is None:
        return None
    tier = _tier_por_umbral(mejor_delta, [(700, "Platino"), (500, "Oro"), (300, "Plata"), (100, "Bronce")])
    if not tier:
        return None
    return {
        "nombre": "Caza-gigantes", "tier": tier, "color": TIER_COLORES[tier],
        "tooltip": (
            f"Venciste a un rival con {mejor_delta:.0f} de elo más que el tuyo en ese momento "
            f"(rival a {mejor_rival_elo:.0f}), el {_fmt_fecha(mejor_fecha)}."
        ),
    }


def insignias_de_perfil(conn: sqlite3.Connection, my_code: str, partidas: dict,
                         mi_elo_history: list, tiempo_total_seg: float) -> list:
    candidatas = [
        insignia_dedicacion(partidas, tiempo_total_seg),
        insignia_identidad(conn, my_code),
        insignia_progreso(partidas),
        insignia_racha(partidas),
        insignia_caza_gigantes_perfil(partidas, mi_elo_history),
    ]
    return [c for c in candidatas if c is not None]


# ==========================================================================
# Insignias POR PARTIDA
# ==========================================================================
def insignias_de_partida(partida: dict, contexto: str) -> list:
    resultado = []
    auto = autodestrucciones(partida)
    gano = partida.get("resultado") == "victoria"
    perdio = partida.get("resultado") == "derrota"
    stocks_perdidos = partida.get("my_stocks_lost")

    if gano and stocks_perdidos == 0:
        resultado.append({
            "nombre": "Victoria perfecta", "color": TIER_COLORES["Oro"],
            "tooltip": "Ganaste sin perder un solo stock.",
        })

    if gano and contexto in ("diferencia_grande_favor_rival", "diferencia_extrema_favor_rival"):
        grado = "muchísimo" if contexto == "diferencia_extrema_favor_rival" else "bastante"
        resultado.append({
            "nombre": "Caza-gigantes", "color": TIER_COLORES["Plata"],
            "tooltip": f"Le ganaste a un rival {grado} mejor rankeado que vos.",
        })

    if gano and auto.get("propias") == 0 and stocks_perdidos and stocks_perdidos > 0:
        resultado.append({
            "nombre": "Disciplina", "color": TIER_COLORES["Bronce"],
            "tooltip": "Ganaste, perdiendo stocks en el camino, sin autodestruirte ni una vez.",
        })

    if perdio and (auto.get("propias") or 0) >= 2 and (auto.get("rival") or 0) == 0:
        resultado.append({
            "nombre": "Te ganó tu propio error", "color": "#8b93a7",
            "tooltip": f"Perdiste con {auto['propias']} autodestrucciones propias y ninguna del rival — no fue el rival, fuiste vos.",
        })

    return resultado
