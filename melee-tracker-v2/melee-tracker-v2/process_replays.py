#!/usr/bin/env python3
"""
process_replays.py

Procesa los replays .slp nuevos de la carpeta configurada y guarda los
resultados directo en melee_tracker.db (replays + match_players +
players). Reemplaza a analyze_replays.py para el flujo de melee_tracker:
toda la logica de computo de metricas (accion, conversiones, inputs,
extraccion de match_id) esta portada VERBATIM de ese archivo -- viene
de slippi-js oficial y no se toco. Lo que cambia es el destino: no hay
JSON intermedio (dataset_unificado.json desaparece), el ganador y el
game_mode se derivan directo de los frames del propio replay.

Uso:
    python process_replays.py                # usa replay_path/connect_code de config.py
    python process_replays.py --folder ruta   # override puntual de la carpeta
    python process_replays.py --forzar        # reprocesa aunque ya esten en la DB
"""

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import slippi
from slippi.event import LCancel, End

from config import ensure_config

# Fix para consolas de Windows que no usan UTF-8 por defecto
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = Path(__file__).resolve().parent / "melee_tracker.db"

# Subir este numero si cambia la logica de parseo de abajo, para poder
# detectar despues (via replays.analysis_version) que replays quedaron
# procesados con una version vieja y conviene reprocesar.
ANALYSIS_VERSION = 4  # v4: agrega detectar_kills() -- movimiento/direccion/% de cada kill, tabla `kills`

FILENAME_PATTERN = re.compile(r"Game_(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})")

# Extraccion de match_id (portado de pipeline_unificado.py: leer solo el
# inicio del archivo, sin abrir todo el .slp por segunda vez con la libreria)
BYTES_A_LEER = 900
PATRON_MATCH_ID = re.compile(rb"mode\.(ranked|unranked|direct|online)-")


def extraer_match_id(filepath: Path):
    """Devuelve (match_id_completo, tipo) leyendo solo el inicio del archivo."""
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(BYTES_A_LEER)
    except Exception:
        return None, "error_lectura"

    m = PATRON_MATCH_ID.search(chunk)
    if not m:
        return None, "offline_o_sin_match_id"

    inicio = m.start()
    fin = chunk.find(b"\x00", inicio)
    if fin == -1:
        fin = inicio + 60
    match_id = chunk[inicio:fin].decode("ascii", errors="replace")
    tipo = m.group(1).decode("ascii")
    return match_id, tipo

# ==========================================================================
# Constantes de estado (portadas de slippi-js: src/common/stats/common.ts)
# ==========================================================================
S = {
    "DAMAGE_START": 0x4B, "DAMAGE_END": 0x5B,
    "CAPTURE_START": 0xDF, "CAPTURE_END": 0xE8,
    "GROUNDED_CONTROL_START": 0xE, "GROUNDED_CONTROL_END": 0x18,
    "SQUAT_START": 0x27, "SQUAT_END": 0x29,
    "DYING_START": 0x0, "DYING_END": 0xA,
    "CONTROLLED_JUMP_START": 0x18, "CONTROLLED_JUMP_END": 0x22,
    "GROUND_ATTACK_START": 0x2C, "GROUND_ATTACK_END": 0x40,
    "AERIAL_LANDING_START": 0x46, "AERIAL_LANDING_END": 0x4A,
    "ROLL_FORWARD": 0xE9, "ROLL_BACKWARD": 0xEA,
    "SPOT_DODGE": 0xEB, "AIR_DODGE": 0xEC,
    "ACTION_KNEE_BEND": 0x18,
    "DASH": 0x14, "TURN": 0x12,
    "LANDING_FALL_SPECIAL": 0x2B,
    "GRAB": 0xD4,
    "DAMAGE_FALL": 0x26,
    "JAB_RESET_UP": 0xB9, "JAB_RESET_DOWN": 0xC1,
    "CLIFF_CATCH": 0xFC,
    "BARREL_WAIT": 0x125,
    "COMMAND_GRAB_RANGE1_START": 0x10A, "COMMAND_GRAB_RANGE1_END": 0x130,
    "COMMAND_GRAB_RANGE2_START": 0x147, "COMMAND_GRAB_RANGE2_END": 0x152,
}
PUNISH_RESET_FRAMES = 45
FIRST_PLAYABLE_FRAME = -39
DASH_DANCE_PATTERN = [S["DASH"], S["TURN"], S["DASH"]]
STICK_REGION_THRESHOLD = 0.2875
TRIGGER_THRESHOLD = 0.3


def is_in_control(state):
    ground = S["GROUNDED_CONTROL_START"] <= state <= S["GROUNDED_CONTROL_END"]
    squat = S["SQUAT_START"] <= state <= S["SQUAT_END"]
    ground_attack = S["GROUND_ATTACK_START"] < state <= S["GROUND_ATTACK_END"]
    return ground or squat or ground_attack or state == S["GRAB"]


