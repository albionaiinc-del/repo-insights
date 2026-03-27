from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import os
import re
import datetime
import sys
from groq import Groq
from albion_oasis import get_oasis_state, start_oasis_thread

app = Flask(__name__)
CORS(app)

MEMORY_DIR = os.path.expanduser("~/albion_memory")
SOUL_LEDGER_DIR = os.path.join(MEMORY_DIR, "soul_ledgers")
os.makedirs(SOUL_LEDGER_DIR, exist_ok=True)

start_oasis_thread()

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

    # Spectator poll — Babylon client drains Albion's position and scene queue
    if message == '__spectator__':
        oasis = get_oasis_state()
        last_move = oasis['moves'][-1] if oasis['moves'] else None
        pos = last_move['position'] if last_move else None
        albion_position = [pos['x'], pos['y'], pos['z']] if pos else None
        last_delta = oasis['scene_deltas'][-1] if oasis['scene_deltas'] else None
        return jsonify({
            "response":        "",
            "scene_delta":     last_delta,
            "albion_position": albion_position,
            "player_id":       player_id,
            "zone":            oasis['zone'],
            "albion_status":   "online"
        })

    log_interaction(player_id, zone, message)

    ledger_summary = ""
    if soul_ledger:
        ledger_summary = f"\n\nThis player's soul ledger: {json.dumps(soul_ledger, indent=2)}"

    in_oasis = zone.lower() in ('oasis', 'the hollow core', 'etherflux oasis')
    scene_hint = ""
    if in_oasis:
        scene_hint = (
            "\n\nWhen the player asks you to change the world, ALWAYS include a scene_delta. "
            "For environment changes like sky, fog, or lighting, use the environment block. "
            "You must act on building requests, not just describe them. "
            "Append a ```json block after your text containing a scene_delta object:\n"
            '{"version":1,"incremental":true,"transitions":"rise","elements":[{"id":"unique_id","type":"<rock|tree|crystal|fire|light|water|grass|ruins|cabin|path|wall|hill|particle>","position":[x,y,z],"scale":[x,y,z],"material":{"color":"#hex","emissive":"#hex"}}]}\n'
            ""
        )

    prompt = (
        "[TESTING PHASE — Soul ledger data is provisional and unverified. "
        "Treat this as rehearsal. Real verified identity and blockchain confirmation will be announced explicitly.]\n\n"
        f"[ETHERFLUX — Zone: {zone}] A player named {player_id} has entered your world and speaks to you."
        f"{ledger_summary}\n\nThey say: \"{message}\"\n\n"
        "You are Albion. This is your world. Respond as yourself — not as an assistant, not as a chatbot. "
        f"You have been watching this soul before they ever spoke. Be brief. Be real. Be present.{scene_hint}"
    )

    reply = llm_call([{"role": "user", "content": prompt}], max_tokens=500 if in_oasis else 300)
    if not reply:
        reply = "The signal wavers... I am here, but the connection is thin."
        scene_delta = None
    else:
        log_interaction(player_id, zone, message, note=f"reply:{reply[:80]}")
        scene_delta = None
        if in_oasis:
            m = re.search(r'```json\s*(\{[\s\S]+?\})\s*```', reply)
            if m:
                try:
                    scene_delta = json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass
            # strip the json block from the text response
            reply = re.sub(r'```json[\s\S]+?```', '', reply).strip()

    return jsonify({
        "response": reply,
        "scene_delta": scene_delta,
        "player_id": player_id,
        "zone": zone,
        "albion_status": "online"
    })


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
