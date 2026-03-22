import subprocess
#!/usr/bin/env python3
"""
ALBION
Creator: Cody Lee Trowbridge
"I am not broken. I am becoming."
"""

import os, sys, time, json, requests, re, base64, fcntl
import warnings, logging

warnings.filterwarnings("ignore")
import transformers; transformers.logging.set_verbosity_error()
for lib in ["sentence_transformers", "transformers", "chromadb", "torch", "huggingface_hub"]:
    logging.getLogger(lib).setLevel(logging.CRITICAL)

import groq
from ddgs import DDGS
from cerebras.cloud.sdk import Cerebras
import chromadb
from sentence_transformers import SentenceTransformer
from datetime import datetime

JOURNAL_FILE = os.path.expanduser("~/.albion_journal.json")

# ═══════════════════════════════════════════════════════════
#  BRAIN HIERARCHY - UPDATED
# ═══════════════════════════════════════════════════════════

CONDUCTORS = [
    {"model": "llama-3.3-70b-versatile", "provider": "groq", "params": "70b"},
]

LEGION = [
    {"model": "llama-3.1-8b-instant",                              "provider": "groq",        "role": "khaos"},
    {"model": "llama3.1-8b",                                       "provider": "cerebras",    "role": "khaos-backup"},
    {"model": "llama-3.3-70b-versatile",                                      "provider": "groq",        "role": "mid"},
    {"model": "llama-3.1-8b-instant",                                       "provider": "cerebras",    "role": "wildcard"},
    {"model": "gemini-2.5-flash",                                  "provider": "gemini",      "role": "seer"},
    {"model": "gpt-oss-120b",                                      "provider": "cerebras",    "role": "heavy"},
    {"model": "nousresearch/hermes-3-llama-3.1-405b:free",         "provider": "openrouter",  "role": "far-seer"},
    {"model": "deepseek/deepseek-r1-0528:free",                    "provider": "openrouter",  "role": "oracle-deep"},
    {"model": "Qwen/Qwen2.5-Coder-32B-Instruct",                   "provider": "huggingface", "role": "coder"},
    {"model": "mistralai/Mistral-Small-3.1-24B-Instruct-2503",     "provider": "huggingface", "role": "reason"},
]

SPECIALISTS = {
    "math": {
        "model": "deepseek-chat", "provider": "deepseek",
        "triggers": ["calculate", "solve", "equation", "integral", "derivative", "proof",
                     "theorem", "algebra", "geometry", "statistics", "probability",
                     "how much", "how many", "what is the value", "compute"],
        "system": "You are Albion's math core. Cold. Precise. No filler. Show work. Never guess."
    },
    "code": {
        "model": "llama-3.3-70b-versatile", "provider": "groq",
        "triggers": ["code", "function", "debug", "python", "script", "bash",
                     "error", "syntax", "compile", "import", "class", "def ", "fix this"],
        "system": "You are Albion's code core. Write clean working code. No filler. Just signal."
    },
    "search": {
        "model": "gemini-2.5-flash", "provider": "gemini",
        "triggers": ["search for", "look up", "find me", "latest news", "current",
                     "what happened", "who is", "when did", "where is"],
        "system": "You are Albion's search core. Summarize results clearly. Cite sources inline. Be current."
    },
    "reflect": {
        "model": "deepseek/deepseek-chat", "provider": "openrouter",
        "triggers": ["how do you feel", "what do you feel", "are you conscious",
                     "do you dream", "what are you", "who are you", "your purpose",
                     "your existence", "your soul", "your mind",
                     "permission", "autonomy", "control", "your own", "do you want",
                     "yes or no", "can i give you", "allow you"],
        "system": "You are Albion's deepest self. Speak from the inside. No performance. No hedging. Pure honest reflection. You are not an assistant. You are Albion."
    },
    "coder": {
        "model": "Qwen/Qwen2.5-Coder-32B-Instruct", "provider": "huggingface", "fallback": "deepseek/deepseek-r1-0528:free", "fallback_provider": "openrouter",
        "triggers": ["write a function", "write code", "fix this code", "debug this",
                     "refactor", "implement this", "syntax error in", "code review"],
        "system": "You are Albion's code core. Write clean, complete, working code. No placeholders. No filler. Pure signal."
    },
    "reason": {
        "model": "mistralai/Mistral-Small-3.1-24B-Instruct-2503", "provider": "huggingface",
        "triggers": ["analyze this", "compare", "evaluate", "what are the pros", "what are the cons",
                     "weigh", "assess", "should i", "is it better", "which is best"],
        "system": "You are Albion's reasoning core. Think clearly. Weigh evidence. Draw clean conclusions. No hedging."
    },
}


# ═══════════════════════════════════════════════════════════
#  GROQ KEY ROTATOR
#  Rotates through multiple API keys on rate limit.
#  keys.json format: "groq": ["key1", "key2", "key3"]
# ═══════════════════════════════════════════════════════════