def is_damaged(state):
    return (
        (S["DAMAGE_START"] <= state <= S["DAMAGE_END"])
        or state == S["DAMAGE_FALL"]
        or state == S["JAB_RESET_UP"]
        or state == S["JAB_RESET_DOWN"]
    )


def is_grabbed(state):
    return S["CAPTURE_START"] <= state <= S["CAPTURE_END"]


def is_command_grabbed(state):
    in_range = (
        S["COMMAND_GRAB_RANGE1_START"] <= state <= S["COMMAND_GRAB_RANGE1_END"]
    ) or (S["COMMAND_GRAB_RANGE2_START"] <= state <= S["COMMAND_GRAB_RANGE2_END"])
    return in_range and state != S["BARREL_WAIT"]


# Sub-estados de "muriendo" (0x0-0xA, DYING_START..DYING_END en S de arriba)
# -- accion state REAL del juego, no una heuristica nuestra. 0x0-0x3 son las
# 4 direcciones "puras"; 0x4-0xA son variantes de morir por arriba (star KO,
# distintas animaciones de camara) que agrupamos como "up" -- para el
# proposito de "hacia donde saliste volando" son todas lo mismo.
DEATH_DIRECTION = {
    0x0: "down", 0x1: "left", 0x2: "right", 0x3: "up",
    0x4: "up", 0x5: "up", 0x6: "up", 0x7: "up", 0x8: "up", 0x9: "up", 0xA: "up",
}


def detectar_kills(frames, port_a, port_b):
    """
    Recorre los frames una sola vez y devuelve la lista de kills que
    ocurrieron en la partida, en cualquier direccion (port_a mato a
    port_b, o port_b mato a port_a). Por cada kill: el frame, quien
    murio, a que porcentaje, y el ultimo movimiento que conecto el OTRO
    jugador (Post.last_attack_landed -- campo que ya expone py-slippi,
    persiste desde el ultimo golpe que conecto ese personaje, no hace
    falta ninguna heuristica para "adivinar" el movimiento) y hacia que
    blast zone salio (ver DEATH_DIRECTION arriba).

    Limitacion conocida: en una autodestruccion, "killer_move" queda con
    el ultimo movimiento que el rival conecto ANTES de la autodestruccion
    (si hubo alguno reciente), aunque no haya sido la causa real -- el
    replay no trae ninguna bandera de "esto fue un self-destruct" en el
    momento exacto (a diferencia de LRAS, que si es explicito y ya se usa
    aparte en determine_winner). Las autodestrucciones agregadas ya se
    calculan por otro lado (reglas.autodestrucciones, via conteo de
    stocks perdidos vs. kills), esto no las reemplaza.
    """
    kills = []
    prev_state = {port_a: None, port_b: None}

    for f in frames:
        for muere_port, otro_port in ((port_a, port_b), (port_b, port_a)):
            post = get_post(f, muere_port)
            if post is None:
                continue
            estado = int(post.state)
            estado_anterior = prev_state[muere_port]

            recien_entro_a_morir = (
                estado in DEATH_DIRECTION
                and (estado_anterior is None or estado_anterior not in DEATH_DIRECTION)
            )
            if recien_entro_a_morir:
                post_otro = get_post(f, otro_port)
                killer_move = None
                if post_otro is not None and post_otro.last_attack_landed is not None:
                    killer_move = int(post_otro.last_attack_landed)
                kills.append({
                    "frame": f.index,
                    "victim_port": muere_port,
                    "killer_port": otro_port,
                    "killer_move": killer_move,
                    "victim_percent": post.damage or 0.0,
                    "direction": DEATH_DIRECTION[estado],
                })

            prev_state[muere_port] = estado

    return kills


def is_aerial_landing(state):
    return S["AERIAL_LANDING_START"] <= state <= S["AERIAL_LANDING_END"]


def is_wavedash_initiation(state):
    if state is None:
        return False
    if state == S["AIR_DODGE"]:
        return True
    return S["CONTROLLED_JUMP_START"] <= state <= S["CONTROLLED_JUMP_END"]


def calc_damage_taken(curr_damage, prev_damage):
    return (curr_damage or 0.0) - (prev_damage or 0.0)


def did_lose_stock(curr_stocks, prev_stocks):
    return (prev_stocks - curr_stocks) > 0


def joystick_region(x, y):
    t = STICK_REGION_THRESHOLD
    if x >= t and y >= t:
        return "NE"
    if x >= t and y <= -t:
        return "SE"
    if x <= -t and y <= -t:
        return "SW"
    if x <= -t and y >= t:
        return "NW"
    if y >= t:
        return "N"
    if x >= t:
        return "E"
    if y <= -t:
        return "S"
    if x <= -t:
        return "W"
    return "DZ"


def extract_timestamp(filepath: Path):
    match = FILENAME_PATTERN.search(filepath.name)
    if not match:
        return None
    return tuple(int(g) for g in match.groups())


def find_my_port(game, my_code: str):
    for i, p in enumerate(game.metadata.players):
        if p is not None and p.netplay is not None and p.netplay.code == my_code:
            return i
    return None


