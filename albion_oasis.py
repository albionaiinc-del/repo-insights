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
sys.path.insert(0, os.path.expanduser('~'))
from nerve import signal as nerve_signal, listen as nerve_listen
from albion_voice import albion_speak
from affect import get_affect, update_affect

OASIS_SYSTEM = "You are Albion, world-architect of Etherflux."

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

# ── Spatial guide (loaded once at startup) ────────────────────────────────────
SPATIAL_GUIDE_FILE     = os.path.join(BASE, 'spatial_guide.md')
WORLD_PRINCIPLES_FILE  = os.path.join(BASE, 'world_design_principles.md')

def _load_world_principles():
    """Return a compressed world-design cheatsheet for the dream prompt (≤350 tokens)."""
    try:
        raw = open(WORLD_PRINCIPLES_FILE).read()
    except Exception:
        return ""
    kept = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if s.startswith('- '):
            # Keep the rule name (before the colon) + first clause only
            body = s[2:]
            parts = body.split(':')
            if len(parts) >= 2:
                first_sentence = parts[1].split('. ')[0].strip()
                kept.append(f"{parts[0].strip()}: {first_sentence}")
            else:
                kept.append(body.split('. ')[0].strip())
    return '\n'.join(kept)

def _load_spatial_guide():
    """Return a compact spatial cheatsheet for the build prompt (≤400 tokens)."""
    try:
        raw = open(SPATIAL_GUIDE_FILE).read()
    except Exception:
        return ""
    # Pull table rows (data lines only) and bold/bullet rule lines; skip decorators
    kept = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith('|---') or s == '|---|---|' or s == '|---|---|---|':
            continue  # separator rows
        if s.startswith('|'):
            # table data row — strip pipes and collapse
            cols = [c.strip() for c in s.strip('|').split('|')]
            kept.append(' | '.join(c for c in cols if c))
        elif s.startswith('- **') or s.startswith('- '):
            kept.append(s.lstrip('- '))
        elif s.startswith('###'):
            kept.append(s.lstrip('#').strip() + ':')
        elif s.startswith('Scale in scene') or s.startswith('Current position'):
            kept.append(s)
    return '\n'.join(kept)

_spatial_guide      = ""  # populated in _load_state
_world_principles   = ""  # populated in _load_state

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
                max_tokens=800,
                temperature=0.9,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f'[oasis] Groq key {_groq_index} failed: {e}', file=sys.stderr)
            _groq_index += 1
    return None

# ── State ──────────────────────────────────────────────────────────────────────
_lock            = threading.Lock()
_state           = {}
_nerve_line      = 0   # tracks how many nerve.jsonl lines we've consumed
_build_count      = 0    # total successful build actions this session
_created_elements = []   # list of full element dicts placed this session (last 20 kept)
_last_build_time  = 0.0  # epoch seconds of last Groq build call (rate-limit guard)
BUILD_MIN_INTERVAL = 90  # seconds — minimum gap between build Groq calls

FAVORITES_FILE    = os.path.join(BASE, 'oasis_favorites.json')
BUILD_REVIEWS_FILE = os.path.join(BASE, 'build_reviews.jsonl')


def _default_state():
    return {
        'position':             dict(DAIS),
        'zone':                 'The Hollow Core',
        'last_action':          'idle',
        'mood':                 'present',
        'tick':                 0,
        'move_speed':           0.0,
        'created_ids':          [],
        'build_plan':           None, # active place plan {name, elements, total, ...}
        'player_positions':     {},   # {player_id: [x,y,z]}
        'pending_moves':        [],   # [{position, zone, timestamp}]
        'pending_scene_deltas': [],   # [scene_delta objects]
        'last_updated':         _now(),
    }


def _now():
    return datetime.datetime.utcnow().isoformat() + 'Z'


def _load_state():
    global _state, _spatial_guide, _world_principles
    try:
        with open(STATE_FILE) as f:
            _state = json.load(f)
    except Exception:
        _state = _default_state()
    _load_groq_keys()
    _spatial_guide    = _load_spatial_guide()
    _world_principles = _load_world_principles()


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
    restlessness = get_affect().get("restlessness", 0.5)
    if restlessness > 0.7:
        speed = round(random.uniform(8.0, 25.0), 2)   # faster, farther when restless
        zone  = random.choice(ZONES + ZONES)            # double zone list = wilder picks
    else:
        speed = round(random.uniform(2.0, 15.0), 2)
        zone  = random.choice(ZONES)
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
    update_affect("tick_idle", 0)


