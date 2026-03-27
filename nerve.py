"""
nerve.py — Albion's inter-head nervous system.

signal(from_head, signal_type, data)  →  append one JSON line to nerve.jsonl
listen(last_line)                     →  return (new_entries, new_last_line)
"""

import json
import os
import datetime

NERVE_FILE = os.path.expanduser('~/albion_memory/nerve.jsonl')


def signal(from_head: str, signal_type: str, data: dict) -> None:
    entry = {
        'ts':   datetime.datetime.utcnow().isoformat() + 'Z',
        'from': from_head,
        'type': signal_type,
        'data': data,
    }
    try:
        with open(NERVE_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass


def listen(last_line: int = 0) -> tuple:
    """
    Return all new entries since last_line (0-indexed line count already read).
    Returns (list_of_dicts, new_last_line).
    """
    entries = []
    try:
        with open(NERVE_FILE) as f:
            lines = f.readlines()
        new_lines = lines[last_line:]
        for line in new_lines:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries, last_line + len(new_lines)
    except Exception:
        return [], last_line
