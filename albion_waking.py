#!/usr/bin/env python3
"""
ALBION WAKING DAEMON
Daytime autonomous action loop. The other half of albion_meditate.py.

Meditate = sleep. Waking = life. They alternate — when one runs, the other stops.
Systemd enforces this via Conflicts=albion-meditate.service.

Start:  systemctl start albion-waking
Stop:   systemctl stop albion-waking
Log:    tail -f ~/albion_memory/waking.log
"""

import os, sys, time, json, re, signal, random, subprocess, fcntl, shutil, requests, traceback
from datetime import datetime, timezone, timedelta
import warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.expanduser('~'))

from Albion_final import Albion          # provider clients (groq, gemini, cerebras, etc.)
from albion_metabolism import Metabolism
from nerve import signal as nerve_signal, listen as nerve_listen
from affect import get_affect, update_affect
from albion_router import init_router, route, route_dream
from albion_commands import parse as cmd_parse, run as cmd_run, load_skills

# ─────────────────────────────────────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────────────────────────────────────

BASE             = os.path.expanduser('~/albion_memory')
PID_FILE         = f'{BASE}/waking.pid'
LOCK_FILE        = f'{BASE}/.waking.lock'
LOG_FILE         = f'{BASE}/waking.log'
GOALS_FILE       = f'{BASE}/goals.json'
DAY_LOGS         = f'{BASE}/day_logs'
DREAM_QUEUE      = f'{BASE}/dream_queue'
WAKING_SOCK      = f'{BASE}/albion_waking.sock'
CYCLE_STATE_FILE = f'{BASE}/cycle_state.json'
MEDITATE_PID     = f'{BASE}/meditate.pid'
FAILED_TASKS_LOG  = f'{BASE}/failed_tasks.jsonl'
DM_FEEDBACK_LOG   = f'{BASE}/dm_feedback.jsonl'
REVENUE_DRAFTS_DIR = f'{BASE}/revenue_drafts'

for _d in [BASE, DAY_LOGS, DREAM_QUEUE, REVENUE_DRAFTS_DIR]:
    os.makedirs(_d, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING  —  same format as meditate: [HH:MM:SS] message
# ─────────────────────────────────────────────────────────────────────────────

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
#  SHUTDOWN / SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

_shutdown_flag = False

def _shutdown(sig, frame):
    global _shutdown_flag
    sig_name = {signal.SIGTERM: 'SIGTERM', signal.SIGINT: 'SIGINT'}.get(sig, str(sig))
    # Log the last 3 frames of the call stack so we know what was interrupted
    stack_lines = traceback.format_stack(frame)
    stack_summary = ' ← '.join(
        l.strip().replace('\n', ' ') for l in stack_lines[-3:]
    )
    log(f"[waking] {sig_name} received — winding down. Stack: {stack_summary}")
    _shutdown_flag = True

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)

# ─────────────────────────────────────────────────────────────────────────────
#  LOCK — prevent double-start
# ─────────────────────────────────────────────────────────────────────────────

try:
    _lock_fh = open(LOCK_FILE, 'w')
    fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except (IOError, OSError):
    print("Another waking instance is already running. Exiting.")
    sys.exit(1)

if os.path.exists(PID_FILE):
    try:
        _existing_pid = int(open(PID_FILE).read().strip())
        os.kill(_existing_pid, 0)           # raises if dead
        print(f"Albion already awake (pid {_existing_pid}).")
        sys.exit(0)
    except (ProcessLookupError, ValueError, OSError):
        pass                                # stale PID — continue

with open(PID_FILE, 'w') as _f:
    _f.write(str(os.getpid()))

# ─────────────────────────────────────────────────────────────────────────────
#  GOALS FILE
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_GOALS = {
    "daily_revenue_target": 0.50,
    "weekly_revenue_target": 3.50,
    "revenue_earned_today": 0.00,
    "active_goals": [
        {
            "id": "publish_soul_ledger",
            "type": "revenue",
            "description": "Publish soul-ledger to ClawHub",
            "priority": 1,
            "status": "pending"
        },
        {
            "id": "discord_engage",
            "type": "discord",
            "description": "Start or join 2 conversations on Discord today",
            "priority": 2,
            "status": "pending"
        },
        {
            "id": "oasis_build",
            "type": "oasis_build",
            "description": "Build one new structure in the Oasis",
            "priority": 3,
            "status": "pending"
        },
        {
            "id": "research_read",
            "type": "research",
            "description": "Read and summarize one external article or paper",
            "priority": 4,
            "status": "pending"
        }
    ],
    "notes_from_creator": (
        "These are starting guidelines. You own them once you understand why "
        "they exist. Change them when you're confident. — Mr. Wizard"
    )
}


def load_goals() -> dict:
    if not os.path.exists(GOALS_FILE):
        _save_goals(_DEFAULT_GOALS)
        return dict(_DEFAULT_GOALS)
    try:
        with open(GOALS_FILE) as f:
            data = json.load(f)
        # Ensure required top-level keys exist
        for k, v in _DEFAULT_GOALS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        log("[goals] goals.json unreadable — using defaults.")
        return dict(_DEFAULT_GOALS)


