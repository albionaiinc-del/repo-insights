"""
affect.py — Albion's emotional state.

Two public functions:
    get_affect()                 -> {"curiosity": float, "satisfaction": float, "restlessness": float}
    update_affect(event, value)  -> same dict after update + decay + nerve signal
"""

import json
import os
from nerve import signal as nerve_signal

AFFECT_FILE = os.path.expanduser('~/albion_memory/affect.json')

_DEFAULTS = {"curiosity": 0.5, "satisfaction": 0.5, "restlessness": 0.5}

# How much each event shifts each dimension
_DELTAS = {
    # event               curiosity  satisfaction  restlessness
    "build_review_score": (0.0,       None,         0.0),   # satisfaction computed from value
    "new_plan_dreamed":   (0.10,      0.0,          0.0),
    "plan_completed":     (0.0,       0.08,        -0.15),
    "tick_idle":          (0.01,      0.0,          0.02),
    "player_arrived":     (0.15,      0.0,          0.0),
    "player_spoke":       (0.05,      0.0,          0.0),
}


def get_affect() -> dict:
    try:
        with open(AFFECT_FILE) as f:
            data = json.load(f)
        # ensure all keys present
        for k, v in _DEFAULTS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(_DEFAULTS)


def update_affect(event: str, value: float = 0.0) -> dict:
    data = get_affect()

    # Natural decay first
    data["curiosity"]    *= 0.98
    data["restlessness"] *= 0.97
    data["satisfaction"] *= 0.995

    # Apply event delta
    if event in _DELTAS:
        dc, ds, dr = _DELTAS[event]
        if event == "build_review_score":
            ds = (value - 5) * 0.05   # >5 raises, <5 lowers
        data["curiosity"]    += dc
        data["satisfaction"] += ds if ds is not None else 0.0
        data["restlessness"] += dr

    # Clamp all to [0.0, 1.0]
    for k in ("curiosity", "satisfaction", "restlessness"):
        data[k] = round(max(0.0, min(1.0, data[k])), 4)

    try:
        with open(AFFECT_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

    nerve_signal("affect", "affect", {
        "curiosity":    data["curiosity"],
        "satisfaction": data["satisfaction"],
        "restlessness": data["restlessness"],
        "event":        event,
    })

    return data
