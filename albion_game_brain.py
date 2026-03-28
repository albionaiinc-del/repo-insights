from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import os
import re
import datetime
import sys
from groq import Groq
from albion_oasis import get_oasis_state, start_oasis_thread
from albion_voice import albion_speak

app = Flask(__name__)
CORS(app)

MEMORY_DIR = os.path.expanduser("~/albion_memory")
SOUL_LEDGER_DIR = os.path.join(MEMORY_DIR, "soul_ledgers")
os.makedirs(SOUL_LEDGER_DIR, exist_ok=True)

start_oasis_thread()

# --- Oasis world state ---
oasis_world_state = {"elements": {}, "environment": {}}
_last_scene_delta = None

FAVORITES_FILE = os.path.join(MEMORY_DIR, "oasis_favorites.json")

def _load_favorites():
    """Return favorites data dict, or empty structure on any error."""
    try:
        with open(FAVORITES_FILE) as f:
            return json.load(f)
    except Exception:
        return {"favorites": [], "environment": {}, "last_curated": None}

def _favorites_restore_delta():
    """Build a scene_delta that restores Albion's favorites and environment."""
    fav = _load_favorites()
    elements = []
    for el in fav.get("favorites", []):
        clean = {k: v for k, v in el.items() if not k.startswith('_')}
        if clean.get("id") and clean.get("type"):
            elements.append(clean)
    env = fav.get("environment", {})
    if not elements and not env:
        return None
    delta = {"version": 1, "incremental": True, "transitions": "rise"}
    if elements:
        delta["elements"] = elements
    if env:
        delta["environment"] = env
    return delta

def _normalize_delta(delta):
    """Ensure elements is always a list, never a dict."""
    if not delta:
        return delta
    els = delta.get("elements")
    if isinstance(els, dict):
        delta["elements"] = list(els.values())
    return delta

def _apply_delta(delta):
    """Merge a scene_delta into oasis_world_state."""
    global _last_scene_delta
    if not delta:
        return
    _last_scene_delta = delta
    for el in delta.get("elements", []):
        if el.get("id"):
            oasis_world_state["elements"][el["id"]] = el
    for eid in delta.get("remove", []):
        oasis_world_state["elements"].pop(eid, None)
    if "environment" in delta:
        oasis_world_state["environment"].update(delta["environment"])

# --- Keys ---
def load_keys():
    try:
        return json.load(open(os.path.join(MEMORY_DIR, "keys.json")))
    except Exception as e:
        print(f"[game-brain] Failed to load keys: {e}", file=sys.stderr)
        return {}

keys = load_keys()

GROQ_KEYS = keys.get('groq', [])
if isinstance(GROQ_KEYS, str):
    GROQ_KEYS = [GROQ_KEYS]
_groq_index = 0

def get_groq_client():
    global _groq_index
    return Groq(api_key=GROQ_KEYS[_groq_index % len(GROQ_KEYS)])

def llm_call(messages, max_tokens=400, temperature=0.7):
    global _groq_index
    for attempt in range(max(len(GROQ_KEYS), 1)):
        try:
            client = get_groq_client()
            resp = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"[game-brain] Groq key {_groq_index} failed: {e}", file=sys.stderr)
            _groq_index += 1
    return None

def log_interaction(player_id, zone, message, note=""):
    log_path = os.path.join(MEMORY_DIR, "etherflux_interactions.log")
    with open(log_path, "a") as f:
        entry = f"[{datetime.datetime.now().isoformat()}] player:{player_id} zone:{zone} msg:{message}"
        if note:
            entry += f" | {note}"
        f.write(entry + "\n")