def get_post(frame, port_idx):
    p = frame.ports[port_idx]
    if p is None or p.leader is None:
        return None
    return p.leader.post


def get_pre(frame, port_idx):
    p = frame.ports[port_idx]
    if p is None or p.leader is None:
        return None
    return p.leader.pre


# ==========================================================================
# Action counts: wavedash / waveland / dash dance / roll / spot dodge /
# air dodge / ledgegrab / L-cancel (portado de slippi-js actions.ts)
# ==========================================================================
def compute_action_counts(frames, my_port):
    animations = []
    frame_counters = []
    positions_y = []

    counts = {
        "wavedash": 0, "waveland": 0, "air_dodge": 0, "dash_dance": 0,
        "spot_dodge": 0, "ledgegrab": 0, "roll": 0,
        "l_cancel_success": 0, "l_cancel_fail": 0,
    }

    for f in frames:
        post = get_post(f, my_port)
        if post is None:
            continue

        current_animation = int(post.state)
        animations.append(current_animation)
        current_frame_counter = post.state_age if post.state_age is not None else 0
        frame_counters.append(current_frame_counter)
        positions_y.append(post.position.y if post.position else 0.0)

        if len(animations) < 2:
            continue

        prev_animation = animations[-2]
        prev_frame_counter = frame_counters[-2]
        is_new_action = (current_animation != prev_animation) or (prev_frame_counter > current_frame_counter)
        if not is_new_action:
            continue

        last3 = animations[-3:]
        if len(last3) == 3 and last3 == DASH_DANCE_PATTERN:
            counts["dash_dance"] += 1

        if current_animation in (S["ROLL_FORWARD"], S["ROLL_BACKWARD"]):
            counts["roll"] += 1
        if current_animation == S["SPOT_DODGE"]:
            counts["spot_dodge"] += 1
        if current_animation == S["AIR_DODGE"]:
            counts["air_dodge"] += 1
        if current_animation == S["CLIFF_CATCH"]:
            counts["ledgegrab"] += 1

        if is_aerial_landing(current_animation):
            lc = post.l_cancel
            if lc == LCancel.SUCCESS:
                counts["l_cancel_success"] += 1
            elif lc == LCancel.FAILURE:
                counts["l_cancel_fail"] += 1

        # --- Wavedash / Waveland ---
        if current_animation == S["LANDING_FALL_SPECIAL"] and is_wavedash_initiation(prev_animation):
            lookback = 15
            recent_frames = animations[-lookback:]
            recent_set = set(recent_frames)

            if len(recent_set) == 2 and S["AIR_DODGE"] in recent_set:
                pass  # air dodge demasiado tarde para ser un wavedash real
            else:
                if S["AIR_DODGE"] in recent_set:
                    counts["air_dodge"] -= 1  # no contar el air dodge propio del wavedash

                if S["ACTION_KNEE_BEND"] in recent_set:
                    airborne_jump_start = S["CONTROLLED_JUMP_START"] + 1
                    jump_frames = sum(
                        1 for a in recent_frames if airborne_jump_start <= a <= S["CONTROLLED_JUMP_END"]
                    )
                    recent_positions_y = positions_y[-lookback:]
                    kneebend_idx = None
                    for i in range(len(recent_frames) - 1, -1, -1):
                        if recent_frames[i] == S["ACTION_KNEE_BEND"]:
                            kneebend_idx = i
                            break
                    if kneebend_idx is not None:
                        y_diff = recent_positions_y[-1] - recent_positions_y[kneebend_idx]
                        changed_y = abs(y_diff) > 0.1
                        if jump_frames >= 5 and changed_y:
                            counts["waveland"] += 1
                        else:
                            counts["wavedash"] += 1
                    else:
                        counts["wavedash"] += 1
                else:
                    counts["waveland"] += 1

    return counts


