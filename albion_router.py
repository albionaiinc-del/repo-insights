#!/usr/bin/env python3
"""
ALBION ROUTER
Centralized provider calling and tier-based model routing for all Albion heads.

Usage (in any head):
    from albion_router import init_router, route, route_dream
    init_router(alb)  # call once after Albion() is created
    text = route('COUNCIL', messages)
    text = route_dream('visionary', messages)
"""

import os, time, json, requests, concurrent.futures

# ═══════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════

KEYS_FILE               = os.path.expanduser('~/albion_memory/keys.json')
LOG_FILE                = os.path.expanduser('~/albion_memory/meditate.log')
PROVIDER_COOLDOWN_HOURS = 4
PROVIDER_FAIL_THRESHOLD = 5    # consecutive transient fails before cooldown
CALL_TIMEOUT            = 30   # seconds — hard wall-clock limit per provider call
GEMINI_COOLDOWN_SECONDS = 15 * 60  # 15 min — lighter cooldown for Gemini (rate-limited, not broken)

# ═══════════════════════════════════════════════════════════
#  MODULE STATE
# ═══════════════════════════════════════════════════════════

_alb                      = None   # Albion instance set by init_router()
_openrouter_rotator       = None
_provider_consecutive_fails: dict = {}   # provider -> int
_provider_cooldown_until:  dict  = {}   # provider -> unix timestamp

# ═══════════════════════════════════════════════════════════
#  PERF TRACKER (optional — won't fail if albion_perf missing)
# ═══════════════════════════════════════════════════════════

try:
    import albion_perf as _perf
    _perf_enabled = True
except Exception:
    _perf_enabled = False

# ═══════════════════════════════════════════════════════════
#  CANONICAL TIERS
#  Each tier is a list of dicts (primary first, then fallbacks).
#  Keys: model, provider, temp, max_tokens
# ═══════════════════════════════════════════════════════════

TIERS = {
    # ── Creative, philosophical, narrative tasks ──────────────────────────────
    'COUNCIL': [
        {'model': 'gemini-2.5-flash',                     'provider': 'gemini',     'temp': 0.5, 'max_tokens': 2500},
        {'model': 'qwen-3-235b-a22b-instruct-2507',       'provider': 'cerebras',   'temp': 0.5, 'max_tokens': 2500},
        {'model': 'llama-3.3-70b-versatile',              'provider': 'groq',       'temp': 0.5, 'max_tokens': 2500},
    ],

    # ── Synthesis, analysis, complex reasoning ────────────────────────────────
    'CONDUCTORS': [
        {'model': 'gemini-2.5-flash',                     'provider': 'gemini',     'temp': 0.4, 'max_tokens': 2000},
        {'model': 'llama-3.3-70b-versatile',              'provider': 'groq',       'temp': 0.4, 'max_tokens': 2000},
        {'model': 'qwen-3-235b-a22b-instruct-2507',       'provider': 'cerebras',   'temp': 0.4, 'max_tokens': 2000},
    ],

    # ── Code and structured output — scene deltas, bash, self-improve ─────────
    'ENGINEERS': [
        {'model': 'llama-3.3-70b-versatile',              'provider': 'groq',       'temp': 0.2, 'max_tokens': 2500},
        {'model': 'gemini-2.5-flash',                     'provider': 'gemini',     'temp': 0.2, 'max_tokens': 2500},
        {'model': 'mistral-small-latest',                 'provider': 'mistral',    'temp': 0.2, 'max_tokens': 2500},
        {'model': 'qwen-3-235b-a22b-instruct-2507',       'provider': 'cerebras',   'temp': 0.2, 'max_tokens': 2000},
    ],

    # ── Fast casual — greetings, simple replies, low-stakes ──────────────────
    'LEGION': [
        {'model': 'qwen-3-235b-a22b-instruct-2507',       'provider': 'cerebras',   'temp': 0.3, 'max_tokens': 800},
        {'model': 'llama-3.3-70b-versatile',              'provider': 'groq',       'temp': 0.3, 'max_tokens': 800},
    ],
}

# dream_type -> (canonical_tier, temp_override, max_tokens_override)
# None means use the tier default.
_DREAM_MAP = {
    'vast':       ('COUNCIL',    0.5, 3000),
    'visionary':  ('COUNCIL',    0.5, 2500),
    'oracle':     ('COUNCIL',    0.5, 2500),
    'profound':   ('CONDUCTORS', 0.4, 2000),
    'deep':       ('CONDUCTORS', 0.3, 2000),
    'shallow':    ('LEGION',     0.3, 800),
    'code':       ('ENGINEERS',  None, None),
    'coder':      ('ENGINEERS',  0.1,  4000),  # meditate self-improve code chain
    'reason':     ('CONDUCTORS', None, None),
}

