#!/usr/bin/env bash
# Real INTERACTIVE start tokens: one fresh session for the baseline, then one per tool removed
# with --disallowedTools. `claude -p` reads ~21k low (no Artifact tool, smaller system prompt), so
# use this when the question is "what does a real session start at".
#
# Usage: probe-interactive.sh [TOOL ...]       default tools: Artifact Workflow Agent AskUserQuestion
# Env:   PROBE_CWD   trusted dir to start sessions in (default: $PWD; a new folder stops on the trust prompt)
#        PROBE_OUT   results dir (default: ./probe-out); log: $PROBE_OUT/probe.log, rows: probe.jsonl
#        LAUNCH_SHELL run claude through a shell, e.g. "zsh -ic" when `claude` is a function in your rc
#        PROBE_WAIT  seconds to wait per session (default 90)
#        PROBE_START seconds for the prompt to reach the session (default 45). If no transcript holds
#                    the prompt by then, the run aborts (exit 3) with the pane saved to $PROBE_OUT, rather
#                    than idling PROBE_WAIT on every remaining session.
# Needs tmux. The prompt goes in as an argument: a bare `claude` may open agent view, where
# tmux send-keys lands in the draft box and never submits. Run it twice: agent view can hand
# you a pre-warmed session that still has the old settings, so the first base row can be stale.
# The prompt goes after `--`: a wrapper that puts a variadic option such as `--add-dir <dirs...>`
# in front of your args would otherwise swallow it, and the session opens empty.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
CWD=$(cd "${PROBE_CWD:-$PWD}" && pwd -P); OUT=${PROBE_OUT:-./probe-out}; mkdir -p "$OUT"
LOG=$OUT/probe.log; ROWS=$OUT/probe.jsonl; WAIT=${PROBE_WAIT:-90}; START=${PROBE_START:-45}; SH=${LAUNCH_SHELL:-}
PROJ=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(printf '%s' "$CWD" | sed 's#[^A-Za-z0-9]#-#g')
TOOLS=("$@"); [ ${#TOOLS[@]} -eq 0 ] && TOOLS=(Artifact Workflow Agent AskUserQuestion)
command -v tmux >/dev/null || { echo "needs tmux" >&2; exit 2; }
probe() { # tag, extra flags
  local tag=$1 flags=$2 s="probe$$$RANDOM" f="" t="" t0=$SECONDS
  echo "$(date +%T) start $tag ($flags)" | tee -a "$LOG"
  local run="claude $flags -- \"Reply with exactly: OK [$tag]\""; [ -n "$SH" ] && run="$SH '$run'"
  tmux new-session -d -s "$s" -x 200 -y 50 -c "$CWD" "$run" \; set-option -t "$s" remain-on-exit on
  while [ $((SECONDS-t0)) -lt "$WAIT" ]; do
    sleep 2
    f=$(grep -l -F "OK [$tag]" "$PROJ"/*.jsonl 2>/dev/null | head -1)
    [ -n "$f" ] && grep -q '"end_turn"' "$f" && break
    if [ -z "$f" ] && { [ $((SECONDS-t0)) -ge "$START" ] || [ "$(tmux display -p -t "$s" '#{pane_dead}' 2>/dev/null)" != 0 ]; }; then
      tmux capture-pane -p -t "$s" > "$OUT/$tag.pane.txt" 2>/dev/null; tmux kill-session -t "$s" 2>/dev/null
      echo "$(date +%T) ABORT $tag: the prompt never reached a session within $((SECONDS-t0))s (no transcript in $PROJ holds it)." | tee -a "$LOG" >&2
      echo "  Screen: $OUT/$tag.pane.txt. Launched: $run" | tee -a "$LOG" >&2
      echo "  Usual causes: a trust or login prompt, agent view, or a shell wrapper that dropped the prompt." | tee -a "$LOG" >&2
      exit 3
    fi
  done
  tmux kill-session -t "$s" 2>/dev/null
  [ -n "$f" ] && t=$(python3 "$HERE/start_tokens.py" "$f" --json | python3 -c 'import json,sys;r=json.load(sys.stdin);print(r[0]["start"] if r else "")')
  printf '{"variant": "%s", "start_tokens": %s}\n' "$tag" "${t:-null}" | tee -a "$ROWS" "$LOG"
  [ -n "$t" ] || { echo "$(date +%T) ABORT $tag: no finished first turn within ${WAIT}s ($f)" | tee -a "$LOG" >&2; exit 4; }
}
R=$(date +%H%M%S)
probe "base-$R" ""
for tool in "${TOOLS[@]}"; do probe "no-$tool-$R" "--disallowedTools $tool"; done
probe "base2-$R" ""
echo "$(date +%T) DONE" >> "$LOG"; echo "rows:"; grep -- "-$R\"" "$ROWS"