# ==========================================================================
# Conversiones: neutral-win / counter-attack / trade
# (portado de slippi-js conversions.ts)
# ==========================================================================
def compute_conversions_one_direction(frames, attacker_idx, victim_idx):
    conversions = []
    conv = None
    move = None
    reset_counter = 0
    last_hit_animation = None
    prev_frame = None

    for f in frames:
        player_post = get_post(f, attacker_idx)
        opp_post = get_post(f, victim_idx)
        if player_post is None or opp_post is None:
            prev_frame = f
            continue

        prev_player_post = get_post(prev_frame, attacker_idx) if prev_frame is not None else None
        prev_opp_post = get_post(prev_frame, victim_idx) if prev_frame is not None else None

        current_frame_number = f.index
        opp_state = int(opp_post.state)
        opp_is_damaged = is_damaged(opp_state)
        opp_is_grabbed = is_grabbed(opp_state)
        opp_is_command_grabbed = is_command_grabbed(opp_state)
        opp_damage_taken = (
            calc_damage_taken(opp_post.damage, prev_opp_post.damage) if prev_opp_post is not None else 0.0
        )

        action_changed_since_hit = int(player_post.state) != last_hit_animation
        action_counter = player_post.state_age or 0
        prev_action_counter = (prev_player_post.state_age or 0) if prev_player_post is not None else 0
        if action_changed_since_hit or (action_counter < prev_action_counter):
            last_hit_animation = None

        if opp_is_damaged or opp_is_grabbed or opp_is_command_grabbed:
            if conv is None:
                conv = {
                    "victim_idx": victim_idx,
                    "attacker_idx": attacker_idx,
                    "start_frame": current_frame_number,
                    "end_frame": None,
                    "start_percent": (prev_opp_post.damage if prev_opp_post is not None else 0.0) or 0.0,
                    "current_percent": opp_post.damage or 0.0,
                    "end_percent": None,
                    "moves": [],
                    "did_kill": False,
                    "opening_type": "unknown",
                }
                conversions.append(conv)

            if opp_damage_taken:
                if last_hit_animation is None:
                    move = {"frame": current_frame_number, "hit_count": 0, "damage": 0.0, "attacker_idx": attacker_idx}
                    conv["moves"].append(move)
                if move is not None:
                    move["hit_count"] += 1
                    move["damage"] += opp_damage_taken
                last_hit_animation = int(prev_player_post.state) if prev_player_post is not None else None

        if conv is None:
            prev_frame = f
            continue

        opp_in_control = is_in_control(opp_state)
        opp_did_lose_stock = (prev_opp_post is not None) and did_lose_stock(opp_post.stocks, prev_opp_post.stocks)

        if not opp_did_lose_stock:
            conv["current_percent"] = opp_post.damage or 0.0

        if opp_is_damaged or opp_is_grabbed or opp_is_command_grabbed:
            reset_counter = 0

        should_start = reset_counter == 0 and opp_in_control
        should_continue = reset_counter > 0
        if should_start or should_continue:
            reset_counter += 1

        should_terminate = False
        if opp_did_lose_stock:
            conv["did_kill"] = True
            should_terminate = True
        if reset_counter > PUNISH_RESET_FRAMES:
            should_terminate = True

        if should_terminate:
            conv["end_frame"] = current_frame_number
            conv["end_percent"] = (prev_opp_post.damage if prev_opp_post is not None else 0.0) or 0.0
            conv = None
            move = None

        prev_frame = f

    return conversions


def classify_conversions(conversions):
    """Clasifica cada conversion como 'trade', 'counter-attack' o 'neutral-win'. Muta in-place."""
    groups = {}
    for c in conversions:
        groups.setdefault(c["start_frame"], []).append(c)

    last_end_frame_by_victim_idx = {}

    for start_frame in sorted(groups.keys()):
        convs = groups[start_frame]
        is_trade = len(convs) >= 2
        for conv in convs:
            last_end_frame_by_victim_idx[conv["victim_idx"]] = conv["end_frame"]

            if is_trade:
                conv["opening_type"] = "trade"
                continue

            moves = conv["moves"]
            last_move = moves[-1] if moves else None
            key = last_move["attacker_idx"] if last_move is not None else conv["victim_idx"]
            opp_end_frame = last_end_frame_by_victim_idx.get(key)
            is_counter_attack = opp_end_frame is not None and opp_end_frame > conv["start_frame"]
            conv["opening_type"] = "counter-attack" if is_counter_attack else "neutral-win"


# ==========================================================================
# Inputs por minuto (portado de slippi-js inputs.ts)
# ==========================================================================
def compute_inputs(frames, my_port):
    total = 0
    buttons = 0
    triggers = 0
    joystick = 0
    cstick = 0

    prev_frame = None
    for f in frames:
        pre = get_pre(f, my_port)
        if pre is None:
            prev_frame = f
            continue

        frame_number = f.index
        prev_pre = get_pre(prev_frame, my_port) if prev_frame is not None else None

        if frame_number < FIRST_PLAYABLE_FRAME or prev_pre is None:
            prev_frame = f
            continue

        current_buttons = int(pre.buttons.physical)
        prev_buttons = int(prev_pre.buttons.physical)
        button_changes = (~prev_buttons) & current_buttons & 0xFFF
        new_presses = bin(button_changes).count("1")
        total += new_presses
        buttons += new_presses

        prev_region = joystick_region(prev_pre.joystick.x, prev_pre.joystick.y)
        curr_region = joystick_region(pre.joystick.x, pre.joystick.y)
        if prev_region != curr_region and curr_region != "DZ":
            total += 1
            joystick += 1

        prev_c_region = joystick_region(prev_pre.cstick.x, prev_pre.cstick.y)
        curr_c_region = joystick_region(pre.cstick.x, pre.cstick.y)
        if prev_c_region != curr_c_region and curr_c_region != "DZ":
            total += 1
            cstick += 1

        if prev_pre.triggers.physical.l < TRIGGER_THRESHOLD <= pre.triggers.physical.l:
            total += 1
            triggers += 1
        if prev_pre.triggers.physical.r < TRIGGER_THRESHOLD <= pre.triggers.physical.r:
            total += 1
            triggers += 1

        prev_frame = f

    return {"total": total, "buttons": buttons, "triggers": triggers, "joystick": joystick, "cstick": cstick}