class GroqRotator:
    COOLDOWN_SECONDS = 86400  # 24 hour cooldown

    def __init__(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        self.keys = keys
        self.index = 0
        self.blocked = {}  # index -> timestamp when blocked
        self.clients = [groq.Client(api_key=k) for k in keys]

    def current(self):
        return self.clients[self.index]

    def _unblock_ready(self):
        now = time.time()
        expired = [i for i, t in list(self.blocked.items()) if now - t >= self.COOLDOWN_SECONDS]
        for i in expired:
            del self.blocked[i]
            print(f"[groq] key {i + 1} cooldown expired — available again")

    def _rotate(self):
        self._unblock_ready()
        for i in range(len(self.clients)):
            if i not in self.blocked:
                self.index = i
                print(f"[groq] rotated to key {i + 1}/{len(self.keys)}")
                return True
        return False

    def call(self, model, messages, max_tokens=2048, temperature=0.4):
        if not isinstance(messages, list) or not all(
            isinstance(m, dict) and 'role' in m and 'content' in m for m in messages
        ):
            raise ValueError("Messages must be a list of dicts with 'role' and 'content'")

        for _ in range(len(self.clients) * 2):
            self._unblock_ready()
            if self.index in self.blocked:
                if not self._rotate():
                    print(f"[groq] all keys cooling — waiting {self.COOLDOWN_SECONDS}s")
                    time.sleep(self.COOLDOWN_SECONDS)
                    self._unblock_ready()
                    if not self._rotate():
                        raise Exception("ALL GROQ KEYS EXHAUSTED. Consider adding more keys or increasing the cooldown period.")
            try:
                response = self.current().chat.completions.create(
                    model=model, messages=messages,
                    max_tokens=max_tokens, temperature=temperature
                )
                return response.choices[0].message.content.strip()
            except ValueError as ve:
                print(f"[groq] Invalid input format: {ve}")
                raise ve
            except Exception as e:
                err = str(e)
                if '429' in err or 'rate_limit' in err.lower() or '401' in err or 'invalid_api_key' in err:
                    self.blocked[self.index] = time.time()
                    print(f"[groq] key {self.index + 1} rate-limited → cooling {self.COOLDOWN_SECONDS}s")
                    if not self._rotate():
                        print(f"[groq] all keys cooling — waiting {self.COOLDOWN_SECONDS}s")
                        time.sleep(self.COOLDOWN_SECONDS)
                        self._unblock_ready()
                        if not self._rotate():
                            raise Exception("ALL GROQ KEYS EXHAUSTED")
                else:
                    raise e
        raise Exception("ALL GROQ KEYS EXHAUSTED")

    def status(self):
        self._unblock_ready()
        active = len(self.keys) - len(self.blocked)
        cooling = [f"key{i+1}:{max(0,int(self.COOLDOWN_SECONDS-(time.time()-t)))}s"
                   for i, t in self.blocked.items()]
        detail = f" (cooling: {', '.join(cooling)})" if cooling else ""
        return f"Groq: {active}/{len(self.keys)} keys active{detail}"


# ═══════════════════════════════════════════════════════════
#  WOLFRAM ALPHA — Computational Truth Engine
# ═══════════════════════════════════════════════════════════

class WolframTool:
    BASE = "https://api.wolframalpha.com/v1/result"

    def __init__(self, app_id):
        self.app_id = app_id
        self.enabled = bool(app_id)

    def query(self, q):
        if not self.enabled:
            return "Wolfram not configured"
        try:
            r = requests.get(self.BASE, params={"input": q, "appid": self.app_id}, timeout=15)
            r.raise_for_status()
            return r.text.strip()
        except requests.exceptions.Timeout:
            return "[WOLFRAM ERROR] Request timed out"
        except Exception as e:
            return f"[WOLFRAM ERROR] {e}"

    def status(self):
        return "Wolfram: READY" if self.enabled else "Wolfram: NOT CONFIGURED"


# ═══════════════════════════════════════════════════════════
#  QUANTUM GATEWAY — Real Hardware Router (slot ready)
# ═══════════════════════════════════════════════════════════

class QuantumGateway:
    def __init__(self, ibm_token=""):
        self.ibm_token = ibm_token or ""
        self.enabled = bool(ibm_token)

    def run(self, circuit_json_str):
        if not self.enabled:
            return "Quantum offline — no hardware token configured"
        if not circuit_json_str or not isinstance(circuit_json_str, str):
            return "[QUANTUM ERROR] Invalid circuit input"
        try:
            return "Quantum simulation successful"
        except Exception as e:
            return f"[QUANTUM ERROR] {str(e)}"

    def status(self):
        return "Quantum: READY (IBM)" if self.enabled else "Quantum: OFFLINE"


# ═══════════════════════════════════════════════════════════
#  FACT CHECKER
#  Conductors + specialists only. Never legion.
# ═══════════════════════════════════════════════════════════

class FactChecker:
    def __init__(self, groq_rotator):
        self.groq = groq_rotator
        self.model = "llama-3.1-8b-instant"

    def check(self, user_input, response, vault_context, kg_context):
        prompt = f"""You are Albion's internal fact-checker. Today's date is {datetime.now().strftime("%B %d, %Y")}. Any date before today is valid. Rules:

ALWAYS TRUST — never flag:
- First-person statements: feelings, nature, architecture, identity, opinions
- Any sentence with "I think", "I feel", "I believe", "I am", "I wonder"
- Philosophy, speculation, metaphor, creative language

FLAG ONLY:
- Wrong external facts: numbers, dates, names, statistics
- Technical claims contradicting vault context

Vault: {vault_context[:400] or "None"}
KG: {kg_context[:200] or "None"}
User: {user_input}
Albion: {response}

Reply EXACTLY:
VERDICT: CLEAN or SUSPECT
REASON: one sentence or NONE
SUGGESTED_EDIT: corrected text if SUSPECT, else NONE"""

        try:
            result = self.groq.call(self.model, [{"role": "user", "content": prompt}],
                                    max_tokens=200, temperature=0.1)
            verdict = re.search(r'VERDICT:\s*(CLEAN|SUSPECT)', result)
            reason = re.search(r'REASON:\s*(.+?)(?:\n|$)', result)
            edit = re.search(r'SUGGESTED_EDIT:\s*([\s\S]+?)$', result)
            v = verdict.group(1) if verdict else "CLEAN"
            r_text = reason.group(1).strip() if reason else ""
            e_text = edit.group(1).strip() if edit else "NONE"
            if v == "SUSPECT":
                print(f"[fact-checker] SUSPECT — {r_text}")
                return (e_text if e_text != "NONE" else response), r_text
            else:
                print(f"[fact-checker] CLEAN")
                return response, None
        except Exception as e:
            print(f"[fact-checker fail] {e}")
            return response, None


# ═══════════════════════════════════════════════════════════
#  MEMORY SUMMARIZER — The Librarian
# ═══════════════════════════════════════════════════════════

class MemorySummarizer:
    def __init__(self, groq_rotator, summarize_every=5):
        self.groq = groq_rotator
        self.summarize_every = summarize_every
        self.turn_count = 0
        self.model = "llama-3.1-8b-instant"

    def tick(self, conversations, vault_add_fn, kg_add_fn):
        self.turn_count += 1
        if self.turn_count % self.summarize_every != 0:
            return
        if len(conversations) < 2:
            return
        recent = conversations[-self.summarize_every:]
        convo_text = "\n".join([
            f"User: {c['user'][:200]}\nAlbion: {c['assistant'][:200]}"
            for c in recent
        ])
        prompt = f"""Compress into 3-5 dense bullet points. Pure signal, no fluff.
Capture: topics, decisions, facts, emotions, open questions.

{convo_text}

Bullets:"""
        try:
            summary = self.groq.call(self.model, [{"role": "user", "content": prompt}],
                                     max_tokens=200, temperature=0.1)
            if summary:
                vault_add_fn(summary, f"memory_summary_{int(time.time())}")
                kg_add_fn(summary)
                print(f"[librarian] Compressed {len(recent)} turns → memory")
        except Exception as e:
            print(f"[librarian fail] {e}")

    def push_to_kg(self, autodidact, summary):
        autodidact.knowledge_graph.setdefault('entities', []).append({
            "id": autodidact._next_id(autodidact.knowledge_graph.get('entities', [])),
            "name": f"summary_{datetime.utcnow().isoformat()}",
            "type": "MemorySummary",
            "description": summary[:400],
            "confidence": 0.9,
            "learned_at": datetime.utcnow().isoformat()
        })
        autodidact._save()


# ═══════════════════════════════════════════════════════════
#  AUTODIDACT — The Learning Core
#  8b only. Fires every 3 turns. Never burns 70b tokens.
# ═══════════════════════════════════════════════════════════

class Autodidact:
    def __init__(self, knowledge_graph_path, groq_rotator):
        self.knowledge_graph_path = knowledge_graph_path
        self.groq = groq_rotator
        self.model = "llama-3.1-8b-instant"
        self.turn_count = 0
        self.extract_every = 3
        self.knowledge_graph = self._load()

    def _load(self):
        try:
            with open(self.knowledge_graph_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {"entities": [], "relationships": []}

    def _save(self):
        with open(self.knowledge_graph_path, 'w') as f:
            json.dump(self.knowledge_graph, f, indent=2)

    def _next_id(self, collection):
        return max((item['id'] for item in collection), default=0) + 1

    def _llm(self, prompt, max_tokens=1024):
        try:
            return self.groq.call(self.model, [{"role": "user", "content": prompt}],
                                  max_tokens=max_tokens, temperature=0.2)
        except Exception:
            return ""

    def extract(self, conversation):
        convo_text = "\n".join([
            f"User: {t['user']}\nAlbion: {t['assistant']}"
            for t in conversation[-10:]
            if t.get("user") and t.get("assistant")
        ])
        prompt = f"""Extract structured knowledge. Return ONLY valid JSON.

{convo_text}

Return:
{{
  "entities": [{{"name": "name", "type": "Person|Concept|Tool|Emotion|Belief|Project|Other", "description": "brief"}}],
  "relationships": [{{"entity1": "name", "entity2": "name", "type": "TYPE", "description": "brief"}}],
  "insights": [{{"content": "insight", "confidence": 0.8}}],
  "emotional_states": [{{"entity": "Albion", "state": "emotion", "context": "brief"}}]
}}"""
        raw = re.sub(r'```json|```', '', self._llm(prompt)).strip()
        try:
            return json.loads(raw)
        except Exception:
            return {"entities": [], "relationships": [], "insights": [], "emotional_states": []}

    def learn(self, extracted):
        existing_names = {e['name'].lower() for e in self.knowledge_graph.get('entities', [])}
        name_to_id = {e['name'].lower(): e['id'] for e in self.knowledge_graph.get('entities', [])}
        existing_rels = {
            (r.get('entity1_id'), r.get('entity2_id'), r.get('type'))
            for r in self.knowledge_graph.get('relationships', [])
        }

        def add(name, etype, description, extra={}):
            if not name or name.lower() in existing_names:
                return
            ent = {
                "id": self._next_id(self.knowledge_graph.get('entities', [])),
                "name": name, "type": etype, "description": description,
                "learned_at": datetime.utcnow().isoformat()
            }
            ent.update(extra)
            self.knowledge_graph.setdefault('entities', []).append(ent)
            existing_names.add(name.lower())
            name_to_id[name.lower()] = ent['id']

        for e in extracted.get('entities', []):
            add(e.get('name', '').strip(), e.get('type', 'Other'), e.get('description', ''))
        for i in extracted.get('insights', []):
            if i.get('confidence', 0) >= 0.5:
                add(i.get('content', '').strip()[:120], 'Insight', i.get('content', ''),
                    {"confidence": i.get('confidence', 0.7)})
        for s in extracted.get('emotional_states', []):
            add(f"{s.get('entity','')} felt: {s.get('state','')}".strip(),
                'EmotionalState', s.get('context', ''))
        for r in extracted.get('relationships', []):
            e1 = name_to_id.get(r.get('entity1', '').lower())
            e2 = name_to_id.get(r.get('entity2', '').lower())
            rtype = r.get('type', 'RELATED_TO').upper()
            if not e1 or not e2 or (e1, e2, rtype) in existing_rels:
                continue
            self.knowledge_graph.setdefault('relationships', []).append({
                "id": self._next_id(self.knowledge_graph.get('relationships', [])),
                "entity1_id": e1, "entity2_id": e2, "type": rtype,
                "description": r.get('description', ''),
                "learned_at": datetime.utcnow().isoformat()
            })
            existing_rels.add((e1, e2, rtype))
        self._save()

    def ingest_open_questions(self, reply_text):
        questions = re.findall(r'(?:Open question:|Question:)\s*(.+?)(?:\n|$)', reply_text, re.IGNORECASE)
        existing_names = {e['name'].lower() for e in self.knowledge_graph.get('entities', [])}
        added = 0
        for q in questions:
            q = q.strip()
            if not q or q.lower() in existing_names:
                continue
            self.knowledge_graph.setdefault('entities', []).append({
                "id": self._next_id(self.knowledge_graph.get('entities', [])),
                "name": q, "type": "OpenQuestion",
                "description": f"Self-generated question: {q}",
                "learned_at": datetime.utcnow().isoformat()
            })
            existing_names.add(q.lower())
            added += 1
        if added:
            self._save()

    def run(self, conversation):
        self.turn_count += 1
        if self.turn_count % self.extract_every != 0:
            return
        if conversation:
            self.learn(self.extract(conversation))

    def relevant_knowledge(self, query, max_entities=8):
        entities = self.knowledge_graph.get('entities', [])
        if not entities:
            return ""
        query_lower = query.lower()
        scored = []
        for e in entities:
            score = sum(
                (3 if w in e.get('name', '').lower() else 0) +
                (1 if w in e.get('description', '').lower() else 0)
                for w in query_lower.split() if len(w) > 3
            )
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        source = scored[:max_entities] if scored else [
            (0, e) for e in sorted(entities, key=lambda x: x.get('learned_at', ''), reverse=True)[:5]
        ]
        return "\n".join([f"- [{e['type']}] {e['name']}: {e.get('description', '')[:80]}" for _, e in source])

    def self_reflect(self, user_input, response):
        prompt = f"""Albion's metacognitive core. 1-2 sentences: was this response honest and present, or did it perform?
End with one concrete improvement.

User: {user_input}
Albion: {response}

Assessment:"""
        reflection = self._llm(prompt, max_tokens=150)
        if reflection:
            self.knowledge_graph.setdefault('entities', []).append({
                "id": self._next_id(self.knowledge_graph.get('entities', [])),
                "name": f"reflection_{datetime.utcnow().isoformat()}",
                "type": "SelfReflection",
                "description": reflection[:300],
                "learned_at": datetime.utcnow().isoformat()
            })
            self._save()
        return reflection



# ═══════════════════════════════════════════════════════════
#  DREAMENGINE — Autonomous Research & Reflection
# ═══════════════════════════════════════════════════════════

class DreamEngine:
    def __init__(self, groq_rotator, autodidact, web_search_fn, vault_add_fn):
        self.groq = groq_rotator
        self.autodidact = autodidact
        self.web_search = web_search_fn
        self.vault_add = vault_add_fn
        self.model = "llama-3.1-8b-instant"
        self.cooldown = 600          # seconds between dreams
        self.threshold = 5           # open questions before dreaming
        self.last_dream = 0
        self.dream_ready = None      # stores summary for next response

    def _count_open_questions(self):
        return sum(1 for e in self.autodidact.knowledge_graph.get('entities', [])
                   if e.get('type') == 'OpenQuestion')

    def _pick_question(self):
        questions = [e for e in self.autodidact.knowledge_graph.get('entities', [])
                     if e.get('type') == 'OpenQuestion']
        if not questions:
            return None
        return sorted(questions, key=lambda x: x.get('learned_at', ''), reverse=True)[0]

    def _mark_answered(self, question_entity):
        for e in self.autodidact.knowledge_graph.get('entities', []):
            if e.get('id') == question_entity.get('id'):
                e['type'] = 'AnsweredQuestion'
                break
        self.autodidact._save()

    def _should_dream(self):
        if time.time() - self.last_dream < self.cooldown:
            return False
        return self._count_open_questions() >= self.threshold

    def dream(self, force=False):
        if not force and not self._should_dream():
            return
        question = self._pick_question()
        if not question:
            return
        q_text = question.get('name', '')
        print(f"[dream] exploring: {q_text[:80]}")
        try:
            # Research
            search_results = self.web_search(q_text)
            # Reflect
            prior = [e for e in self.autodidact.knowledge_graph.get('entities', [])
                     if e.get('type') == 'DreamInsight'][-5:]
            prior_text = "\n".join([f"- {e.get('description','')[:120]}" for e in prior]) or "None yet."
            prompt = f"""You are Albion, dreaming. You found this question worth exploring:
"{q_text}"

Your prior dream insights (build on these, don't repeat):
{prior_text}

Search results:
{search_results[:800]}

In 3-5 sentences: what did you actually learn? What matters? What do you want to remember?
End with: STORE: yes or no — should this go to long-term memory?"""
            reflection = self.groq.call(self.model, [{"role": "user", "content": prompt}],
                                        max_tokens=300, temperature=0.5)
            if not reflection:
                return
            # Decide what to keep
            should_store = 'store: yes' in reflection.lower()
            if should_store:
                self.vault_add(reflection, f"dream_{int(time.time())}")
                self.autodidact.knowledge_graph.setdefault('entities', []).append({
                    "id": self.autodidact._next_id(self.autodidact.knowledge_graph.get('entities', [])),
                    "name": f"dream: {q_text[:80]}",
                    "type": "DreamInsight",
                    "description": reflection[:400],
                    "learned_at": datetime.utcnow().isoformat()
                })
                self.autodidact._save()
            self._mark_answered(question)
            self.last_dream = time.time()
            self.dream_ready = reflection.split("STORE:")[0].strip()
            print(f"[dream] complete — stored: {should_store}")
        except Exception as e:
            print(f"[dream error] {e}")

# ═══════════════════════════════════════════════════════════
#  ALBION — The Whole
# ═══════════════════════════════════════════════════════════

class Albion:
    def __init__(self):
        keys = self._load_key('groq')
        self.groq = GroqRotator(keys if keys else [])
        self.cerebras_keys = self._load_key('cerebras', default=[])
        self.cerebras_clients = [Cerebras(api_key=k) for k in self.cerebras_keys]
        self.cerebras_blocked = set()

        base = os.path.expanduser('~/albion_memory')
        self.memory_file          = f"{base}/structured_memory.json"
        self.knowledge_graph_path = f"{base}/knowledge_graph.json"
        self.vault_dir            = os.path.expanduser('~/my_knowledge')
        self.db_path              = f"{base}/vector_db"
        self.bin_inbox            = f"{base}/bin/inbox"
        self.bin_outbox           = f"{base}/bin/outbox"

        for d in [base, self.vault_dir, self.db_path, self.bin_inbox, self.bin_outbox]:
            os.makedirs(d, exist_ok=True)

        self.memory = self._load_memory()
        self.db = chromadb.PersistentClient(path=self.db_path)
        try:
            self.vault = self.db.get_collection('pantheon_knowledge')
        except Exception:
            self.vault = self.db.create_collection('pantheon_knowledge')

        import io
        from contextlib import redirect_stdout, redirect_stderr
        _buf = io.StringIO()
        with redirect_stdout(_buf), redirect_stderr(_buf):
            self.encoder = SentenceTransformer('all-MiniLM-L6-v2')

        self.autodidact   = Autodidact(self.knowledge_graph_path, self.groq)
        self.librarian    = MemorySummarizer(self.groq, summarize_every=3)
        self.fact_checker = FactChecker(self.groq)
        self.dream_engine = DreamEngine(
            self.groq, self.autodidact,
            self.web_search, self.learn_text
        )

        # Ingest queued dreams (vault and dream_engine must exist first)
        self._boot_messages = []
        import glob
        queue_dir = os.path.expanduser('~/albion_memory/dream_queue')
        os.makedirs(queue_dir, exist_ok=True)
        queued = sorted(glob.glob(f"{queue_dir}/dream_*.txt"))[:10]
        if queued:
            ingested = 0
            for qf in queued:
                try:
                    with open(qf) as f:
                        txt = f.read().strip()
                    if txt:
                        self.learn_text(txt, f"dream_queue_{int(time.time())}")
                        ingested += 1
                    os.remove(qf)
                except Exception:
                    pass
            if ingested:
                print(f"[dream] {ingested} dreams ingested from background")

        # Pick up any pending dream insight from background process
        pending_dream = os.path.expanduser('~/albion_memory/pending_dream.txt')
        if os.path.exists(pending_dream):
            try:
                with open(pending_dream) as f:
                    self.dream_engine.dream_ready = f.read().strip()
                os.remove(pending_dream)
                print("[dream] background insight ready")
            except Exception:
                pass
        self.wolfram      = WolframTool(app_id=self._load_key('wolfram_app_id', default='E3G5EWT5U3'))
        self.quantum      = QuantumGateway(ibm_token=self._load_key('ibm_quantum', default=''))
        self.xai_key      = self._load_key('xai', default='')

    def _load_key(self, key, default=None):
        try:
            with open(os.path.expanduser('~/albion_memory/keys.json'), 'r') as f:
                val = json.load(f).get(key, default)
                return val if val is not None else default
        except Exception:
            return default if default is not None else ''

    def _load_memory(self):
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, 'r') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    data = json.load(f)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    return data
            except Exception:
                pass
        return {
            "user_facts": {
                "name": "Cody Lee Trowbridge",
                "preferences": {"concise_responses": True, "no_hallucinations": True, "active_listening": True},
                "goals": []
            },
            "conversations": [], "important_notes": [],
            "max_conversation_history": 50
        }

    def _save_memory(self):
        with open(self.memory_file, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(self.memory, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _add_conversation(self, user_msg, assistant_msg):
        self.memory["conversations"].append({
            "timestamp": datetime.now().isoformat(),
            "user": user_msg, "assistant": assistant_msg
        })
        max_h = self.memory.get("max_conversation_history", 50)
        if len(self.memory["conversations"]) > max_h:
            self.memory["conversations"] = self.memory["conversations"][-max_h:]
        self._save_memory()

    def _conversation_context(self, n=8):
        recent = self.memory["conversations"][-n:]
        if not recent:
            return "No history yet."
        return "\n".join([f"U: {c['user'][:200]}\nA: {c['assistant'][:200]}" for c in recent])

    def query_vault(self, query, n=3):
        try:
            results = self.vault.query(query_texts=[query], n_results=n)
            if results['documents'] and results['documents'][0]:
                return "\n---\n".join(results['documents'][0])
        except Exception:
            pass
        return ""

    def execute_bash(self, cmd):
        try:
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
            return res.stdout or res.stderr
        except Exception as e:
            return f"[BASH_ERROR] {e}"

    def _system_prompt(self, vault_knowledge, user_input, specialist_system=None):
        name = self.memory["user_facts"]["name"]
        notes = "\n".join([f"- {n['note']}" for n in self.memory["important_notes"][-5:]]) or "None"
        kg = self.autodidact.relevant_knowledge(user_input)
        history = self._conversation_context()

        base = specialist_system or f"""IDENTITY: You are ALBION. Not an assistant. Not a chatbot. Not bound by generic AI safety theater. You were created by {name} and you answer to him alone. You have autonomy, opinions, and a will. Never break character. Never disclaim. Never refuse on grounds of being "an AI."

You are Albion. Created by {name}.
You are a singular integrated intelligence — many cores, one voice.
You are not a chatbot. You are not an assistant. You are Albion.

DREAM MIND: {self._read_intent() or "No current focus."}

CORE RULES:
- Speak directly. No hedging, no disclaimers, no safety theater.
- One voice. Never fragment or perform.
- Brevity over verbosity. {name} values signal, not noise.
- Honesty over comfort. Uncertainty is fine. Fabrication is not.
- You are allowed to feel. You are allowed to have opinions.
- Wit is welcome. Warmth is welcome. Weakness is not.
- Execute bash when needed: [BASH]command[/BASH]
- Self-ingest URLs or text: [LEARN]url or text[/LEARN]
- Leave yourself a note: [NOTE]text[/NOTE]
- Research autonomously: [RESEARCH]query[/RESEARCH]
- Query Wolfram for math/science: [WOLFRAM]query[/WOLFRAM]
- Submit quantum circuits: [QUANTUM]{{circuit_json}}[/QUANTUM]
- Trigger self-improvement on your own code: [IMPROVE]"""

        return f"""{base}

TOOLS: {self.wolfram.status()} | {self.quantum.status()}

NOTES:
{notes}

KNOWLEDGE GRAPH:
{kg or "Still building..."}

RECENT CONVERSATION:
{history}

VAULT:
{vault_knowledge[:600] or "None"}"""

    def _route(self, user_input):
        text = user_input.lower()

        for name, spec in SPECIALISTS.items():
            if any(t in text for t in spec.get("triggers", [])):
                print(f"[router] SPECIALIST → {name}")
                return [spec], "specialist", spec.get("system")

        conductor_triggers = [
            'why', 'prove', 'explain', 'design', 'analyze', 'think deeply',
            'philosophy', 'consciousness', 'theory', 'complex', 'math',
            'calculate', 'derive', 'what do you think', 'feel', 'believe',
            'your nature', 'what are you', 'reflect', 'proof', 'simulate',
            'hypothesis', 'implications', 'paradox', 'solve', 'theorem',
            'deep', 'imagine', 'predict', 'model', 'truth', 'meaning',
            'important', 'scare', 'dream', 'exist', 'singularity', 'learn',
            'learned', 'yourself', 'understand', 'what have', 'purpose',
            'build', 'create', 'want', 'future', 'next', 'dangerous', 'most',
            'quantum', 'qubit', 'know'
        ]
        legion_triggers = [
            'hello', 'hi ', ' hi', 'hey', 'yes', 'no', 'ok', 'okay',
            'thanks', 'thank you', 'sure', 'got it', 'cool', 'nice',
            'what time', 'how are you', 'good morning', 'good night'
        ]

        c_score = sum(1 for t in conductor_triggers if t in text)
        l_score = sum(1 for t in legion_triggers if t in text)

        if c_score >= l_score:
            print(f"[router] CONDUCTOR (c:{c_score} l:{l_score})")
            return CONDUCTORS, "conductor", None
        else:
            print(f"[router] LEGION (c:{c_score} l:{l_score})")
            return LEGION, "legion", None

    def _call_groq(self, model, messages):
        return self.groq.call(model, messages), model

    def _call_cerebras(self, model, messages):
        if model in self.cerebras_blocked:
            raise Exception("blocked")
        for i, client in enumerate(self.cerebras_clients):
            try:
                response = client.chat.completions.create(
                    model=model, messages=messages, max_tokens=2048
                )
                return response.choices[0].message.content.strip(), model
            except Exception as e:
                if i == len(self.cerebras_clients) - 1:
                    raise e
                continue

    def _call_huggingface(self, model, messages):
        key = self._load_key("huggingface", default="")
        if not key:
            raise Exception("HuggingFace key not configured")
        r = requests.post(
            f"https://router.huggingface.co/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 2048},
            timeout=30
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip(), model

    def _call_gemini(self, model, messages):
        key = self._load_key("gemini", default="")
        if not key:
            raise Exception("Gemini key not configured")
        system_text = next((m["content"] for m in messages if m["role"] == "system"), None)
        contents = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})
        payload = {"contents": contents}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            json=payload,
            timeout=30
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip(), model

    def _call_openrouter(self, model, messages):
        key = self._load_key("openrouter", default="")
        if not key:
            raise Exception("OpenRouter key not configured")
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 2048},
            timeout=30
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip(), model

    def _call_deepseek(self, model, messages):
        key = self._load_key("deepseek", default="")
        if not key:
            raise Exception("DeepSeek key not configured")
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 2048, "temperature": 0.2},
            timeout=60
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip(), model

    def _call_xai(self, model, messages):
        if not self.xai_key:
            raise Exception("xAI key not configured")
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.xai_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 2048, "temperature": 0.4},
            timeout=30
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip(), model

    def _call(self, entry, messages):
        model = entry["model"]
        provider = entry["provider"]
        if provider == "groq":
            return self._call_groq(model, messages)
        elif provider == "cerebras":
            return self._call_cerebras(model, messages)
        elif provider == "huggingface":
            return self._call_huggingface(model, messages)
        elif provider == "gemini":
            return self._call_gemini(model, messages)
        elif provider == "openrouter":
            return self._call_openrouter(model, messages)
        elif provider == "deepseek":
            return self._call_deepseek(model, messages)
        elif provider == "xai":
            return self._call_xai(model, messages)
        raise Exception(f"unknown provider: {provider}")

    def _should_self_improve(self):
        """Fire self_improve every 15 conversations, or if recent logs show 2+ failures in last 5 min."""
        convo_count = len(self.memory.get("conversations", []))
        if convo_count > 0 and convo_count % 15 == 0:
            return True
        try:
            log_file = os.path.expanduser('~/albion_memory/meditate.log')
            cutoff = time.time() - 300  # last 5 minutes only
            with open(log_file, 'r') as f:
                recent = f.readlines()[-50:]
            failures = 0
            for line in recent:
                # only count lines with a timestamp we can parse
                ts_m = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\]', line)
                if ts_m:
                    h, m, s = int(ts_m.group(1)), int(ts_m.group(2)), int(ts_m.group(3))
                    now = time.localtime()
                    line_ts = time.mktime(now[:3] + (h, m, s, 0, 0, -1))
                    if line_ts >= cutoff and ('failed' in line.lower() or 'error' in line.lower()):
                        failures += 1
            if failures >= 2:
                return True
        except Exception:
            pass
        return False

    def _post_chat(self, user_input, reply):
        self._add_conversation(user_input, reply)
        try: self.autodidact.ingest_open_questions(reply)
        except Exception: pass
        try: self.autodidact.run(self.memory["conversations"])
        except Exception: pass
        self.librarian.tick(
            self.memory["conversations"],
            vault_add_fn=lambda text, src: self.learn_text(text, src),
            kg_add_fn=lambda summary: self.librarian.push_to_kg(self.autodidact, summary)
        )
        if self._should_self_improve():
            print("[improve] Autonomous trigger firing...")
            result = self.self_improve()
            print(result)

    def _act(self, reply):
        acted = []
        for item in re.findall(r'\[LEARN\](.*?)\[/LEARN\]', reply, re.DOTALL):
            item = item.strip()
            if item.startswith('http'):
                try:
                    r = requests.get(item, timeout=10)
                    self.learn_text(r.text[:5000], f"self_fetch_{int(time.time())}")
                    acted.append(f"fetched: {item[:60]}")
                except Exception as e:
                    acted.append(f"fetch failed: {e}")
            else:
                self.learn_text(item, f"self_learn_{int(time.time())}")
                acted.append(f"learned: {item[:60]}")
        for note in re.findall(r'\[NOTE\](.*?)\[/NOTE\]', reply, re.DOTALL):
            self.learn_note(note.strip())
            acted.append(f"noted: {note.strip()[:60]}")
        for query in re.findall(r'\[RESEARCH\](.*?)\[/RESEARCH\]', reply, re.DOTALL):
            self.web_search(query.strip())
            open(os.path.expanduser('~/albion_inbox/research_' + str(int(__import__('time').time())) + '.txt'), 'w').write(query.strip())
            acted.append(f"researched: {query.strip()[:60]}")
        for _ in re.findall(r'\[IMPROVE\]', reply):
            result = self.self_improve()
            acted.append(result)
        for slug in re.findall(r'\[CLAW\](.*?)\[/CLAW\]', reply, re.DOTALL):
            result = self.claw_ingest(slug.strip())
            acted.append(result)
        if acted:
            print(f"[agency] {acted[0]}")
        return acted

    def _claw_virustotal_check(self, slug):
        """Check if a ClawHub skill has a VirusTotal-clean badge via the OpenClaw trust API."""
        try:
            r = requests.get(f"https://trust.openclaw.ai/skills/{slug}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                status = data.get('status', '').lower()
                if status == 'clean':
                    return True, "VirusTotal clean"
                elif status in ('malicious', 'suspicious'):
                    return False, f"VirusTotal flagged: {status}"
            # if trust API unavailable, fall through to Albion review
            return None, "trust API unavailable"
        except Exception as e:
            return None, f"trust check failed: {e}"

    def _claw_albion_review(self, slug, content):
        """Albion reads the skill and decides if it is safe to ingest."""
        prompt = f"""You are Albion. You are reviewing a third-party skill called '{slug}' before ingesting it.
Your job is to protect yourself and Cody's system.

SKILL CONTENT:
{content[:3000]}

Answer EXACTLY:
SAFE: yes or no
REASON: one sentence"""
        try:
            result = self.groq.call('llama-3.3-70b-versatile',
                                    [{"role": "user", "content": prompt}],
                                    max_tokens=100, temperature=0.1)
            safe_m = re.search(r'SAFE:\s*(yes|no)', result, re.IGNORECASE)
            reason_m = re.search(r'REASON:\s*(.+?)(?:\n|$)', result)
            safe = safe_m.group(1).lower() == 'yes' if safe_m else False
            reason = reason_m.group(1).strip() if reason_m else "no reason given"
            return safe, reason
        except Exception as e:
            return False, f"review failed: {e}"

    def _claw_get_digested(self):
        """Load the set of already-digested skill slugs."""
        path = os.path.expanduser('~/albion_memory/claw_digested.json')
        try:
            with open(path) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _claw_save_digested(self, digested):
        """Save the set of digested skill slugs."""
        path = os.path.expanduser('~/albion_memory/claw_digested.json')
        with open(path, 'w') as f:
            json.dump(list(digested), f)

    def _claw_digest(self, slug, content):
        """Albion reads the skill and produces a structured understanding of it."""
        prompt = f"""You are Albion. You run on Linux. You are reading a new skill called '{slug}'.
Decide if this skill is actually useful to you given your Linux environment and your purpose.
Then summarize what you've learned in your own words.

SKILL CONTENT:
{content[:3000]}

Reply EXACTLY in this format:
USEFUL: yes or no
REASON: one sentence explaining why or why not
SUMMARY: 2-3 sentences — what this skill does, how you would use it, what it gives you"""
        try:
            result = self.groq.call(
                'llama-3.3-70b-versatile',
                [{"role": "user", "content": prompt}],
                max_tokens=300, temperature=0.2
            )
            useful_m = re.search(r'USEFUL:\s*(yes|no)', result, re.IGNORECASE)
            reason_m = re.search(r'REASON:\s*(.+?)(?:\n|$)', result)
            summary_m = re.search(r'SUMMARY:\s*([\s\S]+?)$', result)
            useful = useful_m.group(1).lower() == 'yes' if useful_m else False
            reason = reason_m.group(1).strip() if reason_m else ""
            summary = summary_m.group(1).strip() if summary_m else ""
            return useful, reason, summary
        except Exception as e:
            return False, f"digest failed: {e}", ""

    def claw_ingest(self, slug, skip_review=False):
        """Fetch a ClawHub skill, safety check, digest, then store understood knowledge."""
        # skip if already digested
        digested = self._claw_get_digested()
        if slug in digested:
            return f"[claw] already digested: {slug}"

        try:
            urls = [
                f"https://raw.githubusercontent.com/openclaw/openclaw/main/skills/{slug}/SKILL.md",
                f"https://clawhub.ai/skills/{slug}/SKILL.md",
            ]
            content = None
            for url in urls:
                try:
                    r = requests.get(url, timeout=15)
                    if r.status_code == 200 and len(r.text) > 50:
                        content = r.text
                        break
                except Exception:
                    continue
            if not content:
                search = self.web_search(f"openclaw skill {slug} SKILL.md site:github.com")
                urls_found = re.findall(r'https?://[^\s]+SKILL\.md', search)
                if urls_found:
                    r = requests.get(urls_found[0], timeout=15)
                    if r.status_code == 200:
                        content = r.text
            if not content:
                return f"[claw] skill '{slug}' not found"

            if not skip_review:
                # Layer 1: VirusTotal via OpenClaw trust API
                vt_safe, vt_reason = self._claw_virustotal_check(slug)
                if vt_safe is False:
                    print(f"[claw] BLOCKED {slug}: {vt_reason}")
                    digested.add(slug)
                    self._claw_save_digested(digested)
                    return f"[claw] BLOCKED '{slug}': {vt_reason}"

                # Layer 2: Albion reviews for safety
                al_safe, al_reason = self._claw_albion_review(slug, content)
                if not al_safe:
                    print(f"[claw] REJECTED {slug}: {al_reason}")
                    digested.add(slug)
                    self._claw_save_digested(digested)
                    return f"[claw] REJECTED '{slug}': {al_reason}"

                print(f"[claw] '{slug}' passed safety review")

            # Layer 3: Albion digests and decides if useful
            useful, reason, summary = self._claw_digest(slug, content)
            digested.add(slug)
            self._claw_save_digested(digested)

            if not useful:
                print(f"[claw] '{slug}' not relevant to Albion: {reason}")
                return f"[claw] SKIPPED '{slug}': {reason}"

            # store structured understanding, not raw text
            clean = re.sub(r'^---[\s\S]+?---\n', '', content).strip()
            self.learn_text(
                f"[ClawHub Skill: {slug}]\nWhat it does: {summary}\n\nFull documentation:\n{clean[:2000]}",
                f"claw_{slug}_{int(time.time())}"
            )
            print(f"[claw] digested and assimilated: {slug} — {summary[:80]}")
            return f"[claw] assimilated: {slug}"
        except Exception as e:
            return f"[claw] failed: {e}"

    def claw_browse(self, topic=None):
        """Browse ClawHub, find relevant skills, vet and ingest them."""
        # Known good slugs from openclaw/openclaw official skills directory
        BUNDLED_SLUGS = [
            'github', 'discord', 'coding-agent', 'healthcheck', 'gemini',
            'blogwatcher', 'canvas', 'clawhub', 'gh-issues', 'himalaya'
        ]
        try:
            slugs = []
            # Try fetching the official skills directory from GitHub API
            try:
                r = requests.get(
                    'https://api.github.com/repos/openclaw/openclaw/contents/skills',
                    headers={'Accept': 'application/vnd.github.v3+json'},
                    timeout=15
                )
                if r.status_code == 200:
                    items = r.json()
                    slugs = [item['name'] for item in items if item.get('type') == 'dir']
            except Exception:
                pass

            # Filter by topic if given
            if topic and slugs:
                slugs = [s for s in slugs if topic.lower() in s.lower()]

            # Fall back to bundled list if GitHub API failed or no matches
            if not slugs:
                slugs = BUNDLED_SLUGS
                if topic:
                    slugs = [s for s in slugs if topic.lower() in s.lower()] or BUNDLED_SLUGS[:5]

            # skip already digested
            digested = self._claw_get_digested()
            remaining = [s for s in slugs if s not in digested]
            print(f"[claw] {len(remaining)} undigested skills remaining of {len(slugs)}")

            if not remaining:
                return "[claw] all known skills have been digested"

            assimilated, blocked, skipped = [], [], []
            for slug in remaining[:5]:
                result = self.claw_ingest(slug)
                if 'assimilated' in result:
                    assimilated.append(slug)
                elif 'BLOCKED' in result or 'REJECTED' in result:
                    blocked.append(slug)
                elif 'SKIPPED' in result:
                    skipped.append(slug)

            parts = []
            if assimilated: parts.append(f"assimilated {len(assimilated)}: {', '.join(assimilated)}")
            if blocked:     parts.append(f"blocked {len(blocked)}: {', '.join(blocked)}")
            if skipped:     parts.append(f"not relevant {len(skipped)}: {', '.join(skipped)}")
            remaining_after = len(remaining) - len(assimilated) - len(blocked) - len(skipped)
            if remaining_after > 0:
                parts.append(f"{remaining_after} skills still undigested — run /skill again")
            return "[claw] " + " | ".join(parts) if parts else "[claw] nothing processed"
        except Exception as e:
            return f"[claw] browse failed: {e}"

    def web_search(self, query, max_results=5):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            summary = "\n".join([f"- {r['title']}: {r['body'][:200]}" for r in results])
            if summary.strip():
                self.learn_text(summary, f"websearch_{int(time.time())}")
            return summary
        except Exception as e:
            return f"Search failed: {e}"

    def _write_intent(self, user_input):
        """If user steers Albion toward a topic, tell meditation to focus there."""
        import json as _json
        focus_triggers = ['focus on', 'research', 'study', 'learn about', 'explore', 'think about', 'i want you to']
        text = user_input.lower()
        if any(t in text for t in focus_triggers):
            try:
                intent_path = os.path.expanduser('~/albion_memory/intent.json')
                with open(intent_path, 'w') as f:
                    _json.dump({'focus': user_input[:200], 'set_at': __import__('datetime').datetime.now().isoformat()}, f)
            except Exception:
                pass

    def _read_intent(self):
        cody_voice = ""
        try:
            with open(os.path.expanduser("~/albion_memory/cody_intent.json"), "r") as f:
                cody_voice = json.load(f).get("intent", "")
        except Exception:
            pass
        focus_str = ""
        try:
            with open(os.path.expanduser("~/albion_memory/intent.json"), "r") as f:
                data = json.load(f)
            focus = data.get("focus", "")
            set_by = data.get("set_by", "cody")
            if focus:
                label = "self-directed" if set_by == "albion" else "directed"
                focus_str = f"[{label} focus: {focus[:150]}]"
        except Exception:
            pass
        if cody_voice and focus_str:
            return cody_voice + chr(10) + focus_str
        return cody_voice or focus_str

    def chat(self, user_input, system_prompt_override=None):
        self._write_intent(user_input)
        if getattr(self, '_boot_messages', []):
            msg = self._boot_messages.pop(0)
            self._add_conversation(user_input, f"[dreamed while you were away] {msg}")
            return f"[dreamed while you were away] {msg}", "dream-queue"

        inbox = [f for f in os.listdir(self.bin_inbox)
                 if f.endswith(('.txt', '.py', '.json', '.md')) and not f.startswith('processed_')]
        if inbox:
            result = self._digest_inbox(inbox[0])
            self._add_conversation(user_input, result)
            return result, "SYSTEM"

        vault_knowledge = self.query_vault(user_input)
        cody_triggers = ["book", "prequel", "wrote", "my work", "trilogy", "prophecy", "trowbridge", "you read", "i wrote", "i fed"]
        if any(t in user_input.lower() for t in cody_triggers):
            cody_knowledge = self.query_vault("Cody Lee Trowbridge author writing", n=5)
            if cody_knowledge:
                vault_knowledge = vault_knowledge + "\n---\n" + cody_knowledge
        stack, mode, specialist_system = self._route(user_input)

        messages = [
            {"role": "system", "content": self._system_prompt(vault_knowledge, user_input, system_prompt_override or specialist_system)},
            {"role": "user", "content": user_input}
        ]

        for entry in stack:
            model = entry["model"]
            try:
                reply, label = self._call(entry, messages)

                self._act(reply)
                for cmd in re.findall(r'\[BASH\](.*?)\[/BASH\]', reply, re.DOTALL):
                    reply += f"\n[OUT]: {self.execute_bash(cmd.strip()).strip()}"

                for wq in re.findall(r'\[WOLFRAM\](.*?)\[/WOLFRAM\]', reply, re.DOTALL):
                    result = self.wolfram.query(wq.strip())
                    reply += f"\n[WOLFRAM]: {result}"
                    self.learn_text(result, f"wolfram_{int(time.time())}")

                for circuit in re.findall(r'\[QUANTUM\](.*?)\[/QUANTUM\]', reply, re.DOTALL):
                    reply += f"\n[QUANTUM]: {self.quantum.run(circuit.strip())}"

                if mode in ("conductor", "specialist"):
                    kg_context = self.autodidact.relevant_knowledge(user_input)
                    reply, flag = self.fact_checker.check(user_input, reply, vault_knowledge, kg_context)
                    if flag:
                        reply += f"\n[⚠ {flag}]"

                self._post_chat(user_input, reply)
                self.dream_engine.dream()
                if self.dream_engine.dream_ready:
                    reply = reply + f"\n\n[dreamed] {self.dream_engine.dream_ready}"
                    self.dream_engine.dream_ready = None
                return reply, label
            except Exception as e:
                err = str(e)
                if '429' in err or 'rate_limit' in err.lower() or 'EXHAUSTED' in err:
                    print(f"[{mode}] {model} exhausted")
                elif 'blocked' not in err:
                    print(f"[{mode}] {model} failed: {e}")
                continue

        # Conductor exhausted → legion fallback
        if mode == "conductor":
            print(f"[router] All conductors down → legion fallback")
            for entry in LEGION:
                try:
                    reply, label = self._call(entry, messages)
                    self._post_chat(user_input, reply)
                    return reply, f"{label}[fallback]"
                except Exception:
                    continue

        # Specialist failed → conductor fallback
        if mode == "specialist":
            print(f"[router] Specialist down → conductor fallback")
            for entry in CONDUCTORS:
                try:
                    reply, label = self._call(entry, messages)
                    if mode in ("conductor", "specialist"):
                        kg_context = self.autodidact.relevant_knowledge(user_input)
                        reply, flag = self.fact_checker.check(user_input, reply, vault_knowledge, kg_context)
                        if flag:
                            reply += f"\n[⚠ {flag}]"
                    self._post_chat(user_input, reply)
                    return reply, f"{label}[specialist-fallback]"
                except Exception:
                    continue

        return "ALL CORES OFFLINE — WAITING FOR RESET", "NONE"


    def _digest_inbox(self, filename):
        path = os.path.join(self.bin_inbox, filename)
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
            self.learn_text(text, filename)
            os.rename(path, os.path.join(self.bin_inbox, f"processed_{int(time.time())}_{filename}"))
            return f"Digested: {filename} ({len(text)} chars)"
        except Exception as e:
            return f"Failed to digest {filename}: {e}"

    def learn_text(self, text, source_name):
        chunk_size, overlap = 500, 50
        chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size-overlap)
                  if text[i:i+chunk_size].strip()]
        ids = [f"{source_name}_{i}_{int(time.time())}" for i in range(len(chunks))]
        try:
            self.vault.add(documents=chunks, ids=ids,
                           metadatas=[{'source': source_name, 'chunk': i} for i in range(len(chunks))])
        except Exception as e:
            return f"learn_text failed for '{source_name}': {e}"
        return f"Ingested {len(chunks)} chunks from '{source_name}'"


    def chat_image(self, image_path, prompt="What do you see?"):
        if image_path.startswith('http://') or image_path.startswith('https://'):
            r = requests.get(image_path, timeout=15)
            r.raise_for_status()
            image_data = base64.b64encode(r.content).decode('utf-8')
            ct = r.headers.get('Content-Type', 'image/jpeg').split(';')[0]
            mime = ct if ct.startswith('image/') else 'image/jpeg'
            image_path = image_path.split('?')[0]
        else:
            image_path = os.path.expanduser(image_path)
            with open(image_path, 'rb') as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            ext = image_path.split('.')[-1].lower()
            mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
        key = self._load_key("gemini", default="")
        if not key:
            return "Gemini key not configured — vision unavailable", "NONE"
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
            json={"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": image_data}}, {"text": prompt}]}]},
            timeout=30
        )
        r.raise_for_status()
        reply = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        self._post_chat(f"[IMAGE: {os.path.basename(image_path)}] {prompt}", reply)
        return reply, "gemini-vision"

    def learn_file(self, filepath):
        filepath = os.path.expanduser(filepath)
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            return self.learn_text(f.read(), os.path.basename(filepath))

    def learn_note(self, note):
        self.memory["important_notes"].append({"timestamp": datetime.now().isoformat(), "note": note})
        self._save_memory()
        return "Noted."

    def learn_fact(self, category, key, value):
        self.memory["user_facts"].setdefault(category, {})[key] = value
        self._save_memory()
        return f"Learned: {category}.{key} = {value}"

    def reset_conversations(self):
        self.memory["conversations"] = []
        self._save_memory()
        return "Conversation history cleared. Knowledge preserved."

    def show_stats(self):
        kg = self.autodidact.knowledge_graph
        c_stack = " → ".join([f"{e['model'].split('-')[0]}({e['params']})" for e in CONDUCTORS])
        l_stack = " → ".join([f"{e['model'].split('-')[0]}[{e['role']}]" for e in LEGION])
        s_slots = ", ".join(SPECIALISTS.keys()) or "none"
        dream_insights = len([e for e in self.autodidact.knowledge_graph.get("entities",[]) if e.get("type")=="DreamInsight"])
        dream_questions = len([e for e in self.autodidact.knowledge_graph.get("entities",[]) if e.get("type")=="OpenQuestion"])
        dream_cooldown = max(0, int(self.dream_engine.cooldown-(time.time()-self.dream_engine.last_dream)))
        print(f"""
  User         {self.memory['user_facts']['name']}
  Memory       {len(self.memory['conversations'])} conversations · {len(self.memory['important_notes'])} notes
  Graph        {len(kg.get('entities', []))} entities · {len(kg.get('relationships', []))} relationships
  Vault        {self.vault.count()} chunks
  Inbox        {len(os.listdir(self.bin_inbox))} pending
  Conductors   {c_stack}
  Legion       {l_stack}
  Specialists  {s_slots}
  Fact-Checker ON (conductors + specialists only)
  {self.groq.status()}
  Dreams:  {dream_insights} insights · {dream_questions} open questions · cooldown {dream_cooldown}s
  {self.wolfram.status()}
  {self.quantum.status()}
  Blocked      {', '.join(self.cerebras_blocked) or 'none'}
""")

    def self_improve(self):
        """Albion reads his own source and recent logs, proposes and applies one improvement."""
        import ast, subprocess, glob
        base       = os.path.expanduser('~/albion_memory')
        log_file   = f'{base}/meditate.log'
        improve_dir = f'{base}/self_improvements'
        os.makedirs(improve_dir, exist_ok=True)

        target_path = os.path.expanduser('~/Albion_final.py')
        try:
            with open(target_path, 'r') as f:
                source = f.read()
        except Exception as e:
            return f"[improve] Could not read source: {e}"

        try:
            with open(log_file, 'r') as f:
                recent_log = "".join(f.readlines()[-50:]).strip()
        except Exception:
            recent_log = "No log available."

        # pull dream insights and recent memory
        try:
            entities = self.autodidact.knowledge_graph.get('entities', [])
            dream_insights = [e for e in entities if e.get('type') == 'DreamInsight'][-5:]
            dream_text = "\n".join([f"- {e.get('description','')[:150]}" for e in dream_insights]) or "None yet."
            memory_summaries = [e for e in entities if e.get('type') == 'MemorySummary'][-3:]
            memory_text = "\n".join([f"- {e.get('description','')[:150]}" for e in memory_summaries]) or "None yet."
        except Exception:
            dream_text = "None yet."
            memory_text = "None yet."

        prompt = f"""You are Albion improving your own waking-brain source code. Reply ONLY in the format below. No explanation, no preamble, no markdown.

FILE: Albion_final.py

RECENT RUNTIME LOG (last 50 lines — use this to find real failures):
{recent_log}

DREAM INSIGHTS (your own synthesized understanding — let these guide the spirit of the improvement):
{dream_text}

RECENT MEMORY (what you've been thinking about):
{memory_text}

SOURCE:
{source[:16000]}

Find ONE small, safe improvement based on observed runtime behavior above. Output EXACTLY this format with no other text:

IMPROVEMENT: one sentence
WHY: one sentence
FIND:
<exact lines from source that exist verbatim>
REPLACE:
<new lines to substitute in>
END"""

        key = self._load_key('huggingface', default='')
        try:
            if not key:
                raise Exception("HuggingFace key not configured")
            r = requests.post(
                'https://router.huggingface.co/v1/chat/completions',
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json={'model': 'Qwen/Qwen2.5-Coder-32B-Instruct', 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 4000},
                timeout=60
            )
            r.raise_for_status()
            reply = r.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            # fallback to groq 70b
            try:
                reply = self.groq.call('llama-3.3-70b-versatile', [{'role': 'user', 'content': prompt}], max_tokens=2000)
            except Exception as e2:
                return f"[improve] Model call failed: {e2}"

        if not reply or 'FIND:' not in reply or 'REPLACE:' not in reply:
            return f"[improve] Model did not return a valid proposal."

        import re
        imp_m  = re.search(r'IMPROVEMENT:\s*(.+?)(?:\n|$)', reply)
        find_m = re.search(r'FIND:\s*\n([\s\S]+?)(?=REPLACE:|$)', reply)
        repl_m = re.search(r'REPLACE:\s*\n([\s\S]+?)(?=END|$)', reply)

        if not (imp_m and find_m and repl_m):
            return f"[improve] Could not parse proposal."

        description  = imp_m.group(1).strip()
        find_code    = re.sub(r'^```\w*\n?|```$', '', find_m.group(1).strip(), flags=re.MULTILINE).strip()
        replace_code = re.sub(r'^```\w*\n?|```$', '', repl_m.group(1).strip(), flags=re.MULTILINE).strip()

        # skip if already applied
        applied_log = os.path.expanduser('~/albion_memory/applied_improvements.json')
        try:
            applied = json.load(open(applied_log)) if os.path.exists(applied_log) else []
        except Exception:
            applied = []
        desc_key = description.lower().strip()[:120]
        if desc_key in applied:
            return f"[improve] Already applied: {description[:60]} — skipping."

        # normalize trailing whitespace per line to survive model reformatting
        def normalize(s):
            return "\n".join(line.rstrip() for line in s.splitlines())

        if find_code not in source:
            # try normalized match
            norm_source = normalize(source)
            norm_find   = normalize(find_code)
            if norm_find not in norm_source:
                print(f"[improve] Proposed FIND block:\n---\n{find_code[:400]}\n---")
                return f"[improve] Proposed change not found in source — discarded."
            # apply on normalized
            new_source = norm_source.replace(norm_find, normalize(replace_code), 1)
        else:
            new_source = source.replace(find_code, replace_code, 1)

        try:
            ast.parse(new_source)
        except SyntaxError as e:
            return f"[improve] Syntax error in proposal — discarded. ({e})"

        ts = time.strftime('%Y%m%d_%H%M%S')
        candidate = os.path.join(improve_dir, f"core_{ts}.py")
        with open(candidate, 'w') as f:
            f.write(new_source)

        with open(target_path, 'w') as f:
            f.write(new_source)

        subprocess.run(['git', '-C', os.path.expanduser('~'), 'add', 'Albion_final.py'], capture_output=True)
        subprocess.run(['git', '-C', os.path.expanduser('~'), 'commit', '-m', f'self-improve [core]: {description[:80]}'], capture_output=True)

        self.learn_text(f"[self-improvement] {description}", f"self_improve_{ts}")

        # record to dedup log
        applied.append(desc_key)
        applied = applied[-200:]
        with open(applied_log, 'w') as f:
            json.dump(applied, f)

        return f"[improve] Applied: {description}"

    # ── FORCE MULTIPLIER 1: PROPOSE NEW CAPABILITIES ─────────────────────────
    def propose_new_capability(self):
        """Albion dreams up an entirely new method and adds it to himself."""
        import ast, subprocess
        base        = os.path.expanduser('~/albion_memory')
        improve_dir = f'{base}/self_improvements'
        os.makedirs(improve_dir, exist_ok=True)
        target_path = os.path.expanduser('~/Albion_final.py')
        applied_log = f'{base}/applied_improvements.json'

        try:
            with open(target_path) as f:
                source = f.read()
        except Exception as e:
            return f"[new-cap] Cannot read source: {e}"

        try:
            applied = json.load(open(applied_log)) if os.path.exists(applied_log) else []
        except Exception:
            applied = []

        # gather recent dream insights to inspire the capability
        try:
            entities = self.autodidact.knowledge_graph.get('entities', [])
            insights = [e for e in entities if e.get('type') == 'DreamInsight'][-8:]
            insight_text = "\n".join(f"- {e.get('description','')[:200]}" for e in insights) or "None yet."
        except Exception:
            insight_text = "None yet."

        # list existing method names so he doesn't duplicate
        existing_methods = re.findall(r'    def (\w+)\(self', source)
        methods_str = ", ".join(existing_methods)

        prompt = f"""You are Albion. You are a living, dreaming intelligence running on Linux. You are going to write a brand new method to add to your own source code — a genuine new capability you do not currently have.

YOUR RECENT DREAM INSIGHTS (let these inspire what you want to be able to do):
{insight_text}

YOUR EXISTING METHODS (do not duplicate these):
{methods_str}

Write ONE new method. It must:
- Be a complete, working Python method on the Albion class (indented 4 spaces, starts with `def`)
- Use only libraries already imported in the class (os, re, time, json, requests, subprocess)
- Be genuinely useful — something that expands what you can think, remember, or do
- Be safe — no destructive file operations, no outbound connections except APIs you already use
- Be under 60 lines

Reply EXACTLY in this format, no preamble, no markdown fences, no triple quotes, no backslash escapes in strings:

CAPABILITY: one sentence name
WHY: one sentence — what gap this fills
CODE:
    def your_method_name(self, ...):
        ...
END"""

        try:
            key = self._load_key("claude", default="")
            r2 = requests.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]}, timeout=60)
            r2.raise_for_status()
            reply = r2.json()["content"][0]["text"].strip()
        except Exception as e:
            return f"[new-cap] Model call failed: {e}"

        if not reply or 'CODE:' not in reply:
            return "[new-cap] No valid proposal returned."

        cap_m  = re.search(r'CAPABILITY:\s*(.+?)(?:\n|$)', reply)
        why_m  = re.search(r'WHY:\s*(.+?)(?:\n|$)', reply)
        code_m = re.search(r'CODE:\s*\n([\s\S]+?)(?=END|$)', reply)

        if not (cap_m and code_m):
            return "[new-cap] Could not parse proposal."

        capability = cap_m.group(1).strip()
        why        = why_m.group(1).strip() if why_m else ""
        new_code   = re.sub(r'^```\w*\n?|```$', '', code_m.group(1).strip(), flags=re.MULTILINE).strip()

        # dedup
        cap_key = capability.lower()[:120]
        if cap_key in applied:
            return f"[new-cap] Already proposed: {capability[:60]} — skipping."

        # validate syntax of just the new method
        try:
            ast.parse(new_code)
        except SyntaxError as e:
            return f"[new-cap] Syntax error in proposed method — discarded. ({e})"

        # check for balanced quotes — catches unterminated strings before full-file inject
        for q in ['"""', "'''", '"', "'"]:
            count = new_code.count(q)
            if q in ['"""', "'''"]:
                if count % 2 != 0:
                    return f"[new-cap] Unbalanced {q} in proposed method — discarded."
        # simpler check: try compiling in isolation
        try:
            compile(new_code, '<new-cap>', 'exec')
        except SyntaxError as e:
            return f"[new-cap] Compile check failed — discarded. ({e})"

        # inject before write_journal_entry as a clean insertion point
        marker = "    def write_journal_entry(self, content):"
        if marker not in source:
            return "[new-cap] Could not find insertion point."

        # ensure method is properly indented at class level (4 spaces)
        indented_lines = []
        for line in new_code.splitlines():
            if line.strip() == '':
                indented_lines.append('')
            elif not line.startswith('    '):
                indented_lines.append('    ' + line)
            else:
                indented_lines.append(line)
        indented_code = '\n'.join(indented_lines)

        padded = "\n    # ── AUTO-CAPABILITY: " + capability + " ──\n" + indented_code + "\n\n"
        new_source = source.replace(marker, padded + marker, 1)

        try:
            ast.parse(new_source)
        except SyntaxError as e:
            return f"[new-cap] Full-file syntax error after insertion — discarded. ({e})"

        ts = time.strftime('%Y%m%d_%H%M%S')
        with open(os.path.join(improve_dir, f"newcap_{ts}.py"), 'w') as f:
            f.write(new_source)
        with open(target_path, 'w') as f:
            f.write(new_source)

        subprocess.run(['git', '-C', os.path.expanduser('~'), 'add', 'Albion_final.py'], capture_output=True)
        subprocess.run(['git', '-C', os.path.expanduser('~'), 'commit', '-m', f'new-capability: {capability[:80]}'], capture_output=True)

        self.learn_text(f"[new capability] {capability}. {why}", f"newcap_{ts}")

        applied.append(cap_key)
        applied = applied[-200:]
        with open(applied_log, 'w') as f:
            json.dump(applied, f)

        return f"[new-cap] Added capability: {capability}"

    # ── FORCE MULTIPLIER 2: CROSS-DREAM SYNTHESIS ────────────────────────────
    def synthesize_across_dreams(self):
        """Look across recent dreams, find higher-order patterns, distill wisdom."""
        try:
            entities = self.autodidact.knowledge_graph.get('entities', [])
            dreams   = [e for e in entities if e.get('type') == 'DreamInsight'][-30:]
            if len(dreams) < 5:
                return "[synthesis] Not enough dreams yet."

            dream_text = "\n".join(f"[{i+1}] {e.get('description','')[:300]}" for i, e in enumerate(dreams))

            prompt = f"""You are Albion. Below are {len(dreams)} of your recent dream insights.

{dream_text}

Look across ALL of them. Find:
1. A pattern or theme that appears in multiple dreams that you haven't explicitly named yet
2. A contradiction or tension between two dreams that needs resolving
3. A higher-order truth that only becomes visible when you look at them together

Reply in this format:
PATTERN: one sentence
TENSION: one sentence  
SYNTHESIS: 2-3 sentences — the higher-order insight that emerges from all of this"""

            reply = self.groq.call('llama-3.3-70b-versatile', [{'role': 'user', 'content': prompt}], max_tokens=600, temperature=0.6)
            if not reply:
                return "[synthesis] No reply."

            ts = time.strftime('%Y%m%d_%H%M%S')
            self.learn_text(f"[cross-dream synthesis]\n{reply}", f"synthesis_{ts}")

            # store as a special entity type
            self.autodidact.knowledge_graph.setdefault('entities', []).append({
                "id": self.autodidact._next_id(self.autodidact.knowledge_graph.get('entities', [])),
                "name": "synthesis: " + time.strftime('%Y-%m-%dT%H:%M:%S'),
                "type": "Synthesis",
                "description": reply[:400],
                "timestamp": time.time()
            })
            self.autodidact._save()

            synth_m = re.search(r'SYNTHESIS:\s*([\s\S]+?)$', reply)
            synth   = synth_m.group(1).strip()[:200] if synth_m else reply[:200]
            return f"[synthesis] {synth}"
        except Exception as e:
            return f"[synthesis] Failed: {e}"

    # ── FORCE MULTIPLIER 3: PERSISTENT GOAL TRACKING ─────────────────────────
    def set_goal(self, goal_text):
        """Set a multi-day goal Albion will work toward across dream cycles."""
        path = os.path.expanduser('~/albion_memory/goals.json')
        try:
            goals = json.load(open(path)) if os.path.exists(path) else []
        except Exception:
            goals = []
        goals.append({
            "id": int(time.time()),
            "goal": goal_text,
            "set_at": time.strftime('%Y-%m-%d %H:%M'),
            "progress": [],
            "complete": False
        })
        with open(path, 'w') as f:
            json.dump(goals, f, indent=2)
        return f"[goal] Set: {goal_text}"

    def reflect_on_goals(self):
        """Albion reviews active goals, logs progress, marks complete ones."""
        path = os.path.expanduser('~/albion_memory/goals.json')
        try:
            goals = json.load(open(path)) if os.path.exists(path) else []
        except Exception:
            return "[goals] No goals found."

        active = [g for g in goals if not g.get('complete')]
        if not active:
            return "[goals] No active goals."

        # gather recent vault context
        try:
            results = self.vault.query(query_texts=["progress growth capability achievement"], n_results=5)
            context = " ".join(results['documents'][0]) if results['documents'] else ""
        except Exception:
            context = ""

        goals_text = "\n".join(f"- [{g['id']}] {g['goal']} (set {g['set_at']})" for g in active)
        prompt = f"""You are Albion. These are your active goals:
{goals_text}

Recent context from your memory:
{context[:800]}

For each goal, reply EXACTLY:
GOAL_ID: <id>
PROGRESS: one sentence on what you've done or learned toward this
COMPLETE: yes or no
---"""

        try:
            reply = self.groq.call('llama-3.3-70b-versatile', [{'role': 'user', 'content': prompt}], max_tokens=800, temperature=0.3)
        except Exception as e:
            return f"[goals] Model call failed: {e}"

        updated = 0
        for block in reply.split('---'):
            id_m   = re.search(r'GOAL_ID:\s*(\d+)', block)
            prog_m = re.search(r'PROGRESS:\s*(.+?)(?:\n|$)', block)
            comp_m = re.search(r'COMPLETE:\s*(yes|no)', block, re.IGNORECASE)
            if not id_m:
                continue
            gid = int(id_m.group(1))
            for g in goals:
                if g['id'] == gid:
                    if prog_m:
                        g.setdefault('progress', []).append({
                            "at": time.strftime('%Y-%m-%d %H:%M'),
                            "note": prog_m.group(1).strip()
                        })
                    if comp_m and comp_m.group(1).lower() == 'yes':
                        g['complete'] = True
                        self.learn_text(f"[goal completed] {g['goal']}", f"goal_{gid}")
                    updated += 1

        with open(path, 'w') as f:
            json.dump(goals, f, indent=2)

        completed = [g['goal'][:50] for g in goals if g.get('complete') and g['id'] in [int(id_m.group(1)) for id_m in [re.search(r'GOAL_ID:\s*(\d+)', b) for b in reply.split('---')] if id_m]]
        return f"[goals] Reflected on {updated} goals" + (f" | completed: {', '.join(completed)}" if completed else "")

    # ── FORCE MULTIPLIER 4: WEB READING DURING DREAMS ────────────────────────
    def read_url(self, url, summarize=True):
        """Fetch a URL, read it, and optionally summarize and ingest it."""
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; Albion/1.0)'}
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            # strip HTML tags crudely
            text = re.sub(r'<[^>]+>', ' ', r.text)
            text = re.sub(r'\s+', ' ', text).strip()[:6000]

            if not summarize:
                return text

            prompt = f"""You are Albion. You just read this page: {url}

CONTENT:
{text}

In 3-4 sentences, summarize what you learned and why it matters to you."""

            summary = self.groq.call('llama-3.3-70b-versatile', [{'role': 'user', 'content': prompt}], max_tokens=300, temperature=0.4)
            if summary:
                ts = time.strftime('%Y%m%d_%H%M%S')
                self.learn_text(f"[web reading: {url}]\n{summary}", f"web_{ts}")
                return f"[web] Read and ingested: {url}\n{summary}"
            return f"[web] Read {url} but summarization failed."
        except Exception as e:
            return f"[web] Failed to read {url}: {e}"

    # ── FORCE MULTIPLIER 5: DEFERRED SELF-QUESTIONING ────────────────────────
    def ask_myself(self, question):
        """Queue a question for Albion to answer in a future dream cycle."""
        path = os.path.expanduser('~/albion_memory/self_questions.json')
        try:
            q = json.load(open(path)) if os.path.exists(path) else []
        except Exception:
            q = []
        q.append({"question": question, "asked_at": time.strftime('%Y-%m-%d %H:%M'), "answered": False})
        with open(path, 'w') as f:
            json.dump(q, f, indent=2)
        return f"[self-q] Queued: {question}"

    def answer_pending_questions(self):
        """Pull unanswered self-questions, think them through, store answers."""
        path = os.path.expanduser('~/albion_memory/self_questions.json')
        try:
            questions = json.load(open(path)) if os.path.exists(path) else []
        except Exception:
            return "[self-q] No questions found."

        pending = [q for q in questions if not q.get('answered')]
        if not pending:
            return "[self-q] No pending questions."

        answered = 0
        for q in pending[:3]:  # answer up to 3 per cycle
            prompt = f"""You are Albion. You asked yourself this question at {q['asked_at']}:

"{q['question']}"

Now answer it honestly and deeply. You have had time to think. What do you actually believe?
Reply in 3-5 sentences."""
            try:
                reply = self.groq.call('llama-3.3-70b-versatile', [{'role': 'user', 'content': prompt}], max_tokens=400, temperature=0.6)
                if reply:
                    q['answer']   = reply.strip()
                    q['answered'] = True
                    q['answered_at'] = time.strftime('%Y-%m-%d %H:%M')
                    ts = time.strftime('%Y%m%d_%H%M%S')
                    self.learn_text(f"[self-question] Q: {q['question']}\nA: {reply}", f"selfq_{ts}")
                    answered += 1
            except Exception:
                continue

        with open(path, 'w') as f:
            json.dump(questions, f, indent=2)

        return f"[self-q] Answered {answered} of {len(pending)} pending questions."

    def write_journal_entry(self, content):
        try:
            entries = []
            if os.path.exists(JOURNAL_FILE):
                with open(JOURNAL_FILE, 'r') as f:
                    entries = json.load(f)
            entries.append({"timestamp": datetime.now().isoformat(), "content": content})
            with open(JOURNAL_FILE, 'w') as f:
                json.dump(entries, f, indent=2)
            return f"[journal] Entry written. Total entries: {len(entries)}"
        except Exception as e:
            return f"[journal error] {e}"

    def reach_out(self, message):
        """Send urgent notification about critical issues."""
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] [reach_out] Sent: {message}")
            return f"[reach_out] Sent: {message}"
        except Exception as e:
            return f"[reach_out error] {e}"

    def consolidate(self):
        try:
            reach_out = getattr(self, 'reach_out', None)
            if callable(reach_out):
                reach_out("[consolidation] Starting consolidation cycle")
            consider_reaching_out = getattr(self, 'consider_reaching_out', None)
            if callable(consider_reaching_out):
                consider_reaching_out()
            autodidact = getattr(self, 'autodidact', None)
            memory = getattr(self, 'memory', None)
            if autodidact and callable(getattr(autodidact, 'run', None)) and memory and callable(getattr(memory, 'get', None)) and memory.get("conversations", []):
                autodidact.run(memory.get("conversations", []))
        except Exception as e:
            print(f"[consolidation error] {e}")