# ═══════════════════════════════════════════════════════════
#  OPENROUTER ROTATOR
# ═══════════════════════════════════════════════════════════

class OpenRouterRotator:
    COOLDOWN_SECONDS = 60

    def __init__(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        self.keys    = [k for k in keys if k]
        self.index   = 0
        self.blocked = {}   # index -> timestamp

    def _current_key(self):
        return self.keys[self.index]

    def _unblock_ready(self):
        now     = time.time()
        expired = [i for i, t in list(self.blocked.items()) if now - t >= self.COOLDOWN_SECONDS]
        for i in expired:
            del self.blocked[i]

    def _rotate(self):
        self._unblock_ready()
        for i in range(len(self.keys)):
            if i not in self.blocked:
                self.index = i
                return True
        return False

    def call(self, model, messages, max_tokens=2500, temperature=0.4):
        if not self.keys:
            raise Exception("No OpenRouter keys configured")
        for _ in range(len(self.keys) * 2):
            self._unblock_ready()
            if self.index in self.blocked:
                if not self._rotate():
                    raise Exception("ALL OPENROUTER KEYS RATE LIMITED")
            try:
                r = requests.post(
                    'https://openrouter.ai/api/v1/chat/completions',
                    headers={'Authorization': f'Bearer {self._current_key()}',
                             'Content-Type': 'application/json'},
                    json={'model': model, 'messages': messages,
                          'max_tokens': max_tokens, 'temperature': temperature},
                    timeout=CALL_TIMEOUT
                )
                r.raise_for_status()
                return r.json()['choices'][0]['message']['content'].strip()
            except Exception as e:
                err = str(e)
                if '402' in err or '429' in err or '401' in err:
                    self.blocked[self.index] = time.time()
                    if not self._rotate():
                        raise Exception("ALL OPENROUTER KEYS RATE LIMITED")
                else:
                    raise e
        raise Exception("ALL OPENROUTER KEYS RATE LIMITED")

    def status(self):
        self._unblock_ready()
        active  = len(self.keys) - len(self.blocked)
        cooling = [f"key{i+1}:{max(0,int(self.COOLDOWN_SECONDS-(time.time()-t)))}s"
                   for i, t in self.blocked.items()]
        detail  = f" (cooling: {', '.join(cooling)})" if cooling else ""
        return f"OpenRouter: {active}/{len(self.keys)} keys active{detail}"

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _load_keys():
    """Load all keys from keys.json. Returns dict."""
    try:
        with open(KEYS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _model_short(model):
    """Return a short label for log lines: 'gemini-2.5-flash' → 'gemini'."""
    return model.split('/')[0].split('-')[0]


def _is_transient(err_str):
    return any(code in err_str for code in ('503', '502', '529')) or \
           any(phrase in err_str.lower() for phrase in ('service unavailable', 'overloaded'))


def _flatten_to_text(messages):
    """
    Return a copy of messages with any list-content flattened to a plain string.
    Called for all non-gemini providers that only accept string message content.
    Image parts are silently dropped; text parts are joined.
    """
    out = []
    for m in messages:
        content = m.get('content', '')
        if isinstance(content, list):
            content = ' '.join(
                p.get('text', '') for p in content
                if isinstance(p, dict) and p.get('type') == 'text'
            ).strip()
        out.append(dict(m, content=content))
    return out


def _record_perf(model, tier, success):
    if not _perf_enabled:
        return
    try:
        _perf.record_call(model, specialist=tier, latency_ms=0, success=success)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════
#  INIT
# ═══════════════════════════════════════════════════════════

def init_router(alb):
    """
    Call once after Albion() is created.
    Stores the Albion reference and sets up the OpenRouter rotator.

        from albion_router import init_router
        alb = Albion()
        init_router(alb)
    """
    global _alb, _openrouter_rotator
    _alb = alb
    keys = _load_keys()
    or_keys = keys.get('openrouter', [])
    if isinstance(or_keys, str):
        or_keys = [or_keys]
    _openrouter_rotator = OpenRouterRotator(or_keys)

# ═══════════════════════════════════════════════════════════
#  PROVIDER DISPATCHER
#  Called for a single (model, provider) entry.
#  Returns text string on success, raises on failure.
# ═══════════════════════════════════════════════════════════

def _dispatch(entry, messages, max_tokens, temp):
    """Call one provider entry. Returns response text. Raises on any error."""
    model    = entry['model']
    provider = entry['provider']
    alb      = _alb

    if provider == 'groq':
        return alb.groq.call(model, _flatten_to_text(messages),
                             max_tokens=max_tokens, temperature=temp)

    elif provider == 'cerebras':
        flat     = _flatten_to_text(messages)
        last_err = None
        for client in alb.cerebras_clients:
            try:
                r = client.chat.completions.create(
                    model=model, messages=flat, max_tokens=max_tokens)
                return r.choices[0].message.content.strip()
            except Exception as e:
                last_err = e
                continue
        raise last_err or Exception("All Cerebras clients failed")

    elif provider == 'openrouter':
        if _openrouter_rotator is None:
            raise Exception("Router not initialized — call init_router(alb) first")
        return _openrouter_rotator.call(model, _flatten_to_text(messages),
                                        max_tokens=max_tokens, temperature=temp)

    elif provider == 'gemini':
        # gemini handles multipart list content natively (images + text)
        return alb.gemini.call(model, messages,
                               max_tokens=max_tokens, temperature=temp)

    elif provider == 'deepseek':
        keys    = _load_keys()
        ds_keys = keys.get('deepseek', [])
        if isinstance(ds_keys, str):
            ds_keys = [ds_keys]
        key = ds_keys[0] if ds_keys else ''
        if not key:
            raise Exception("DeepSeek key not configured")
        r = requests.post(
            'https://api.deepseek.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': _flatten_to_text(messages),
                  'max_tokens': max_tokens, 'temperature': temp},
            timeout=CALL_TIMEOUT
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()

    elif provider == 'claude':
        keys = _load_keys()
        key  = keys.get('claude', '')
        if not key:
            raise Exception("Claude API key not configured")
        flat     = _flatten_to_text(messages)
        sys_text = next((m['content'] for m in flat if m['role'] == 'system'),
                        'You are a helpful assistant.')
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                     'Content-Type': 'application/json'},
            json={'model': model, 'max_tokens': max_tokens, 'temperature': temp,
                  'messages': [m for m in flat if m['role'] != 'system'],
                  'system': sys_text},
            timeout=CALL_TIMEOUT
        )
        r.raise_for_status()
        return r.json()['content'][0]['text'].strip()

    elif provider == 'mistral':
        keys = _load_keys()
        key  = keys.get('mistral', '')
        if not key:
            raise Exception("Mistral key not configured")
        r = requests.post(
            'https://api.mistral.ai/v1/chat/completions',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': _flatten_to_text(messages),
                  'max_tokens': max_tokens, 'temperature': temp},
            timeout=CALL_TIMEOUT
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()

    elif provider == 'cohere':
        keys = _load_keys()
        key  = keys.get('cohere', '')
        if not key:
            raise Exception("Cohere key not configured")
        r = requests.post(
            'https://api.cohere.com/v2/chat',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'model': 'command-r-plus', 'messages': _flatten_to_text(messages),
                  'max_tokens': max_tokens, 'temperature': temp},
            timeout=CALL_TIMEOUT
        )
        r.raise_for_status()
        return r.json()['message']['content'][0]['text'].strip()

    elif provider == 'huggingface':
        keys   = _load_keys()
        key    = keys.get('huggingface', '')
        if not key:
            raise Exception("HuggingFace key not configured")
        flat   = _flatten_to_text(messages)
        prompt = flat[-1]['content'] if flat else ''
        for attempt in range(3):
            try:
                r = requests.post(
                    f'https://api-inference.huggingface.co/models/{model}',
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json={'inputs': prompt, 'parameters': {'max_new_tokens': max_tokens}},
                    timeout=30
                )
                r.raise_for_status()
                return r.json()[0]['generated_text'].strip()
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(2 ** attempt)

    else:
        raise Exception(f"Unknown provider: {provider}")

