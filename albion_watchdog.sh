#!/usr/bin/env bash
# ─── Albion Watchdog ───────────────────────────────────────────────
# Tails meditate.log, catches errors, feeds them to Claude Code.
# Deduplicates, rate-limits, and logs everything.
#
# Usage:  ~/albion_watchdog.sh &
# Stop:   pkill -f albion_watchdog
# Log:    tail -f ~/albion_memory/watchdog.log
# ────────────────────────────────────────────────────────────────────

set -euo pipefail

LOG="$HOME/albion_memory/meditate.log"
WATCHDOG_LOG="$HOME/albion_memory/watchdog.log"
SEEN_FILE="/tmp/albion_watchdog_seen"
PROJECT_ROOT="$HOME"

# Tuning
COOLDOWN=300          # seconds between Claude Code calls
MIN_ERROR_GAP=30      # seconds before same error re-triggers
MAX_DAILY_CALLS=80    # stop feeding Claude after this many per day
CONTEXT_LINES=15      # lines of log context to send with each error

# State
last_call=0
daily_calls=0
current_day=$(date +%Y-%m-%d)

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$WATCHDOG_LOG"; }
log "Watchdog started. Tailing $LOG"

: > "$SEEN_FILE"

tail -F "$LOG" 2>/dev/null | while IFS= read -r line; do

  # Reset daily counter at midnight
  today=$(date +%Y-%m-%d)
  if [[ "$today" != "$current_day" ]]; then
    daily_calls=0
    current_day="$today"
    : > "$SEEN_FILE"
    log "New day — counters reset"
  fi

  # Skip if not an error
  echo "$line" | grep -qiE "Cycle error|traceback|exception|crash|critical|\[ERROR\]" || continue

  # Skip noise
  echo "$line" | grep -qiE "chromadb|std::bad_alloc|deprecat" && continue

  # Skip Albion's own content — these tags carry his thoughts, not real errors
  # Log lines are timestamped: [HH:MM:SS] [tag] content — match tag after timestamp
  echo "$line" | grep -qiE "^\[[0-9:]+\] \[?(nerve|synthesis|reach_out|research|dream|reflect|journal|vantage|intent|improve|profound|vast|visionary|shallow|deep|code|coder|reason|affect|recall|new-cap|oracle|skill-refresh|claude_coder|claude_review|deepseek_review)" && continue
  echo "$line" | grep -qiE "Dreaming:|Scored [0-9]|dream_queue|Task emitted:|Nerve review:|Sent:|Insight:" && continue
  echo "$line" | grep -qiE "^\[[0-9:]+\] \[gate\]" && continue

  # Dedup: hash the core error message
  err_hash=$(echo "$line" | sed 's/[0-9]\{10,\}//g' | md5sum | cut -c1-12)
  now=$(date +%s)

  if grep -q "$err_hash" "$SEEN_FILE" 2>/dev/null; then
    last_seen=$(grep "$err_hash" "$SEEN_FILE" | tail -1 | cut -d' ' -f2)
    elapsed=$((now - last_seen))
    [[ $elapsed -lt $MIN_ERROR_GAP ]] && continue
  fi

  # Record this error
  echo "$err_hash $now" >> "$SEEN_FILE"

  # Respect cooldown
  since_last=$((now - last_call))
  if [[ $since_last -lt $COOLDOWN ]]; then
    log "SKIP (cooldown ${since_last}s/${COOLDOWN}s): $line"
    continue
  fi

  # Respect daily cap
  if [[ $daily_calls -ge $MAX_DAILY_CALLS ]]; then
    log "SKIP (daily cap $daily_calls/$MAX_DAILY_CALLS): $line"
    continue
  fi

  # Grab surrounding context
  context=$(tail -n "$CONTEXT_LINES" "$LOG")

  log "DISPATCH #$((daily_calls + 1)): $line"

  # Feed to Claude Code
  prompt="Error detected in meditate.log. Investigate and fix if possible. Do not modify anything in ~/albion_memory/. Commit any fixes.

Error line:
$line

Recent log context:
$context"

  cd "$PROJECT_ROOT"
  echo "$prompt" | claude -p 2>> "$WATCHDOG_LOG" &

  last_call=$now
  daily_calls=$((daily_calls + 1))

done