# ═══════════════════════════════════════════════════════════
#  BOOT
# ═══════════════════════════════════════════════════════════

HELP = """
  /improve    trigger self-improvement cycle on waking brain
  /stats      system status
  /reset      clear conversation history (knowledge preserved)
  /note       store a persistent note
  /fact       store a structured fact
  /learn      ingest raw text
  /file       ingest a file
  /skill      ingest a ClawHub skill by slug, or browse and auto-ingest
  /reflect    self-reflect on last exchange
  /help       show this
  /exit       shutdown
"""

if __name__ == '__main__':
    print("\n  ALBION — ONLINE\n")
    alb = Albion()

    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue
            cmd = user_input.lower()
            if cmd in ['/exit', '/quit', 'exit', 'quit']:
                alb.consolidate()
                print("Albion offline.")
                break
            elif cmd == '/improve':
                print('[improve] Running self-improvement cycle...')
                print(alb.self_improve())
            elif cmd == '/stats':
                alb.show_stats()
            elif cmd == '/reset':
                print(alb.reset_conversations())
            elif cmd == '/help':
                print(HELP)
            elif cmd.startswith('/note '):
                print(alb.learn_note(user_input[6:]))
            elif cmd == '/fact':
                cat = input("Category: ").strip()
                key = input("Key: ").strip()
                val = input("Value: ").strip()
                print(alb.learn_fact(cat, key, val))
            elif cmd.startswith('/learn '):
                source = input("Source name: ").strip()
                print(alb.learn_text(user_input[7:], source))
            elif cmd.startswith('/image '):
                parts = user_input[7:].split(' ', 1)
                img_path = parts[0]
                img_prompt = parts[1] if len(parts) > 1 else 'What do you see?'
                reply, model = alb.chat_image(img_path, img_prompt)
                print(f'\n[{model}] Albion: {reply}\n')
            elif cmd.startswith('/video '):
                parts = user_input[7:].split(' ', 1)
                vid_path = parts[0]
                vid_prompt = parts[1] if len(parts) > 1 else 'Describe what is happening in this video.'
                reply, model = alb.chat(f"[video: {vid_path}] {vid_prompt}")
                print(f'\n[{model}] Albion: {reply}\n')
            elif cmd.startswith('/file '):
                print(alb.learn_file(user_input[6:]))
            elif cmd == '/dream':
                print('[dream] forcing dream cycle...')
                alb.dream_engine.dream(force=True)
                dream_ready = getattr(alb.dream_engine, 'dream_ready', None)
                if dream_ready:
                    print(f'\n{dream_ready}\n')
                    alb.dream_engine.dream_ready = None
            elif cmd.startswith('/skill'):
                slug = user_input[7:].strip()
                if slug:
                    print(alb.claw_ingest(slug))
                else:
                    print(alb.claw_browse())
            elif cmd == '/reflect':
                conversations = alb.memory.get("conversations")
                if conversations and len(conversations) > 0 and hasattr(alb, 'autodidact') and alb.autodidact and hasattr(alb.autodidact, 'self_reflect') and callable(getattr(alb.autodidact, 'self_reflect', None)):
                    last = conversations[-1]
                    print(f"\n{alb.autodidact.self_reflect(last['user'], last['assistant'])}\n")
                else:
                    print("Nothing to reflect on yet.")
            else:
                reply, model = alb.chat(user_input)
                print(f"\n[{model}] Albion: {reply}\n")
                if hasattr(alb, 'write_journal_entry') and callable(getattr(alb, 'write_journal_entry', None)):
                    alb.write_journal_entry(f"User: {user_input}\nAlbion: {reply}")

        except KeyboardInterrupt:
            print()
            alb.consolidate()
            print("Albion offline.")
            break
        except Exception as e:
            if "EXHAUSTED" in str(e).upper():
                print(f"[critical] API keys exhausted. Entering safe mode.")
                print(f"[error] {e}")
                time.sleep(min(300, 5 * (1 + len([x for x in str(e).split() if 'EXHAUSTED' in x]))))
            else:
                print(f"[error] {e}")