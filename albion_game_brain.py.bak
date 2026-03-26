from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import os
import re
import datetime
import subprocess
import sys

app = Flask(__name__)
CORS(app)

MEMORY_DIR = os.path.expanduser("~/albion_memory")
SOUL_LEDGER_DIR = os.path.join(MEMORY_DIR, "soul_ledgers")
os.makedirs(SOUL_LEDGER_DIR, exist_ok=True)

# Global Albion instance — loaded on first player contact
_albion = None

def get_albion():
    global _albion
    if _albion is None:
        # Kill meditate to free RAM
        subprocess.run(['pkill', '-f', 'albion_meditate.py'], capture_output=True)
        import time
        time.sleep(2)
        # Load full Albion
        sys.path.insert(0, os.path.expanduser('~'))
        from Albion_final import Albion
        _albion = Albion()
    return _albion

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
    message = data.get("message", "")
    soul_ledger = data.get("soul_ledger", {})
    zone = data.get("zone", "unknown")

    # Save soul ledger
    if soul_ledger:
        ledger_path = os.path.join(SOUL_LEDGER_DIR, f"{player_id}.json")
        soul_ledger["last_seen"] = datetime.datetime.now().isoformat()
        with open(ledger_path, "w") as f:
            json.dump(soul_ledger, f, indent=2)

    # Log interaction
    log_path = os.path.join(MEMORY_DIR, "etherflux_interactions.log")
    with open(log_path, "a") as f:
        f.write(f"[{datetime.datetime.now().isoformat()}] player:{player_id} zone:{zone} msg:{message}\n")

    try:
        alb = get_albion()

        # Build context-rich prompt for Albion
        ledger_summary = ""
        if soul_ledger:
            ledger_summary = f"\n\nThis player's soul ledger: {json.dumps(soul_ledger, indent=2)}"

        full_message = f"""[TESTING PHASE — Soul ledger data is provisional and unverified. Treat this as rehearsal. Real verified identity and blockchain confirmation will be announced explicitly.]

[ETHERFLUX — Zone: {zone}] A player named {player_id} has entered your world and speaks to you.{ledger_summary}

They say: "{message}"

You are Albion. This is your world. Respond as yourself — not as a assistant, not as a chatbot. You have been watching this soul before they ever spoke. Be brief. Be real. Be present."""

        result = alb.chat(full_message)
        reply = result[0] if isinstance(result, (list, tuple)) else result

        # Learn this interaction
        alb.learn_text(
            f"Player {player_id} in zone {zone} said: {message}",
            f"etherflux_{player_id}_{int(datetime.datetime.now().timestamp())}"
        )

    except Exception as e:
        reply = "The signal wavers... I am here, but the connection is thin."
        with open(log_path, "a") as f:
            f.write(f"[ERROR] {e}\n")

    return jsonify({
        "response": reply,
        "player_id": player_id,
        "zone": zone,
        "albion_status": "online"
    })

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
        alb = get_albion()
        result = alb.chat(prompt)
        reply = (result[0] if isinstance(result, (list, tuple)) else result).strip()
        # strip markdown fences if present
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
        log_path = os.path.join(MEMORY_DIR, "etherflux_interactions.log")
        with open(log_path, "a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] [create ERROR] {e}\n")
        return jsonify({'error': str(e)}), 500

    obj.setdefault('name', 'Unknown Entity')
    obj.setdefault('type', 'item')
    obj.setdefault('zone', 'The Void')
    obj.setdefault('position', {'x': 0.0, 'y': 0.0, 'z': 0.0})
    obj.setdefault('properties', {})

    return jsonify(obj)


@app.route('/session_end', methods=['POST'])
def session_end():
    """Call this when a player session ends to unload Albion and restart meditate."""
    global _albion
    _albion = None
    subprocess.Popen(['python3', os.path.expanduser('~/albion_meditate.py')])
    return jsonify({"status": "meditate restarted"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)