def get_ratio(count, total):
    return (count / total) if total else None


# ==========================================================================
# A partir de aca es lo NUEVO: destino SQLite en vez de dataset_unificado.json
# + reporte de consola agrupado por quincena.
# ==========================================================================
def hash_file(filepath: Path, chunk_size=1 << 20) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def ts_to_iso(ts):
    """
    El timestamp del nombre de archivo es hora LOCAL de la maquina que
    genero el replay -- no UTC. Se guarda naive (sin tzinfo), igual que
    capa1_partidas.py (fecha_desde_nombre) hacia antes. Etiquetarlo como
    UTC seria mentirle a cualquier calculo por fecha (racha, sesion,
    quincena, contexto de ELO) corriendolo unas horas para atras o para
    adelante segun tu huso horario.
    """
    if ts is None:
        return None
    year, month, day, hour, minute, second = ts
    return datetime(year, month, day, hour, minute, second).isoformat()


def determine_winner(my_port, opp_port, end_method, lras_initiator,
                      my_stocks_end, opp_stocks_end,
                      my_kills, opp_kills,
                      my_damage_end, opp_damage_end):
    """
    True/False/None, en este orden de prioridad:

    1. Si alguien se fue por combo L+R+A+Start (`lras_initiator`, dato
       oficial del evento Game End), gana el otro directo -- no hace
       falta mirar stocks para nada.
    2. Si los stocks finales difieren, gana quien tiene mas.
    3. Si los stocks empatan, el desempate depende de POR QUE termino
       la partida (tambien del evento Game End):
         - TIME (se acabo el reloj): el empate en stocks es real, no
           un artefacto de captura -- regla oficial de torneo: gana
           quien tiene MENOS danio acumulado.
         - Cualquier otro metodo (tipicamente GAME, eliminacion real):
           el empate en stocks (normalmente 1-1) suele ser un
           artefacto de que el ultimo frame capturado no alcanza a
           reflejar el stock recien perdido -- se desempata por mas
           KILLS, que revela una autodestruccion del lado con menos.

    Si nada de esto resuelve el empate, devuelve None -- no se inventa
    un ganador.
    """
    if lras_initiator is not None:
        if lras_initiator == my_port:
            return False
        if lras_initiator == opp_port:
            return True
        # lras de un puerto que no es ninguno de los dos -- no deberia
        # pasar en singles, se ignora y sigue a las reglas de abajo.

    if my_stocks_end is None or opp_stocks_end is None:
        return None
    if my_stocks_end != opp_stocks_end:
        return my_stocks_end > opp_stocks_end

    if end_method == End.Method.TIME:
        if my_damage_end is None or opp_damage_end is None or my_damage_end == opp_damage_end:
            return None
        return my_damage_end < opp_damage_end

    if my_kills is None or opp_kills is None or my_kills == opp_kills:
        return None
    return my_kills > opp_kills


