"""
app.py

Primera pestana de la UI nueva: GENERAL. Usa pywebview para mostrar
HTML/CSS dentro de una ventana de escritorio propia (sin barra de
navegador) -- reemplaza la construccion de widgets de Tkinter por
HTML+CSS. Esta pestana no necesita graficos pesados (son barras
simples en CSS); Chart.js se suma mas adelante para las pestanas que
sí lo necesiten (HISTORIAL, etc.).

Toda la logica de calculo sale de reglas.py sin tocar una linea -- acá
solo se consultan datos y se arma el HTML.

Uso:
    python app.py
"""

import sqlite3
import sys
import base64
from datetime import datetime
from pathlib import Path

import webview

import process_replays
import consultar_rangos
import build_sets
import backfill_elo
from config import ensure_config
from reglas import (
    cargar_partidas_propias, cargar_mi_elo_history, mi_elo_en_fecha,
    contexto_rival, stats_historicas, zscore_detalle,
    autodestrucciones, resumen_dia, resumen_ultima_sesion,
    tiempo_total_seg, duracion_promedio_seg, partida_extrema,
    racha_actual_dias, dias_jugados,
    filtrar_por_origen, stats_quincena_reciente, peso_resultado,
    METRICAS_EJECUCION, MUESTRA_MINIMA_PESO,
)

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"

# Misma paleta que ya tenia el tracker viejo
from formato import (
    BG, SURFACE, SURFACE2, BORDER, TEXT, MUTED, ACCENT, ACCENT2, GREEN, RED,
    TRAMO_COLORES, TIER_COLORES, CONTEXTO_LABELS, METRICA_LABELS, METRICA_FORMATO,
    nombre_legible, char_nombre, stage_nombre, attack_nombre, fmt_horas, fmt_seg, fmt_metrica, fmt_dias_desde,
)
import insignias

# Carpeta de assets con los stock icons de personajes -- mismo esquema de
# nombres que ya generaba download_stock_icons() en el tracker viejo
# (ej. "FoxHeadSSBM.png", "DonkeyKongHeadSSBM.png"). Si el archivo no
# existe para un personaje puntual, no rompe nada: el que llama decide
# el fallback (medalla, texto solo, etc.) -- ver _char_icon_html().
CHARS_DIR = Path(__file__).resolve().parent / "assets" / "chars"

# pywebview carga el HTML de app.py como STRING (webview.create_window(...,
# html=...)), no como archivo -- en ese modo, el motor de navegador (WebView2
# en Windows) bloquea cualquier file:// que la pagina intente pedir despues
# ("Not allowed to load local resource", limitacion conocida y documentada
# del propio pywebview). Por eso los iconos van incrustados en base64 adentro
# del propio HTML en vez de referenciados por ruta -- no es un recurso
# externo, viaja adentro del documento.
_char_icon_cache: dict = {}


def _char_icon_slug(nombre: str) -> str:
    return nombre.replace(" ", "").replace(".", "").replace("&", "") + "HeadSSBM.png"


def _char_icon_data_uri(slug: str):
    """Lee y cachea en memoria el base64 de un icono -- se llama muchas
    veces por refresh (el mismo personaje aparece en varias tarjetas/
    pestañas), asi que evita releer y re-codificar el archivo cada vez.
    None si el archivo no existe o no se pudo leer."""
    if slug in _char_icon_cache:
        return _char_icon_cache[slug]

    ruta = CHARS_DIR / slug
    if not ruta.exists():
        _char_icon_cache[slug] = None
        return None

    try:
        datos_b64 = base64.b64encode(ruta.read_bytes()).decode("ascii")
        ext = ruta.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg"}.get(ext, ext or "png")
        _char_icon_cache[slug] = f"data:image/{mime};base64,{datos_b64}"
    except OSError:
        _char_icon_cache[slug] = None

    return _char_icon_cache[slug]


def _char_icon_html(character_id, size=32, css_class="char-icon") -> str:
    """<img> con el stock icon del personaje si el archivo existe en
    assets/chars/ -- cadena vacia si no (para que el que llama pueda
    hacer `_char_icon_html(...) or '<algun fallback>'`)."""
    if character_id is None:
        return ""
    nombre = char_nombre(character_id)
    data_uri = _char_icon_data_uri(_char_icon_slug(nombre))
    if not data_uri:
        return ""
    return f'<img src="{data_uri}" width="{size}" height="{size}" class="{css_class}" alt="{nombre}">'


# Escala S..E para el "grade" de una partida segun diferencial de stocks
# (mios - rival) -- independiente de si ganaste o perdiste, mide que tan
# contundente fue el resultado. Usada en la pestaña PROGRESO.
GRADE_ORDEN = ["S", "A", "B", "C", "D", "E"]
GRADE_COLORES = {"S": "#fbbf24", "A": GREEN, "B": ACCENT2, "C": MUTED, "D": "#f97316", "E": RED}
GRADE_LABELS = {
    "S": "Dominación total", "A": "Victoria cómoda", "B": "Victoria ajustada",
    "C": "Partida pareja", "D": "Derrota ajustada", "E": "Dominado",
}


def _stock_grade(diff):
    if diff is None:
        return None
    if diff >= 3:
        return "S"
    if diff == 2:
        return "A"
    if diff == 1:
        return "B"
    if diff == 0:
        return "C"
    if diff == -1:
        return "D"
    return "E"