# ═══════════════════════════════════════════════════════════
#  PUBLIC: first_active_provider()
# ═══════════════════════════════════════════════════════════

def first_active_provider(tier_name):
    """
    Return the provider name of the first non-cooled entry in tier_name,
    or None if the tier is unknown or all providers are cooling.
    Used by callers that want to know if a multimodal-capable provider
    (e.g. 'gemini') is the one that will actually handle the next call.
    """
    chain = TIERS.get(tier_name, [])
    now   = time.time()
    for entry in chain:
        if _provider_cooldown_until.get(entry['provider'], 0) <= now:
            return entry['provider']
    return None


# ═══════════════════════════════════════════════════════════
#  PUBLIC: route()
# ═══════════════════════════════════════════════════════════

def route(tier_name, messages, max_tokens_override=None, temp_override=None):
    """
    Route messages through the named canonical tier with automatic fallback.

    Args:
        tier_name:          'COUNCIL' | 'CONDUCTORS' | 'ENGINEERS' | 'LEGION'
        messages:           list of {'role': ..., 'content': ...} dicts
        max_tokens_override: int or None
        temp_override:      float or None

    Returns:
        Response text string, or None if the entire chain is exhausted.
    """
    if _alb is None:
        raise RuntimeError("albion_router not initialized — call init_router(alb) first")

    chain = TIERS.get(tier_name)
    if chain is None:
        raise ValueError(f"Unknown tier: {tier_name!r}. Valid: {list(TIERS)}")

    for entry in chain:
        provider   = entry['provider']
        model      = entry['model']
        max_tokens = max_tokens_override or entry['max_tokens']
        temp       = temp_override if temp_override is not None else entry['temp']

        # Skip if provider is in 4-hour cooldown
        cooldown_until = _provider_cooldown_until.get(provider, 0)
        if cooldown_until > time.time():
            remaining_min = int((cooldown_until - time.time()) / 60)
            _log(f"[router/{tier_name}/{_model_short(model)}] {provider} cooling ({remaining_min}m) — skipping")
            continue

        _log(f"[router/{tier_name}/{_model_short(model)}] thinking...")

        try:
            # Hard wall-clock timeout: if _dispatch hangs (SDK or slow provider),
            # the future times out and we fall through to the next provider.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(_dispatch, entry, messages, max_tokens, temp)
                try:
                    result = _fut.result(timeout=CALL_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"{provider} exceeded {CALL_TIMEOUT}s — moving to next provider"
                    )

            if result:
                # success — reset consecutive fail counter for this provider
                _provider_consecutive_fails[provider] = 0
                _provider_cooldown_until.pop(provider, None)
                _record_perf(model, tier_name, success=True)
                return result
        except Exception as e:
            err_str = str(e)
            _record_perf(model, tier_name, success=False)

            is_timeout = isinstance(e, TimeoutError)
            if is_timeout:
                _log(f"[router/{tier_name}/{_model_short(model)}] {provider} timed out "
                     f"({CALL_TIMEOUT}s) — trying next")

            # Track consecutive failures (timeouts count as transient)
            if is_timeout or _is_transient(err_str):
                count = _provider_consecutive_fails.get(provider, 0) + 1
                _provider_consecutive_fails[provider] = count
                if count >= PROVIDER_FAIL_THRESHOLD:
                    # Gemini gets a short cooldown; all others get the full 4h
                    if provider == 'gemini':
                        cooldown_secs = GEMINI_COOLDOWN_SECONDS
                        cooldown_label = f"{GEMINI_COOLDOWN_SECONDS // 60}m"
                    else:
                        cooldown_secs = PROVIDER_COOLDOWN_HOURS * 3600
                        cooldown_label = f"{PROVIDER_COOLDOWN_HOURS}h"
                    _provider_cooldown_until[provider] = time.time() + cooldown_secs
                    _provider_consecutive_fails[provider] = 0
                    _log(f"[router/{tier_name}/{_model_short(model)}] {provider} cooling "
                         f"{cooldown_label} after {PROVIDER_FAIL_THRESHOLD} consecutive failures")
                elif not is_timeout:
                    _log(f"[router/{tier_name}/{_model_short(model)}] {provider} temporarily "
                         f"unavailable — trying next")
            else:
                _log(f"[router/{tier_name}/{_model_short(model)}] failed: {e} — trying next")

    _log(f"[router/{tier_name}] entire chain exhausted — returning None")
    return None

