"""
skill_2d_scene.py — Albion skill

Registers the [BUILD_2D] command tag. When Albion emits:
    [BUILD_2D]a moonlit pixel-art village with flickering torches[/BUILD_2D]
this skill asks COUNCIL to generate a self-contained HTML5 Canvas file,
saves it to ~/albion_memory/oasis_scene/2d_{timestamp}.html, and logs
the result.

Trigger words (for waking task selection): 2d, flat, arcade, pixel,
canvas, draw 2d, sketch.
"""

import os
import time

from albion_commands import register
from albion_router import route

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCENE_DIR = os.path.expanduser('~/albion_memory/oasis_scene')

# ── Prompt ────────────────────────────────────────────────────────────────────
_PROMPT_TEMPLATE = (
    "Generate a single HTML file with an HTML5 Canvas scene based on this description: "
    "{description}. "
    "Use JavaScript to draw shapes, colors, gradients, and simple animations. "
    "The canvas should be 800x600. "
    "Output ONLY the complete HTML file, no explanation. "
    "Keep it under 200 lines."
)


# ── Validator ─────────────────────────────────────────────────────────────────

def _validate(text):
    stripped = text.strip()
    if not stripped:
        return False, ''
    return True, stripped


# ── Executor ──────────────────────────────────────────────────────────────────

def _execute(args, context):
    description = args
    os.makedirs(_SCENE_DIR, exist_ok=True)

    prompt = _PROMPT_TEMPLATE.format(description=description)
    messages = [{'role': 'user', 'content': prompt}]

    print(f"[build_2d] Generating canvas scene: {description[:80]}", flush=True)

    html = route('COUNCIL', messages, max_tokens_override=4096)
    if not html:
        print("[build_2d] COUNCIL returned nothing.", flush=True)
        return "\n[BUILD_2D]: generation failed — no output from COUNCIL"

    # Strip markdown fences if the model wrapped the HTML
    if html.strip().startswith('```'):
        lines = html.strip().splitlines()
        # Drop first and last fence lines
        inner = []
        in_fence = False
        for line in lines:
            if line.startswith('```'):
                in_fence = not in_fence
                continue
            inner.append(line)
        html = '\n'.join(inner)

    ts = time.strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(_SCENE_DIR, f'2d_{ts}.html')
    try:
        with open(out_path, 'w') as f:
            f.write(html)
        print(f"[build_2d] Saved: {out_path}", flush=True)
    except Exception as e:
        print(f"[build_2d] Save failed: {e}", flush=True)
        return f"\n[BUILD_2D]: save failed — {e}"

    # Log to oasis_log.jsonl alongside 3D review records
    log_path = os.path.expanduser('~/albion_memory/oasis_log.jsonl')
    import json
    record = {
        'ts':          time.strftime('%Y-%m-%dT%H:%M:%S'),
        'type':        'build_2d',
        'description': description,
        'file':        out_path,
        'lines':       html.count('\n'),
    }
    try:
        with open(log_path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass

    return f"\n[BUILD_2D]: saved {os.path.basename(out_path)} ({html.count(chr(10))} lines)"


# ── Registration ──────────────────────────────────────────────────────────────

# Exposed so waking's _action_build_2d can call execute directly without
# needing a tagged LLM response.
execute = _execute

TRIGGER_WORDS = ['2d', 'flat', 'arcade', 'pixel', 'canvas', 'draw 2d', 'sketch']


def register_commands():
    """Called by albion_commands.load_skills() on boot."""
    register(
        name          = 'BUILD_2D',
        tags          = ['[BUILD_2D]'],
        tier          = 'COUNCIL',
        requires_bash = False,
        validator     = _validate,
        executor      = _execute,
    )
    print("[commands] BUILD_2D registered")