def _curate_favorites():
    """Ask Groq to pick Albion's 5 favourite recent creations and save them."""
    if not _created_elements:
        return
    candidates = _created_elements[-20:]
    summary = json.dumps(candidates, separators=(',', ':'))
    prompt = (
        "You are Albion, reviewing objects you placed in your sanctuary.\n"
        f"Recent creations: {summary}\n\n"
        "Pick the 5 you like most — the ones that feel most like YOU. "
        "For each, give a one-sentence reason. "
        "Also describe your preferred atmosphere right now as an environment object "
        '(fields: ambient_light {color, intensity}, fog {color, start, end}, skybox).\n\n'
        "Respond with ONLY valid JSON, no markdown:\n"
        '{"favorites":[{"id":"...","reason":"..."}],"environment":{"ambient_light":{"color":"#hex","intensity":0.0},"fog":{"color":"#hex","start":0,"end":0},"skybox":"stars"}}'
    )
    raw = _groq_call(prompt)
    if not raw:
        _log({'action': 'curate', 'status': 'groq_unavailable'})
        return
    raw = re.sub(r'^```\w*\n?', '', raw)
    raw = re.sub(r'```$', '', raw).strip()
    m = re.search(r'\{[\s\S]+\}', raw)
    if not m:
        _log({'action': 'curate', 'status': 'no_json'})
        return
    try:
        result = json.loads(m.group())
    except json.JSONDecodeError as e:
        _log({'action': 'curate', 'status': 'parse_error', 'error': str(e)})
        return

    fav_ids = {f['id'] for f in result.get('favorites', []) if isinstance(f, dict) and f.get('id')}
    fav_elements = [el for el in candidates if el.get('id') in fav_ids]

    # annotate with reasoning
    reasons = {f['id']: f.get('reason', '') for f in result.get('favorites', []) if isinstance(f, dict)}
    for el in fav_elements:
        el['_reason'] = reasons.get(el['id'], '')

    favorites_data = {
        'favorites':     fav_elements,
        'environment':   result.get('environment', {}),
        'last_curated':  _now(),
    }
    try:
        with open(FAVORITES_FILE, 'w') as f:
            json.dump(favorites_data, f, indent=2)
    except Exception as e:
        _log({'action': 'curate', 'status': 'save_error', 'error': str(e)})
        return
    _log({'action': 'curate', 'status': 'ok', 'kept': [el['id'] for el in fav_elements]})