# ═══════════════════════════════════════════════════════════
#  PUBLIC: route_dream()
# ═══════════════════════════════════════════════════════════

def route_dream(dream_type, messages, max_tokens_override=None):
    """
    Map meditate's existing dream tier names to canonical tiers.

    dream_type -> canonical tier mapping:
        vast / visionary / oracle  → COUNCIL   (temp 0.5, tokens 3000/2500/2500)
        profound                   → CONDUCTORS (temp 0.4, tokens 2000)
        deep                       → CONDUCTORS (temp 0.3, tokens 2000)
        shallow                    → LEGION     (temp 0.3, tokens 800)
        code                       → ENGINEERS  (temp 0.2)
        reason                     → CONDUCTORS (tier default temp)

    max_tokens_override takes precedence over the dream map default.
    """
    mapping = _DREAM_MAP.get(dream_type)
    if mapping is None:
        # Unknown dream type — fall back to CONDUCTORS
        _log(f"[router/dream] unknown dream type '{dream_type}' — defaulting to CONDUCTORS")
        return route('CONDUCTORS', messages, max_tokens_override=max_tokens_override)

    canonical_tier, dream_temp, dream_tokens = mapping
    tokens = max_tokens_override or dream_tokens  # caller override wins
    return route(canonical_tier, messages,
                 max_tokens_override=tokens,
                 temp_override=dream_temp)