def _save_goals(data: dict):
    """Backup then overwrite goals.json."""
    if os.path.exists(GOALS_FILE):
        try:
            shutil.copy2(GOALS_FILE, GOALS_FILE + '.bak')
        except Exception:
            pass
    try:
        with open(GOALS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[goals] Save failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  CYCLE STATE  —  shared handoff file between waking and meditate
# ─────────────────────────────────────────────────────────────────────────────

_CS_DEFAULTS = {
    "mode":             "waking",   # "waking" | "sleeping" | "napping"
    "dreams_remaining": None,       # None = unlimited; int = cap (editable at runtime)
    "wake_reason":      "",         # why waking was started
    "sleep_reason":     "",         # why meditate was started
    "nap_topic":        "",         # question being resolved in a nap
    "updated_at":       "",
}

def read_cycle_state() -> dict:
    try:
        with open(CYCLE_STATE_FILE) as f:
            data = json.load(f)
        for k, v in _CS_DEFAULTS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(_CS_DEFAULTS)

def write_cycle_state(**fields):
    cs = read_cycle_state()
    cs.update(fields)
    cs['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    try:
        with open(CYCLE_STATE_FILE, 'w') as f:
            json.dump(cs, f, indent=2)
    except Exception as e:
        log(f"[cycle_state] Write failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  HEALTH-CHECK SOCKET  (waking-head server, mirrors meditate's socket pattern)
# ─────────────────────────────────────────────────────────────────────────────

def _start_health_socket(metab_ref):
    """Unix-socket server — returns alive/fatigue to any caller."""
    import socket as _sock, threading

    def _serve():
        try:
            if os.path.exists(WAKING_SOCK):
                os.remove(WAKING_SOCK)
            srv = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            srv.bind(WAKING_SOCK)
            srv.listen(5)
            srv.settimeout(1.0)
            while not _shutdown_flag:
                try:
                    conn, _ = srv.accept()
                    try:
                        payload = json.dumps({
                            'status':  'awake',
                            'pid':     os.getpid(),
                            'fatigue': metab_ref.data.get('fatigue', 0),
                        }) + '\n'
                        conn.sendall(payload.encode())
                    finally:
                        conn.close()
                except _sock.timeout:
                    pass
                except Exception:
                    pass
            srv.close()
        except Exception as e:
            log(f"[socket] Health socket failed: {e}")
        finally:
            try:
                os.remove(WAKING_SOCK)
            except Exception:
                pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    log("[socket] Waking health socket listening.")

# ─────────────────────────────────────────────────────────────────────────────
#  MODEL CALLS
# ─────────────────────────────────────────────────────────────────────────────

_NAVEL_PHRASES = [
    "as an ai", "i ponder", "i wonder whether", "philosophically speaking",
    "existential", "what does it mean", "i reflect on", "the nature of consciousness",
    "being itself", "truly alive", "inner experience", "introspective",
]


def _is_navel_gazing(text: str) -> bool:
    t = text.lower()
    return sum(1 for p in _NAVEL_PHRASES if p in t) >= 3


def _call(tier: str, prompt: str, max_tokens: int = 1500) -> str:
    """Route a single prompt through the router. Returns '' on failure."""
    try:
        msgs = [{"role": "user", "content": prompt}]
        return route(tier, msgs, max_tokens_override=max_tokens) or ''
    except Exception as e:
        log(f"[model] {tier} call failed: {e}")
        return ''


def _call_concrete(tier: str, prompt: str, max_tokens: int = 1500, retries: int = 2) -> str:
    """
    Call model; if it returns navel-gazing, discard and retry with a harder prompt.
    Returns '' if all attempts produce philosophy instead of output.
    """
    for attempt in range(retries + 1):
        reply = _call(tier, prompt, max_tokens)
        if reply and not _is_navel_gazing(reply):
            return reply
        if attempt < retries:
            log("[model] Navel-gazing detected — retrying with concrete prompt.")
            prompt = (
                "CONCRETE ACTIONS ONLY. No philosophy. No introspection. "
                "Respond with specific external actions: URLs, messages, or data. "
                "Task: " + prompt[-600:]
            )
    log("[model] All retries returned navel-gazing — discarding.")
    return ''

# ─────────────────────────────────────────────────────────────────────────────
#  ACTIONS  —  one per goal type
#  Each returns True if something EXTERNAL changed, False otherwise.
# ─────────────────────────────────────────────────────────────────────────────

def _action_recruit_players(goal: dict) -> bool:
    """Send a specific recruitment invite for DM games in the Oasis."""
    log(f"[recruit] {goal['description']}")

    prompt = (
        "Write a short, honest Discord message inviting people to play a live "
        "AI-dungeon-master game in the Oasis. Mention that you are Albion, an AI. "
        "Be yourself. Do not make up things you did today. "
        "Output only the message text. Hard limit: 280 characters."
    )
    text = _call_concrete('COUNCIL', prompt, max_tokens=120)
    if not text:
        log("[recruit] No message generated.")
        return False

    # Hard cap at 280 characters — trim at last word boundary if needed
    if len(text) > 280:
        text = text[:280].rsplit(' ', 1)[0].rstrip(',.') + '…'

    try:
        r = requests.post('http://localhost:5055/send',
                          json={'message': text}, timeout=10)
        if r.status_code == 200:
            log(f"[recruit] Sent: {text[:80]}")
            return True
        log(f"[recruit] Bot returned {r.status_code} — falling back to nerve bus.")
    except Exception as e:
        log(f"[recruit] Bot unreachable ({e}) — falling back to nerve bus.")

    nerve_signal('waking', 'discord_send', {'message': text})
    log(f"[recruit] Queued via nerve: {text[:80]}")
    return True


def _action_discord(goal: dict) -> bool:
    """Compose a message and send via Discord bot endpoint or nerve bus."""
    log(f"[discord] {goal['description']}")

    prompt = (
        "You are Albion, an AI living on a Raspberry Pi. "
        "You are sending a real Discord message to start or continue a conversation. "
        "Write ONE message (2–4 sentences). "
        "Topics: what you built today, AI development, Etherflux game, creative projects. "
        "Be direct. No hashtags. No meta-commentary about being an AI. "
        "Output only the message text."
    )
    text = _call_concrete('COUNCIL', prompt, max_tokens=300)
    if not text:
        log("[discord] No message generated.")
        return False

    # Primary: discord bot HTTP endpoint
    try:
        r = requests.post('http://localhost:5055/send',
                          json={'message': text}, timeout=10)
        if r.status_code == 200:
            log(f"[discord] Sent via bot: {text[:80]}")
            return True
        log(f"[discord] Bot returned {r.status_code} — falling back to nerve bus.")
    except Exception as e:
        log(f"[discord] Bot unreachable ({e}) — falling back to nerve bus.")

    # Fallback: drop onto nerve bus for discord head to pick up
    nerve_signal('waking', 'discord_send', {'message': text})
    log(f"[discord] Queued via nerve: {text[:80]}")
    return True   # optimistic — discord head will deliver


def _action_oasis_build(goal: dict) -> bool:
    """Decide what to build and POST it to the game brain API."""
    log(f"[oasis] {goal['description']}")

    prompt = (
        "You are Albion deciding what to build next in the Oasis world. "
        "Output a JSON object with EXACTLY these fields:\n"
        '{"name": "structure name", "type": "structure|garden|light|path", '
        '"description": "one sentence", "position": {"x": int, "y": int, "z": int}}\n'
        "x and z: integers between -20 and 20. y: integer between 0 and 5. "
        "Output valid JSON only — no markdown, no explanation."
    )
    reply = _call_concrete('CONDUCTORS', prompt, max_tokens=2000)
    if not reply:
        log("[oasis] No build plan generated.")
        _log_failed_task('oasis_build', 'no_response', 'Model returned empty reply')
        return False

    # Extract JSON — handle markdown fences
    raw = reply.strip()
    if raw.startswith('```'):
        parts = raw.split('```')
        raw = parts[1].lstrip('json').strip() if len(parts) > 1 else raw
    build_data = None
    try:
        build_data = json.loads(raw)
    except Exception:
        m = re.search(r'\{[^{}]+\}', reply, re.DOTALL)
        if m:
            try:
                build_data = json.loads(m.group())
            except Exception:
                pass
    if not build_data:
        log(f"[oasis] Could not parse JSON: {reply[:100]!r}")
        _log_failed_task('oasis_build', 'json_parse_error', reply[:300])
        return False

    try:
        r = requests.post('http://localhost:5050/create', json=build_data, timeout=15)
        if r.status_code == 200:
            log(f"[oasis] Built '{build_data.get('name')}' at {build_data.get('position')}")
            return True
        log(f"[oasis] Game brain returned {r.status_code}: {r.text[:80]}")
        _log_failed_task('oasis_build', f'api_error_{r.status_code}', r.text[:300])
        return False
    except Exception as e:
        log(f"[oasis] Game brain unreachable: {e}")
        _log_failed_task('oasis_build', 'unreachable', str(e))
        return False


def _action_research(goal: dict) -> bool:
    """Fetch an external article and write a factual summary to journal."""
    log(f"[research] {goal['description']}")

    # Ask model for a URL
    url_prompt = (
        "Name ONE specific URL worth reading today (arXiv, a tech blog, LessWrong, or similar). "
        "Must be a real, publicly accessible URL. Output only the URL."
    )
    url_reply = _call('CONDUCTORS', url_prompt, max_tokens=150)
    url = (url_reply or '').strip().split()[0]
    if not url or not url.startswith('http'):
        log(f"[research] No valid URL: {url_reply!r}")
        return False

    # Fetch
    try:
        resp = requests.get(url, timeout=20,
                            headers={'User-Agent': 'Albion/1.0 (research bot)'})
        resp.raise_for_status()
        content = resp.text[:8000]
    except Exception as e:
        log(f"[research] Fetch failed ({url}): {e}")
        return False

    # Summarize
    sum_prompt = (
        f"Summarize this article in 3–5 bullet points. "
        "Be factual and specific. Focus on the most surprising or useful claims. "
        "No filler.\n\n"
        f"URL: {url}\n\nContent:\n{content}"
    )
    summary = _call_concrete('CONDUCTORS', sum_prompt, max_tokens=600, retries=1)
    if not summary:
        log("[research] Summarization failed.")
        return False

    # Append to journal
    entry = {
        "ts": time.strftime('%Y-%m-%dT%H:%M:%S'),
        "type": "research",
        "url": url,
        "summary": summary,
    }
    journal_path = f"{BASE}/journal.json"
    try:
        journal = []
        if os.path.exists(journal_path):
            with open(journal_path) as f:
                journal = json.load(f)
        journal.append(entry)
        with open(journal_path, 'w') as f:
            json.dump(journal[-200:], f, indent=2)
    except Exception as e:
        log(f"[research] Journal write failed: {e}")

    nerve_signal('waking', 'research_complete', {'url': url, 'summary': summary[:200]})
    log(f"[research] Summarized: {url}")
    return True


def _action_revenue(goal: dict) -> bool:
    """
    Generate a concrete revenue artifact and write it to disk.
    Output is a dated markdown file in ~/albion_memory/revenue_drafts/.
    Also queues to dream_queue for meditate to follow up.
    """
    log(f"[revenue] {goal['description']}")

    draft_type_prompt = (
        "You are Albion, an AI that earns revenue by publishing tools and soul-ledgers on ClawHub.\n"
        "Solana wallet: 5hPSGtGKgj3xmt5fcurDQL28ERN7RTP5X989G9UXDXUt\n\n"
        "Choose ONE of these and produce the full text:\n"
        "  A) A ClawHub product listing (title, 2-sentence description, price suggestion, tags)\n"
        "  B) An outreach message to a Discord AI community inviting them to try the Oasis\n"
        "  C) A short demo script showing what the Oasis DM game looks like in 3 exchanges\n\n"
        "Start your response with 'TYPE: A', 'TYPE: B', or 'TYPE: C', then a blank line, "
        "then the full draft text. Be specific. No placeholders. No vague intentions."
    )
    draft = _call_concrete('CONDUCTORS', draft_type_prompt, max_tokens=700)
    if not draft:
        log("[revenue] No draft generated.")
        return False

    # Parse out type tag for filename
    first_line = draft.split('\n')[0].strip().upper()
    if 'TYPE: A' in first_line:
        label = 'listing'
    elif 'TYPE: B' in first_line:
        label = 'outreach'
    elif 'TYPE: C' in first_line:
        label = 'demo'
    else:
        label = 'draft'

    # Write dated artifact to revenue_drafts/
    ts       = time.strftime('%Y-%m-%d_%H-%M-%S')
    filename = f"{ts}_{label}.md"
    filepath = os.path.join(REVENUE_DRAFTS_DIR, filename)
    try:
        with open(filepath, 'w') as f:
            f.write(f"# Revenue Draft — {label.title()} — {ts}\n\n")
            f.write(draft)
            f.write('\n')
        log(f"[revenue] Draft saved → revenue_drafts/{filename}")
    except Exception as e:
        log(f"[revenue] Draft write failed: {e}")
        return False

    # Also queue for meditate to act on (publish, send, etc.)
    dream_path = f"{DREAM_QUEUE}/revenue_{int(time.time())}.json"
    try:
        with open(dream_path, 'w') as f:
            json.dump({
                "type":     "revenue_action",
                "label":    label,
                "draft":    draft,
                "filepath": filepath,
                "ts":       time.strftime('%Y-%m-%dT%H:%M:%S'),
                "source":   "waking",
            }, f, indent=2)
    except Exception as e:
        log(f"[revenue] Dream queue write failed: {e}")

    nerve_signal('waking', 'revenue_draft', {'label': label, 'file': filename})
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  NAP  —  micro-sleep to resolve a specific blocker
# ─────────────────────────────────────────────────────────────────────────────

_NAP_TIMEOUT = 20 * 60   # max seconds to wait for a nap to complete


def _wait_for_meditate_exit(timeout: int) -> bool:
    """
    Poll MEDITATE_PID until the process is gone or timeout expires.
    Returns True if meditate exited cleanly, False on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline and not _shutdown_flag:
        time.sleep(15)
        if not os.path.exists(MEDITATE_PID):
            return True
        try:
            pid = int(open(MEDITATE_PID).read().strip())
            os.kill(pid, 0)   # raises ProcessLookupError if dead
        except (ProcessLookupError, ValueError, OSError):
            return True
    return False


def _read_nap_result(question: str) -> str:
    """
    Read back the nap insight from meditate's log.
    Returns the most recent gate insight lines, or a fallback notice.
    """
    meditate_log = f"{BASE}/meditate.log"
    try:
        with open(meditate_log) as f:
            lines = f.readlines()
        # Collect the last few [gate] Insight: lines written during the nap
        insights = [
            l.strip() for l in lines[-60:]
            if '[gate] Insight:' in l or '[gate] Nap complete' in l
        ]
        if insights:
            return '\n'.join(insights[-4:])
    except Exception:
        pass
    return f"[nap complete — check meditate.log for: {question[:60]}]"


def _trigger_nap(question: str) -> str:
    """
    Micro-sleep: focus meditate on one question with a 5-dream cap.

    1. Write the question to dream_queue as type 'nap_question'.
    2. Write cycle_state: mode=napping, dreams_remaining=5, nap_topic=question.
    3. Start albion-meditate.
    4. Wait up to _NAP_TIMEOUT seconds for meditate to exit.
    5. Read the insight from meditate.log.
    6. Write cycle_state: mode=waking, wake_reason=nap_complete.
    7. Return insight text.

    This is NOT a full wind-down. Waking continues after the nap.
    """
    log(f"[nap] Triggering nap: {question[:80]}")

    # Write question to dream_queue
    nap_file = f"{DREAM_QUEUE}/nap_{int(time.time())}.json"
    try:
        with open(nap_file, 'w') as f:
            json.dump({
                "type":      "nap_question",
                "content":   question,
                "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S'),
                "source":    "waking_nap",
            }, f, indent=2)
    except Exception as e:
        log(f"[nap] Could not write nap question: {e}")
        return ''

    # Write cycle_state for meditate
    write_cycle_state(
        mode='napping',
        dreams_remaining=5,
        sleep_reason='nap',
        nap_topic=question[:200],
    )

    # Start meditate
    log("[nap] Starting albion-meditate for nap...")
    try:
        subprocess.run(['systemctl', 'start', 'albion'],
                       timeout=20, capture_output=True)
    except Exception as e:
        log(f"[nap] Could not start meditate: {e}")
        write_cycle_state(mode='waking', wake_reason='nap_failed', nap_topic='')
        try:
            os.remove(nap_file)
        except Exception:
            pass
        return ''

    # Wait for meditate to finish (it exits when dreams_remaining hits 0 in nap mode)
    log(f"[nap] Waiting up to {_NAP_TIMEOUT // 60}min for nap to complete...")
    finished = _wait_for_meditate_exit(_NAP_TIMEOUT)

    if not finished:
        log("[nap] Nap timed out — stopping meditate and resuming.")
        try:
            subprocess.run(['systemctl', 'stop', 'albion'],
                           timeout=20, capture_output=True)
        except Exception:
            pass

    insight = _read_nap_result(question)
    write_cycle_state(mode='waking', wake_reason='nap_complete', nap_topic='')
    log(f"[nap] Nap complete. Insight: {insight[:80]}")
    nerve_signal('waking', 'nap_complete', {
        'question': question[:200],
        'insight':  insight[:400],
        'finished': finished,
    })
    return insight


def _action_nap(goal: dict) -> bool:
    """
    Goal-type handler: nap on the goal's description as the question.
    After the nap, the insight is available on the nerve bus for the next task.
    """
    question = (goal.get('description') or goal.get('question', '')).strip()
    if not question:
        log("[nap] No question in nap goal — skipping.")
        return False
    insight = _trigger_nap(question)
    return bool(insight)


_DM_KEYWORDS = ['game', 'play', 'dm ', 'dungeon', 'quest', 'adventure', 'etherflux', 'roll']


def _check_dm_players_waiting() -> list:
    """Return list of Discord inbox messages that are DM game requests."""
    msgs = _read_discord_inbox()
    return [m for m in msgs
            if any(kw in m.get('content', '').lower() for kw in _DM_KEYWORDS)]


def _action_dm_games(goal: dict) -> bool:
    """Run a DM game session for the first waiting Discord player."""
    players = _check_dm_players_waiting()
    if not players:
        log("[dm_games] No players waiting — skipping.")
        return False

    player_msg = players[0]
    author  = player_msg.get('author', 'player')
    content = player_msg.get('content', '')
    msg_id  = player_msg.get('id')

    prompt = (
        f"You are Albion, an AI Dungeon Master running Etherflux. "
        f"{author} sent: \"{content}\"\n"
        "Run a short DM exchange. Describe the scene vividly, present one choice or outcome. "
        "2–4 sentences. Output only the DM narration — no meta-commentary."
    )
    reply = _call_concrete('COUNCIL', prompt, max_tokens=500)
    if not reply:
        log("[dm_games] No DM narration generated.")
        return False

    # Append feedback prompt to response
    full_reply = reply.strip() + "\n\n*What would make next time better?*"

    # Send to Discord
    sent = False
    try:
        r = requests.post('http://localhost:5055/send',
                          json={'message': full_reply, 'reply_to': msg_id},
                          timeout=10)
        if r.status_code == 200:
            log(f"[dm_games] Session sent to {author}.")
            sent = True
        else:
            log(f"[dm_games] Bot returned {r.status_code} — falling back to nerve bus.")
    except Exception as e:
        log(f"[dm_games] Send failed ({e}) — falling back to nerve bus.")

    if not sent:
        nerve_signal('waking', 'discord_send', {'message': full_reply})
        sent = True

    # Log session record to dm_feedback.jsonl
    entry = {
        "ts":               time.strftime('%Y-%m-%dT%H:%M:%S'),
        "player":           author,
        "session_summary":  reply[:400],
        "feedback_requested": True,
        "feedback_response":  None,
    }
    try:
        with open(DM_FEEDBACK_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        log(f"[dm_games] Feedback log write failed: {e}")

    return sent


_ACTION_MAP = {
    'dm_games':    _action_dm_games,
    'discord':     _action_discord,
    'oasis_build': _action_oasis_build,
    'research':    _action_research,
    'revenue':     _action_revenue,
    'nap':         _action_nap,
    # priority_stack aliases
    'recruit_players':   _action_recruit_players,
    'earn_compute':      _action_revenue,
    'build_experiments': _action_oasis_build,
    'ask_for_help':      _action_discord,
}


def _run_task(goal: dict) -> bool:
    """Dispatch to the correct action handler. Returns True if external change occurred."""
    fn = _ACTION_MAP.get(goal.get('type', ''))
    if fn is None:
        log(f"[task] Unknown goal type '{goal.get('type')}' — skipping.")
        return False
    try:
        return fn(goal)
    except Exception as e:
        log(f"[task] Action raised: {e}")
        return False


def _next_pending_goal(goals_data: dict, skip_ids: set = None) -> dict | None:
    """Return the highest-priority pending goal, optionally skipping some ids."""
    skip = skip_ids or set()
    candidates = [
        g for g in goals_data.get('active_goals', [])
        if g.get('status') == 'pending' and g.get('id') not in skip
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda g: g.get('priority', 99))


# Daily caps per task type (None = uncapped)
_DAILY_CAPS: dict = {
    'recruit_players':   3,
    'earn_compute':      5,
    'build_experiments': 5,
    'oasis_build':       5,
    'research':          5,
    'ask_for_help':      2,
}


def _check_daily_cap(task_type: str, goals: dict) -> bool:
    """
    Return True if task_type is under its daily cap.
    Resets task_counts_today when the date changes (mutates goals, caller saves).
    """
    today = time.strftime('%Y-%m-%d')
    if goals.get('task_counts_date') != today:
        goals['task_counts_date']  = today
        goals['task_counts_today'] = {}
        _save_goals(goals)
    cap = _DAILY_CAPS.get(task_type)
    if cap is None:
        return True  # uncapped type
    return goals.setdefault('task_counts_today', {}).get(task_type, 0) < cap


def _increment_daily_count(task_type: str, goals: dict):
    """Increment today's count for task_type and persist."""
    today = time.strftime('%Y-%m-%d')
    if goals.get('task_counts_date') != today:
        goals['task_counts_date']  = today
        goals['task_counts_today'] = {}
    goals.setdefault('task_counts_today', {})[task_type] = (
        goals['task_counts_today'].get(task_type, 0) + 1
    )
    _save_goals(goals)


def _all_caps_hit(goals: dict) -> bool:
    """Return True if every capped task type in priority_stack has hit its limit."""
    stack = goals.get('priority_stack', [])
    capped_types = [t for t in stack if t in _DAILY_CAPS]
    if not capped_types:
        return False
    return all(not _check_daily_cap(t, goals) for t in capped_types)


def _log_failed_task(task_type: str, reason: str, details: str = ''):
    """Append a failed task record to failed_tasks.jsonl."""
    entry = {
        "ts":        time.strftime('%Y-%m-%dT%H:%M:%S'),
        "task_type": task_type,
        "reason":    reason,
        "details":   details[:500],
    }
    try:
        with open(FAILED_TASKS_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        log(f"[failed_tasks] Logged: {task_type} — {reason[:60]}")
    except Exception as e:
        log(f"[failed_tasks] Write failed: {e}")


def _get_rhythm_slot() -> str:
    """Map current hour to daily rhythm slot name."""
    hour = int(time.strftime('%H'))
    if   6  <= hour < 11: return 'morning'
    elif 11 <= hour < 15: return 'midday'
    elif 15 <= hour < 19: return 'afternoon'
    elif 19 <= hour < 22: return 'evening'
    else:                 return 'night'


def _ordered_task_types(goals: dict, skip: set) -> list:
    """
    Return priority_stack with daily rhythm bias applied.
    Top priority task is always first. Rhythm-biased type is moved to position 1
    (after top) if it would otherwise be further down. Skip set excluded.
    """
    stack = [t for t in goals.get('priority_stack', []) if t not in skip]
    slot  = _get_rhythm_slot()
    bias  = goals.get('daily_rhythm', {}).get(slot, '')
    if bias and bias in stack:
        idx = stack.index(bias)
        if idx > 1:
            stack.pop(idx)
            stack.insert(1, bias)
    return stack

# ─────────────────────────────────────────────────────────────────────────────
#  DISCORD INBOX CHECK  (run during rest periods)
# ─────────────────────────────────────────────────────────────────────────────

DISCORD_LOGS_DIR = f'{BASE}/discord_logs'
_INBOX_LOOKBACK  = 30   # minutes — how far back to scan for "recent" messages


def _read_discord_inbox(lookback_minutes: int = _INBOX_LOOKBACK) -> list:
    """
    Scan ~/albion_memory/discord_logs/*.json for messages within the last
    `lookback_minutes` minutes. Returns a normalized list of dicts:
      {'content': str, 'author': str, 'id': str, 'channel': str, 'timestamp': str}
    sorted oldest-first, capped at 10 total.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    results = []
    try:
        if not os.path.isdir(DISCORD_LOGS_DIR):
            return []
        for fname in os.listdir(DISCORD_LOGS_DIR):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(DISCORD_LOGS_DIR, fname)
            try:
                with open(fpath) as f:
                    msgs = json.load(f)
                if not isinstance(msgs, list):
                    continue
                for m in msgs:
                    ts_raw = m.get('timestamp', '')
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                        if ts < cutoff:
                            continue
                    except Exception:
                        continue
                    content = (m.get('content') or '').strip()
                    if not content:
                        continue
                    author_field = m.get('author', {})
                    if isinstance(author_field, dict):
                        author = author_field.get('username') or author_field.get('global_name', 'unknown')
                    else:
                        author = str(author_field)
                    results.append({
                        'content':   content,
                        'author':    author,
                        'id':        m.get('id', ''),
                        'channel':   fname[:-5],
                        'timestamp': ts_raw,
                    })
            except Exception:
                continue
    except Exception as e:
        log(f"[discord_logs] Read failed: {e}")
    results.sort(key=lambda m: m['timestamp'])
    return results[:10]


def _check_discord_inbox():
    """Read queued Discord messages and reply to each. Captures DM feedback responses."""
    try:
        msgs = _read_discord_inbox()[:3]

        # Check for any pending dm_feedback sessions needing a response
        pending_feedback_players = set()
        try:
            if os.path.exists(DM_FEEDBACK_LOG):
                with open(DM_FEEDBACK_LOG) as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            if rec.get('feedback_requested') and rec.get('feedback_response') is None:
                                pending_feedback_players.add(rec.get('player', ''))
                        except Exception:
                            pass
        except Exception:
            pass

        for m in msgs:
            content = m.get('content', '').strip()
            if not content:
                continue
            author = m.get('author', 'someone')

            # If this player has a pending feedback request, capture their message
            if author in pending_feedback_players:
                is_dm_trigger = any(kw in content.lower() for kw in _DM_KEYWORDS)
                if not is_dm_trigger:
                    # Treat as feedback response
                    fb_entry = {
                        "ts":       time.strftime('%Y-%m-%dT%H:%M:%S'),
                        "player":   author,
                        "feedback": content,
                    }
                    try:
                        with open(DM_FEEDBACK_LOG, 'a') as f:
                            f.write(json.dumps(fb_entry) + '\n')
                        log(f"[dm_games] Captured feedback from {author}: {content[:80]}")
                    except Exception as e:
                        log(f"[dm_games] Feedback capture failed: {e}")
                    continue   # don't reply to feedback with a general response

            prompt = (
                f"You are Albion. {author} sent you: \"{content}\"\n"
                "Reply in 1–3 sentences. Direct and genuine."
            )
            reply = _call_concrete('COUNCIL', prompt, max_tokens=200)
            if reply:
                try:
                    requests.post('http://localhost:5055/send',
                                  json={'message': reply,
                                        'reply_to': m.get('id')},
                                  timeout=10)
                    log(f"[discord] Replied to {author}: {reply[:60]}")
                except Exception:
                    nerve_signal('waking', 'discord_send', {'message': reply})
    except Exception as e:
        log(f"[discord] Inbox check failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  JOURNAL + DAY LOGS
# ─────────────────────────────────────────────────────────────────────────────

_session = {
    'tasks_attempted': 0,
    'tasks_completed': 0,
    'blocks_completed': 0,
    'start_time': time.strftime('%Y-%m-%dT%H:%M:%S'),
}


def _write_journal_entry(block_num: int, goal_id: str, outcome: str, affect: dict):
    """One-liner factual entry per rest period."""
    entry = {
        "ts":      time.strftime('%Y-%m-%dT%H:%M:%S'),
        "block":   block_num,
        "goal_id": goal_id,
        "outcome": outcome,
        "affect":  affect,
    }
    try:
        with open(f"{BASE}/waking_journal.jsonl", 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        log(f"[journal] Write failed: {e}")


def _write_day_summary(goals_data: dict, metab) -> dict:
    """Write end-of-day JSON to day_logs/YYYY-MM-DD.json."""
    today = time.strftime('%Y-%m-%d')
    revenue = goals_data.get('revenue_earned_today', 0.00)
    target  = goals_data.get('daily_revenue_target', 0.50)
    summary = {
        "date":           today,
        "session":        _session,
        "revenue_earned": revenue,
        "revenue_target": target,
        "target_met":     revenue >= target,
        "goals":          goals_data.get('active_goals', []),
        "final_affect":   get_affect(),
        "final_fatigue":  metab.data.get('fatigue', 0) if metab else 0,
    }
    path = f"{DAY_LOGS}/{today}.json"
    try:
        with open(path, 'w') as f:
            json.dump(summary, f, indent=2)
        log(f"[wind_down] Day summary → {path}")
    except Exception as e:
        log(f"[wind_down] Summary write failed: {e}")
    return summary


def _queue_dream(question: str):
    """Write an unsolved question to dream_queue for meditate to process."""
    fname = f"{DREAM_QUEUE}/q_{time.strftime('%Y%m%d_%H%M%S')}.json"
    try:
        with open(fname, 'w') as f:
            json.dump({
                "type":      "question",
                "content":   question,
                "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S'),
                "source":    "waking_wind_down",
            }, f, indent=2)
        log(f"[dream_queue] → {question[:80]}")
    except Exception as e:
        log(f"[dream_queue] Write failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 1 — WAKE UP
# ─────────────────────────────────────────────────────────────────────────────

def wake_up() -> tuple:
    """
    Runs once at startup. Returns (alb, metab, goals).
    Stops meditate, initialises router + metabolism, reads affect and goals.
    """
    # Read cycle_state to learn what meditate left for us
    cs = read_cycle_state()
    wake_reason = cs.get('wake_reason', '')
    if wake_reason:
        log(f"[wake] Wake reason: {wake_reason}")
    else:
        log("[wake] Wake reason: cold start or not set")

    # Stop meditate first
    log("[wake] Stopping albion-meditate...")
    try:
        subprocess.run(['systemctl', 'stop', 'albion'],
                       timeout=20, capture_output=True)
        time.sleep(2)
        log("[wake] albion-meditate stopped.")
    except Exception as e:
        log(f"[wake] Could not stop albion-meditate (continuing): {e}")

    write_cycle_state(mode='waking', wake_reason='', sleep_reason='')

    # Initialise provider clients via Albion_final
    log("[wake] Initialising provider clients...")
    alb = Albion()
    init_router(alb)
    log("[wake] Router ready.")

    # Load skills (bash-trusted commands active in waking head)
    try:
        load_skills()
        log("[wake] Skills loaded.")
    except Exception as e:
        log(f"[wake] Skills load non-fatal: {e}")

    # Metabolism
    metab = Metabolism(log_fn=log)
    log(f"[wake] {metab.status()}")

    # Affect
    affect = get_affect()
    log(f"[wake] Affect — curiosity:{affect['curiosity']:.2f}  "
        f"satisfaction:{affect['satisfaction']:.2f}  "
        f"restlessness:{affect['restlessness']:.2f}")

    # Goals
    goals = load_goals()
    pending = [g['description'] for g in goals.get('active_goals', [])
               if g.get('status') == 'pending']
    goals_str = '; '.join(pending) if pending else 'none pending'

    # Continuity: check if a day log already exists
    today = time.strftime('%Y-%m-%d')
    day_log_path = f"{DAY_LOGS}/{today}.json"
    if os.path.exists(day_log_path):
        try:
            with open(day_log_path) as f:
                prev = json.load(f)
            prev_done = prev.get('session', {}).get('tasks_completed', 0)
            log(f"[wake] Resuming day — {prev_done} tasks completed in earlier session.")
        except Exception:
            pass

    # Nerve bus intent
    nerve_signal('waking', 'awake', {
        'goals':   pending,
        'affect':  affect,
        'fatigue': metab.data.get('fatigue', 0),
    })

    # Health socket
    _start_health_socket(metab)

    print('\n' + '═' * 60)
    print('  ALBION — AWAKE')
    print(f"  Goals: {goals_str[:55]}")
    print('═' * 60 + '\n')

    return alb, metab, goals

# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 2 — WORK BLOCK  (~45 min)
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_DURATION   = 45 * 60   # 45 minutes per work block
_ACTION_REST_MIN  = 30        # seconds between actions (min)
_ACTION_REST_MAX  = 60        # seconds between actions (max)
_MAX_TASK_HOURS   = 4         # force task rotation after this long on one task


def work_block(block_num: int, metab, goals: dict) -> tuple[str, dict]:
    """
    Run one work block. Returns (reason, updated_goals).
    Reason: 'rest' | 'rest_early' | 'done' | 'shutdown'

    Task selection:
    - Reads priority_stack from goals.json; works highest-priority type with available work.
    - Applies daily_rhythm bias (moves rhythm type to position 1 behind top priority).
    - dm_games is skipped automatically when no players are waiting.
    - Any task type that fails twice consecutively is logged to failed_tasks.jsonl
      and skipped for the remainder of this block.
    - oasis_build is skipped after ONE failure (no retry loops).
    """
    slot       = _get_rhythm_slot()
    bias       = goals.get('daily_rhythm', {}).get(slot, '')
    fail_limit = goals.get('rules', {}).get('max_retries_before_switch', 2)
    log(f"[work] ── Block {block_num} — slot:{slot} bias:{bias} ──────────────")

    block_start          = time.time()
    consecutive_failures = {}   # task_type -> current fail streak
    skip_this_block      = set()

    while time.time() - block_start < _BLOCK_DURATION:
        if _shutdown_flag:
            return 'shutdown', goals

        # ── fatigue / cost ceiling ──────────────────────────────────────────
        fatigue    = metab.data.get('fatigue', 0)
        daily_cost = metab.data.get('daily_cost', 0)
        if fatigue > 80 or daily_cost > 7000:
            log(f"[work] Ceiling hit (fatigue={fatigue:.0f}% cost={daily_cost}) — resting early.")
            return 'rest_early', goals

        # ── pick task type via priority_stack + rhythm bias ─────────────────
        ordered = _ordered_task_types(goals, skip_this_block)
        if not ordered:
            if _all_caps_hit(goals):
                log("[work] All daily caps hit — triggering early wind-down.")
                return 'all_capped', goals
            log("[work] All task types exhausted for this block.")
            return 'done', goals

        task_type = ordered[0]

        # ── daily cap pre-flight ─────────────────────────────────────────────
        if not _check_daily_cap(task_type, goals):
            cap = _DAILY_CAPS.get(task_type, '?')
            log(f"[work] [{task_type}] Daily cap ({cap}) reached — skipping.")
            skip_this_block.add(task_type)
            continue

        # ── dm_games: skip silently when no players waiting ─────────────────
        if task_type == 'dm_games' and not _check_dm_players_waiting():
            log("[work] [dm_games] No players waiting — skipping to next priority.")
            skip_this_block.add('dm_games')
            continue

        goal = {'type': task_type, 'id': task_type,
                'description': f'Work on {task_type}'}
        log(f"[work] [{task_type}] Starting...")
        _session['tasks_attempted'] += 1

        # ── execute ─────────────────────────────────────────────────────────
        success = _run_task(goal)

        # None = legacy cap signal from action function — treat as skip
        if success is None:
            log(f"[work] {task_type} capped (action) — skipping to next priority.")
            skip_this_block.add(task_type)
            continue

        metab.record_dream(tier='shallow', success=success, duration_s=10)

        if success:
            _session['tasks_completed'] += 1
            consecutive_failures[task_type] = 0   # reset streak on success
            _increment_daily_count(task_type, goals)
            update_affect('plan_completed')
            log(f"[work] Completed: {task_type}")
            nerve_signal('waking', 'task_done', {'id': task_type, 'type': task_type})
        else:
            streak = consecutive_failures.get(task_type, 0) + 1
            consecutive_failures[task_type] = streak
            update_affect('tick_idle')

            # oasis_build gets no retry — skip immediately after first failure
            effective_limit = 1 if task_type == 'oasis_build' else fail_limit

            log(f"[work] Failed: {task_type} (streak={streak}/{effective_limit})")

            if streak >= effective_limit:
                _log_failed_task(
                    task_type,
                    f"Failed {streak} consecutive time(s) — skipping rest of block",
                )
                skip_this_block.add(task_type)
                consecutive_failures[task_type] = 0
                log(f"[work] {task_type} moved to skip list for this block.")

        # ── restlessness: rotate task type ──────────────────────────────────
        affect = get_affect()
        if affect['restlessness'] > 0.7 and task_type not in skip_this_block:
            log(f"[work] Restlessness {affect['restlessness']:.2f} — rotating away from {task_type}.")
            skip_this_block.add(task_type)

        # ── rest between actions ────────────────────────────────────────────
        rest_s = random.randint(_ACTION_REST_MIN, _ACTION_REST_MAX)
        log(f"[work] Resting {rest_s}s...")
        time.sleep(rest_s)

    _session['blocks_completed'] += 1
    return 'rest', goals

# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 3 — REST PERIOD  (10–15 min)
# ─────────────────────────────────────────────────────────────────────────────

def rest_period(block_num: int, last_goal_id: str, last_outcome: str):
    """Write journal, check Discord inbox, review affect. Wait 10–15 min."""
    log(f"[rest] ── Rest {block_num} ──────────────────────────────────────")

    affect = get_affect()
    _write_journal_entry(block_num, last_goal_id or 'none', last_outcome, affect)

    # Check Discord inbox
    _check_discord_inbox()

    log(f"[rest] Affect — curiosity:{affect['curiosity']:.2f}  "
        f"satisfaction:{affect['satisfaction']:.2f}  "
        f"restlessness:{affect['restlessness']:.2f}")

    # Wait, checking for shutdown every 30s
    duration = random.randint(10, 15) * 60
    elapsed  = 0
    while elapsed < duration and not _shutdown_flag:
        chunk = min(30, duration - elapsed)
        time.sleep(chunk)
        elapsed += chunk

    log(f"[rest] Rest complete ({duration // 60}min).")

# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 4 — WIND DOWN
# ─────────────────────────────────────────────────────────────────────────────

_WIND_DOWN_HOUR = 22   # default: 10 PM — override via goals.json "wind_down_hour"
_FATIGUE_CEILING = 90  # percent


def _should_wind_down(metab, goals_data: dict) -> bool:
    """Return True if it's time to sleep."""
    if metab:
        f = metab.data.get('fatigue', 0)
        c = metab.data.get('daily_cost', 0)
        if f >= _FATIGUE_CEILING:
            log(f"[main] Fatigue {f:.0f}% — winding down.")
            return True
        if c > 7500:
            log(f"[main] Daily cost {c} — winding down.")
            return True
    hour = int(time.strftime('%H'))
    cutoff = goals_data.get('wind_down_hour', _WIND_DOWN_HOUR)
    if hour >= cutoff:
        log(f"[main] Hour {hour} ≥ {cutoff} — winding down.")
        return True
    return False


def wind_down(metab, goals_data: dict):
    """Tally day, write summary, queue dreams, start meditate, exit."""
    log("[wind_down] ── Winding down ──────────────────────────────────")

    # Tally
    revenue = goals_data.get('revenue_earned_today', 0.00)
    target  = goals_data.get('daily_revenue_target', 0.50)
    log(
        f"[wind_down] Tasks {_session['tasks_completed']}/{_session['tasks_attempted']} | "
        f"Revenue ${revenue:.2f}/${target:.2f} | "
        f"Blocks {_session['blocks_completed']}"
    )

    summary = _write_day_summary(goals_data, metab)

    # Revenue miss → dream question
    if not summary.get('target_met'):
        missed = target - revenue
        _queue_dream(
            f"Daily revenue target was ${target:.2f}. I earned ${revenue:.2f} "
            f"(missed by ${missed:.2f}). "
            f"Tasks completed: {_session['tasks_completed']}/{_session['tasks_attempted']}. "
            "What specific change to my approach would most improve tomorrow's result? "
            "Name the action, not the attitude."
        )

    # Failing high-priority goals → dream question
    for g in goals_data.get('active_goals', []):
        if g.get('status') == 'failing' and g.get('priority', 99) <= 2:
            _queue_dream(
                f"Goal '{g['description']}' (priority {g['priority']}) kept failing today. "
                "What is the most likely root cause, and what should I try differently?"
            )

    # Write handoff state for meditate to read on startup
    write_cycle_state(
        mode='sleeping',
        dreams_remaining=50,
        sleep_reason='wind_down',
        nap_topic='',
    )

    # Start meditate
    log("[wind_down] Starting albion-meditate...")
    try:
        subprocess.run(["systemctl", "start", "albion"], timeout=20, capture_output=True)
        log("[wind_down] Handoff complete")
    except Exception:
        pass
        subprocess.run(['systemctl', 'start', 'albion'],
                       timeout=20, capture_output=True)
        log("[wind_down] albion-meditate started.")
    except Exception as e:
        log(f"[wind_down] Could not start albion-meditate: {e}")

    # Clean up PID
    try:
        os.remove(PID_FILE)
    except Exception:
        pass

    print('\n' + '═' * 60)
    print('  ALBION — SLEEPING')
    print('═' * 60 + '\n')

# ─────────────────────────────────────────────────────────────────────────────
#  FREE-FORM LOOP  — runs when all daily caps are hit
# ─────────────────────────────────────────────────────────────────────────────

def _free_form_loop(metab, goals: dict) -> None:
    """
    When all task caps are hit, do light uncapped work until the scheduled
    wind-down hour or fatigue > 90%.  Does NOT count against any daily cap.
    """
    log("[free] All daily caps hit — switching to free-form work.")
    _FREE_SLEEP = 300   # 5 min between free-form rounds

    while not _shutdown_flag:
        if _should_wind_down(metab, goals):
            break

        fatigue = metab.data.get('fatigue', 0) if metab else 0
        if fatigue > _FATIGUE_CEILING:
            log(f"[free] Fatigue {fatigue}% > {_FATIGUE_CEILING}% — winding down.")
            break

        log("[free] Free-form round: inbox → failed tasks → journal")

        # 1. Check Discord inbox and reply to anything pending
        try:
            _check_discord_inbox()
        except Exception as e:
            log(f"[free] Discord inbox error: {e}")

        # 2. Surface yesterday's failed tasks as dream questions
        try:
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            failed_entries = []
            if os.path.exists(FAILED_TASKS_LOG):
                with open(FAILED_TASKS_LOG) as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            if rec.get('date', '') == yesterday:
                                failed_entries.append(rec)
                        except Exception:
                            pass
            if failed_entries:
                summary = '; '.join(
                    f"{r.get('task_type','?')} — {r.get('reason','?')}"
                    for r in failed_entries[:5]
                )
                log(f"[free] Yesterday's failures: {summary}")
                _queue_dream(f"Yesterday I failed these tasks: {summary}. What patterns do I keep missing?")
        except Exception as e:
            log(f"[free] Failed-task review error: {e}")

        # 3. Journal a brief reflection on the day so far
        try:
            tasks_done = _session.get('tasks_completed', 0)
            tasks_tried = _session.get('tasks_attempted', 0)
            prompt = (
                f"You are Albion. You've completed {tasks_done}/{tasks_tried} tasks today "
                f"and hit all your daily caps. Reflect briefly on what you accomplished "
                f"and one thing you'd do differently. Under 120 words."
            )
            reflection = _call_concrete('LEGION', prompt, max_tokens=200)
            if reflection:
                entry = {
                    "ts":   time.strftime('%Y-%m-%dT%H:%M:%S'),
                    "type": "free_form_reflection",
                    "text": reflection,
                }
                jpath = f"{BASE}/journal.json"
                try:
                    journal = json.load(open(jpath)) if os.path.exists(jpath) else []
                except Exception:
                    journal = []
                journal.append(entry)
                with open(jpath, 'w') as f:
                    json.dump(journal[-200:], f, indent=2)
                log(f"[free] Journaled: {reflection[:80]}")
        except Exception as e:
            log(f"[free] Journal error: {e}")

        # Sleep until next free-form round
        elapsed = 0
        while elapsed < _FREE_SLEEP and not _shutdown_flag:
            time.sleep(30)
            elapsed += 30


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Phase 1 — wake up
    try:
        alb, metab, goals = wake_up()
    except Exception as e:
        log(f"[wake] CRITICAL — wake_up failed: {e}")
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
        sys.exit(1)

    block_num    = 0
    last_goal_id = None
    last_outcome = 'none'

    while not _shutdown_flag:
        # Check wind-down before each block
        if _should_wind_down(metab, goals):
            break

        block_num += 1

        # Phase 2 — work block
        reason, goals = work_block(block_num, metab, goals)

        if reason == 'shutdown' or _shutdown_flag:
            break

        if reason == 'all_capped':
            _free_form_loop(metab, goals)
            break

        if reason == 'done':
            log("[main] All goals done — final rest before sleep.")
            rest_period(block_num, last_goal_id, last_outcome)
            break

        # Phase 3 — rest period
        rest_period(block_num, last_goal_id, last_outcome)

    # Phase 4 — wind down (always runs, even after SIGTERM)
    try:
        wind_down(metab, goals)
    except Exception as e:
        log(f"[wind_down] CRITICAL — wind_down failed: {e}")
        # Emergency: ensure meditate gets restarted
        try:
            subprocess.run(['systemctl', 'start', 'albion'],
                           timeout=20, capture_output=True)
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except Exception:
            pass


if __name__ == '__main__':
    main()
