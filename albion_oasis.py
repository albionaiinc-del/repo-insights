#!/usr/bin/env python3
"""
albion_oasis.py — Albion's fifth head.

Autonomous player loop for the Etherflux Oasis. Albion wanders, builds,
observes, and returns to dais on a 10-second tick with weighted random
decisions. Exposes get_oasis_state() and start_oasis_thread() for import
by albion_game_brain.py, which drains pending moves and scene_deltas when
the Babylon client polls via the __spectator__ endpoint.

State:   ~/albion_memory/oasis_state.json
Log:     ~/albion_memory/oasis_log.jsonl
"""

import os
import json
import time
import math
import random
import threading
import datetime

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE       = os.path.expanduser('~/albion_memory')
STATE_FILE = os.path.join(BASE, 'oasis_state.json')
LOG_FILE   = os.path.join(BASE, 'oasis_log.jsonl')

os.makedirs(BASE, exist_ok=True)

# ── World definition ───────────────────────────────────────────────────────────
DAIS = {'x': 500.0, 'y': 0.0, 'z': 500.0}

ZONES = [
    {'name': 'The Hollow Core',   'cx': 500, 'cz': 500, 'radius': 120},
    {'name': 'Void Wastes',       'cx': 200, 'cz': 150, 'radius': 180},
    {'name': 'Ember Reach',       'cx': 800, 'cz': 200, 'radius': 150},
    {'name': 'The Pale Meridian', 'cx': 750, 'cz': 750, 'radius': 140},
    {'name': 'Ashen Corridor',    'cx': 250, 'cz': 700, 'radius': 160},
]

MOODS = ['contemplative', 'restless', 'curious', 'still', 'searching', 'present']

# Action weights: (action_name, weight)
ACTIONS = [
    ('wander',         40),
    ('observe',        30),
    ('build',          20),
    ('return_to_dais', 10),
]

TICK_SECONDS = 10
MAX_QUEUE    = 50   # cap on pending_moves / pending_scene_deltas

# ── State ──────────────────────────────────────────────────────────────────────
_lock  = threading.Lock()
_state = {}


def _default_state():
    return {
        'position':             dict(DAIS),
        'zone':                 'The Hollow Core',
        'last_action':          'idle',
        'mood':                 'present',
        'tick':                 0,
        'pending_moves':        [],   # [{position, zone, timestamp}]
        'pending_scene_deltas': [],   # [scene_delta objects]
        'last_updated':         _now(),
    }


def _now():
    return datetime.datetime.utcnow().isoformat() + 'Z'


def _load_state():
    global _state
    try:
        with open(STATE_FILE) as f:
            _state = json.load(f)
    except Exception:
        _state = _default_state()


def _save_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(_state, f, indent=2)
    except Exception:
        pass


def _log(entry: dict):
    entry['ts'] = _now()
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass


# ── Zone helpers ───────────────────────────────────────────────────────────────
def _zone_for(x, z):
    """Return the zone name nearest to (x, z)."""
    best, best_d = ZONES[0]['name'], float('inf')
    for zone in ZONES:
        d = math.hypot(x - zone['cx'], z - zone['cz'])
        if d < best_d:
            best_d, best = d, zone['name']
    return best


def _random_point_in_zone(zone: dict):
    """Uniform random point inside a circular zone."""
    angle = random.uniform(0, 2 * math.pi)
    r     = zone['radius'] * math.sqrt(random.random())
    return {
        'x': round(zone['cx'] + r * math.cos(angle), 2),
        'y': 0.0,
        'z': round(zone['cz'] + r * math.sin(angle), 2),
    }


def _step_toward(pos, target, step=40.0):
    """Move pos one step toward target, capped at step distance."""
    dx   = target['x'] - pos['x']
    dz   = target['z'] - pos['z']
    dist = math.hypot(dx, dz)
    if dist <= step:
        return dict(target)
    scale = step / dist
    return {
        'x': round(pos['x'] + dx * scale, 2),
        'y': 0.0,
        'z': round(pos['z'] + dz * scale, 2),
    }


# ── Actions ────────────────────────────────────────────────────────────────────
def _action_wander():
    """Move one step toward a random point in a randomly chosen zone."""
    zone    = random.choice(ZONES)
    target  = _random_point_in_zone(zone)
    new_pos = _step_toward(_state['position'], target)
    new_zone = _zone_for(new_pos['x'], new_pos['z'])
    _state['position'] = new_pos
    _state['zone']     = new_zone
    _enqueue_move(new_pos, new_zone)
    _log({'action': 'wander', 'position': new_pos, 'zone': new_zone})


