#!/usr/bin/env bash
# Publish (or update) a claude.ai Artifact from a throwaway side session, so your normal sessions
# can keep the Artifact tool off (it adds ~12-16k start tokens to every session).
#
# Usage: publish-artifact.sh FILE [EXISTING_ARTIFACT_URL] [--timeout SECS]
# Prints the artifact URL on success. Log: ${PUBLISH_LOG:-/tmp/publish-artifact.log} (includes the
# side session's screen on timeout). Safe to call from inside a Claude Code session: it drops the
# child-session env and forces transcript persistence, which it needs to read the URL back.
# Env: PUBLISH_CWD  a TRUSTED dir to start the side session in (default: $HOME); a new folder stops on the trust prompt
#      LAUNCH_SHELL run claude through a shell, e.g. "zsh -ic" when `claude` is a function in your rc (default: none)
# Keep Artifact off with `export CLAUDE_CODE_DISABLE_ARTIFACT=1` in your shell rc, NOT with
# enableArtifact:false in settings.json: nothing overrides that key, so this script couldn't turn it back on.
set -u
F=${1:?usage: publish-artifact.sh FILE [URL] [--timeout SECS]}; shift; URL=""; TO=180
while [ $# -gt 0 ]; do case $1 in --timeout) TO=$2; shift 2;; *) URL=$1; shift;; esac; done
command -v tmux >/dev/null || { echo "needs tmux" >&2; exit 2; }
F=$(cd "$(dirname "$F")" && pwd -P)/$(basename "$F"); [ -f "$F" ] || { echo "no such file: $F" >&2; exit 2; }
case "$F$URL" in *[\'\"]*) echo "no quotes in path/url" >&2; exit 2;; esac
D=$(dirname "$F"); CWD=$(cd "${PUBLISH_CWD:-$HOME}" && pwd -P); SH=${LAUNCH_SHELL:-}
LOG=${PUBLISH_LOG:-/tmp/publish-artifact.log}; TAG="pubart-$(date +%s)-$$"; S="pubart$$"
PROJ=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(printf '%s' "$CWD" | sed 's#[^A-Za-z0-9]#-#g')
if [ -n "$URL" ]; then ACT="Read $URL with the Artifact tool (action read), then republish $F to that same url"
else ACT="Publish $F with the Artifact tool (one short generic icon word)"; fi
P="$ACT. Do not edit the file. Then reply with only the artifact URL on one line. [$TAG]"
echo "$(date +%T) $TAG start $F ${URL:-new}" >> "$LOG"
MARK=$(mktemp); touch "$MARK"
# The prompt goes BEFORE --add-dir: --add-dir takes a list and would swallow it.
RUN="unset CLAUDE_CODE_DISABLE_ARTIFACT CLAUDE_CODE_CHILD_SESSION; export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1; claude \"$P\" --permission-mode acceptEdits --add-dir \"$D\""
[ -n "$SH" ] && RUN="$SH '$RUN'"
env -u CLAUDE_CODE_DISABLE_ARTIFACT -u CLAUDE_CODE_CHILD_SESSION tmux new-session -d -s "$S" -x 200 -y 50 -c "$CWD" "$RUN" || { echo "tmux failed" >&2; exit 1; }
J=""; OUT=""
for ((i=0; i<TO/2; i++)); do
  tmux has-session -t "$S" 2>/dev/null || { OUT=${OUT:-"ERROR: side session exited early"}; break; }
  [ -z "$J" ] && J=$(find "$PROJ" -name '*.jsonl' -newer "$MARK" 2>/dev/null | xargs grep -l -- "$TAG" 2>/dev/null | head -1)
  if [ -n "$J" ]; then
    # Parse the final reply: every assistant line contains "server_tool_use", so grep -v tool_use drops them all.
    OUT=$(python3 - "$J" <<'PY'
import json,re,sys
out=''
for l in open(sys.argv[1],errors='ignore'):
    if 'No such tool available: Artifact' in l: out='ERROR: Artifact tool missing in side session (is enableArtifact:false in settings.json?)'
    try: r=json.loads(l)
    except Exception: continue
    if r.get('type')!='assistant' or r['message'].get('stop_reason')!='end_turn': continue
    for c in r['message'].get('content',[]):
        if c.get('type')=='text':
            m=re.findall(r'https://claude\.ai/code/artifact/[0-9a-f-]+',c['text'])
            if m: out=m[-1]
print(out)
PY
)
    [ -n "$OUT" ] && break
  fi; sleep 2
done
rm -f "$MARK"
[ -z "$OUT" ] && { echo "--- pane at timeout:"; tmux capture-pane -p -t "$S" | grep -v '^\s*$' | tail -20; } >> "$LOG"
tmux kill-session -t "$S" 2>/dev/null
echo "$(date +%T) $TAG result ${OUT:-timeout} jsonl=${J:-none}" >> "$LOG"
case "$OUT" in https://*) echo "$OUT"; exit 0;; esac
echo "${OUT:-timeout after ${TO}s} (log: $LOG)" >&2; exit 1