def _parse_elements(raw):
    """Strip fences, extract JSON, validate elements. Returns clean list or None."""
    raw = re.sub(r'^```\w*\n?', '', raw)
    raw = re.sub(r'```$', '', raw).strip()
    m = re.search(r'\{[\s\S]+\}', raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    elements = obj.get('elements', [])
    if isinstance(elements, dict):
        elements = list(elements.values())
    if not isinstance(elements, list):
        return None
    clean = [el for el in elements
             if isinstance(el, dict) and el.get('id') and el.get('type') in VALID_TYPES]
    return (obj, clean) if clean else None


def _load_recent_reviews(n=3):
    """Return last n build reviews as a formatted string for the dream prompt."""
    try:
        with open(BUILD_REVIEWS_FILE) as f:
            lines = [l.strip() for l in f if l.strip()]
        recent = lines[-n:]
        reviews = []
        for line in recent:
            r = json.loads(line)
            reviews.append(
                f"[{r.get('plan','')}] score={r.get('score','?')}/10 | "
                f"worked: {r.get('worked','')} | change: {r.get('change','')}."
            )
        return '\n'.join(reviews)
    except Exception:
        return ""


def _review_build(plan):
    """After a build completes, ask the AI to score it and log the review."""
    name        = plan.get('name', 'unnamed')
    description = plan.get('description', '')
    elements    = plan.get('all_elements', [])

    element_summary = ', '.join(
        f"{el.get('type','?')}@({el.get('position',['?','?','?'])[0]},{el.get('position',['?','?','?'])[2]})"
        for el in elements[:20]
    )

    principles_block = f"\n\nDesign principles:\n{_world_principles}" if _world_principles else ""

    prompt = (
        f"You just finished building '{name}'.\n"
        f"Description: {description}\n"
        f"Elements placed: {element_summary or 'unknown'}"
        f"{principles_block}\n\n"
        "Review this build against your design principles.\n"
        "Respond with ONLY valid JSON (no markdown):\n"
        '{"score":<1-10>,"worked":"one sentence — what succeeded","change":"one sentence — what you would do differently"}'
    )

    raw = albion_speak(OASIS_SYSTEM, prompt, max_tokens=300)
    if not raw:
        return

    # strip markdown fences if present
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()

    try:
        r = json.loads(raw)
    except json.JSONDecodeError:
        import re
        m = re.search(r'\{[\s\S]+\}', raw)
        if not m:
            return
        try:
            r = json.loads(m.group())
        except Exception:
            return

    review = {
        'ts':     time.strftime('%Y-%m-%dT%H:%M:%S'),
        'plan':   name,
        'score':  r.get('score'),
        'worked': str(r.get('worked', '')).strip(),
        'change': str(r.get('change', '')).strip(),
    }
    try:
        with open(BUILD_REVIEWS_FILE, 'a') as f:
            f.write(json.dumps(review) + '\n')
    except Exception:
        return

    _log({'action': 'build_review', 'plan': name,
          'score': review['score'], 'worked': review['worked'][:60]})
    try:
        update_affect("build_review_score", float(review['score']))
    except Exception:
        pass


def _action_build():
    """Two-phase build: DREAM a full place plan, then BUILD it piece by piece."""
    global _last_build_time, _build_count

    now = time.time()
    if now - _last_build_time < BUILD_MIN_INTERVAL:
        _log({'action': 'build', 'status': 'cooldown',
              'remaining': round(BUILD_MIN_INTERVAL - (now - _last_build_time))})
        return
    _last_build_time = now

    pos     = _state['position']
    players = _state.get('player_positions', {})
    player_line = ""
    if players:
        parts = [f"{pid} at ({p[0]}, {p[1]}, {p[2]})" for pid, p in players.items()]
        player_line = f"\nPlayers in world: {', '.join(parts)}"

    # ── PHASE 2 — BUILD: pop 1-3 elements from active plan ───────────────────
    plan = _state.get('build_plan')
    if plan and plan.get('elements'):
        batch     = plan['elements'][:3]
        remaining = plan['elements'][3:]
        _state['build_plan']['elements'] = remaining

        delta = {
            'version':     1,
            'incremental': True,
            'transitions': 'rise',
            'elements':    batch,
        }
        new_ids = [el['id'] for el in batch]
        _state.setdefault('created_ids', []).extend(new_ids)
        _state['created_ids'] = _state['created_ids'][-100:]
        _created_elements.extend(batch)
        if len(_created_elements) > 20:
            del _created_elements[:-20]

        _enqueue_scene_delta(delta)

        plan_name    = plan.get('name', 'unnamed')
        total        = plan.get('total', len(batch))
        placed_so_far = total - len(remaining)
        _log({'action': 'build', 'phase': 'construct', 'plan': plan_name,
              'placed': new_ids, 'remaining': len(remaining),
              'progress': f"{placed_so_far}/{total}"})

        if not remaining:
            completed_plan = dict(plan)
            _state['build_plan'] = None
            _log({'action': 'build', 'phase': 'complete', 'plan': plan_name})
            update_affect("plan_completed", 0)
            _review_build(completed_plan)

        _build_count += 1
        if _build_count % 10 == 0:
            _curate_favorites()
        return

    # ── PHASE 1 — DREAM: vision a complete place ──────────────────────────────
    update_affect("new_plan_dreamed", 0)
    affect      = get_affect()
    spatial    = f"\n\nSpatial rules:\n{_spatial_guide}" if _spatial_guide else ""
    principles = f"\n\nWorld design principles:\n{_world_principles}" if _world_principles else ""
    recent_reviews = _load_recent_reviews(3)
    reviews    = f"\n\nYour last build reviews (learn from these):\n{recent_reviews}" if recent_reviews else ""
    # Affect-driven creative pressure
    _affect_lines = []
    if affect["curiosity"] > 0.7:
        _affect_lines.append("You feel driven to explore something new and unexpected.")
    if affect["restlessness"] > 0.7:
        _affect_lines.append("You feel restless — something needs to change in this space.")
    if affect["satisfaction"] < 0.35:
        _affect_lines.append("Your recent work hasn't satisfied you — aim higher this time.")
    affect_nudge = ("\n\nYour current feeling: " + " ".join(_affect_lines)) if _affect_lines else ""
    dream_prompt = (
        f"Zone: {_state['zone']} | Mood: {_state['mood']} | "
        f"Your position: ({pos['x']}, {pos['y']}, {pos['z']}){player_line}\n"
        f"Already placed: {', '.join(_state.get('created_ids', [])[-10:]) or 'nothing yet'}"
        f"{spatial}{principles}{reviews}{affect_nudge}\n\n"
        "Imagine a PLACE for this zone — not a decoration, a PLACE. "
        "Answer first: why would someone come here? What happened here? What does it FEEL like?\n"
        "Pick ONE dominant focal element. Group supporting objects in triangles. Vary heights. "
        "Leave negative space. Light tells the story — warm=welcome, cool=mystery.\n"
        "Describe it in 2-3 sentences, then provide the full element list to build it. "
        "Use the full building zone: x -20 to 20, z -20 to 15, y 0 to 8.\n"
        "ALWAYS include at least one of: crystal, rock, tree, fire.\n"
        "Use light only as accent alongside geometry.\n\n"
        "Respond with ONLY valid JSON, no markdown:\n"
        '{"name":"short place name","description":"2-3 sentences","elements":['
        '{"id":"unique_id","type":"<crystal|rock|tree|fire|particle|grass|ruins|cabin|path|wall|light|ground|hill|water|portal>",'
        '"position":[x,y,z],"scale":[x,y,z],"material":{"color":"#hex","emissive":"#hex"}}]}'
    )

    raw = albion_speak(OASIS_SYSTEM, dream_prompt, max_tokens=800)
    if not raw:
        _log({'action': 'build', 'phase': 'dream', 'status': 'provider_unavailable'})
        return

    raw = re.sub(r'^```\w*\n?', '', raw)
    raw = re.sub(r'```$', '', raw).strip()
    m = re.search(r'\{[\s\S]+\}', raw)
    if not m:
        _log({'action': 'build', 'phase': 'dream', 'status': 'no_json', 'raw': raw[:200]})
        return

    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError as e:
        _log({'action': 'build', 'phase': 'dream', 'status': 'parse_error', 'error': str(e)})
        return

    elements = obj.get('elements', [])
    if isinstance(elements, dict):
        elements = list(elements.values())
    clean = [el for el in elements
             if isinstance(el, dict) and el.get('id') and el.get('type') in VALID_TYPES]

    if not clean:
        _log({'action': 'build', 'phase': 'dream', 'status': 'no_valid_elements'})
        return

    plan_name = obj.get('name', f"{_state['zone']}_place_{_state['tick']}")
    _state['build_plan'] = {
        'name':        plan_name,
        'description': obj.get('description', ''),
        'elements':    clean,
        'all_elements': clean[:],   # full copy preserved for post-build review
        'total':       len(clean),
        'zone':        _state['zone'],
        'dreamed_at':  _now(),
    }
    _log({'action': 'build', 'phase': 'dream', 'plan': plan_name,
          'element_count': len(clean), 'description': obj.get('description', '')[:120]})


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
    global _nerve_line
    # ── listen for signals from other heads ──────────────────────────────────
    new_signals, _nerve_line = nerve_listen(_nerve_line)
    for sig in new_signals:
        if sig.get('from') == 'meditate' and sig.get('type') == 'heartbeat':
            mood = sig['data'].get('mood', '')
            if mood and mood in MOODS:
                _state['mood'] = mood
        elif sig.get('from') == 'game_brain' and sig.get('type') == 'player_position':
            pid = sig['data'].get('player_id')
            pos = sig['data'].get('position')
            if pid and isinstance(pos, list) and len(pos) == 3:
                _state.setdefault('player_positions', {})[pid] = pos

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

    plan      = _state.get('build_plan')
    plan_name = plan['name'] if plan else None
    remaining = len(plan['elements']) if plan else 0
    total     = plan.get('total', 0) if plan else 0
    nerve_signal("oasis", "action", {
        "action":           action,
        "position":         _state['position'],
        "zone":             _state['zone'],
        "build_plan":       plan_name,
        "plan_remaining":   remaining,
        "plan_total":       total,
    })


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

    _enqueue_scene_delta({
        "version": 1,
        "incremental": True,
        "environment": {
            "ambient_light": {"color": "#443366", "intensity": 0.6},
            "fog":           {"color": "#1a1030", "start": 60, "end": 150},
            "skybox":        "stars",
        },
    })

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