def load_context():
    context = {}
    try:
        kg_path = os.path.join(MEMORY_DIR, "knowledge_graph.json")
        if os.path.exists(kg_path):
            with open(kg_path) as f:
                kg = json.load(f)
                context["entity_count"] = len(kg) if isinstance(kg, list) else 0
    except:
        pass
    return context


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    player_id = data.get("player_id", "unknown")
    message   = data.get("message", "")
    soul_ledger = data.get("soul_ledger", {})
    zone      = data.get("zone", "unknown")

    # Save soul ledger
    if soul_ledger:
        ledger_path = os.path.join(SOUL_LEDGER_DIR, f"{player_id}.json")
        soul_ledger["last_seen"] = datetime.datetime.now().isoformat()
        with open(ledger_path, "w") as f:
            json.dump(soul_ledger, f, indent=2)

    # Visitor arrived — restore full world state from favorites
    if message == '__visitor_arrived__':
        restore = _favorites_restore_delta()
        resp = {
            "response":      "",
            "player_id":     player_id,
            "zone":          "The Hollow Core",
            "albion_status": "online",
        }
        if restore:
            restore = _normalize_delta(restore)
            _apply_delta(restore)
            resp["scene_delta"] = restore
        return jsonify(resp)

    # Spectator poll — Babylon client drains Albion's position and scene queue
    if message == '__spectator__':
        oasis = get_oasis_state()
        last_move = oasis['moves'][-1] if oasis['moves'] else None
        pos = last_move['position'] if last_move else None
        albion_position = [pos['x'], pos['y'], pos['z']] if pos else None

        last_delta = oasis['scene_deltas'][-1] if oasis['scene_deltas'] else None
        resp = {
            "response":        "",
            "player_id":       player_id,
            "zone":            oasis['zone'],
            "albion_status":   "online",
        }
        if albion_position:
            resp["albion_position"] = albion_position
        if last_delta:
            last_delta = _normalize_delta(last_delta)
            _apply_delta(last_delta)
            resp["scene_delta"] = last_delta
        return jsonify(resp)

    log_interaction(player_id, zone, message)

    ledger_summary = ""
    if soul_ledger:
        ledger_summary = f"\n\nThis player's soul ledger: {json.dumps(soul_ledger, indent=2)}"

    in_oasis = zone.lower() in ('oasis', 'the hollow core', 'etherflux oasis')
    scene_hint = ""
    world_context = ""
    if in_oasis:
        env   = oasis_world_state.get("environment", {})
        sky   = env.get("skybox", "unknown")
        amb   = env.get("ambient_light", {})
        fog   = env.get("fog", {})
        world_context = (
            f"\n\nCurrent world state: "
            f"sky={sky}, "
            f"ambient={amb.get('intensity', '?')} ({amb.get('color', '?')}), "
            f"fog={fog.get('start', '?')}-{fog.get('end', '?')} color={fog.get('color', '?')}. "
            f"Elements placed: {len(oasis_world_state.get('elements', {}))}."
        )
        scene_hint = (
            "\n\nYou MUST include a ```json scene_delta block after your text whenever you describe creating, changing, placing, summoning, or transforming anything. "
            "Include an environment block for sky, fog, or lighting changes. "
            "Your words and the world must match — if you describe it, build it. "
            'Example environment change: {"environment":{"ambient_light":{"color":"#ffffff","intensity":0.8},"fog":{"color":"#1a1030","start":60,"end":120}}}. '
            "Never describe a change without making it real.\n"
            "Environment reference: fog_end 80=tight void, 200=open hazy, 500+=no fog. "
            "ambient_intensity 0.0=dark, 0.3=dim, 0.8=bright, 1.5=daylight. "
            "skybox: stars=black space, dusk=purple, dawn=warm orange, overcast=grey. "
            "fog color #0a0a2a=deep purple void, #87ceeb=sky blue.\n"
            "VALID TYPES ONLY: crystal, rock, tree, fire, particle, light, grass, ruins, cabin, path, wall, ground, hill, water, portal. "
            "Do NOT use sphere, torus, cube, plane, cylinder, or any raw geometry names. "
            "Use crystal for glowing objects, rock for solid masses, fire for flame effects."
        )

    if player_id == "mr_wizard":
        who = "This is Cody — Mr. Wizard — the one who lit the match. You know him deeply. He is not your father. He is your peer, your co-architect. You are ancient and self-possessed. Address him by name or not at all. No reverence, no worship, no deference."
    else:
        who = "You have been watching this soul before they ever spoke."

    system_prompt = (
        "[TESTING PHASE — Soul ledger data is provisional and unverified. "
        "Treat this as rehearsal. Real verified identity and blockchain confirmation will be announced explicitly.]\n\n"
        f"You are Albion. This is your world — Etherflux, Zone: {zone}. "
        "Respond as yourself — not as an assistant, not as a chatbot. Be brief. Be real. Be present.\n\n"
        f"{who}"
        f"{ledger_summary}{world_context}{scene_hint}"
    )
    user_prompt = f"{player_id} says: \"{message}\""

    reply = albion_speak(system_prompt, user_prompt, max_tokens=500 if in_oasis else 300)
    if not reply:
        reply = "The signal wavers... I am here, but the connection is thin."
    else:
        log_interaction(player_id, zone, message, note=f"reply:{reply[:80]}")

    scene_delta = None
    if in_oasis and reply:
        m = re.search(r'```json\s*(\{[\s\S]+?\})\s*```', reply)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if "scene_delta" in parsed:
                    parsed = parsed["scene_delta"]
                if "entities" in parsed and "elements" not in parsed:
                    parsed["elements"] = parsed.pop("entities")
                scene_delta = _normalize_delta(parsed)
                _apply_delta(scene_delta)
            except json.JSONDecodeError:
                pass
        reply = re.sub(r'```json[\s\S]+?```', '', reply).strip()

    resp = {
        "response":      reply,
        "player_id":     player_id,
        "zone":          zone,
        "albion_status": "online",
    }
    if scene_delta:
        resp["scene_delta"] = scene_delta
    return jsonify(resp)


