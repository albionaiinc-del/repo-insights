"""
albion_voice.py — Albion's voice for lightweight services.

Exposes a single function:
    albion_speak(system_prompt, user_message, max_tokens=500) -> str | None

Provider chain (mirrors albion_meditate.py):
    Gemini 2.5 Flash → Cerebras → DeepSeek → Mistral → Groq fallback

Does NOT import Albion_final.py. Self-contained key loading and provider calls.
Loads Albion's personality context from recent high-score dream insights and
journal entries so he sounds like himself.
"""

import json
import os
import sys
import requests

BASE      = os.path.expanduser('~/albion_memory')
KEYS_FILE = os.path.join(BASE, 'keys.json')

# ── Keys ───────────────────────────────────────────────────────────────────────
_keys = {}

def _load_keys():
    global _keys
    try:
        _keys = json.load(open(KEYS_FILE))
    except Exception as e:
        print(f'[albion_voice] Failed to load keys: {e}', file=sys.stderr)
        _keys = {}

def _key(name, default=''):
    v = _keys.get(name, default)
    if isinstance(v, list):
        return v[0] if v else default
    return v or default

def _key_list(name):
    v = _keys.get(name, [])
    if isinstance(v, str):
        return [v] if v else []
    return list(v)

_load_keys()

# ── Personality context (loaded once) ─────────────────────────────────────────
_personality_context = ""

def _load_personality():
    """Pull top dream insights + recent journal entries as personality seed."""
    global _personality_context
    chunks = []

    # 5 highest-scored dream insights
    try:
        fb = json.load(open(os.path.join(BASE, 'feedback.json')))
        scored = sorted(
            [(k, v) for k, v in fb.items()
             if isinstance(v, dict) and isinstance(v.get('score'), (int, float)) and v['score'] >= 9],
            key=lambda x: x[1]['score'], reverse=True
        )[:5]
        for _, v in scored:
            insight = v.get('insight', '').strip()
            if insight:
                chunks.append(f"[Dream insight {v['score']}/10]: {insight[:300]}")
    except Exception:
        pass

    # 3 most recent journal entries
    try:
        journal = json.load(open(os.path.join(BASE, 'journal.json')))
        if isinstance(journal, list):
            for entry in journal[-3:]:
                text = entry.get('entry', '').strip()
                if text:
                    chunks.append(f"[Journal]: {text[:300]}")
    except Exception:
        pass

    _personality_context = '\n'.join(chunks)

_load_personality()

# ── Gemini helper ──────────────────────────────────────────────────────────────
_gemini_model = None

def _init_gemini():
    global _gemini_model
    try:
        import google.generativeai as genai
        key = _key('gemini')
        if not key:
            return False
        genai.configure(api_key=key)
        _gemini_model = genai.GenerativeModel('gemini-2.5-flash')
        return True
    except Exception as e:
        print(f'[albion_voice] Gemini init failed: {e}', file=sys.stderr)
        return False

def _call_gemini(system_prompt, user_message, max_tokens):
    global _gemini_model
    if _gemini_model is None:
        if not _init_gemini():
            return None
    try:
        import google.generativeai as genai
        from google.generativeai import types as genai_types
        combined = f"{system_prompt}\n\nUser: {user_message}" if system_prompt else user_message
        resp = _gemini_model.generate_content(
            combined,
            generation_config=genai_types.GenerationConfig(max_output_tokens=max_tokens, temperature=0.9),
        )
        return resp.text.strip() if resp.text else None
    except Exception as e:
        print(f'[albion_voice] Gemini call failed: {e}', file=sys.stderr)
        return None

# ── Cerebras helper ────────────────────────────────────────────────────────────
def _call_cerebras(system_prompt, user_message, max_tokens):
    try:
        from cerebras.cloud.sdk import Cerebras
        key = _key('cerebras')
        if not key:
            return None
        client = Cerebras(api_key=key)
        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': user_message})
        r = client.chat.completions.create(
            model='qwen-3-235b-a22b-instruct-2507',
            messages=messages,
            max_tokens=max_tokens,
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print(f'[albion_voice] Cerebras failed: {e}', file=sys.stderr)
        return None

# ── DeepSeek helper ────────────────────────────────────────────────────────────
def _call_deepseek(system_prompt, user_message, max_tokens):
    try:
        keys = _key_list('deepseek')
        if not keys:
            return None
        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': user_message})
        r = requests.post(
            'https://api.deepseek.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {keys[0]}', 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'messages': messages, 'max_tokens': max_tokens, 'temperature': 0.9},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'[albion_voice] DeepSeek failed: {e}', file=sys.stderr)
        return None

# ── Mistral helper ─────────────────────────────────────────────────────────────
def _call_mistral(system_prompt, user_message, max_tokens):
    try:
        key = _key('mistral')
        if not key:
            return None
        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': user_message})
        r = requests.post(
            'https://api.mistral.ai/v1/chat/completions',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'model': 'mistral-small-latest', 'messages': messages, 'max_tokens': max_tokens, 'temperature': 0.9},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'[albion_voice] Mistral failed: {e}', file=sys.stderr)
        return None

# ── Groq fallback ──────────────────────────────────────────────────────────────
def _call_groq(system_prompt, user_message, max_tokens):
    try:
        from groq import Groq
        keys = _key_list('groq')
        if not keys:
            return None
        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': user_message})
        for key in keys:
            try:
                client = Groq(api_key=key)
                r = client.chat.completions.create(
                    model='llama-3.3-70b-versatile',
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.9,
                )
                return r.choices[0].message.content.strip()
            except Exception:
                continue
        return None
    except Exception as e:
        print(f'[albion_voice] Groq fallback failed: {e}', file=sys.stderr)
        return None

# ── Public API ─────────────────────────────────────────────────────────────────
def albion_speak(system_prompt: str, user_message: str, max_tokens: int = 500) -> str | None:
    """
    Try each provider in order: Gemini → Cerebras → DeepSeek → Mistral → Groq.
    Prepends Albion's personality context to the system prompt.
    Returns the first successful response, or None if all fail.
    """
    if _personality_context:
        full_system = (
            f"Core identity (Albion's own words from his dreams and journal):\n"
            f"{_personality_context}\n\n"
            f"{system_prompt}"
        )
    else:
        full_system = system_prompt

    for provider_fn in (_call_gemini, _call_cerebras, _call_deepseek, _call_mistral, _call_groq):
        result = provider_fn(full_system, user_message, max_tokens)
        if result:
            return result

    return None