def matchup_winrate(conn: sqlite3.Connection, my_code: str, my_char, rival_char, min_partidas=3):
    if my_char is None or rival_char is None:
        return None
    fila = conn.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN mio.is_winner = 1 THEN 1 ELSE 0 END)
        FROM match_players mio
        JOIN match_players riv ON riv.replay_id = mio.replay_id AND riv.connect_code != mio.connect_code
        WHERE mio.connect_code = ? AND mio.character_id = ? AND riv.character_id = ?
          AND mio.is_winner IS NOT NULL
        """,
        (my_code, my_char, rival_char),
    ).fetchone()
    total, victorias = fila
    if not total or total < min_partidas:
        return None
    return {"total": total, "victorias": victorias, "wr": round(100 * victorias / total, 1)}


def cargar_ultima_partida(conn: sqlite3.Connection, my_code: str, partidas: dict, mi_elo_history: list):
    con_fecha = [(k, p) for k, p in partidas.items() if p.get("fecha")]
    if not con_fecha:
        return None
    archivo, partida = max(con_fecha, key=lambda kv: kv[1]["fecha"])

    fila = conn.execute(
        """
        SELECT r.stage_id, r.end_method, r.lras_port,
               mio.character_id, mio.port,
               riv.connect_code, riv.character_id, riv.elo_at_match_exacto
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        WHERE r.file_path = ?
        """,
        (my_code, my_code, archivo),
    ).fetchone()
    if fila is None:
        return None
    stage_id, end_method, lras_port, my_char, my_port, rival_code, rival_char, elo_exacto = fila

    fecha = datetime.fromisoformat(partida["fecha"])
    elo_mio = mi_elo_en_fecha(mi_elo_history, fecha)
    ctx = contexto_rival(elo_mio, partida.get("elo_rival"))

    hist = stats_historicas(partidas)
    zscore_det = zscore_detalle(partida, hist)
    auto = autodestrucciones(partida)

    detalle_metricas = []
    for campo, z in zscore_det[:3]:
        media, _, n_muestra = hist.get(campo, (None, None, 0))
        detalle_metricas.append({
            "campo": campo,
            "valor": partida.get(campo),
            "promedio": media,
            "n_muestra": n_muestra,
            "mejor_que_usual": z >= 0,
        })

    se_rindio = None
    if lras_port is not None:
        se_rindio = "vos" if lras_port == my_port else "el rival"

    return {
        "resultado": partida.get("resultado"),
        "rival_code": rival_code,
        "my_char": char_nombre(my_char),
        "rival_char": char_nombre(rival_char),
        "my_char_id": my_char,
        "rival_char_id": rival_char,
        "stage": stage_nombre(stage_id),
        "fecha": fecha,
        "contexto": ctx,
        "elo_mio": elo_mio,
        "elo_rival": partida.get("elo_rival"),
        "contexto_aproximado": (elo_exacto == 0) if partida.get("elo_rival") is not None else None,
        "detalle_metricas": detalle_metricas,
        "autodestrucciones": auto,
        "se_rindio": se_rindio,
        "matchup": matchup_winrate(conn, my_code, my_char, rival_char),
        "insignias": insignias.insignias_de_partida(partida, ctx),
    }


def cargar_ranked(conn: sqlite3.Connection, my_code: str):
    fila = conn.execute(
        """
        SELECT p.elo, p.rank_tier, rt.tramo_grande, p.rank_updated_at, p.ranked_wins, p.ranked_losses
        FROM players p LEFT JOIN rank_tiers rt ON rt.tramo = p.rank_tier
        WHERE p.connect_code = ?
        """,
        (my_code,),
    ).fetchone()
    if not fila:
        elo, rank_tier, tramo_grande, rank_updated_at, wins, losses = (None,) * 6
    else:
        elo, rank_tier, tramo_grande, rank_updated_at, wins, losses = fila
    peak = conn.execute(
        "SELECT MAX(elo) FROM elo_history WHERE connect_code = ?", (my_code,)
    ).fetchone()[0]
    return {
        "elo": elo, "rank_tier": rank_tier, "tramo_grande": tramo_grande,
        "rank_updated_at": rank_updated_at, "peak": peak, "wins": wins, "losses": losses,
    }


def cargar_personaje_principal(conn: sqlite3.Connection, my_code: str):
    """El personaje que mas jugaste (por cantidad de partidas, sin
    importar resultado) -- para el icono de "perfil" en GENERAL, estilo
    luckystats. None si todavia no hay ninguna partida con personaje
    identificado."""
    fila = conn.execute(
        """
        SELECT character_id FROM match_players
        WHERE connect_code = ? AND character_id IS NOT NULL
        GROUP BY character_id
        ORDER BY COUNT(*) DESC
        LIMIT 1
        """,
        (my_code,),
    ).fetchone()
    return fila[0] if fila else None


def cargar_stats_char_propios(conn: sqlite3.Connection, my_code: str) -> list:
    """
    Victorias/derrotas agrupadas por el personaje que jugaste VOS. Una
    sola consulta sirve tanto para "con quien mas ganas" (ordenar por
    victorias) como para "con quien mas perdes" (ordenar por derrotas)
    -- quien llama decide el orden, no hace falta traer los datos dos
    veces.
    """
    filas = conn.execute(
        """
        SELECT mio.character_id,
               SUM(CASE WHEN mio.is_winner = 1 THEN 1 ELSE 0 END) AS victorias,
               SUM(CASE WHEN mio.is_winner = 0 THEN 1 ELSE 0 END) AS derrotas
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        WHERE mio.character_id IS NOT NULL
        GROUP BY mio.character_id
        """,
        (my_code,),
    ).fetchall()
    return [
        {"character_id": cid, "nombre": char_nombre(cid), "victorias": v, "derrotas": d}
        for cid, v, d in filas
    ]


def cargar_stats_char_rivales(conn: sqlite3.Connection, my_code: str) -> list:
    """
    Igual que arriba pero agrupado por el personaje del RIVAL --
    "rivales que mas dominas" (victorias) y "rivales que mas te ganan"
    (derrotas), por personaje del rival, no por connect_code puntual
    (eso ya esta en la pestaña RIVALES).
    """
    filas = conn.execute(
        """
        SELECT riv.character_id,
               SUM(CASE WHEN mio.is_winner = 1 THEN 1 ELSE 0 END) AS victorias,
               SUM(CASE WHEN mio.is_winner = 0 THEN 1 ELSE 0 END) AS derrotas
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        WHERE riv.character_id IS NOT NULL
        GROUP BY riv.character_id
        """,
        (my_code, my_code),
    ).fetchall()
    return [
        {"character_id": cid, "nombre": char_nombre(cid), "victorias": v, "derrotas": d}
        for cid, v, d in filas
    ]


def cargar_stats_stage(conn: sqlite3.Connection, my_code: str) -> list:
    """Victorias/derrotas por stage -- de aca salen "stage favorito" (mas
    victorias) y "stage dificil" (mas derrotas), mirados por separado:
    un stage puede ser tu favorito aunque tambien hayas perdido ahi
    alguna vez, igual que con los personajes de arriba."""
    filas = conn.execute(
        """
        SELECT r.stage_id,
               SUM(CASE WHEN mio.is_winner = 1 THEN 1 ELSE 0 END) AS victorias,
               SUM(CASE WHEN mio.is_winner = 0 THEN 1 ELSE 0 END) AS derrotas
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        WHERE r.stage_id IS NOT NULL
        GROUP BY r.stage_id
        """,
        (my_code,),
    ).fetchall()
    return [
        {"stage_id": sid, "nombre": stage_nombre(sid), "victorias": v, "derrotas": d}
        for sid, v, d in filas
    ]


def cargar_stats_stocks(conn: sqlite3.Connection, my_code: str) -> dict:
    """
    Analisis de stocks: cuantos stocks quedan al ganar/perder, el
    diferencial promedio, y el grade (S..E) de cada partida segun ese
    diferencial. Mas honesto que el win rate solo -- una victoria 4-3
    y una 4-0 cuentan lo mismo como "victoria" pero acá se distinguen.
    """
    filas = conn.execute(
        """
        SELECT mio.stocks_remaining, riv.stocks_remaining, mio.is_winner
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        WHERE mio.stocks_remaining IS NOT NULL AND riv.stocks_remaining IS NOT NULL
        """,
        (my_code, my_code),
    ).fetchall()

    distribucion = {g: 0 for g in GRADE_ORDEN}
    if not filas:
        return {
            "valid_games": 0, "avg_my_win": None, "avg_opp_win": None,
            "avg_my_loss": None, "avg_opp_loss": None, "avg_diff": None,
            "overall_grade": None, "distribucion": distribucion,
        }

    wins = [(m, o) for m, o, w in filas if w == 1]
    losses = [(m, o) for m, o, w in filas if w == 0]

    def _prom(pares, idx):
        vals = [p[idx] for p in pares]
        return round(sum(vals) / len(vals), 2) if vals else None

    diffs = [m - o for m, o, _w in filas]
    avg_diff = round(sum(diffs) / len(diffs), 2)

    for d in diffs:
        g = _stock_grade(d)
        if g:
            distribucion[g] += 1

    return {
        "valid_games": len(filas),
        "avg_my_win": _prom(wins, 0), "avg_opp_win": _prom(wins, 1),
        "avg_my_loss": _prom(losses, 0), "avg_opp_loss": _prom(losses, 1),
        "avg_diff": avg_diff,
        "overall_grade": _stock_grade(round(avg_diff)),
        "distribucion": distribucion,
    }


def cargar_dominio_por_personaje(conn: sqlite3.Connection, my_code: str, min_partidas: int = 3) -> list:
    """Diferencial de stocks promedio contra cada personaje rival, de mas
    dominado (el rival te aplasta) a mas dominante (vos aplastas). Filtra
    personajes con menos de `min_partidas` para no mostrar un -3.00
    sacado de una sola partida."""
    filas = conn.execute(
        """
        SELECT riv.character_id, mio.stocks_remaining, riv.stocks_remaining
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        WHERE riv.character_id IS NOT NULL
          AND mio.stocks_remaining IS NOT NULL AND riv.stocks_remaining IS NOT NULL
        """,
        (my_code, my_code),
    ).fetchall()

    por_char = {}
    for cid, mios, rivs in filas:
        por_char.setdefault(cid, []).append(mios - rivs)

    resultado = []
    for cid, diffs in por_char.items():
        if len(diffs) < min_partidas:
            continue
        n = len(diffs)
        avg = round(sum(diffs) / n, 2)
        dominado = sum(1 for d in diffs if d <= -2)
        renido = sum(1 for d in diffs if abs(d) <= 1)
        resultado.append({
            "character_id": cid, "nombre": char_nombre(cid), "partidas": n,
            "avg_diff": avg, "dominado_pct": round(100 * dominado / n),
            "renido_pct": round(100 * renido / n), "grade": _stock_grade(round(avg)),
        })

    resultado.sort(key=lambda x: x["avg_diff"])
    return resultado


def cargar_datos_progreso(conn: sqlite3.Connection, my_code: str, mi_elo_history: list) -> dict:
    """Datos de la pestaña PROGRESO. `mi_elo_history` ya viene cargado por
    cargar_datos() (misma lista que usa Regla 2) -- no se vuelve a
    consultar elo_history dos veces."""
    return {
        "historial_elo": mi_elo_history[-30:],
        "stocks": cargar_stats_stocks(conn, my_code),
        "dominio_personajes": cargar_dominio_por_personaje(conn, my_code),
    }


DIAS_NOMBRES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def _racha_maxima_dias(partidas: dict) -> int:
    """Récord histórico de racha de días jugados consecutivos -- distinto
    de racha_actual_dias() (reglas.py), que solo mira la racha vigente
    hoy."""
    dias = sorted({datetime.fromisoformat(p["fecha"]).date()
                   for p in partidas.values() if p.get("fecha")})
    if not dias:
        return 0
    mejor = actual = 1
    for i in range(1, len(dias)):
        if (dias[i] - dias[i - 1]).days == 1:
            actual += 1
            mejor = max(mejor, actual)
        else:
            actual = 1
    return mejor


def _dia_con_mas_partidas(partidas: dict):
    conteo = {}
    for p in partidas.values():
        if not p.get("fecha"):
            continue
        dia = p["fecha"][:10]
        conteo[dia] = conteo.get(dia, 0) + 1
    if not conteo:
        return None
    fecha, cantidad = max(conteo.items(), key=lambda kv: kv[1])
    return {"fecha": fecha, "cantidad": cantidad}


def _dia_con_mas_tiempo(partidas: dict):
    duraciones = {}
    for p in partidas.values():
        if not p.get("fecha"):
            continue
        dia = p["fecha"][:10]
        duraciones[dia] = duraciones.get(dia, 0) + (p.get("duracion_seg") or 0)
    if not duraciones:
        return None
    fecha, segundos = max(duraciones.items(), key=lambda kv: kv[1])
    return {"fecha": fecha, "segundos": segundos}


def _rachas_resultado(partidas: dict) -> dict:
    """Rachas de victorias/derrotas consecutivas: actual (mirando desde
    la partida mas reciente hacia atras) y record historico. Partidas
    sin resultado determinado no cortan una racha en construccion para
    el record (se ignoran, igual que insignias.racha_victorias_maxima),
    pero SI cortan la racha actual si aparecen antes de llegar al final
    de la lista -- mismo criterio que ya tenia el tracker viejo."""
    con_fecha = sorted(
        (p["fecha"], p.get("resultado")) for p in partidas.values() if p.get("fecha")
    )

    win_best = loss_best = 0
    w = l = 0
    for _fecha, resultado in con_fecha:
        if resultado == "victoria":
            w += 1
            l = 0
            win_best = max(win_best, w)
        elif resultado == "derrota":
            l += 1
            w = 0
            loss_best = max(loss_best, l)

    w = l = 0
    for _fecha, resultado in reversed(con_fecha):
        if resultado == "victoria":
            if l > 0:
                break
            w += 1
        elif resultado == "derrota":
            if w > 0:
                break
            l += 1
        else:
            break

    return {"win_cur": w, "win_best": win_best, "loss_cur": l, "loss_best": loss_best}


def cargar_matchups_dificiles(conn: sqlite3.Connection, my_code: str, top_n: int = 3) -> list:
    """Los personajes rivales contra los que peor te va (mas derrotas),
    con winrate historico y de las ultimas 10 partidas contra ese
    personaje puntual -- para ver si el matchup esta mejorando o
    empeorando, no solo el numero acumulado."""
    filas = conn.execute(
        """
        SELECT riv.character_id, mio.is_winner
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        WHERE riv.character_id IS NOT NULL AND mio.is_winner IS NOT NULL AND r.played_at IS NOT NULL
        ORDER BY r.played_at
        """,
        (my_code, my_code),
    ).fetchall()

    por_char = {}
    for cid, is_winner in filas:
        por_char.setdefault(cid, []).append(bool(is_winner))

    candidatos = sorted(
        por_char.items(),
        key=lambda kv: sum(1 for r in kv[1] if not r),
        reverse=True,
    )[:top_n]

    salida = []
    for cid, resultados in candidatos:
        total = len(resultados)
        wr_total = round(100 * sum(resultados) / total, 1)
        ultimas10 = resultados[-10:]
        wr_last10 = round(100 * sum(ultimas10) / len(ultimas10), 1) if ultimas10 else 0
        salida.append({
            "character_id": cid, "nombre": char_nombre(cid), "total": total,
            "wr_total": wr_total, "wr_last10": wr_last10,
            "trend": round(wr_last10 - wr_total, 1),
        })
    return salida


def _winrate_por_turno(partidas: dict) -> dict:
    """Mañana 03-12h / Tarde 12-18h / Noche 18-03h -- mismos cortes que
    ya usaba el tracker viejo."""
    turnos = {"mañana": [0, 0], "tarde": [0, 0], "noche": [0, 0]}
    for p in partidas.values():
        if not p.get("fecha") or p.get("resultado") not in ("victoria", "derrota"):
            continue
        hora = datetime.fromisoformat(p["fecha"]).hour
        if 3 <= hora < 12:
            turno = "mañana"
        elif 12 <= hora < 18:
            turno = "tarde"
        else:
            turno = "noche"
        turnos[turno][0 if p["resultado"] == "victoria" else 1] += 1

    resultado = {}
    for turno, (w, l) in turnos.items():
        total = w + l
        resultado[turno] = {"wins": w, "losses": l, "wr": round(100 * w / total, 1) if total else None}
    return resultado


def _winrate_por_dia_semana(partidas: dict) -> list:
    datos = {i: [0, 0] for i in range(7)}
    for p in partidas.values():
        if not p.get("fecha") or p.get("resultado") not in ("victoria", "derrota"):
            continue
        wd = datetime.fromisoformat(p["fecha"]).weekday()
        datos[wd][0 if p["resultado"] == "victoria" else 1] += 1

    salida = []
    for wd in range(7):
        w, l = datos[wd]
        total = w + l
        salida.append({
            "dia": DIAS_NOMBRES[wd], "wins": w, "losses": l, "total": total,
            "wr": round(100 * w / total, 1) if total else None,
        })
    return salida


def _winrate_por_mes(partidas: dict) -> list:
    datos = {}
    for p in partidas.values():
        if not p.get("fecha") or p.get("resultado") not in ("victoria", "derrota"):
            continue
        mes = p["fecha"][:7]
        w, l = datos.get(mes, (0, 0))
        datos[mes] = (w + 1, l) if p["resultado"] == "victoria" else (w, l + 1)

    salida = []
    for mes in sorted(datos.keys()):
        w, l = datos[mes]
        total = w + l
        salida.append({"mes": mes, "wins": w, "losses": l, "wr": round(100 * w / total, 1) if total else 0})
    return salida


def _delta_primera_segunda_mitad(partidas: dict, gap_minutos: int = 30):
    """Compara el winrate de la primera vs la segunda mitad de cada
    sesion (sesiones de 4+ partidas, mismo criterio de gap que
    resumen_ultima_sesion en reglas.py, pero aplicado a TODAS las
    sesiones, no solo la ultima) -- promedio del delta entre sesiones."""
    con_fecha = sorted(
        (datetime.fromisoformat(p["fecha"]), p) for p in partidas.values()
        if p.get("fecha") and p.get("resultado") in ("victoria", "derrota")
    )
    if not con_fecha:
        return None

    sesiones = []
    actual = [con_fecha[0]]
    for i in range(1, len(con_fecha)):
        gap = (con_fecha[i][0] - con_fecha[i - 1][0]).total_seconds() / 60
        if gap > gap_minutos:
            sesiones.append(actual)
            actual = []
        actual.append(con_fecha[i])
    sesiones.append(actual)

    def _wr(grupo):
        if not grupo:
            return 0
        w = sum(1 for _fecha, p in grupo if p["resultado"] == "victoria")
        return 100 * w / len(grupo)

    deltas = []
    for ses in sesiones:
        if len(ses) < 4:
            continue
        mitad = len(ses) // 2
        deltas.append(_wr(ses[mitad:]) - _wr(ses[:mitad]))

    return round(sum(deltas) / len(deltas), 1) if deltas else None


def cargar_datos_historial(conn: sqlite3.Connection, my_code: str, partidas: dict) -> dict:
    rachas = _rachas_resultado(partidas)
    por_dia = _winrate_por_dia_semana(partidas)
    con_datos = [d for d in por_dia if d["total"] > 0]

    return {
        "racha_actual_dias": racha_actual_dias(partidas),
        "racha_record_dias": _racha_maxima_dias(partidas),
        "dias_jugados": dias_jugados(partidas),
        "dia_mas_partidas": _dia_con_mas_partidas(partidas),
        "dia_mas_tiempo": _dia_con_mas_tiempo(partidas),
        "win_streak_cur": rachas["win_cur"], "win_streak_best": rachas["win_best"],
        "loss_streak_cur": rachas["loss_cur"], "loss_streak_best": rachas["loss_best"],
        "matchups_dificiles": cargar_matchups_dificiles(conn, my_code),
        "winrate_turno": _winrate_por_turno(partidas),
        "winrate_dia_semana": por_dia,
        "mejor_dia": max(con_datos, key=lambda d: d["wr"], default=None),
        "peor_dia": min(con_datos, key=lambda d: d["wr"], default=None),
        "winrate_mes": _winrate_por_mes(partidas),
        "delta_mitad_sesion": _delta_primera_segunda_mitad(partidas),
    }


def _etiqueta_peso(peso):
    """Traduce un valor de Regla 5 (peso_resultado, -1 a 1) a una
    etiqueta corta + color -- mismo criterio que ya tenia el tracker
    viejo, una sola fuente para no repetir la escala en otro lado."""
    if peso is None:
        return "sin datos suficientes", MUTED
    if peso >= 0.3:
        return "muy merecido", GREEN
    if peso >= 0.05:
        return "a tu favor", GREEN
    if peso > -0.05:
        return "parejo", MUTED
    if peso > -0.3:
        return "en tu contra", RED
    return "poco representativo", RED


def cargar_datos_rendimiento(conn: sqlite3.Connection, my_code: str, partidas: dict) -> dict:
    """
    Datos de la pestaña RENDIMIENTO -- Regla 5 (peso del resultado) y
    tendencia de ejecucion vs. la ultima quincena completa. Casi todo
    sale directo de reglas.py; lo unico que se agrega aca es rival_code
    y el marcador de stocks de las ultimas 20 partidas (cargar_partidas_
    propias() no los trae, esta pensada para las reglas historicas, no
    para mostrar el detalle puntual de una partida).
    """
    con_mecanica = filtrar_por_origen(partidas, "mecanica")
    hist = stats_historicas(partidas)
    quinc = stats_quincena_reciente(partidas)
    n_quincena = next(iter(quinc.values()))[2] if quinc else 0

    completas = filtrar_por_origen(partidas, "mecanica+resultado")
    recientes = sorted(completas.values(), key=lambda p: p.get("fecha") or "", reverse=True)[:20]

    archivos = [p["archivo"] for p in recientes]
    detalle_extra = {}
    if archivos:
        placeholders = ",".join("?" * len(archivos))
        filas = conn.execute(
            f"""
            SELECT r.file_path, riv.connect_code, mio.stocks_remaining, riv.stocks_remaining
            FROM replays r
            JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
            JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
            WHERE r.file_path IN ({placeholders})
            """,
            (my_code, my_code, *archivos),
        ).fetchall()
        detalle_extra = {
            file_path: {"rival_code": rival_code, "mis_stocks": mis, "stocks_rival": riv}
            for file_path, rival_code, mis, riv in filas
        }

    pesos_validos = []
    filas_tabla = []
    for p in recientes:
        peso = peso_resultado(p, hist)
        if peso is not None:
            pesos_validos.append(peso)
        extra = detalle_extra.get(p["archivo"], {})
        filas_tabla.append({
            "fecha": p.get("fecha"),
            "rival_code": extra.get("rival_code"),
            "mis_stocks": extra.get("mis_stocks"),
            "stocks_rival": extra.get("stocks_rival"),
            "resultado": p.get("resultado"),
            "peso": peso,
        })

    return {
        "n_mecanica": len(con_mecanica),
        "hist": hist,
        "quincena": quinc,
        "n_quincena": n_quincena,
        "recientes": filas_tabla,
        "promedio_recientes": round(sum(pesos_validos) / len(pesos_validos), 2) if pesos_validos else None,
        "n_promedio": len(pesos_validos),
    }


def cargar_bandas_diferencia_elo(conn: sqlite3.Connection, my_code: str, mi_elo_history: list) -> list:
    """
    Agrupa las partidas de RANKED por diferencia real de ELO (rival -
    vos) al momento EXACTO de cada partida -- no por tramo/rango, que es
    ancho y esconde matices. Usa match_players.elo_at_match (snapshot
    del rival en esa partida puntual) contra mi_elo_en_fecha() (Regla 2)
    para tu propio ELO en ese mismo momento -- mas preciso que comparar
    todo el historico contra tu ELO actual, como hacia el tracker viejo.
    """
    filas = conn.execute(
        """
        SELECT r.played_at, riv.elo_at_match, mio.is_winner
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        WHERE r.game_mode = 'ranked' AND riv.elo_at_match IS NOT NULL
          AND mio.is_winner IS NOT NULL AND r.played_at IS NOT NULL
        """,
        (my_code, my_code),
    ).fetchall()

    BANDAS = [
        (-10 ** 9, -300, "Mucho más débil (−300 o más)"),
        (-300, -100, "Más débil (−300 a −100)"),
        (-100, 100, "Nivel similar (±100)"),
        (100, 300, "Más fuerte (+100 a +300)"),
        (300, 10 ** 9, "Mucho más fuerte (+300 o más)"),
    ]
    conteo = [{"v": 0, "d": 0} for _ in BANDAS]
    for played_at, elo_rival, is_winner in filas:
        mi_elo = mi_elo_en_fecha(mi_elo_history, datetime.fromisoformat(played_at))
        if mi_elo is None:
            continue
        diff = elo_rival - mi_elo
        for i, (lo, hi, _label) in enumerate(BANDAS):
            if lo <= diff < hi:
                conteo[i]["v" if is_winner else "d"] += 1
                break

    return [{"label": lbl, "v": c["v"], "d": c["d"]} for (_lo, _hi, lbl), c in zip(BANDAS, conteo)]


def cargar_estadistica_rivales(conn: sqlite3.Connection, my_code: str, mi_elo_history: list) -> dict:
    """
    Datos de la pestaña MI ESTADISTICA. A diferencia del tracker viejo,
    no depende de un pipeline aparte de JSONs (rivales.json, rangos_
    cache.json, dataset_unificado.json...) -- todo sale directo de la
    base: elo_at_match para el ELO ponderado (pesa cada PARTIDA, no cada
    rival, y usa el ELO que tenia en ese momento, no un cache del
    ultimo valor) y r.game_mode (ya calculado por process_replays.py)
    para filtrar ranked.
    """
    elos_partida = [
        e for (e,) in conn.execute(
            """
            SELECT riv.elo_at_match
            FROM replays r
            JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
            JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
            WHERE riv.elo_at_match IS NOT NULL
            """,
            (my_code, my_code),
        ).fetchall()
    ]
    elos_partida.sort()
    media_pond = round(sum(elos_partida) / len(elos_partida), 1) if elos_partida else None
    mediana_pond = round(elos_partida[len(elos_partida) // 2], 1) if elos_partida else None

    total_rivales = conn.execute("SELECT COUNT(*) FROM players WHERE is_self = 0").fetchone()[0]
    sin_ranked = conn.execute(
        "SELECT COUNT(*) FROM players WHERE is_self = 0 AND elo IS NULL AND rank_updated_at IS NOT NULL"
    ).fetchone()[0]
    sin_datos = conn.execute(
        "SELECT COUNT(*) FROM players WHERE is_self = 0 AND rank_updated_at IS NULL"
    ).fetchone()[0]
    ultima_actualizacion = conn.execute("SELECT MAX(rank_updated_at) FROM players").fetchone()[0]

    top_frecuentes = conn.execute(
        """
        SELECT riv.connect_code, COUNT(*) enfrentamientos
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        GROUP BY riv.connect_code
        ORDER BY enfrentamientos DESC
        LIMIT 3
        """,
        (my_code, my_code),
    ).fetchall()

    extremos_filas = conn.execute(
        """
        SELECT riv.connect_code, p.elo, COUNT(*) enfrentamientos, rt.tramo_grande
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        JOIN players p ON p.connect_code = riv.connect_code
        LEFT JOIN rank_tiers rt ON rt.tramo = p.rank_tier
        WHERE p.elo IS NOT NULL
        GROUP BY riv.connect_code
        """,
        (my_code, my_code),
    ).fetchall()
    mas_fuerte_fila = max(extremos_filas, key=lambda f: f[1], default=None)
    mas_debil_fila = min(extremos_filas, key=lambda f: f[1], default=None)

    def _fila_a_extremo(fila):
        if not fila:
            return None
        code, elo, veces, tramo_grande = fila
        return {"connect_code": code, "elo": elo, "veces": veces, "tramo_grande": tramo_grande}

    mi_elo_actual = conn.execute("SELECT elo FROM players WHERE connect_code = ?", (my_code,)).fetchone()
    mi_elo_actual = mi_elo_actual[0] if mi_elo_actual else None

    ranked_rivales = conn.execute(
        """
        SELECT DISTINCT riv.connect_code, p.elo
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        JOIN players p ON p.connect_code = riv.connect_code
        WHERE r.game_mode = 'ranked' AND p.elo IS NOT NULL
        """,
        (my_code, my_code),
    ).fetchall()

    vs_campo = None
    if mi_elo_actual is not None and ranked_rivales:
        elos_ranked = sorted(e for _c, e in ranked_rivales)
        total = len(elos_ranked)
        arriba = sum(1 for e in elos_ranked if e > mi_elo_actual)
        abajo = sum(1 for e in elos_ranked if e < mi_elo_actual)
        vs_campo = {
            "mi_elo": mi_elo_actual, "total_ranked": total,
            "pct_arriba": round(100 * arriba / total), "pct_abajo": round(100 * abajo / total),
            "media_ranked": round(sum(elos_ranked) / total, 1),
            "mediana_ranked": round(elos_ranked[total // 2], 1),
        }

    histograma_tramo = dict(conn.execute(
        """
        SELECT rt.tramo_grande, COUNT(DISTINCT riv.connect_code)
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        JOIN players p ON p.connect_code = riv.connect_code
        JOIN rank_tiers rt ON rt.tramo = p.rank_tier
        GROUP BY rt.tramo_grande
        """,
        (my_code, my_code),
    ).fetchall())

    resultados_tramo = {
        tramo: {"v": v, "d": d, "diff": round(diff, 2) if diff is not None else None}
        for tramo, v, d, diff in conn.execute(
            """
            SELECT rt.tramo_grande,
                   SUM(CASE WHEN mio.is_winner = 1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN mio.is_winner = 0 THEN 1 ELSE 0 END),
                   AVG(mio.stocks_remaining - riv.stocks_remaining)
            FROM replays r
            JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
            JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
            JOIN players p ON p.connect_code = riv.connect_code
            JOIN rank_tiers rt ON rt.tramo = p.rank_tier
            WHERE mio.is_winner IS NOT NULL
            GROUP BY rt.tramo_grande
            """,
            (my_code, my_code),
        ).fetchall()
    }

    return {
        "media_ponderada": media_pond, "mediana_ponderada": mediana_pond,
        "total_rivales": total_rivales, "sin_ranked": sin_ranked, "sin_datos": sin_datos,
        "ultima_actualizacion": ultima_actualizacion,
        "top_frecuentes": [{"connect_code": c, "veces": n} for c, n in top_frecuentes],
        "mas_fuerte": _fila_a_extremo(mas_fuerte_fila),
        "mas_debil": _fila_a_extremo(mas_debil_fila),
        "vs_campo": vs_campo,
        "histograma_tramo": histograma_tramo,
        "resultados_tramo": resultados_tramo,
        "bandas_elo": cargar_bandas_diferencia_elo(conn, my_code, mi_elo_history),
    }


def cargar_rivales_detalle(conn: sqlite3.Connection, my_code: str) -> list:
    """
    Detalle completo, rival por rival: enfrentamientos, record V/D,
    diferencial de stocks promedio, y ELO/rango actual (el ultimo
    valor conocido -- no el de cada partida puntual, eso ya esta en
    MI ESTADISTICA via elo_at_match). "estado" distingue si el rival
    tiene perfil ranked, no tiene, o todavia no fue consultado -- no
    se puede distinguir de forma confiable un error transitorio de
    "nunca consultado" sin guardar ese dato aparte (consultar_rangos.py
    no lo persiste), asi que ambos casos caen en "sin_datos".
    """
    filas = conn.execute(
        """
        SELECT riv.connect_code,
               COUNT(*) AS veces,
               SUM(CASE WHEN mio.is_winner = 1 THEN 1 ELSE 0 END) AS victorias,
               SUM(CASE WHEN mio.is_winner = 0 THEN 1 ELSE 0 END) AS derrotas,
               AVG(mio.stocks_remaining - riv.stocks_remaining) AS diff_stocks,
               p.elo, p.rank_tier, rt.tramo_grande, p.rank_updated_at
        FROM replays r
        JOIN match_players mio ON mio.replay_id = r.id AND mio.connect_code = ?
        JOIN match_players riv ON riv.replay_id = r.id AND riv.connect_code != ?
        JOIN players p ON p.connect_code = riv.connect_code
        LEFT JOIN rank_tiers rt ON rt.tramo = p.rank_tier
        GROUP BY riv.connect_code
        """,
        (my_code, my_code),
    ).fetchall()

    resultado = []
    for code, veces, v, d, diff, elo, rank_tier, tramo_grande, rank_updated_at in filas:
        v, d = v or 0, d or 0
        con_resultado = v + d
        if elo is not None:
            estado = "ranked"
        elif rank_updated_at is not None:
            estado = "sin_ranked"
        else:
            estado = "sin_datos"
        resultado.append({
            "connect_code": code, "veces": veces, "victorias": v, "derrotas": d,
            "winrate": round(100 * v / con_resultado, 1) if con_resultado else None,
            "diff_stocks": round(diff, 2) if diff is not None else None,
            "elo": elo, "rank_tier": rank_tier, "tramo_grande": tramo_grande,
            "estado": estado,
        })
    return resultado


def _kills_agrupado_por_char(conn: sqlite3.Connection, campo_code: str, campo_char: str,
                              my_code: str, min_kills: int) -> dict:
    """
    Helper compartido por cargar_kills_por_movimiento() y
    cargar_muertes_por_movimiento() -- misma agregacion (movimiento mas
    usado, cruzado por personaje), solo cambia si se agrupa mirando el
    lado "killer" (con que matas) o el lado "victim" (con que te matan).
    `campo_code`/`campo_char` son nombres de columna fijos definidos por
    esta misma funcion (nunca vienen de afuera), asi que interpolarlos
    en el SQL es seguro -- no es un valor de usuario.
    """
    filas = conn.execute(
        f"""
        SELECT {campo_char}, killer_move, COUNT(*) AS cantidad, AVG(victim_percent) AS pct_promedio
        FROM kills
        WHERE {campo_code} = ? AND killer_move IS NOT NULL AND {campo_char} IS NOT NULL
        GROUP BY {campo_char}, killer_move
        """,
        (my_code,),
    ).fetchall()

    por_personaje = {}
    for char_id, move_id, cantidad, pct_prom in filas:
        por_personaje.setdefault(char_id, []).append({
            "move_id": move_id, "nombre": attack_nombre(move_id), "cantidad": cantidad,
            "pct_promedio": round(pct_prom, 1) if pct_prom is not None else None,
        })

    resultado = {}
    for char_id, movimientos in por_personaje.items():
        total = sum(m["cantidad"] for m in movimientos)
        if total < min_kills:
            continue
        for m in movimientos:
            m["porcentaje"] = round(100 * m["cantidad"] / total, 1)
        movimientos.sort(key=lambda m: m["cantidad"], reverse=True)
        resultado[char_id] = {"nombre": char_nombre(char_id), "total": total, "movimientos": movimientos[:5]}

    return resultado


def cargar_kills_por_movimiento(conn: sqlite3.Connection, my_code: str, min_kills: int = 2) -> dict:
    """Con que movimiento matas mas, cruzado por el personaje que jugabas
    VOS en ese momento. Requiere haber reprocesado los replays con la
    version que agrega detectar_kills() (ver ANALYSIS_VERSION en
    process_replays.py) -- replays viejos sin reprocesar no aportan nada
    aca, no rompen nada tampoco."""
    return _kills_agrupado_por_char(conn, "killer_connect_code", "killer_character_id", my_code, min_kills)


def cargar_muertes_por_movimiento(conn: sqlite3.Connection, my_code: str, min_kills: int = 2) -> dict:
    """Contraparte de arriba: con que movimiento te matan mas, cruzado
    por el personaje que jugabas VOS cuando te mataron (no el del rival
    -- asi se ve, por ejemplo, "jugando Fox, te matan mas con Fsmash"
    sin importar contra que personaje puntual estabas)."""
    return _kills_agrupado_por_char(conn, "victim_connect_code", "victim_character_id", my_code, min_kills)


def cargar_datos_personajes(conn: sqlite3.Connection, my_code: str, partidas: dict) -> dict:
    """
    Datos de la pestaña PERSONAJES. Reusa el mismo `partidas` que ya
    cargo cargar_datos() para el resumen (nada de repetir el JOIN
    grande de cargar_partidas_propias) -- solo suma 3 consultas de
    agregacion chicas (por personaje propio, por personaje rival, por
    stage).
    """
    char_propios = cargar_stats_char_propios(conn, my_code)
    char_rivales = cargar_stats_char_rivales(conn, my_code)
    stages = cargar_stats_stage(conn, my_code)

    con_resultado = [p for p in partidas.values() if p.get("resultado")]
    victorias = sum(1 for p in con_resultado if p["resultado"] == "victoria")
    total_con_resultado = len(con_resultado)

    fav_stage = max(stages, key=lambda s: s["victorias"], default=None)
    if fav_stage and fav_stage["victorias"] == 0:
        fav_stage = None
    bad_stage = max(stages, key=lambda s: s["derrotas"], default=None)
    if bad_stage and bad_stage["derrotas"] == 0:
        bad_stage = None

    return {
        "top_ganas": sorted((c for c in char_propios if c["victorias"] > 0),
                            key=lambda c: c["victorias"], reverse=True)[:3],
        "top_perdes": sorted((c for c in char_propios if c["derrotas"] > 0),
                             key=lambda c: c["derrotas"], reverse=True)[:3],
        "top_domino": sorted((c for c in char_rivales if c["victorias"] > 0),
                             key=lambda c: c["victorias"], reverse=True)[:3],
        "top_me_ganan": sorted((c for c in char_rivales if c["derrotas"] > 0),
                               key=lambda c: c["derrotas"], reverse=True)[:3],
        "total_partidas": total_con_resultado,
        "victorias": victorias,
        "derrotas": total_con_resultado - victorias,
        "winrate_pct": round(100 * victorias / total_con_resultado, 1) if total_con_resultado else None,
        "duracion_promedio": duracion_promedio_seg(partidas),
        "fav_stage": fav_stage,
        "bad_stage": bad_stage,
        "kills_por_movimiento": cargar_kills_por_movimiento(conn, my_code),
        "muertes_por_movimiento": cargar_muertes_por_movimiento(conn, my_code),
    }


def cargar_datos(conn: sqlite3.Connection, my_code: str) -> dict:
    partidas = cargar_partidas_propias(conn, my_code)
    mi_elo_history = cargar_mi_elo_history(conn, my_code)
    tiempo_total = tiempo_total_seg(partidas)

    return {
        "my_code": my_code,
        "total_partidas": len(partidas),
        "ultima_partida": cargar_ultima_partida(conn, my_code, partidas, mi_elo_history),
        "hoy": resumen_dia(partidas),
        "sesion": resumen_ultima_sesion(partidas),
        "ranked": cargar_ranked(conn, my_code),
        "personaje_principal": cargar_personaje_principal(conn, my_code),
        "tiempo_total": tiempo_total,
        "duracion_promedio": duracion_promedio_seg(partidas),
        "mas_larga": partida_extrema(partidas, mas_larga=True),
        "mas_corta": partida_extrema(partidas, mas_larga=False),
        "racha": racha_actual_dias(partidas),
        "dias_jugados": dias_jugados(partidas),
        "insignias_perfil": insignias.insignias_de_perfil(conn, my_code, partidas, mi_elo_history, tiempo_total),
        "personajes": cargar_datos_personajes(conn, my_code, partidas),
        "progreso": cargar_datos_progreso(conn, my_code, mi_elo_history),
        "historial": cargar_datos_historial(conn, my_code, partidas),
        "rendimiento": cargar_datos_rendimiento(conn, my_code, partidas),
        "mi_estadistica": cargar_estadistica_rivales(conn, my_code, mi_elo_history),
        "rivales": cargar_rivales_detalle(conn, my_code),
    }


def ultima_partida_html(up):
    if up is None:
        return '<div class="card"><div class="card-title">Última Partida</div><div class="muted">Sin partidas registradas todavía.</div></div>'

    resultado = up["resultado"]
    color_resultado = GREEN if resultado == "victoria" else (RED if resultado == "derrota" else MUTED)
    texto_resultado = resultado.upper() if resultado else "INDETERMINADO"

    icono_rival = _char_icon_html(up.get("rival_char_id"), size=20)
    icono_mio = _char_icon_html(up.get("my_char_id"), size=20)

    detalle_partes = [f'vs <b>{up["rival_code"]}</b> {icono_rival}({up["rival_char"]})',
                       f'jugando {icono_mio}{up["my_char"]}', f'en {up["stage"]}']
    if up["fecha"]:
        detalle_partes.append(up["fecha"].strftime("%d/%m/%Y %H:%M"))
    detalle = " &middot; ".join(detalle_partes)

    contexto_html = ""
    if up["contexto"]:
        sufijo = " (aproximado)" if up.get("contexto_aproximado") else ""
        label = CONTEXTO_LABELS.get(up["contexto"], up["contexto"])
        elo_txt = ""
        if up.get("elo_mio") is not None and up.get("elo_rival") is not None:
            diff = up["elo_rival"] - up["elo_mio"]
            elo_txt = f' · {up["elo_mio"]:.0f} vs {up["elo_rival"]:.0f} ({diff:+.0f})'
        contexto_html = f'<div class="pill">{label}{elo_txt}{sufijo}</div>'

    rendicion_html = ""
    if up["se_rindio"]:
        rendicion_html = f'<div class="pill pill-muted">Se rindió: {up["se_rindio"]}</div>'

    insignias_html = "".join(
        f'<div class="pill" style="border-color:{i["color"]}; color:{i["color"]}" title="{i["tooltip"]}">{i["nombre"]}</div>'
        for i in up.get("insignias", [])
    )

    metricas_html = ""
    if up["detalle_metricas"]:
        items = "".join(
            (
                f'<li><span class="metrica-nombre">{METRICA_LABELS.get(d["campo"], d["campo"])}:</span> '
                f'<b>{fmt_metrica(d["campo"], d["valor"])}</b> esta partida '
                f'(<span style="color:{GREEN if d["mejor_que_usual"] else RED}">'
                f'{"mejor" if d["mejor_que_usual"] else "peor"} que tu promedio</span> '
                f'de {fmt_metrica(d["campo"], d["promedio"], es_promedio=True)}, '
                f'sobre {d["n_muestra"]} partidas)</li>'
            )
            for d in up["detalle_metricas"]
        )
        metricas_html = f'<div class="sub-title">Ejecución vs. histórico</div><ul class="metricas-list">{items}</ul>'

    auto = up["autodestrucciones"]
    auto_html = ""
    if auto["propias"] is not None or auto["rival"] is not None:
        auto_html = (
            f'<div class="sub-title">Autodestrucciones</div>'
            f'<div>Tuyas: <b>{auto["propias"] if auto["propias"] is not None else "N/D"}</b>'
            f' &nbsp;|&nbsp; Rival: <b>{auto["rival"] if auto["rival"] is not None else "N/D"}</b></div>'
        )

    matchup_html = ""
    if up["matchup"]:
        m = up["matchup"]
        matchup_html = (
            f'<div class="sub-title">Matchup {up["my_char"]} vs {up["rival_char"]}</div>'
            f'<div>{m["victorias"]}-{m["total"] - m["victorias"]} ({m["wr"]}% WR en {m["total"]} partidas)</div>'
        )

    return f'''
    <div class="card">
        <div class="card-title">Última Partida</div>
        <div class="resultado" style="color:{color_resultado}">{texto_resultado}</div>
        <div class="detalle">{detalle}</div>
        <div class="pills">{contexto_html}{rendicion_html}{insignias_html}</div>
        <div class="analisis-cols">
            <div>{auto_html}</div>
            <div>{metricas_html}</div>
        </div>
        {matchup_html}
    </div>
    '''


def render_body_html(datos: dict) -> str:
    """
    Todo lo que SÍ cambia entre refrescos -- va dentro de #app-content.
    El shell (head/estilos/script) se arma una sola vez en render_html()
    y nunca se vuelve a tocar.
    """
    hoy = datos["hoy"]
    hoy_html = (
        f'{hoy["victorias"]}-{hoy["derrotas"]} ({hoy["winrate_pct"]}% WR)' if hoy["partidas"]
        else "Sin partidas hoy"
    )

    sesion = datos["sesion"]
    sesion_html = (
        f'{sesion["fecha"]} · {sesion["partidas"]} partidas · {fmt_horas(sesion["duracion_seg"])}'
        if sesion else "N/D"
    )

    r = datos["ranked"]
    tramo_color = TRAMO_COLORES.get(r["tramo_grande"], MUTED)
    elo_badge_html = (
        f'<div class="elo-badge" style="border-color:{tramo_color}; background:{tramo_color}1a;">'
        f'<div class="elo-badge-tramo" style="color:{tramo_color}">{r["rank_tier"] or "Sin rango"}</div>'
        f'<div class="elo-badge-valor" style="color:{tramo_color}">{r["elo"]:.0f} Elo</div>'
        f'</div>'
    ) if r["elo"] is not None else '<div class="muted">Sin datos de rango todavía</div>'
    peak_html = f'{r["peak"]:.0f}' if r["peak"] is not None else "N/D"
    record_html = f'{r["wins"]}-{r["losses"]}' if r["wins"] is not None else "N/D"
    actualizado_html = fmt_dias_desde(r["rank_updated_at"])
    actualizado_title = r["rank_updated_at"] or ""

    # Pills compactas, solo nombre -- tier + descripcion van en el tooltip
    # nativo (title), no en el pill. Orden: tier mas alto primero, lo mas
    # destacado lidera. (Con pocas insignias todavia en juego, este orden
    # es un default razonable -- se afina mas adelante con mas variedad real.)
    ORDEN_TIER = {"Platino": 4, "Oro": 3, "Plata": 2, "Bronce": 1}
    insignias_ordenadas = sorted(
        datos["insignias_perfil"], key=lambda i: ORDEN_TIER.get(i["tier"], 0), reverse=True
    )
    insignias_perfil_html = "".join(
        f'<div class="pill" style="border-color:{i["color"]}; color:{i["color"]}" '
        f'title="{i["tier"]} — {i["tooltip"]}">{i["nombre"]}</div>'
        for i in insignias_ordenadas
    )

    # Perfil (estilo luckystats): icono del personaje mas usado + tu
    # connect code, a la izquierda del badge de ELO y las insignias. El
    # nombre siempre se muestra (my_code siempre existe, viene de config.py);
    # el icono es opcional -- si no hay ningun personaje identificado
    # todavia (base recien creada) o falta el archivo en assets/chars/,
    # se muestra el nombre solo.
    icono_principal_html = _char_icon_html(
        datos.get("personaje_principal"), size=48, css_class="perfil-avatar"
    )
    perfil_identidad_html = f'''
    <div class="perfil-identidad">
        {icono_principal_html}
        <div class="perfil-nombre">{datos["my_code"]}</div>
    </div>
    '''

    return '''
    <h1>Melee Tracker</h1>
    <div class="subtitle">{MY_CODE} &middot; {TOTAL_PARTIDAS} partidas registradas</div>

    <div class="card" style="margin-bottom:18px">
        <div class="perfil-row">
            {PERFIL_IDENTIDAD}
            {ELO_BADGE}
            <div class="pills">{INSIGNIAS_PERFIL}</div>
        </div>
    </div>

    <div class="grid">
        {ULTIMA_PARTIDA}

        <div class="card card-half">
            <div class="card-title">Hoy</div>
            <div class="resultado" style="font-size:18px">{HOY}</div>
        </div>
        <div class="card card-half">
            <div class="card-title">Última Sesión</div>
            <div style="font-size:14px">{SESION}</div>
        </div>

        <div class="card card-half">
            <div class="card-title">Ranked — Slippi.gg</div>
            <div class="grid-2">
                <div class="stat-cell"><div class="stat-value" style="color:#fbbf24">{PEAK}</div><div class="stat-label">Pico</div></div>
                <div class="stat-cell"><div class="stat-value" style="font-size:13px">{RECORD}</div><div class="stat-label">Record oficial</div></div>
            </div>
            <div class="sub-title" title="{ACTUALIZADO_TITLE}">Actualizado {ACTUALIZADO}</div>
        </div>

        <div class="card card-half">
            <div class="card-title">Histórico</div>
            <div class="grid-7">
                <div class="stat-cell"><div class="stat-value">{TIEMPO_TOTAL}</div><div class="stat-label">Tiempo total</div></div>
                <div class="stat-cell"><div class="stat-value">{DURACION_PROM}</div><div class="stat-label">Duración prom.</div></div>
                <div class="stat-cell"><div class="stat-value">{RACHA}</div><div class="stat-label">Racha (días)</div></div>
                <div class="stat-cell"><div class="stat-value">{DIAS_JUGADOS}</div><div class="stat-label">Días jugados</div></div>
            </div>
        </div>
    </div>
    '''.format(
        MY_CODE=datos["my_code"], TOTAL_PARTIDAS=datos["total_partidas"],
        ULTIMA_PARTIDA=ultima_partida_html(datos["ultima_partida"]),
        ELO_BADGE=elo_badge_html, INSIGNIAS_PERFIL=insignias_perfil_html, PERFIL_IDENTIDAD=perfil_identidad_html,
        HOY=hoy_html, SESION=sesion_html,
        PEAK=peak_html, RECORD=record_html, ACTUALIZADO=actualizado_html, ACTUALIZADO_TITLE=actualizado_title,
        TIEMPO_TOTAL=fmt_horas(datos["tiempo_total"]), DURACION_PROM=fmt_seg(datos["duracion_promedio"]),
        RACHA=datos["racha"], DIAS_JUGADOS=datos["dias_jugados"],
    )


def _char_top_html(entries: list, campo: str, sufijo: str, color: str, titulo: str) -> str:
    """Una tarjeta de "top 3" (icono del personaje + nombre + cifra)
    reusada por las 4 secciones de PERSONAJES -- solo cambia que campo
    mostrar (victorias o derrotas), el sufijo del numero y el color/
    titulo de la tarjeta. Si no hay icono descargado para ese personaje
    (ver _char_icon_html), cae a la medalla de posicion como antes."""
    medallas = ["🥇", "🥈", "🥉"]
    if not entries:
        cuerpo = '<div class="muted" style="padding:4px 0">Sin datos todavía.</div>'
    else:
        cuerpo = "".join(
            f'''
            <div class="char-col">
                <div>{_char_icon_html(e.get("character_id"), size=36) or f'<span class="medal">{medallas[i]}</span>'}</div>
                <div class="char-nombre">{e["nombre"]}</div>
                <div class="char-cifra">{e[campo]}{sufijo}</div>
            </div>
            '''
            for i, e in enumerate(entries)
        )
    return f'''
    <div class="card card-half">
        <div class="card-title" style="color:{color}">{titulo}</div>
        <div class="char-row">{cuerpo}</div>
    </div>
    '''


def _kill_move_dist_html(movimientos: list) -> str:
    if not movimientos:
        return '<div class="dominio-sub">Sin datos todavía</div>'
    return "".join(
        f'<div class="killmove-row"><span class="killmove-nombre">{m["nombre"]}</span>'
        f'<span class="killmove-pct">{m["porcentaje"]}%</span></div>'
        for m in movimientos[:4]
    )


def _kill_moves_html(kills_por_char: dict, muertes_por_char: dict) -> str:
    personajes = sorted(
        set(kills_por_char) | set(muertes_por_char),
        key=lambda cid: -(kills_por_char.get(cid, {}).get("total", 0)
                          + muertes_por_char.get(cid, {}).get("total", 0)),
    )
    if not personajes:
        return ('<div class="muted">Sin datos todavía -- este dato se agregó después de que ya tenías '
                'replays procesados. Corré "▶ Procesar replays nuevos" con --forzar para reprocesarlos.</div>')

    tarjetas = []
    for cid in personajes[:4]:
        icono = _char_icon_html(cid, size=28)
        k = kills_por_char.get(cid)
        m = muertes_por_char.get(cid)
        tarjetas.append(f'''
        <div class="killmove-card">
            <div class="killmove-header">{icono}<span class="dominio-nombre">{char_nombre(cid)}</span></div>
            <div class="killmove-cols">
                <div>
                    <div class="killmove-titulo" style="color:{GREEN}">Cómo matás</div>
                    {_kill_move_dist_html(k["movimientos"] if k else [])}
                </div>
                <div>
                    <div class="killmove-titulo" style="color:{RED}">Cómo te matan</div>
                    {_kill_move_dist_html(m["movimientos"] if m else [])}
                </div>
            </div>
        </div>
        ''')
    return "".join(tarjetas)


def personajes_html(datos: dict) -> str:
    if datos["total_partidas"] == 0:
        return '''
        <h1>Personajes</h1>
        <div class="card"><div class="muted">Sin partidas con resultado registradas todavía.</div></div>
        '''

    top_ganas = _char_top_html(datos["top_ganas"], "victorias", " victorias", GREEN, "🏆 Con quién más ganás")
    top_perdes = _char_top_html(datos["top_perdes"], "derrotas", " derrotas", RED, "💀 Con quién más perdés")
    top_domino = _char_top_html(datos["top_domino"], "victorias", " veces", ACCENT2, "😤 Rivales que más dominás")
    top_me_ganan = _char_top_html(datos["top_me_ganan"], "derrotas", " veces", "#f97316", "😰 Rivales que más te ganan")

    winrate_txt = f'{datos["winrate_pct"]}%' if datos["winrate_pct"] is not None else "N/D"
    color_wr = GREEN if (datos["winrate_pct"] or 0) >= 50 else RED

    fav, bad = datos["fav_stage"], datos["bad_stage"]
    fav_html = (f'{fav["nombre"]}<br><span style="color:{GREEN}">{fav["victorias"]}V</span>'
                if fav else "N/D")
    bad_html = (f'{bad["nombre"]}<br><span style="color:{RED}">{bad["derrotas"]}D</span>'
                if bad else "N/D")

    return f'''
    <h1>Personajes</h1>

    <div class="grid">
        {top_ganas}
        {top_perdes}
        {top_domino}
        {top_me_ganan}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Resumen</div>
        <div class="grid-4">
            <div class="stat-cell"><div class="stat-value">{datos["total_partidas"]}</div><div class="stat-label">Partidas</div></div>
            <div class="stat-cell"><div class="stat-value" style="color:{GREEN}">{datos["victorias"]}</div><div class="stat-label">Victorias</div></div>
            <div class="stat-cell"><div class="stat-value" style="color:{RED}">{datos["derrotas"]}</div><div class="stat-label">Derrotas</div></div>
            <div class="stat-cell"><div class="stat-value" style="color:{color_wr}">{winrate_txt}</div><div class="stat-label">Win Rate</div></div>
        </div>
        <div class="grid-3" style="margin-top:12px">
            <div class="stat-cell"><div class="stat-value" style="font-size:13px">{fmt_seg(datos["duracion_promedio"])}</div><div class="stat-label">Duración prom.</div></div>
            <div class="stat-cell"><div class="stat-value" style="font-size:13px">{fav_html}</div><div class="stat-label">Stage favorito</div></div>
            <div class="stat-cell"><div class="stat-value" style="font-size:13px">{bad_html}</div><div class="stat-label">Stage difícil</div></div>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Cómo matás / cómo te matan, por personaje</div>
        {_kill_moves_html(datos["kills_por_movimiento"], datos["muertes_por_movimiento"])}
    </div>
    '''


def _elo_svg_html(historial: list) -> str:
    """Grafico de linea liviano en SVG puro (sin Chart.js todavia -- eso
    se suma mas adelante para lo que realmente lo necesite). historial:
    lista de (datetime, elo) ya ordenada por fecha."""
    if len(historial) < 2:
        return f'<div class="muted" style="padding:16px 0">Sin suficiente historial de ELO todavía -- hace falta correr consultar_rangos.py más de una vez.</div>'

    width, height, pad = 600, 140, 12
    valores = [elo for _, elo in historial]
    mn, mx = min(valores), max(valores)
    span = mx - mn or 1

    def px(i):
        return pad + i * (width - 2 * pad) / (len(valores) - 1)

    def py(v):
        return pad + (1 - (v - mn) / span) * (height - 2 * pad)

    puntos = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(valores))
    circulos = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" fill="{ACCENT}" />'
        for i, v in enumerate(valores)
    )

    return f'''
    <svg viewBox="0 0 {width} {height}" style="width:100%; height:{height}px; display:block;">
        <polyline points="{puntos}" fill="none" stroke="{ACCENT}" stroke-width="2" />
        {circulos}
    </svg>
    <div style="display:flex; justify-content:space-between; font-size:11px; color:{MUTED}; margin-top:4px">
        <span>{historial[0][0].strftime("%d/%m/%Y")} · {historial[0][1]:.0f}</span>
        <span>{historial[-1][0].strftime("%d/%m/%Y")} · {historial[-1][1]:.0f}</span>
    </div>
    '''


def _elo_tabla_html(historial: list, n: int = 10) -> str:
    ultimos = list(reversed(historial[-n:]))
    if not ultimos:
        return '<div class="muted">Sin actualizaciones registradas.</div>'
    filas = []
    for i, (fecha, elo) in enumerate(ultimos):
        delta_html = ""
        if i + 1 < len(ultimos):
            delta = elo - ultimos[i + 1][1]
            color = GREEN if delta >= 0 else RED
            signo = "+" if delta >= 0 else ""
            delta_html = f'<span style="color:{color}">{signo}{delta:.0f}</span>'
        filas.append(
            f'<div class="elo-row"><span class="elo-fecha">{fecha.strftime("%d/%m/%Y %H:%M")}</span>'
            f'<span class="elo-valor">{elo:.0f}</span><span class="elo-delta">{delta_html}</span></div>'
        )
    return "".join(filas)


def _grade_dist_html(distribucion: dict, total: int) -> str:
    filas = []
    for g in GRADE_ORDEN:
        cnt = distribucion.get(g, 0)
        pct = round(100 * cnt / total) if total else 0
        color = GRADE_COLORES[g]
        filas.append(f'''
        <div class="grade-row">
            <span class="grade-letra" style="color:{color}">{g}</span>
            <div class="grade-barra-bg"><div class="grade-barra-fg" style="width:{pct}%; background:{color}"></div></div>
            <span class="grade-cifra">{cnt} ({pct}%)</span>
        </div>
        ''')
    return "".join(filas)


def _dominio_personaje_html(dominio: list) -> str:
    if not dominio:
        return '<div class="muted">Sin suficientes partidas por personaje todavía (mínimo 3 con datos de stocks).</div>'
    filas = []
    for d in dominio[:8]:
        color = GREEN if d["avg_diff"] > 0.3 else (RED if d["avg_diff"] < -0.3 else MUTED)
        signo = "+" if d["avg_diff"] > 0 else ""
        grade_color = GRADE_COLORES.get(d["grade"], MUTED)
        filas.append(f'''
        <div class="dominio-row">
            <div>
                <span class="dominio-grade" style="color:{grade_color}">{d["grade"] or "?"}</span>
                {_char_icon_html(d.get("character_id"), size=22)}
                <span class="dominio-nombre">{d["nombre"]}</span>
                <div class="dominio-sub">{d["partidas"]} partidas con datos</div>
            </div>
            <div style="text-align:right">
                <div style="color:{color}; font-weight:700">{signo}{d["avg_diff"]:.2f} stocks</div>
                <div class="dominio-sub">Dominado {d["dominado_pct"]}% · Reñido {d["renido_pct"]}%</div>
            </div>
        </div>
        ''')
    return "".join(filas)


def progreso_html(datos: dict) -> str:
    elo = datos["historial_elo"]
    sk = datos["stocks"]
    dominio = datos["dominio_personajes"]

    grade = sk["overall_grade"]
    grade_color = GRADE_COLORES.get(grade, MUTED)

    def _fmt(v):
        return f"{v:.2f}" if v is not None else "N/D"

    diff = sk["avg_diff"]
    diff_color = GREEN if (diff or 0) > 0 else (RED if (diff or 0) < 0 else MUTED)
    diff_txt = f'{"+" if diff and diff > 0 else ""}{diff:.2f}' if diff is not None else "N/D"

    return f'''
    <h1>Progreso</h1>

    <div class="card">
        <div class="card-title">Historial de ELO</div>
        {_elo_svg_html(elo)}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Últimas actualizaciones</div>
        <div class="elo-tabla">{_elo_tabla_html(elo)}</div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">⚔ Análisis de stocks</div>
        <div class="grade-resumen">
            <div class="grade-promedio">
                <div class="stat-label">GRADE PROMEDIO</div>
                <div class="grade-letra-grande" style="color:{grade_color}">{grade or "—"}</div>
                <div class="muted">{GRADE_LABELS.get(grade, "Sin datos suficientes")}</div>
            </div>
            <div class="grade-dist">
                <div class="stat-label" style="margin-bottom:6px">DISTRIBUCIÓN</div>
                {_grade_dist_html(sk["distribucion"], sk["valid_games"])}
            </div>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Stocks promedio por resultado</div>
        <div class="grid-3">
            <div class="stat-cell"><div class="stat-value" style="color:{GREEN}">{_fmt(sk["avg_my_win"])}</div><div class="stat-label">Mis stocks al ganar</div></div>
            <div class="stat-cell"><div class="stat-value">{_fmt(sk["avg_opp_win"])}</div><div class="stat-label">Stocks rival al ganar</div></div>
            <div class="stat-cell"><div class="stat-value" style="color:{diff_color}">{diff_txt}</div><div class="stat-label">Diferencial prom.</div></div>
        </div>
        <div class="grid-3" style="margin-top:12px">
            <div class="stat-cell"><div class="stat-value">{_fmt(sk["avg_my_loss"])}</div><div class="stat-label">Mis stocks al perder</div></div>
            <div class="stat-cell"><div class="stat-value" style="color:{RED}">{_fmt(sk["avg_opp_loss"])}</div><div class="stat-label">Stocks rival al perder</div></div>
            <div class="stat-cell"><div class="stat-value">{sk["valid_games"]}</div><div class="stat-label">Partidas con datos</div></div>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Dominio por personaje rival</div>
        <div class="dominio-lista">{_dominio_personaje_html(dominio)}</div>
    </div>
    '''


def _fmt_fecha_corta(fecha_iso: str) -> str:
    if not fecha_iso:
        return "N/D"
    return datetime.fromisoformat(fecha_iso).strftime("%d/%m/%Y")


def _matchups_dificiles_html(matchups: list) -> str:
    if not matchups:
        return '<div class="muted">Sin suficientes partidas con rival identificado todavía.</div>'
    tarjetas = []
    for m in matchups:
        trend = m["trend"]
        color_trend = GREEN if trend > 0 else (RED if trend < 0 else MUTED)
        simbolo = "▲" if trend > 0 else ("▼" if trend < 0 else "─")
        tarjetas.append(f'''
        <div class="matchup-card">
            {_char_icon_html(m.get("character_id"), size=32)}
            <div class="matchup-nombre">{m["nombre"]}</div>
            <div class="dominio-sub">{m["total"]} partidas</div>
            <div class="dominio-sub" style="margin-top:6px">Histórico: {m["wr_total"]}%</div>
            <div style="color:{color_trend}">Últimas 10: {m["wr_last10"]}% {simbolo}{abs(trend)}%</div>
        </div>
        ''')
    return f'<div class="matchup-row">{"".join(tarjetas)}</div>'


def _dia_semana_html(dias: list) -> str:
    celdas = []
    for d in dias:
        wr = d["wr"]
        color = GREEN if wr is not None and wr >= 50 else (RED if wr is not None else MUTED)
        wr_txt = f"{wr}%" if wr is not None else "—"
        total_txt = f'{d["total"]} partida{"s" if d["total"] != 1 else ""}' if d["total"] else "sin datos"
        celdas.append(f'''
        <div class="dia-col">
            <div class="dia-nombre">{d["dia"][:3]}</div>
            <div class="dia-wr" style="color:{color}">{wr_txt}</div>
            <div class="dia-total">{total_txt}</div>
        </div>
        ''')
    return f'<div class="dia-semana-row">{"".join(celdas)}</div>'


def _winrate_mes_html(meses: list) -> str:
    if not meses:
        return '<div class="muted">Sin datos por mes todavía.</div>'
    filas = []
    for m in meses:
        total = m["wins"] + m["losses"]
        color = GREEN if m["wr"] >= 50 else RED
        filas.append(
            f'<div class="elo-row"><span class="elo-fecha">{m["mes"]}</span>'
            f'<span style="color:{color}; font-weight:600">{m["wr"]}%</span>'
            f'<span class="dominio-sub">{total} partidas · {m["wins"]}V/{m["losses"]}D</span></div>'
        )
    return "".join(filas)


def historial_html(datos: dict) -> str:
    streak_cur = datos["racha_actual_dias"]
    streak_best = datos["racha_record_dias"]

    dia_top = datos["dia_mas_partidas"]
    dia_top_html = (f'{_fmt_fecha_corta(dia_top["fecha"])}<br><span class="stat-label">{dia_top["cantidad"]} partidas</span>'
                     if dia_top else "N/D")
    tiempo_top = datos["dia_mas_tiempo"]
    tiempo_top_html = (f'{_fmt_fecha_corta(tiempo_top["fecha"])}<br><span class="stat-label">{fmt_horas(tiempo_top["segundos"])}</span>'
                        if tiempo_top else "N/D")

    turnos = datos["winrate_turno"]

    def _turno_cell(clave, etiqueta):
        t = turnos[clave]
        wr_txt = f'{t["wr"]}%  ({t["wins"]}V/{t["losses"]}D)' if t["wr"] is not None else "Sin datos"
        color = GREEN if (t["wr"] or 0) >= 50 else (RED if t["wr"] is not None else MUTED)
        return (f'<div class="stat-cell"><div class="stat-value" style="font-size:14px; color:{color}">{wr_txt}</div>'
                f'<div class="stat-label">{etiqueta}</div></div>')

    mejor, peor = datos["mejor_dia"], datos["peor_dia"]
    mejor_html = f'{mejor["dia"]}<br><span style="color:{GREEN}">{mejor["wr"]}% ({mejor["wins"]}V/{mejor["losses"]}D)</span>' if mejor else "N/D"
    peor_html = f'{peor["dia"]}<br><span style="color:{RED}">{peor["wr"]}% ({peor["wins"]}V/{peor["losses"]}D)</span>' if peor else "N/D"

    delta = datos["delta_mitad_sesion"]
    if delta is None:
        delta_txt, delta_color, interpretacion = "Sin datos", MUTED, "—"
    else:
        delta_color = GREEN if delta > 2 else (RED if delta < -2 else TEXT)
        delta_txt = f'{"+" if delta > 0 else ""}{delta}%'
        interpretacion = "Calentás bien 🔥" if delta > 2 else ("Te cansás 😓" if delta < -2 else "Consistente ✓")

    return f'''
    <h1>Historial</h1>

    <div class="card">
        <div class="card-title">Consistencia</div>
        <div class="grid-3">
            <div class="stat-cell"><div class="stat-value">{streak_cur} día{"s" if streak_cur != 1 else ""}</div><div class="stat-label">Racha actual</div></div>
            <div class="stat-cell"><div class="stat-value">{streak_best} día{"s" if streak_best != 1 else ""}</div><div class="stat-label">Récord de racha</div></div>
            <div class="stat-cell"><div class="stat-value">{datos["dias_jugados"]}</div><div class="stat-label">Días totales</div></div>
        </div>
        <div class="grid-3" style="margin-top:12px">
            <div class="stat-cell"><div class="stat-value" style="font-size:13px">{dia_top_html}</div><div class="stat-label">Día con más partidas</div></div>
            <div class="stat-cell"><div class="stat-value" style="font-size:13px">{tiempo_top_html}</div><div class="stat-label">Día con más tiempo</div></div>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Rendimiento</div>
        <div class="grid-4">
            <div class="stat-cell"><div class="stat-value" style="color:{GREEN}">{datos["win_streak_cur"]}</div><div class="stat-label">Racha victorias</div></div>
            <div class="stat-cell"><div class="stat-value">{datos["win_streak_best"]}</div><div class="stat-label">Récord victorias</div></div>
            <div class="stat-cell"><div class="stat-value" style="color:{RED}">{datos["loss_streak_cur"]}</div><div class="stat-label">Racha derrotas</div></div>
            <div class="stat-cell"><div class="stat-value">{datos["loss_streak_best"]}</div><div class="stat-label">Récord derrotas</div></div>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Matchups difíciles</div>
        {_matchups_dificiles_html(datos["matchups_dificiles"])}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Rendimiento por turno</div>
        <div class="grid-3">
            {_turno_cell("mañana", "🌅 Mañana (03–12h)")}
            {_turno_cell("tarde", "☀️ Tarde (12–18h)")}
            {_turno_cell("noche", "🌙 Noche (18–03h)")}
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Rendimiento por día</div>
        {_dia_semana_html(datos["winrate_dia_semana"])}
        <div class="grid-3" style="margin-top:12px">
            <div class="stat-cell"><div class="stat-value" style="font-size:13px">{mejor_html}</div><div class="stat-label">Mejor día</div></div>
            <div class="stat-cell"><div class="stat-value" style="font-size:13px">{peor_html}</div><div class="stat-label">Peor día</div></div>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Winrate por mes</div>
        <div class="elo-tabla">{_winrate_mes_html(datos["winrate_mes"])}</div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Primera vs. segunda mitad de sesión</div>
        <div class="grid-3">
            <div class="stat-cell"><div class="stat-value" style="color:{delta_color}">{delta_txt}</div><div class="stat-label">Delta promedio</div></div>
            <div class="stat-cell" style="grid-column: span 2"><div class="stat-value" style="font-size:14px; color:{delta_color}">{interpretacion}</div><div class="stat-label">Interpretación</div></div>
        </div>
    </div>
    '''


def _peso_barra_html(peso, width=100):
    if peso is None:
        return '<div class="muted" style="font-size:11px">sin dato</div>'
    color = GREEN if peso >= 0 else RED
    centro = width / 2
    frac = min(abs(peso), 1.0) * (centro - 2)
    left = centro if peso >= 0 else centro - frac
    return f'''
    <div class="peso-track">
        <div class="peso-centro"></div>
        <div class="peso-fill" style="left:{left:.1f}px; width:{frac:.1f}px; background:{color}"></div>
    </div>
    <span style="font-size:11px; color:{color}">{peso:+.2f}</span>
    '''


def _rendimiento_tabla_html(hist: dict, quinc: dict, n_quincena: int) -> str:
    filas = []
    for campo, signo in METRICAS_EJECUCION.items():
        media_h, _desvio_h, _n_h = hist.get(campo, (None, None, 0))
        media_q, _desvio_q, _n_q = quinc.get(campo, (None, None, 0))
        nombre = METRICA_LABELS.get(campo, campo)

        txt_h = fmt_metrica(campo, media_h, es_promedio=True)

        if n_quincena < MUESTRA_MINIMA_PESO:
            txt_q, color_q = "faltan datos", MUTED
        elif media_q is None:
            txt_q, color_q = "—", MUTED
        else:
            txt_q, color_q = fmt_metrica(campo, media_q, es_promedio=True), TEXT

        if media_h is not None and media_q is not None and n_quincena >= MUESTRA_MINIMA_PESO:
            delta = (media_q - media_h) * signo
            if abs(delta) < 0.01:
                tend_txt, tend_color = "→ estable", MUTED
            elif delta > 0:
                tend_txt, tend_color = f"↑ mejorando ({delta:+.2f})", GREEN
            else:
                tend_txt, tend_color = f"↓ bajando ({delta:+.2f})", RED
        else:
            tend_txt, tend_color = "—", MUTED

        filas.append(f'''
        <div class="rend-row">
            <span class="rend-nombre">{nombre}</span>
            <span class="rend-valor">{txt_h}</span>
            <span class="rend-valor" style="color:{color_q}">{txt_q}</span>
            <span class="rend-tend" style="color:{tend_color}">{tend_txt}</span>
        </div>
        ''')
    return "".join(filas)


def _rendimiento_lista_html(recientes: list) -> str:
    if not recientes:
        return '<div class="muted">No hay partidas completas todavía.</div>'
    filas = []
    for p in recientes:
        fecha_corta = (p.get("fecha") or "—")[:10]
        rival = p.get("rival_code") or "—"
        mis, riv = p.get("mis_stocks"), p.get("stocks_rival")
        marcador = f"{mis}-{riv}" if mis is not None and riv is not None else "N/D"
        color_res = GREEN if p.get("resultado") == "victoria" else RED
        etiqueta, color_e = _etiqueta_peso(p["peso"])
        filas.append(f'''
        <div class="peso-row">
            <span class="peso-fecha">{fecha_corta}</span>
            <span class="peso-rival">{rival}</span>
            <span class="peso-marcador" style="color:{color_res}">{marcador}</span>
            <div class="peso-barra-cont">{_peso_barra_html(p["peso"])}</div>
            <span class="peso-etiqueta" style="color:{color_e}">{etiqueta}</span>
        </div>
        ''')
    return "".join(filas)


def rendimiento_html(datos: dict) -> str:
    status = (
        f'{datos["n_mecanica"]} partidas analizadas · última quincena: '
        f'{datos["n_quincena"]} (mínimo {MUESTRA_MINIMA_PESO})'
    )

    promedio_html = ""
    if datos["promedio_recientes"] is not None:
        promedio_html = (
            f'<div class="muted" style="text-align:right; margin-top:8px">'
            f'Promedio de estas {datos["n_promedio"]}: {datos["promedio_recientes"]:+.2f}</div>'
        )

    return f'''
    <h1>Rendimiento</h1>
    <div class="muted" style="margin-bottom:12px">{status}</div>

    <div class="card">
        <div class="card-title">Histórico vs. última quincena</div>
        <div class="rend-header">
            <span class="rend-nombre">Métrica</span>
            <span class="rend-valor">Histórico</span>
            <span class="rend-valor">Últ. quincena</span>
            <span class="rend-tend">Tendencia</span>
        </div>
        {_rendimiento_tabla_html(datos["hist"], datos["quincena"], datos["n_quincena"])}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">⚖ Últimas 20 partidas -- ¿el resultado fue merecido?</div>
        <div class="peso-header">
            <span class="peso-fecha">Fecha</span>
            <span class="peso-rival">Rival</span>
            <span class="peso-marcador">Result.</span>
            <span class="peso-barra-cont">contra ← → favor</span>
            <span class="peso-etiqueta"></span>
        </div>
        {_rendimiento_lista_html(datos["recientes"])}
        {promedio_html}
    </div>
    '''


def _podio_rivales_html(top: list) -> str:
    if not top:
        return '<div class="muted">Sin rivales todavía.</div>'
    medallas = ["🥇", "🥈", "🥉"]
    tarjetas = "".join(
        f'''
        <div class="char-col">
            <div class="medal">{medallas[i]}</div>
            <div class="char-nombre">{r["connect_code"]}</div>
            <div class="char-cifra">{r["veces"]} enfrentamientos</div>
        </div>
        '''
        for i, r in enumerate(top)
    )
    return f'<div class="char-row">{tarjetas}</div>'


def _extremo_html(titulo: str, extremo) -> str:
    if not extremo:
        return f'''
        <div class="dominio-row">
            <div class="dominio-nombre">{titulo}</div>
            <div class="dominio-sub">Sin datos todavía</div>
        </div>
        '''
    color = TRAMO_COLORES.get(extremo["tramo_grande"], MUTED)
    return f'''
    <div class="dominio-row">
        <div>
            <div class="dominio-sub">{titulo}</div>
            <div class="dominio-nombre">{extremo["connect_code"]}</div>
            <div class="dominio-sub">{extremo["veces"]} enfrentamiento{"s" if extremo["veces"] != 1 else ""}</div>
        </div>
        <div style="text-align:right">
            <div style="font-weight:700; color:{color}">{extremo["elo"]:.0f}</div>
            <div class="dominio-sub">{extremo["tramo_grande"] or "?"}</div>
        </div>
    </div>
    '''


def _vs_campo_html(vs) -> str:
    if not vs:
        return '<div class="muted">Actualizá tu ELO y procesá partidas de ranked para ver esta comparación.</div>'
    pa, pb = vs["pct_arriba"], vs["pct_abajo"]
    diff_media = vs["media_ranked"] - vs["mi_elo"]
    return f'''
    <div class="muted" style="margin-bottom:6px">Tu ELO actual: <b style="color:{TEXT}">{vs["mi_elo"]:.0f}</b> · comparado contra {vs["total_ranked"]} rival{"es" if vs["total_ranked"] != 1 else ""} de ranked</div>
    <div class="vscampo-track">
        <div class="vscampo-fill" style="width:{pa}%; background:{RED}"></div>
        <div class="vscampo-fill" style="width:{pb}%; background:{GREEN}"></div>
    </div>
    <div style="display:flex; justify-content:space-between; font-size:11px; margin-top:4px">
        <span style="color:{RED}">🔴 {pa}% de mayor nivel</span>
        <span style="color:{GREEN}">🟢 {pb}% de menor nivel</span>
    </div>
    <div class="muted" style="margin-top:8px">ELO promedio de rivales en ranked: {vs["media_ranked"]:.0f} · mediana: {vs["mediana_ranked"]:.0f} · diferencia promedio: {diff_media:+.0f}</div>
    '''


def _histograma_tramo_html(histograma: dict) -> str:
    valores = [histograma.get(t, 0) for t in TRAMO_COLORES.keys()]
    if not any(valores):
        return '<div class="muted">Sin rivales con ELO cargado todavía.</div>'
    max_val = max(valores)
    filas = []
    for tramo in TRAMO_COLORES.keys():
        cnt = histograma.get(tramo, 0)
        pct = round(100 * cnt / max_val) if max_val else 0
        color = TRAMO_COLORES[tramo]
        filas.append(f'''
        <div class="grade-row">
            <span style="width:64px; font-size:12px; color:{MUTED}">{tramo}</span>
            <div class="grade-barra-bg"><div class="grade-barra-fg" style="width:{pct}%; background:{color}"></div></div>
            <span class="grade-cifra">{cnt}</span>
        </div>
        ''')
    return "".join(filas)


def _resultados_tramo_html(resultados: dict) -> str:
    filas = []
    for tramo in TRAMO_COLORES.keys():
        r = resultados.get(tramo)
        if not r or (r["v"] + r["d"]) == 0:
            continue
        total = r["v"] + r["d"]
        wr = round(100 * r["v"] / total, 1)
        color_wr = GREEN if wr >= 50 else RED
        diff_txt = f' · {r["diff"]:+.1f} stocks prom.' if r["diff"] is not None else ""
        filas.append(f'''
        <div class="dominio-row">
            <div>
                <div class="dominio-nombre" style="color:{TRAMO_COLORES[tramo]}">{tramo}</div>
                <div class="dominio-sub">{total} enfrentamiento{"s" if total != 1 else ""}</div>
            </div>
            <div style="text-align:right">
                <div style="font-weight:700; color:{color_wr}">{r["v"]}V - {r["d"]}D</div>
                <div class="dominio-sub">{wr}% WR{diff_txt}</div>
            </div>
        </div>
        ''')
    if not filas:
        return '<div class="muted">Sin resultados por tramo todavía.</div>'
    return "".join(filas)


def _bandas_elo_html(bandas: list) -> str:
    if not bandas or all((b["v"] + b["d"]) == 0 for b in bandas):
        return '<div class="muted">Sin partidas de ranked con ELO registrado todavía.</div>'
    filas = []
    for b in bandas:
        total = b["v"] + b["d"]
        if total == 0:
            filas.append(f'<div class="banda-row"><span class="banda-label">{b["label"]}</span><span class="muted">sin partidas</span></div>')
            continue
        wr = round(100 * b["v"] / total, 1)
        color = GREEN if wr >= 50 else (MUTED if wr >= 30 else RED)
        filas.append(f'''
        <div class="banda-row">
            <span class="banda-label">{b["label"]}</span>
            <span style="font-family:'Consolas',monospace">{b["v"]}V - {b["d"]}D</span>
            <span style="color:{color}; font-weight:600; margin-left:12px">{wr}% WR</span>
            <span class="dominio-sub" style="margin-left:12px">{total} partida{"s" if total != 1 else ""}</span>
        </div>
        ''')
    return "".join(filas)


def mi_estadistica_html(datos: dict) -> str:
    ultima_txt = fmt_dias_desde(datos["ultima_actualizacion"]) if datos["ultima_actualizacion"] else "nunca actualizado"
    media_txt = f'{datos["media_ponderada"]:.0f}' if datos["media_ponderada"] is not None else "N/D"
    mediana_txt = f'{datos["mediana_ponderada"]:.0f}' if datos["mediana_ponderada"] is not None else "N/D"

    return f'''
    <h1>Mi Estadística</h1>

    <div class="card">
        <div class="card-title">Resumen</div>
        <div class="grid-3">
            <div class="stat-cell"><div class="stat-value">{media_txt}</div><div class="stat-label">ELO prom. ponderado</div></div>
            <div class="stat-cell"><div class="stat-value">{mediana_txt}</div><div class="stat-label">ELO mediana ponderado</div></div>
            <div class="stat-cell"><div class="stat-value">{datos["total_rivales"]}</div><div class="stat-label">Rivales únicos</div></div>
        </div>
        <div class="grid-3" style="margin-top:12px">
            <div class="stat-cell"><div class="stat-value">{datos["sin_ranked"]}</div><div class="stat-label">Sin perfil ranked</div></div>
            <div class="stat-cell"><div class="stat-value">{datos["sin_datos"]}</div><div class="stat-label">Sin consultar todavía</div></div>
            <div class="stat-cell"><div class="stat-value" style="font-size:13px" title="{datos["ultima_actualizacion"] or ""}">{ultima_txt}</div><div class="stat-label">Última actualización</div></div>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Top rivales frecuentes</div>
        {_podio_rivales_html(datos["top_frecuentes"])}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Extremos de nivel</div>
        <div class="dominio-lista">
            {_extremo_html("Rival más fuerte enfrentado", datos["mas_fuerte"])}
            {_extremo_html("Rival más débil enfrentado", datos["mas_debil"])}
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Vos vs. el campo (solo ranked)</div>
        {_vs_campo_html(datos["vs_campo"])}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Resultados por diferencia real de ELO (ranked)</div>
        {_bandas_elo_html(datos["bandas_elo"])}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Rivales por tramo de rango</div>
        {_histograma_tramo_html(datos["histograma_tramo"])}
    </div>

    <div class="card" style="margin-top:16px">
        <div class="card-title">Resultados por tramo de rango</div>
        {_resultados_tramo_html(datos["resultados_tramo"])}
    </div>
    '''


ORDEN_RIVALES_OPCIONES = [
    ("veces_desc", "🔽 Más jugados"),
    ("veces_asc", "🔼 Menos jugados"),
    ("rango_desc", "⭐ Rango más alto"),
    ("rango_asc", "🔻 Rango más bajo"),
    ("winrate_asc", "😰 Peor winrate"),
    ("winrate_desc", "😤 Mejor winrate"),
]


def _ordenar_rivales(lista: list, orden: str) -> list:
    if orden == "veces_asc":
        return sorted(lista, key=lambda r: r["veces"])
    if orden == "rango_desc":
        con = [r for r in lista if r["elo"] is not None]
        sin = [r for r in lista if r["elo"] is None]
        return sorted(con, key=lambda r: r["elo"], reverse=True) + sin
    if orden == "rango_asc":
        con = [r for r in lista if r["elo"] is not None]
        sin = [r for r in lista if r["elo"] is None]
        return sorted(con, key=lambda r: r["elo"]) + sin
    if orden == "winrate_asc":
        con = [r for r in lista if r["winrate"] is not None]
        sin = [r for r in lista if r["winrate"] is None]
        return sorted(con, key=lambda r: r["winrate"]) + sin
    if orden == "winrate_desc":
        con = [r for r in lista if r["winrate"] is not None]
        sin = [r for r in lista if r["winrate"] is None]
        return sorted(con, key=lambda r: r["winrate"], reverse=True) + sin
    return sorted(lista, key=lambda r: r["veces"], reverse=True)  # veces_desc, default


def _orden_botones_html(orden_actual: str) -> str:
    botones = []
    for modo, etiqueta in ORDEN_RIVALES_OPCIONES:
        clase_activa = " orden-activo" if modo == orden_actual else ""
        botones.append(
            f'<button class="orden-btn{clase_activa}" onclick="cambiarTab(\'rivales:{modo}\')">{etiqueta}</button>'
        )
    return "".join(botones)


def _rival_card_html(r: dict) -> str:
    con_resultado = r["victorias"] + r["derrotas"]
    if con_resultado:
        wr_color = GREEN if (r["winrate"] or 0) >= 50 else RED
        sub = f'{r["winrate"]}% WR'
        if r["diff_stocks"] is not None:
            sub += f' · {r["diff_stocks"]:+.1f} stocks prom.'
        medio_html = (f'<div style="font-weight:700; color:{wr_color}">{r["victorias"]}V - {r["derrotas"]}D</div>'
                      f'<div class="dominio-sub">{sub}</div>')
    else:
        medio_html = '<div class="dominio-sub">Sin resultado</div>'

    if r["estado"] == "ranked":
        color = TRAMO_COLORES.get(r["tramo_grande"], MUTED)
        derecha_html = (f'<div style="font-weight:700; color:{color}">{r["elo"]:.0f}</div>'
                        f'<div class="dominio-sub" style="color:{color}">{r["rank_tier"] or ""}</div>')
    elif r["estado"] == "sin_ranked":
        derecha_html = '<div class="dominio-sub">Sin perfil ranked</div>'
    else:
        derecha_html = '<div class="dominio-sub">Sin datos</div>'

    return f'''
    <div class="rival-card">
        <div>
            <div class="dominio-nombre">{r["connect_code"]}</div>
            <div class="dominio-sub">{r["veces"]} enfrentamiento{"s" if r["veces"] != 1 else ""}</div>
        </div>
        <div style="text-align:center">{medio_html}</div>
        <div style="text-align:right">{derecha_html}</div>
    </div>
    '''


def rivales_html(rivales: list, orden: str) -> str:
    if not rivales:
        return '''
        <h1>Rivales</h1>
        <div class="card"><div class="muted">Sin rivales todavía. Procesá tus replays y volvé acá.</div></div>
        '''

    ordenados = _ordenar_rivales(rivales, orden)
    tarjetas = "".join(_rival_card_html(r) for r in ordenados)

    return f'''
    <h1>Rivales</h1>

    <div class="orden-row">{_orden_botones_html(orden)}</div>

    <div class="rival-lista">{tarjetas}</div>
    '''


def render_tab_html(datos: dict, tab: str) -> str:
    """Arma el HTML de la pestaña pedida a partir del dict que ya devuelve
    cargar_datos() -- trae GENERAL y PERSONAJES en el mismo viaje a la
    base (ver "personajes" dentro de cargar_datos()), asi que cambiar de
    pestaña no dispara una consulta distinta a la que ya se hizo antes.
    `tab` puede venir con un parametro extra separado por ":" (ej.
    "rivales:winrate_desc" para el orden de la lista de RIVALES) -- las
    demas pestañas ignoran ese extra."""
    base, _, extra = tab.partition(":")
    if base == "personajes":
        return personajes_html(datos["personajes"])
    if base == "progreso":
        return progreso_html(datos["progreso"])
    if base == "historial":
        return historial_html(datos["historial"])
    if base == "rendimiento":
        return rendimiento_html(datos["rendimiento"])
    if base == "mi_estadistica":
        return mi_estadistica_html(datos["mi_estadistica"])
    if base == "rivales":
        return rivales_html(datos["rivales"], extra or "veces_desc")
    return render_body_html(datos)


def render_html(datos: dict) -> str:
    return f'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<style>
    * {{ box-sizing: border-box; }}
    body {{
        background: {BG}; color: {TEXT}; font-family: 'Segoe UI', system-ui, sans-serif;
        margin: 0; padding: 24px;
    }}
    h1 {{ font-size: 20px; font-weight: 600; margin: 0 0 16px 0; }}
    .subtitle {{ color: {MUTED}; font-size: 13px; margin: -12px 0 20px 0; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .grid-2 {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
    .grid-3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
    .grid-7 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 12px; }}
    .card {{
        background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px;
        padding: 18px; grid-column: span 2;
    }}
    .card-half {{ grid-column: span 1; }}
    .card-title {{ font-size: 13px; color: {MUTED}; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }}
    .resultado {{ font-size: 22px; font-weight: 700; }}
    .detalle {{ color: {MUTED}; font-size: 13px; margin: 6px 0 12px 0; }}
    .sub-title {{ font-size: 12px; color: {MUTED}; margin-top: 14px; margin-bottom: 6px; }}
    .pills {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }}
    .pill {{
        background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 999px;
        padding: 4px 12px; font-size: 12px; color: {ACCENT2};
    }}
    .pill-muted {{ color: {MUTED}; }}
    .analisis-cols {{
        display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
        margin-top: 10px; padding-top: 10px; border-top: 1px solid {BORDER};
    }}
    .analisis-cols .sub-title {{ margin-top: 0; }}
    .metricas-list {{ margin: 0; padding-left: 18px; font-size: 13px; }}
    .metricas-list li {{ margin-bottom: 4px; }}
    .elo-badge {{
        display: inline-flex; flex-direction: column; align-items: center; gap: 2px;
        border: 1px solid; border-radius: 10px; padding: 10px 20px; flex-shrink: 0;
    }}
    .elo-badge-tramo {{ font-size: 13px; font-weight: 600; }}
    .elo-badge-valor {{ font-size: 20px; font-weight: 700; }}
    .perfil-row {{ display: flex; align-items: center; justify-content: center; gap: 16px; flex-wrap: wrap; }}
    .perfil-identidad {{ display: flex; align-items: center; gap: 10px; }}
    .perfil-avatar {{
        border-radius: 50%; background: {SURFACE2}; border: 2px solid {BORDER};
        padding: 3px; box-sizing: content-box;
    }}
    .perfil-nombre {{ font-size: 16px; font-weight: 700; color: {TEXT}; }}
    .killmove-card {{
        background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 10px;
        padding: 12px; margin-bottom: 8px;
    }}
    .killmove-card:last-child {{ margin-bottom: 0; }}
    .killmove-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }}
    .killmove-cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .killmove-titulo {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 4px; }}
    .killmove-row {{ display: flex; justify-content: space-between; font-size: 12px; padding: 2px 0; }}
    .killmove-nombre {{ color: {TEXT}; }}
    .killmove-pct {{ color: {MUTED}; font-family: 'Consolas', monospace; }}
    .stat-cell {{
        background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
        padding: 12px; text-align: center;
    }}
    .stat-value {{ font-size: 18px; font-weight: 700; color: {ACCENT2}; }}
    .stat-label {{ font-size: 11px; color: {MUTED}; margin-top: 4px; }}
    .muted {{ color: {MUTED}; }}
    .actions {{ display: flex; gap: 10px; margin-bottom: 18px; }}
    .btn {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px;
        padding: 8px 16px; font-size: 13px; cursor: pointer; font-family: inherit;
    }}
    .btn:hover {{ border-color: {ACCENT}; }}
    .btn:disabled {{ opacity: 0.5; cursor: default; }}
    .btn-primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
    .toast {{
        position: fixed; bottom: 20px; right: 20px; background: {SURFACE2}; border: 1px solid {BORDER};
        border-radius: 8px; padding: 12px 18px; font-size: 13px; display: none; max-width: 400px;
    }}
    .progreso-container {{ margin-bottom: 18px; display: none; }}
    .progreso-texto {{ font-size: 12px; color: {MUTED}; margin-bottom: 6px; }}
    .progreso-track {{ height: 6px; background: {SURFACE2}; border-radius: 3px; overflow: hidden; }}
    .progreso-barra {{ height: 100%; background: {ACCENT}; border-radius: 3px; width: 0%; transition: width 0.2s; }}
    .progreso-barra.indeterminado {{
        background: repeating-linear-gradient(45deg, {ACCENT}, {ACCENT} 10px, {ACCENT2} 10px, {ACCENT2} 20px);
        animation: mover-rayas 1s linear infinite;
    }}
    @keyframes mover-rayas {{ from {{ background-position: 0 0; }} to {{ background-position: 28px 0; }} }}
    .tabs {{ display: flex; gap: 6px; margin-bottom: 16px; }}
    .tab-btn {{
        background: {SURFACE}; color: {MUTED}; border: 1px solid {BORDER}; border-radius: 8px;
        padding: 8px 18px; font-size: 13px; font-weight: 600; letter-spacing: 0.03em;
        cursor: pointer; font-family: inherit;
    }}
    .tab-btn:hover {{ border-color: {ACCENT}; }}
    .tab-activo {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
    .char-row {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .char-col {{
        flex: 1; min-width: 88px; background: {SURFACE2}; border: 1px solid {BORDER};
        border-radius: 10px; padding: 12px 8px; text-align: center;
    }}
    .medal {{ font-size: 20px; }}
    .char-nombre {{ font-size: 12px; color: {TEXT}; margin: 6px 0 2px 0; }}
    .char-cifra {{ font-size: 11px; color: {MUTED}; }}
    .grid-4 {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
    .elo-tabla {{ display: flex; flex-direction: column; gap: 2px; }}
    .elo-row {{
        display: flex; justify-content: space-between; font-size: 12px;
        padding: 5px 0; border-bottom: 1px solid {BORDER};
    }}
    .elo-row:last-child {{ border-bottom: none; }}
    .elo-fecha {{ color: {MUTED}; }}
    .elo-valor {{ color: {TEXT}; font-weight: 600; }}
    .elo-delta {{ min-width: 44px; text-align: right; }}
    .grade-resumen {{ display: flex; gap: 24px; flex-wrap: wrap; }}
    .grade-promedio {{ flex: 0 0 140px; }}
    .grade-letra-grande {{ font-size: 42px; font-weight: 700; line-height: 1; margin: 4px 0; }}
    .grade-dist {{ flex: 1; min-width: 220px; }}
    .grade-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }}
    .grade-letra {{ width: 16px; font-size: 12px; font-weight: 700; }}
    .grade-barra-bg {{ flex: 1; height: 8px; background: {BORDER}; border-radius: 4px; overflow: hidden; }}
    .grade-barra-fg {{ height: 100%; border-radius: 4px; }}
    .grade-cifra {{ font-size: 11px; color: {MUTED}; width: 74px; text-align: right; }}
    .dominio-lista {{ display: flex; flex-direction: column; gap: 6px; }}
    .dominio-row {{
        display: flex; justify-content: space-between; align-items: center;
        background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 12px;
    }}
    .dominio-grade {{ font-weight: 700; margin-right: 6px; }}
    .dominio-nombre {{ font-size: 13px; color: {TEXT}; font-weight: 600; }}
    .dominio-sub {{ font-size: 11px; color: {MUTED}; }}
    .matchup-row {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .matchup-card {{
        flex: 1; min-width: 140px; background: {SURFACE2}; border: 1px solid {BORDER};
        border-radius: 10px; padding: 10px 12px;
    }}
    .matchup-nombre {{ font-size: 13px; font-weight: 600; color: {TEXT}; }}
    .dia-semana-row {{ display: flex; gap: 6px; }}
    .dia-col {{
        flex: 1; background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 8px;
        padding: 8px 4px; text-align: center;
    }}
    .dia-nombre {{ font-size: 11px; color: {MUTED}; }}
    .dia-wr {{ font-size: 13px; font-weight: 700; margin-top: 2px; }}
    .dia-total {{ font-size: 10px; color: {MUTED}; margin-top: 2px; }}
    .char-icon {{ border-radius: 6px; display: inline-block; vertical-align: middle; object-fit: contain; margin-right: 4px; }}
    .rend-header, .rend-row {{
        display: grid; grid-template-columns: 2fr 1fr 1fr 1.4fr; gap: 8px;
        align-items: center; padding: 6px 0; font-size: 12px;
    }}
    .rend-header {{ color: {MUTED}; border-bottom: 1px solid {BORDER}; font-size: 11px; text-transform: uppercase; }}
    .rend-row {{ border-bottom: 1px solid {BORDER}; }}
    .rend-row:last-child {{ border-bottom: none; }}
    .rend-nombre {{ color: {TEXT}; }}
    .rend-valor {{ color: {TEXT}; font-family: 'Consolas', monospace; text-align: center; }}
    .rend-tend {{ font-size: 11px; text-align: right; }}
    .peso-header, .peso-row {{
        display: grid; grid-template-columns: 0.9fr 1fr 0.7fr 1.2fr 1.3fr; gap: 8px;
        align-items: center; padding: 6px 0; font-size: 12px;
    }}
    .peso-header {{ color: {MUTED}; border-bottom: 1px solid {BORDER}; font-size: 11px; text-transform: uppercase; }}
    .peso-row {{ border-bottom: 1px solid {BORDER}; }}
    .peso-row:last-child {{ border-bottom: none; }}
    .peso-fecha {{ color: {MUTED}; font-family: 'Consolas', monospace; font-size: 11px; }}
    .peso-rival {{ color: {TEXT}; }}
    .peso-marcador {{ font-family: 'Consolas', monospace; font-weight: 600; text-align: center; }}
    .peso-barra-cont {{ display: flex; flex-direction: column; gap: 2px; }}
    .peso-etiqueta {{ font-size: 11px; }}
    .peso-track {{ position: relative; height: 8px; background: {SURFACE2}; border-radius: 4px; overflow: hidden; width: 100px; }}
    .peso-centro {{ position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: {BORDER}; }}
    .peso-fill {{ position: absolute; top: 1px; bottom: 1px; border-radius: 3px; }}
    .vscampo-track {{ display: flex; height: 14px; border-radius: 7px; overflow: hidden; background: {SURFACE2}; }}
    .vscampo-fill {{ height: 100%; }}
    .banda-row {{
        display: flex; justify-content: space-between; align-items: center;
        padding: 6px 0; border-bottom: 1px solid {BORDER}; font-size: 12px;
    }}
    .banda-row:last-child {{ border-bottom: none; }}
    .banda-label {{ color: {TEXT}; flex: 1; }}
    .orden-row {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }}
    .orden-btn {{
        background: {SURFACE}; color: {MUTED}; border: 1px solid {BORDER}; border-radius: 8px;
        padding: 6px 12px; font-size: 12px; cursor: pointer; font-family: inherit;
    }}
    .orden-btn:hover {{ border-color: {ACCENT}; }}
    .orden-activo {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
    .rival-lista {{ display: flex; flex-direction: column; gap: 6px; }}
    .rival-card {{
        display: grid; grid-template-columns: 1.4fr 1fr 1fr; gap: 8px; align-items: center;
        background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 10px 14px;
    }}
</style>
</head>
<body>
    <div class="tabs">
        <button class="tab-btn tab-activo" data-tab="general" onclick="cambiarTab('general')">GENERAL</button>
        <button class="tab-btn" data-tab="personajes" onclick="cambiarTab('personajes')">PERSONAJES</button>
        <button class="tab-btn" data-tab="progreso" onclick="cambiarTab('progreso')">PROGRESO</button>
        <button class="tab-btn" data-tab="historial" onclick="cambiarTab('historial')">HISTORIAL</button>
        <button class="tab-btn" data-tab="rendimiento" onclick="cambiarTab('rendimiento')">RENDIMIENTO</button>
        <button class="tab-btn" data-tab="mi_estadistica" onclick="cambiarTab('mi_estadistica')">MI ESTADISTICA</button>
        <button class="tab-btn" data-tab="rivales" onclick="cambiarTab('rivales')">RIVALES</button>
    </div>

    <div class="actions">
        <button class="btn" onclick="refrescar()">↻ Refrescar</button>
        <button class="btn btn-primary" id="btn-procesar" onclick="procesarNuevos()">▶ Procesar replays nuevos</button>
    </div>
    <div class="progreso-container" id="progreso-container">
        <div class="progreso-texto" id="progreso-texto"></div>
        <div class="progreso-track"><div class="progreso-barra" id="progreso-barra"></div></div>
    </div>

    <div id="app-content">{render_tab_html(datos, "general")}</div>
    <div class="toast" id="toast"></div>

    <script>
        let tabActual = 'general';

        function mostrarToast(texto, ms) {{
            const t = document.getElementById('toast');
            t.textContent = texto;
            t.style.display = 'block';
            setTimeout(() => {{ t.style.display = 'none'; }}, ms || 3000);
        }}

        function actualizarProgreso(pct, texto) {{
            const cont = document.getElementById('progreso-container');
            const label = document.getElementById('progreso-texto');
            const barra = document.getElementById('progreso-barra');
            if (!cont) return;  // el contenido se acaba de reemplazar, este frame ya no aplica
            cont.style.display = 'block';
            label.textContent = texto;
            if (pct === null) {{
                barra.classList.add('indeterminado');
                barra.style.width = '100%';
            }} else {{
                barra.classList.remove('indeterminado');
                barra.style.width = pct + '%';
            }}
        }}

        function ocultarProgreso() {{
            const cont = document.getElementById('progreso-container');
            if (cont) cont.style.display = 'none';
        }}

        async function cambiarTab(tab) {{
            tabActual = tab;
            const base = tab.split(':')[0];
            document.querySelectorAll('.tab-btn').forEach(b => {{
                b.classList.toggle('tab-activo', b.dataset.tab === base);
            }});
            const html = await window.pywebview.api.refrescar(tab);
            document.getElementById('app-content').innerHTML = html;
        }}

        async function refrescar() {{
            const html = await window.pywebview.api.refrescar(tabActual);
            document.getElementById('app-content').innerHTML = html;
        }}

        async function procesarNuevos() {{
            const btn = document.getElementById('btn-procesar');
            btn.disabled = true;
            btn.textContent = 'Procesando...';
            actualizarProgreso(0, 'Iniciando...');

            // Polling en vez de que Python empuje actualizaciones desde su
            // hilo de fondo -- evita el cruce de hilos que causaba el error
            // de COM en Windows. JS pregunta, nunca al reves.
            const intervalo = setInterval(async () => {{
                try {{
                    const p = await window.pywebview.api.obtener_progreso();
                    actualizarProgreso(p.pct, p.texto);
                }} catch (e) {{ /* ventana cerrandose u otra condicion transitoria, se ignora */ }}
            }}, 300);

            try {{
                const respuesta = await window.pywebview.api.procesar_nuevos(tabActual);
                clearInterval(intervalo);
                const s = respuesta.resumen;

                // El HTML se aplica siempre, haya o no replays nuevos --
                // consultar_rangos/backfill_elo pueden haber actualizado
                // datos (ej. ELO vencido por TTL) aunque no haya .slp
                // nuevos, y antes esos cambios quedaban calculados en la
                // base pero invisibles hasta el proximo refresh manual.
                document.getElementById('app-content').innerHTML = respuesta.html;
                ocultarProgreso();
                btn.disabled = false;
                btn.textContent = '▶ Procesar replays nuevos';

                if (s.rangos && s.rangos.error === 'sin_requests') {{
                    mostrarToast('Falta instalar "requests" (pip install requests) -- se procesaron los replays igual', 6000);
                }} else if (s.replays.procesados > 0) {{
                    mostrarToast(
                        `${{s.replays.procesados}} replay(s) nuevo(s) · ` +
                        `${{s.rangos.ok || 0}} rango(s) actualizado(s) · ` +
                        `${{s.elo_at_match.exactos + s.elo_at_match.aproximados}} elo_at_match completado(s) · ` +
                        `${{s.sets.sets}} sets`,
                        6000
                    );
                }} else {{
                    mostrarToast(`Sin replays nuevos (${{s.replays.encontrados}} encontrados, todos ya procesados)`, 4000);
                }}
            }} catch (err) {{
                clearInterval(intervalo);
                ocultarProgreso();
                btn.disabled = false;
                btn.textContent = '▶ Procesar replays nuevos';
                mostrarToast('Error al procesar: ' + err, 5000);
            }}
        }}
    </script>
</body>
</html>'''


class Api:
    """
    Metodos expuestos a JS via pywebview.api.<metodo>(...). Cada uno
    abre su propia conexion (sqlite3 no es thread-safe para compartir
    una conexion entre el hilo de pywebview y el de la ventana).

    El progreso NO se empuja con window.evaluate_js() desde el hilo de
    fondo -- eso fue la causa mas probable del error COM/hilo que
    aparecio en Windows ("solo se puede acceder desde el hilo que lo
    creo"). En cambio, procesar_nuevos() corre en su hilo y solo
    actualiza self._progreso (una variable en memoria); JS pregunta
    "como vas" cada tanto via obtener_progreso() -- todas las llamadas
    JS->Python son del tipo que pywebview ya soporta bien, nunca al reves.
    """

    def __init__(self, my_code: str, folder: str):
        self.my_code = my_code
        self.folder = Path(folder)
        self._progreso = {"pct": None, "texto": ""}

    def _set_progreso(self, etapa: str, actual: int = None, total: int = None):
        if total:
            self._progreso = {"pct": int(100 * actual / total), "texto": f"{etapa} ({actual}/{total})"}
        else:
            self._progreso = {"pct": None, "texto": etapa}

    def obtener_progreso(self):
        return self._progreso

    def refrescar(self, tab="general"):
        conn = sqlite3.connect(DB_PATH)
        try:
            datos = cargar_datos(conn, self.my_code)
        finally:
            conn.close()
        return render_tab_html(datos, tab)

    def procesar_nuevos(self, tab="general"):
        """
        Pipeline completo, en cadena: procesar replays nuevos -> consultar
        rangos de jugadores nuevos/vencidos -> completar elo_at_match ->
        rearmar sets. Un solo click, todo queda consistente -- nada de
        pasos manuales sueltos en la terminal. Empuja progreso en vivo
        a la ventana mientras corre (no hay que esperar mudo al final).
        `tab` indica que pestaña esta activa en JS, para devolver el HTML
        de esa pestaña al terminar (y no siempre GENERAL sin importar
        donde estaba parado el usuario).
        """
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        resumen = {}
        try:
            contador = {"actual": 0, "total": 0}

            def on_progress_replays(evento, archivo, detalle):
                if evento == "inicio":
                    contador["total"] = detalle
                    self._set_progreso("Procesando replays", 0, detalle)
                else:
                    contador["actual"] += 1
                    self._set_progreso("Procesando replays", contador["actual"], contador["total"])

            resumen["replays"] = process_replays.procesar_pendientes(
                conn, self.folder, self.my_code, on_progress=on_progress_replays
            )

            contador2 = {"actual": 0, "total": 0}

            def on_progress_rangos(evento, code, detalle):
                if evento == "inicio":
                    contador2["total"] = detalle
                    self._set_progreso("Consultando rangos", 0, detalle)
                else:
                    contador2["actual"] += 1
                    self._set_progreso("Consultando rangos", contador2["actual"], contador2["total"])

            resumen["rangos"] = consultar_rangos.consultar_pendientes(conn, on_progress=on_progress_rangos)

            contador3 = {"actual": 0, "total": 0}

            def on_progress_elo(evento, _code, detalle):
                if evento == "inicio":
                    contador3["total"] = detalle
                    self._set_progreso("Completando elo_at_match", 0, detalle)
                else:
                    contador3["actual"] += 1
                    self._set_progreso("Completando elo_at_match", contador3["actual"], contador3["total"])

            exactos, aproximados = backfill_elo.backfill_elo_at_match(conn, on_progress=on_progress_elo)
            resumen["elo_at_match"] = {"exactos": exactos, "aproximados": aproximados}

            contador4 = {"actual": 0, "total": 0}

            def on_progress_sets(evento, _rival, detalle):
                if evento == "inicio":
                    contador4["total"] = detalle
                    self._set_progreso("Rearmando sets", 0, detalle)
                else:
                    contador4["actual"] += 1
                    self._set_progreso("Rearmando sets", contador4["actual"], contador4["total"])

            n_sets, n_partidas = build_sets.reconstruir_sets(
                conn, self.my_code, gap_minutos=30, on_progress=on_progress_sets
            )
            resumen["sets"] = {"sets": n_sets, "partidas": n_partidas}
        finally:
            conn.close()

        html = self.refrescar(tab)
        return {"resumen": resumen, "html": html}


def main():
    if not DB_PATH.exists():
        print("No existe melee_tracker.db. Corre primero init_db.py")
        sys.exit(1)

    cfg = ensure_config()
    conn = sqlite3.connect(DB_PATH)

    datos = cargar_datos(conn, cfg["connect_code"])
    conn.close()

    html = render_html(datos)
    api = Api(cfg["connect_code"], cfg["replay_path"])
    webview.create_window("Melee Tracker", html=html, js_api=api, width=1000, height=750, background_color=BG)
    webview.start()


if __name__ == "__main__":
    main()
