"""
formato.py

Funciones y constantes de presentacion compartidas entre app.py e
insignias.py -- viven aca para que ninguno de los dos tenga que
importar al otro (evita import circular).
"""

import slippi.id as sid
from slippi.event import Attack

# Paleta de la app (heredada del tracker viejo)
BG = "#0d0f14"
SURFACE = "#161a22"
SURFACE2 = "#1c2130"
BORDER = "#252933"
TEXT = "#e8eaf0"
MUTED = "#8b93a7"
ACCENT = "#7c6aff"
ACCENT2 = "#a78bfa"
GREEN = "#22c55e"
RED = "#ef4444"

# Colores por tramo grande, para el elo como insignia coloreada
TRAMO_COLORES = {
    "Bronze": "#cd7f32",
    "Silver": "#c0c0c0",
    "Gold": "#ffd700",
    "Platinum": "#40e0d0",
    "Diamond": "#b9f2ff",
    "Master+": ACCENT2,
}

# Colores por tier de insignia (bronce/plata/oro/platino) -- mismo
# lenguaje de color que el tramo de elo, pero es una escala distinta
# (dedicacion, progreso, etc.), no se deben confundir una con otra
# aunque compartan nombres.
TIER_COLORES = {
    "Bronce": "#cd7f32",
    "Plata": "#c0c0c0",
    "Oro": "#ffd700",
    "Platino": "#b9f2ff",
}

CONTEXTO_LABELS = {
    "parejo": "Rival parejo en elo",
    "rival_favorito": "Rival favorito",
    "rival_underdog": "Vos favorito",
    "diferencia_grande_favor_rival": "Rival bastante mejor rankeado",
    "diferencia_grande_favor_mio": "Vos bastante mejor rankeado",
    "diferencia_extrema_favor_rival": "Rival muchisimo mejor rankeado",
    "diferencia_extrema_favor_mio": "Vos muchisimo mejor rankeado",
}

METRICA_LABELS = {
    "l_cancel_rate": "L-Cancel", "apm": "APM", "digital_apm": "APM digital",
    "damage_done": "Danio hecho", "damage_per_opening": "Danio por apertura",
    "openings_per_kill": "Aperturas por kill", "wavedash": "Wavedash",
    "waveland": "Waveland", "dash_dance": "Dash dance", "roll": "Roll",
}

METRICA_FORMATO = {
    "l_cancel_rate": {"escala": 100, "suffix": "%", "dec_valor": 0, "dec_prom": 0},
    "apm": {"escala": 1, "suffix": "", "dec_valor": 0, "dec_prom": 0},
    "digital_apm": {"escala": 1, "suffix": "", "dec_valor": 0, "dec_prom": 0},
    "damage_done": {"escala": 1, "suffix": "%", "dec_valor": 1, "dec_prom": 1},
    "damage_per_opening": {"escala": 1, "suffix": "%", "dec_valor": 1, "dec_prom": 1},
    "openings_per_kill": {"escala": 1, "suffix": "", "dec_valor": 1, "dec_prom": 1},
    "wavedash": {"escala": 1, "suffix": "", "dec_valor": 0, "dec_prom": 1},
    "waveland": {"escala": 1, "suffix": "", "dec_valor": 0, "dec_prom": 1},
    "dash_dance": {"escala": 1, "suffix": "", "dec_valor": 0, "dec_prom": 1},
    "roll": {"escala": 1, "suffix": "", "dec_valor": 0, "dec_prom": 1},
}


def nombre_legible(valor_enum: str) -> str:
    return valor_enum.replace("_", " ").title()


def char_nombre(character_id):
    if character_id is None:
        return "?"
    try:
        return nombre_legible(sid.CSSCharacter(character_id).name)
    except Exception:
        return f"#{character_id}"


def stage_nombre(stage_id):
    if stage_id is None:
        return "?"
    try:
        return nombre_legible(sid.Stage(stage_id).name)
    except Exception:
        return f"#{stage_id}"


def attack_nombre(attack_id):
    """Nombre legible de un movimiento (Attack enum de slippi.id -- NAIR,
    FAIR, BAIR, FSMASH, NEUTRAL_B, FTHROW, etc.) para mostrar "con que
    movimiento" en las stats de kills. None/desconocido -> 'Desconocido'
    (a diferencia de char_nombre/stage_nombre, acá SI puede faltar de
    forma legitima -- ver la limitacion documentada en detectar_kills())."""
    if attack_id is None:
        return "Desconocido"
    try:
        return nombre_legible(Attack(attack_id).name)
    except Exception:
        return f"#{attack_id}"


def fmt_horas(segundos):
    if not segundos:
        return "0h 0m"
    h = int(segundos // 3600)
    m = int((segundos % 3600) // 60)
    return f"{h}h {m}m"


def fmt_seg(segundos):
    if segundos is None:
        return "N/D"
    m = int(segundos // 60)
    s = int(segundos % 60)
    return f"{m}m {s}s"


def fmt_metrica(campo, valor, es_promedio=False):
    if valor is None:
        return "N/D"
    f = METRICA_FORMATO.get(campo, {"escala": 1, "suffix": "", "dec_valor": 1, "dec_prom": 1})
    dec = f["dec_prom"] if es_promedio else f["dec_valor"]
    return f"{valor * f['escala']:.{dec}f}{f['suffix']}"


def fmt_dias_desde(fecha_iso: str) -> str:
    """'hace 3 dias' / 'hoy' / 'hace 1 dia', a partir de un ISO naive-local."""
    if not fecha_iso:
        return "nunca actualizado"
    from datetime import datetime
    fecha = datetime.fromisoformat(fecha_iso)
    dias = (datetime.now() - fecha).days
    if dias <= 0:
        return "hoy"
    if dias == 1:
        return "hace 1 día"
    return f"hace {dias} días"
