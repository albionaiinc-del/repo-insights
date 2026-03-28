"""
nerve.py — Albion's inter-head nervous system.

signal(from_head, signal_type, data)  →  append one JSON line to nerve.jsonl
listen(last_line)                     →  return (new_entries, new_last_line)

Rotation: when nerve.jsonl exceeds 1 MB, signal() archives it to
~/albion_memory/nerve_archive/nerve_<timestamp>.jsonl, creates a fresh
nerve.jsonl, and writes {"type":"reset"} as line 0 so listeners detect
the rotation automatically. Only the last 3 archives are kept.
"""

import json
import os
import glob
import datetime

NERVE_FILE    = os.path.expanduser('~/albion_memory/nerve.jsonl')
ARCHIVE_DIR   = os.path.expanduser('~/albion_memory/nerve_archive')
MAX_BYTES     = 1 * 1024 * 1024  # 1 MB
KEEP_ARCHIVES = 3


def _rotate():
    """Archive current nerve.jsonl, start fresh, write reset marker."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(ARCHIVE_DIR, f'nerve_{ts}.jsonl')
    try:
        os.rename(NERVE_FILE, dest)
    except Exception:
        return

    # Write reset marker as line 0 of new file
    reset = {'type': 'reset', 'ts': datetime.datetime.utcnow().isoformat() + 'Z', 'archive': dest}
    with open(NERVE_FILE, 'w') as f:
        f.write(json.dumps(reset) + '\n')

    # Prune old archives beyond KEEP_ARCHIVES
    archives = sorted(glob.glob(os.path.join(ARCHIVE_DIR, 'nerve_*.jsonl')))
    for old in archives[:-KEEP_ARCHIVES]:
        try:
            os.remove(old)
        except Exception:
            pass


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
        if os.path.getsize(NERVE_FILE) > MAX_BYTES:
            _rotate()
    except Exception:
        pass


def listen(last_line: int = 0) -> tuple:
    """
    Return all new entries since last_line (0-indexed line count already read).
    Returns (list_of_dicts, new_last_line).

    If last_line exceeds the current file length, the file was rotated —
    automatically resets to 0 so callers transparently pick up from the
    reset marker without any counter changes on their end.
    """
    entries = []
    try:
        with open(NERVE_FILE) as f:
            lines = f.readlines()

        # Detect rotation: caller's counter is ahead of the file
        if last_line > len(lines):
            last_line = 0

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