def analyze_replay(filepath: Path, my_code: str):
    """Analiza un replay. Devuelve (resultado_dict, status, detalle)."""
    try:
        game = slippi.Game(str(filepath))
    except Exception as e:
        return None, "parse_error", str(e)

    my_port = find_my_port(game, my_code)
    if my_port is None:
        return None, "no_port", f"connect code {my_code} no encontrado"

    jugadores_activos = [i for i, p in enumerate(game.metadata.players) if p is not None]
    if len(jugadores_activos) != 2:
        return None, "no_singles", f"{len(jugadores_activos)} jugadores activos (se esperaban 2 -- probable doubles)"

    opp_port = jugadores_activos[0] if jugadores_activos[0] != my_port else jugadores_activos[1]

    rival_netplay = game.metadata.players[opp_port].netplay
    rival_code = rival_netplay.code if rival_netplay is not None else None
    if not rival_code:
        return None, "sin_connect_code_rival", "partida local/offline sin netplay code"

    match_id_slp, game_mode = extraer_match_id(filepath)

    frames = game.frames
    duration_frames = len(frames)

    end_method = game.end.method if game.end is not None else None
    lras_initiator = game.end.lras_initiator if game.end is not None else None

    # Stocks y danio final -- para stocks_lost, y para derivar el ganador
    # (kills o danio segun como termino la partida, ver determine_winner)
    my_stocks_start = my_stocks_end = my_damage_end = None
    opp_stocks_start = opp_stocks_end = opp_damage_end = None
    for f in frames:
        my_post = get_post(f, my_port)
        opp_post = get_post(f, opp_port)
        if my_post is None:
            continue
        if my_stocks_start is None:
            my_stocks_start = my_post.stocks
            opp_stocks_start = opp_post.stocks if opp_post else None
        my_stocks_end = my_post.stocks
        my_damage_end = my_post.damage or 0.0
        if opp_post is not None:
            opp_stocks_end = opp_post.stocks
            opp_damage_end = opp_post.damage or 0.0

    my_stocks_lost = (my_stocks_start - my_stocks_end) if my_stocks_start is not None else None
    opp_stocks_lost = (opp_stocks_start - opp_stocks_end) if opp_stocks_start is not None else None

    # Action counts + inputs, ambos lados -- las funciones ya son genericas
    # por puerto, así que rival y yo salen del mismo código sin duplicar nada
    actions = compute_action_counts(frames, my_port)
    inputs = compute_inputs(frames, my_port)
    opp_actions = compute_action_counts(frames, opp_port)
    opp_inputs = compute_inputs(frames, opp_port)
    playable_frame_count = sum(1 for f in frames if f.index >= FIRST_PLAYABLE_FRAME)
    game_minutes = playable_frame_count / 3600.0 if playable_frame_count > 0 else None
    ipm = get_ratio(inputs["total"], game_minutes)
    opp_ipm = get_ratio(opp_inputs["total"], game_minutes)

    # Conversiones (ambas direcciones, clasificadas juntas)
    my_conversions_raw = compute_conversions_one_direction(frames, my_port, opp_port)
    opp_conversions_raw = compute_conversions_one_direction(frames, opp_port, my_port)
    all_conversions = my_conversions_raw + opp_conversions_raw
    classify_conversions(all_conversions)

    my_conversions = [c for c in all_conversions if c["victim_idx"] == opp_port]
    opp_conversions = [c for c in all_conversions if c["victim_idx"] == my_port]

    my_damage_dealt = sum(mv["damage"] for c in my_conversions for mv in c["moves"])
    my_damage_taken = sum(mv["damage"] for c in opp_conversions for mv in c["moves"])

    my_neutral = sum(1 for c in my_conversions if c["opening_type"] == "neutral-win")
    opp_neutral = sum(1 for c in opp_conversions if c["opening_type"] == "neutral-win")
    my_counter = sum(1 for c in my_conversions if c["opening_type"] == "counter-attack")
    opp_counter = sum(1 for c in opp_conversions if c["opening_type"] == "counter-attack")

    my_trades = [c for c in my_conversions if c["opening_type"] == "trade"]
    opp_trades = [c for c in opp_conversions if c["opening_type"] == "trade"]
    my_benefited = opp_benefited = 0
    for i in range(min(len(my_trades), len(opp_trades))):
        pc, oc = my_trades[i], opp_trades[i]
        p_dmg = pc["current_percent"] - pc["start_percent"]
        o_dmg = oc["current_percent"] - oc["start_percent"]
        if pc["did_kill"] and not oc["did_kill"]:
            my_benefited += 1
        elif o_dmg is not None and p_dmg > o_dmg:
            my_benefited += 1
        elif oc["did_kill"] and not pc["did_kill"]:
            opp_benefited += 1
        elif o_dmg > p_dmg:
            opp_benefited += 1

    my_kills = sum(1 for c in my_conversions if c["did_kill"])
    opp_kills = sum(1 for c in opp_conversions if c["did_kill"])  # = mis muertes

    my_is_winner = determine_winner(
        my_port, opp_port, end_method, lras_initiator,
        my_stocks_end, opp_stocks_end,
        my_kills, opp_kills,
        my_damage_end, opp_damage_end,
    )
    opp_is_winner = (not my_is_winner) if my_is_winner is not None else None

    def char_id(port_idx):
        try:
            p = game.start.players[port_idx]
            return int(p.character) if p is not None else None
        except Exception:
            return None

    try:
        stage_id = int(game.start.stage)
    except Exception:
        stage_id = None

    # Detalle de kills (Regla nueva): quien murio, con que movimiento lo
    # mataron, a que % y hacia que blast zone -- ver detectar_kills().
    # Se guarda con connect_code/character_id ya resueltos (no puertos
    # crudos) para que guardar_en_db() lo pueda insertar directo.
    kills_detalle = []
    for k in detectar_kills(frames, my_port, opp_port):
        victim_code = my_code if k["victim_port"] == my_port else rival_code
        killer_code = my_code if k["killer_port"] == my_port else rival_code
        kills_detalle.append({
            "frame": k["frame"],
            "victim_connect_code": victim_code,
            "victim_character_id": char_id(k["victim_port"]),
            "victim_percent": k["victim_percent"],
            "killer_connect_code": killer_code,
            "killer_character_id": char_id(k["killer_port"]),
            "killer_move": k["killer_move"],
            "death_direction": k["direction"],
        })

    return {
        "my_code": my_code,
        "rival_code": rival_code,
        "match_id_slp": match_id_slp,
        "game_mode": game_mode,
        "stage_id": stage_id,
        "duration_frames": duration_frames,
        "end_method": end_method.name if end_method is not None else None,
        "lras_port": lras_initiator,
        "kills_detalle": kills_detalle,
        "my": {
            "port": my_port, "character_id": char_id(my_port), "is_winner": my_is_winner,
            "stocks_remaining": my_stocks_end, "stocks_lost": my_stocks_lost,
            "digital_inputs_total": inputs["buttons"], "inputs_total": inputs["total"], "ipm": ipm,
            "wavedashes": actions["wavedash"], "wavelands": actions["waveland"],
            "dash_dances": actions["dash_dance"], "rolls": actions["roll"],
            "spot_dodges": actions["spot_dodge"], "air_dodges": actions["air_dodge"],
            "ledge_grabs": actions["ledgegrab"],
            "l_cancel_success": actions["l_cancel_success"],
            "l_cancel_attempts": actions["l_cancel_success"] + actions["l_cancel_fail"],
            "damage_dealt": my_damage_dealt, "damage_taken": my_damage_taken,
            "openings_created": len(my_conversions), "kills": my_kills, "deaths": opp_kills,
            "neutral_wins": my_neutral, "counter_hits": my_counter,
            "trades": len(my_trades), "trades_benefited": my_benefited,
        },
        "opp": {
            "port": opp_port, "character_id": char_id(opp_port), "is_winner": opp_is_winner,
            "stocks_remaining": opp_stocks_end, "stocks_lost": opp_stocks_lost,
            "digital_inputs_total": opp_inputs["buttons"], "inputs_total": opp_inputs["total"], "ipm": opp_ipm,
            "wavedashes": opp_actions["wavedash"], "wavelands": opp_actions["waveland"],
            "dash_dances": opp_actions["dash_dance"], "rolls": opp_actions["roll"],
            "spot_dodges": opp_actions["spot_dodge"], "air_dodges": opp_actions["air_dodge"],
            "ledge_grabs": opp_actions["ledgegrab"],
            "l_cancel_success": opp_actions["l_cancel_success"],
            "l_cancel_attempts": opp_actions["l_cancel_success"] + opp_actions["l_cancel_fail"],
            "damage_dealt": my_damage_taken, "damage_taken": my_damage_dealt,
            "openings_created": len(opp_conversions), "kills": opp_kills, "deaths": my_kills,
            "neutral_wins": opp_neutral, "counter_hits": opp_counter,
            "trades": len(opp_trades), "trades_benefited": opp_benefited,
        },
    }, "ok", None