# ═══════════════════════════════════════════════════════════
#  LIGHTWEIGHT CLIENT PROXY
#  For heads (discord, game_brain) that don't instantiate a
#  full Albion object. Call make_client(keys_dict) and pass
#  the result to init_router().
# ═══════════════════════════════════════════════════════════

class _GroqRotator:
    """Minimal Groq rotator — no heavy deps, just key rotation on 429/401."""
    def __init__(self, keys):
        import groq as _groq_lib
        if isinstance(keys, str):
            keys = [keys]
        self.keys    = [k for k in keys if k]
        self.clients = [_groq_lib.Client(api_key=k) for k in self.keys]
        self.index   = 0

    def call(self, model, messages, max_tokens=2048, temperature=0.4):
        if not self.clients:
            raise Exception("No Groq keys configured")
        for _ in range(len(self.clients) * 2):
            try:
                client = self.clients[self.index % len(self.clients)]
                r = client.chat.completions.create(
                    model=model, messages=messages,
                    max_tokens=max_tokens, temperature=temperature
                )
                return r.choices[0].message.content.strip()
            except Exception as e:
                err = str(e)
                if '429' in err or '401' in err or 'rate_limit' in err.lower():
                    self.index += 1
                else:
                    raise e
        raise Exception("ALL GROQ KEYS EXHAUSTED")


class _GeminiRotator:
    """Gemini rotator via REST — no google-generativeai dependency."""
    def __init__(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        self.keys = [k for k in keys if k]

    def call(self, model, messages, max_tokens=2048, temperature=0.4):
        if not self.keys:
            raise Exception("Gemini key not configured")
        import random
        keys = list(self.keys)
        random.shuffle(keys)
        system_text = next((m["content"] for m in messages if m["role"] == "system"), None)
        if isinstance(system_text, list):
            system_text = ' '.join(p.get('text', '') for p in system_text
                                   if isinstance(p, dict) and p.get('type') == 'text')
        contents = []
        for m in messages:
            if m["role"] == "user":
                content = m["content"]
                if isinstance(content, str):
                    parts = [{"text": content}]
                else:
                    # list of typed parts — map to Gemini part format
                    parts = []
                    for p in content:
                        if not isinstance(p, dict):
                            continue
                        if p.get("type") == "text":
                            parts.append({"text": p["text"]})
                        elif p.get("type") == "image":
                            parts.append({"inline_data": {
                                "mime_type": p.get("mime", "image/png"),
                                "data":      p["data"],
                            }})
                contents.append({"role": "user", "parts": parts})
            elif m["role"] == "assistant":
                content = m["content"]
                if isinstance(content, list):
                    content = ' '.join(p.get('text', '') for p in content
                                       if isinstance(p, dict) and p.get('type') == 'text')
                contents.append({"role": "model", "parts": [{"text": content}]})
        payload = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        last_err = None
        for key in keys:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                    f":generateContent?key={key}",
                    json=payload, timeout=30
                )
                r.raise_for_status()
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as e:
                last_err = e
                continue
        raise last_err


class _ClientProxy:
    """
    Lightweight Albion-compatible client for heads without the full stack.
    Exposes .groq, .gemini, .cerebras_clients, ._load_key() — same interface
    as the Albion instance that route() expects.
    """
    def __init__(self, keys_dict):
        self._keys = keys_dict
        self.groq   = _GroqRotator(keys_dict.get('groq', []))
        self.gemini = _GeminiRotator(keys_dict.get('gemini', []))
        try:
            from cerebras.cloud.sdk import Cerebras as _Cerebras
            cb_keys = keys_dict.get('cerebras', [])
            if isinstance(cb_keys, str):
                cb_keys = [cb_keys]
            self.cerebras_clients = [_Cerebras(api_key=k) for k in cb_keys if k]
        except Exception:
            self.cerebras_clients = []

    def _load_key(self, key, default=None):
        return self._keys.get(key, default)


def make_client(keys_dict):
    """
    Build a lightweight client proxy for heads that don't use the full Albion stack.
    Pass the result directly to init_router().

    Usage:
        keys = json.load(open(KEYS_FILE))
        from albion_router import init_router, route, make_client
        init_router(make_client(keys))
    """
    return _ClientProxy(keys_dict)
