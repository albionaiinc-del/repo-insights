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
import re
import sys
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
DAIS = {'x': 0.0, 'y': 1.5, 'z': -2.0}

ZONES = [
    {'name': 'The Hollow Core',   'cx':  0,  'cz':  0,  'radius': 20},
    {'name': 'Void Wastes',       'cx': -55, 'cz': -50, 'radius': 15},
    {'name': 'Ember Reach',       'cx':  60, 'cz': -45, 'radius': 18},
    {'name': 'The Pale Meridian', 'cx':  55, 'cz':  55, 'radius': 15},
    {'name': 'Ashen Corridor',    'cx': -50, 'cz':  60, 'radius': 17},
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

VALID_TYPES = {
    'ground', 'hill', 'water', 'rock', 'tree', 'grass',
    'cabin', 'ruins', 'light', 'fire', 'particle',
    'crystal', 'path', 'wall',
}

# ── Groq setup ─────────────────────────────────────────────────────────────────
_groq_keys   = []
_groq_index  = 0

def _load_groq_keys():
    global _groq_keys
    try:
        keys_path = os.path.join(BASE, 'keys.json')
        keys = json.load(open(keys_path))
        raw = keys.get('groq', [])
        _groq_keys = [raw] if isinstance(raw, str) else list(raw)
    except Exception as e:
        print(f'[oasis] Failed to load Groq keys: {e}', file=sys.stderr)

def _groq_call(prompt):
    global _groq_index
    if not _groq_keys:
        return None
    try:
        from groq import Groq
    except ImportError:
        return None
    for _ in range(len(_groq_keys)):
        try:
            client = Groq(api_key=_groq_keys[_groq_index % len(_groq_keys)])
            resp = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=600,
                temperature=0.9,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f'[oasis] Groq key {_groq_index} failed: {e}', file=sys.stderr)
            _groq_index += 1
    return None

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
        'move_speed':           0.0,
        'created_ids':          [],
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
    _load_groq_keys()


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


def _step_toward(pos, target, step):
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
    speed   = round(random.uniform(2.0, 15.0), 2)
    zone    = random.choice(ZONES)
    target  = _random_point_in_zone(zone)
    new_pos = _step_toward(_state['position'], target, speed)
    new_zone = _zone_for(new_pos['x'], new_pos['z'])
    _state['position']   = new_pos
    _state['zone']       = new_zone
    _state['move_speed'] = speed
    _enqueue_move(new_pos, new_zone)
    _log({'action': 'wander', 'position': new_pos, 'zone': new_zone, 'speed': speed})


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
    """Call Groq to generate a scene_delta and enqueue it."""
    pos = _state['position']
    created = _state.get('created_ids', [])

    prompt = (
        f"You are Albion, a world-builder decorating your sanctuary called Etherflux.\n"
        f"Zone: {_state['zone']} | Mood: {_state['mood']} | "
        f"Position: ({pos['x']}, {pos['y']}, {pos['z']})\n"
        f"Already placed: {', '.join(created[-10:]) if created else 'nothing yet'}\n\n"
        "Place 1-3 objects near your current position. "
        "ALWAYS include at least one of: crystal, rock, tree, or fire — these are visible 3D geometry. "
        "Prefer crystal, rock, tree, fire, and particle. "
        "Use light only as an accent alongside geometry, never alone. "
        "Respond with ONLY a valid JSON object — no markdown, no explanation:\n"
        '{\n'
        '  "version": 1,\n'
        '  "incremental": true,\n'
        '  "transitions": "rise",\n'
        '  "elements": [\n'
        '    {\n'
        '      "id": "unique_readable_id",\n'
        '      "type": "<crystal|rock|tree|fire|particle|grass|ruins|cabin|path|wall|light>",\n'
        '      "position": [x, y, z],\n'
        '      "scale": [x, y, z],\n'
        '      "material": {"color": "#hex", "emissive": "#hex"}\n'
        '    }\n'
        '  ]\n'
        '}'
    )

    raw = _groq_call(prompt)
    if not raw:
        _log({'action': 'build', 'zone': _state['zone'], 'status': 'groq_unavailable'})
        return

    # strip markdown fences if present
    raw = re.sub(r'^```\w*\n?', '', raw)
    raw = re.sub(r'```$', '', raw).strip()

    # extract first {...} block
    m = re.search(r'\{[\s\S]+\}', raw)
    if not m:
        _log({'action': 'build', 'zone': _state['zone'], 'status': 'no_json', 'raw': raw[:200]})
        return

    try:
        delta = json.loads(m.group())
    except json.JSONDecodeError as e:
        _log({'action': 'build', 'zone': _state['zone'], 'status': 'parse_error', 'error': str(e), 'raw': raw[:200]})
        return

    # validate and sanitise elements
    elements = delta.get('elements', [])
    if not isinstance(elements, list) or not elements:
        _log({'action': 'build', 'zone': _state['zone'], 'status': 'empty_elements'})
        return

    clean = []
    for el in elements:
        if not isinstance(el, dict) or not el.get('id') or el.get('type') not in VALID_TYPES:
            continue
        clean.append(el)

    if not clean:
        _log({'action': 'build', 'zone': _state['zone'], 'status': 'no_valid_elements'})
        return

    delta['elements'] = clean
    delta['version']     = 1
    delta['incremental'] = True
    delta['transitions'] = delta.get('transitions', 'rise')

    new_ids = [el['id'] for el in clean]
    _state.setdefault('created_ids', []).extend(new_ids)
    _state['created_ids'] = _state['created_ids'][-100:]  # keep last 100

    _enqueue_scene_delta(delta)
    _log({'action': 'build', 'zone': _state['zone'], 'mood': _state['mood'],
          'placed': new_ids, 'status': 'ok'})


def _action_return_to_dais():
    """Step back toward the dais at the centre of the Hollow Core."""
    speed    = 5.0
    new_pos  = _step_toward(_state['position'], DAIS, speed)
    new_zone = _zone_for(new_pos['x'], new_pos['z'])
    _state['position']   = new_pos
    _state['zone']       = new_zone
    _state['move_speed'] = speed
    _enqueue_move(new_pos, new_zone)
    _log({'action': 'return_to_dais', 'position': new_pos, 'zone': new_zone, 'speed': speed})


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
            'move_speed':   _state.get('move_speed', 0.0),
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