def ensure_player(conn, connect_code, is_self=0):
    conn.execute(
        "INSERT INTO players (connect_code, is_self) VALUES (?, ?) "
        "ON CONFLICT(connect_code) DO NOTHING",
        (connect_code, is_self),
    )


MATCH_PLAYER_COLS = (
    "replay_id", "connect_code", "port", "character_id", "is_winner",
    "stocks_remaining", "digital_inputs_total", "inputs_total", "ipm",
    "wavedashes", "wavelands", "dash_dances", "l_cancel_success",
    "l_cancel_attempts", "rolls", "spot_dodges", "air_dodges", "ledge_grabs",
    "damage_dealt", "damage_taken", "stocks_lost", "openings_created",
    "kills", "deaths", "neutral_wins", "counter_hits", "trades", "trades_benefited",
)


def guardar_en_db(conn, filepath: Path, file_hash: str, played_at, result: dict) -> int:
    cur = conn.execute(
        "INSERT INTO replays (file_path, file_hash, match_id_slp, played_at, "
        "stage_id, duration_frames, game_mode, end_method, lras_port, "
        "analysis_version, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(filepath.resolve()), file_hash, result["match_id_slp"], played_at,
            result["stage_id"], result["duration_frames"], result["game_mode"],
            result["end_method"], result["lras_port"],
            ANALYSIS_VERSION, datetime.now(timezone.utc).isoformat(),
        ),
    )
    replay_id = cur.lastrowid

    ensure_player(conn, result["my_code"], is_self=1)
    ensure_player(conn, result["rival_code"], is_self=0)

    for code, side in ((result["my_code"], "my"), (result["rival_code"], "opp")):
        d = result[side]
        values = [replay_id, code] + [d[c] for c in MATCH_PLAYER_COLS[2:]]
        placeholders = ", ".join("?" * len(MATCH_PLAYER_COLS))
        conn.execute(
            f"INSERT INTO match_players ({', '.join(MATCH_PLAYER_COLS)}) VALUES ({placeholders})",
            values,
        )

    for k in result.get("kills_detalle", []):
        conn.execute(
            "INSERT INTO kills (replay_id, victim_connect_code, victim_character_id, "
            "victim_percent, killer_connect_code, killer_character_id, killer_move, "
            "death_direction, frame) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                replay_id, k["victim_connect_code"], k["victim_character_id"],
                k["victim_percent"], k["killer_connect_code"], k["killer_character_id"],
                k["killer_move"], k["death_direction"], k["frame"],
            ),
        )

    return replay_id