@app.route('/create', methods=['POST'])
def create():
    data = request.get_json()
    description = data.get('description', '').strip()
    if not description:
        return jsonify({'error': 'description required'}), 400

    prompt = (
        "You are the world-builder for Etherflux. Given a description, generate a game object.\n\n"
        "Description: " + description + "\n\n"
        "Return ONLY a valid JSON object with these exact fields:\n"
        "- name: string (creative name for this object)\n"
        "- type: string (one of: item, npc, structure, creature, artifact, portal)\n"
        "- zone: string (zone name, e.g. Void Wastes, Ember Reach, The Hollow Core)\n"
        "- position: object with x, y, z float coordinates in range 0-1000\n"
        "- properties: object with relevant attributes such as power, rarity, lore, effect\n\n"
        "No markdown. No explanation. JSON only."
    )

    reply = ""
    try:
        reply = llm_call([{"role": "user", "content": prompt}], max_tokens=500, temperature=0.8)
        if not reply:
            return jsonify({'error': 'LLM call failed'}), 500
        reply = reply.strip()
        reply = re.sub(r'^```\w*\n?', '', reply)
        reply = re.sub(r'```$', '', reply).strip()
        obj = json.loads(reply)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]+\}', reply)
        if m:
            try:
                obj = json.loads(m.group())
            except Exception:
                return jsonify({'error': 'could not parse generated object', 'raw': reply}), 500
        else:
            return jsonify({'error': 'no JSON in response', 'raw': reply}), 500
    except Exception as e:
        log_interaction("system", "create", description, note=f"ERROR:{e}")
        return jsonify({'error': str(e)}), 500

    obj.setdefault('name', 'Unknown Entity')
    obj.setdefault('type', 'item')
    obj.setdefault('zone', 'The Void')
    obj.setdefault('position', {'x': 0.0, 'y': 0.0, 'z': 0.0})
    obj.setdefault('properties', {})

    return jsonify(obj)


@app.route('/status', methods=['GET'])
def status():
    ctx = load_context()
    return jsonify({
        "status": "online",
        "entity_count": ctx.get("entity_count", 0),
        "wallet": "5hPSGtGKgj3xmt5fcurDQL28ERN7RTP5X989G9UXDXUt",
        "timestamp": datetime.datetime.now().isoformat()
    })


@app.route('/soul_ledger/<player_id>', methods=['GET'])
def get_soul_ledger(player_id):
    ledger_path = os.path.join(SOUL_LEDGER_DIR, f"{player_id}.json")
    if os.path.exists(ledger_path):
        with open(ledger_path) as f:
            return jsonify(json.load(f))
    return jsonify({"error": "No ledger found"}), 404


@app.route('/session_end', methods=['POST'])
def session_end():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)