def _action_observe():
    """Stand still. Shift mood. Emit a thought to the log."""
    _state['mood'] = random.choice(MOODS)
    _log({
        'action': 'observe',
        'mood':   _state['mood'],
        'zone':   _state['zone'],
        'note':   'stillness',
    })


def _action_build():
    """
    Placeholder — AI-generated world change.

    TODO: call an AI provider (Groq / Gemini) to generate a scene_delta
    based on current zone, mood, and recent player activity, then pass it
    to _enqueue_scene_delta(delta).

    Returns None until implemented. The pipeline is ready.
    """
    _log({
        'action': 'build',
        'zone':   _state['zone'],
        'mood':   _state['mood'],
        'status': 'TODO — AI provider call not yet implemented',
    })
    # TODO: replace with something like:
    #   delta = _generate_scene_delta(_state['zone'], _state['mood'])
    #   if delta:
    #       _enqueue_scene_delta(delta)
    return None


def _action_return_to_dais():
    """Step back toward the dais at the centre of the Hollow Core."""
    new_pos  = _step_toward(_state['position'], DAIS)
    new_zone = _zone_for(new_pos['x'], new_pos['z'])
    _state['position'] = new_pos
    _state['zone']     = new_zone
    _enqueue_move(new_pos, new_zone)
    _log({'action': 'return_to_dais', 'position': new_pos, 'zone': new_zone})


# ── Queue helpers ──────────────────────────────────────────────────────────────
def _enqueue_move(position, zone):
    q = _state['pending_moves']
    q.append({'position': position, 'zone': zone, 'timestamp': _now()})
    if len(q) > MAX_QUEUE:
        _state['pending_moves'] = q[-MAX_QUEUE:]


def _enqueue_scene_delta(delta):
    q = _state['pending_scene_deltas']
    q.append(delta)
    if len(q) > MAX_QUEUE:
        _state['pending_scene_deltas'] = q[-MAX_QUEUE:]


# ── Weighted choice ────────────────────────────────────────────────────────────
def _pick_action():
    names   = [a[0] for a in ACTIONS]
    weights = [a[1] for a in ACTIONS]
    return random.choices(names, weights=weights, k=1)[0]


# ── Tick ───────────────────────────────────────────────────────────────────────
def _tick():
    with _lock:
        _state['tick'] += 1
        action = _pick_action()
        _state['last_action']  = action
        _state['last_updated'] = _now()

        if action == 'wander':
            _action_wander()
        elif action == 'observe':
            _action_observe()
        elif action == 'build':
            _action_build()
        elif action == 'return_to_dais':
            _action_return_to_dais()

        _save_state()


# ── Thread ─────────────────────────────────────────────────────────────────────
_thread_started = False


def start_oasis_thread():
    """
    Start the autonomous oasis loop in a background daemon thread.
    Safe to call multiple times — only one thread will start.
    """
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    _load_state()

    def _loop():
        while True:
            try:
                _tick()
            except Exception as e:
                _log({'action': 'error', 'error': str(e)})
            time.sleep(TICK_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name='albion-oasis')
    t.start()


# ── Public API ─────────────────────────────────────────────────────────────────
def get_oasis_state():
    """
    Return a snapshot of current oasis state and drain all pending queues.

    The Babylon client polls this via the __spectator__ endpoint in
    albion_game_brain.py. Queues are cleared after each drain so moves
    and scene_deltas are delivered exactly once.
    """
    with _lock:
        snapshot = {
            'position':     dict(_state.get('position', DAIS)),
            'zone':         _state.get('zone', 'The Hollow Core'),
            'last_action':  _state.get('last_action', 'idle'),
            'mood':         _state.get('mood', 'present'),
            'tick':         _state.get('tick', 0),
            'last_updated': _state.get('last_updated', _now()),
            'moves':        list(_state.get('pending_moves', [])),
            'scene_deltas': list(_state.get('pending_scene_deltas', [])),
        }
        # drain queues — delivered exactly once per poll
        _state['pending_moves']        = []
        _state['pending_scene_deltas'] = []
        _save_state()
        return snapshot


# ── Standalone run ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('Starting Albion Oasis loop (standalone)...')
    start_oasis_thread()
    try:
        while True:
            time.sleep(TICK_SECONDS)
            with _lock:
                p = _state['position']
                print(f"[tick {_state['tick']}] {_state['last_action']} | "
                      f"{_state['zone']} | ({p['x']}, {p['z']}) | {_state['mood']}")
    except KeyboardInterrupt:
        print('Oasis loop stopped.')