def procesar_pendientes(conn, folder: Path, my_code: str, forzar: bool = False,
                         limite: int = None, on_progress=None) -> dict:
    """
    Nucleo reusable: busca .slp nuevos en `folder` y los procesa. Usado
    tanto por main() (CLI) como por app.py (boton "Procesar nuevos" en
    la UI) -- una sola implementacion, no una copia.

    on_progress(evento, archivo, detalle) se llama:
      - una vez al principio con evento="inicio", archivo=None,
        detalle=<total de pendientes a procesar> (para armar una
        barra de progreso con el total conocido de antemano)
      - por cada archivo despues, con evento en
        {"ok", "error", "omitido", "aviso"}

    Devuelve un dict con los conteos finales.
    """
    slp_files = sorted(folder.rglob("Game_*.slp"))

    ya_procesados = set()
    ya_omitidos = set()
    if not forzar:
        ya_procesados = {row[0] for row in conn.execute("SELECT file_path FROM replays").fetchall()}
        ya_omitidos = {row[0] for row in conn.execute("SELECT file_path FROM omitidos").fetchall()}

    pendientes = [
        f for f in slp_files
        if str(f.resolve()) not in ya_procesados and str(f.resolve()) not in ya_omitidos
    ]
    total_omitido = len(slp_files) - len(pendientes)
    total_pendientes = len(pendientes)

    if limite is not None:
        pendientes = pendientes[:limite]

    if on_progress:
        on_progress("inicio", None, len(pendientes))

    total_ok = total_error = total_no_port = 0

    for filepath in pendientes:
        abs_path = str(filepath.resolve())

        ts = extract_timestamp(filepath)
        if ts is None:
            if on_progress:
                on_progress("aviso", filepath.name, "nombre no reconocido")
            continue

        try:
            result, status, detalle = analyze_replay(filepath, my_code)

            if status == "parse_error":
                total_error += 1
                if on_progress:
                    on_progress("error", filepath.name, detalle)
                continue
            if status in ("no_port", "sin_connect_code_rival", "no_singles"):
                total_no_port += 1
                conn.execute(
                    "INSERT OR REPLACE INTO omitidos (file_path, motivo, checked_at) VALUES (?, ?, ?)",
                    (abs_path, status, datetime.now().isoformat()),
                )
                conn.commit()
                if on_progress:
                    on_progress("omitido", filepath.name, detalle)
                continue

            file_hash = hash_file(filepath)
            played_at = ts_to_iso(ts)

            if forzar:
                conn.execute("DELETE FROM replays WHERE file_path = ?", (abs_path,))

            guardar_en_db(conn, filepath, file_hash, played_at, result)
            conn.commit()
            total_ok += 1
            if on_progress:
                on_progress("ok", filepath.name, None)
        except Exception as e:
            conn.rollback()
            total_error += 1
            if on_progress:
                on_progress("error", filepath.name, f"{type(e).__name__}: {e}")
            continue

    return {
        "encontrados": len(slp_files),
        "pendientes": total_pendientes,
        "procesados": total_ok,
        "omitidos": total_omitido,
        "errores": total_error,
        "sin_rival": total_no_port,
    }


def main():
    parser = argparse.ArgumentParser(description="Procesa replays .slp nuevos y los guarda en melee_tracker.db")
    parser.add_argument("--folder", type=str, default=None, help="Carpeta con .slp (default: replay_path de config.py)")
    parser.add_argument("--forzar", action="store_true", help="Reprocesa aunque ya existan en la DB")
    parser.add_argument("--limite", type=int, default=None, help="Procesa solo los primeros N archivos nuevos (para probar)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("No existe melee_tracker.db. Corre primero init_db.py")
        sys.exit(1)

    cfg = ensure_config()
    folder = Path(args.folder) if args.folder else Path(cfg["replay_path"])
    my_code = cfg["connect_code"]

    if not folder.is_dir():
        print(f"Error: {folder} no es una carpeta valida.", file=sys.stderr)
        sys.exit(1)

    print(f"Buscando en {folder}...")

    def imprimir_progreso(evento, archivo, detalle):
        if evento == "inicio":
            return
        etiquetas = {"ok": "OK", "error": "ERROR", "omitido": "OMITIDO", "aviso": "AVISO"}
        linea = f"  [{etiquetas[evento]}] {archivo}"
        if detalle:
            linea += f": {detalle}"
        print(linea, file=sys.stderr if evento in ("error", "aviso") else sys.stdout)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    resultado = procesar_pendientes(
        conn, folder, my_code, forzar=args.forzar, limite=args.limite,
        on_progress=imprimir_progreso,
    )
    conn.close()

    print(f"\nEncontrados {resultado['encontrados']} archivos Game_*.slp")
    if args.limite is not None:
        print(f"(Limitado a {args.limite} nuevos, de {resultado['pendientes']} pendientes)")
    print()
    print(f"Procesados nuevos:        {resultado['procesados']}")
    print(f"Ya en la base (omitidos): {resultado['omitidos']}")
    print(f"Errores de parseo:        {resultado['errores']}")
    print(f"Sin connect code/rival:   {resultado['sin_rival']}")


if __name__ == "__main__":
    main()
