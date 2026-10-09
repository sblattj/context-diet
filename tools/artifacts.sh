#!/usr/bin/env bash
# Check / fix how the Artifact tool is switched off.  Usage: artifacts.sh [status|fix]
# Recommended: `export CLAUDE_CODE_DISABLE_ARTIFACT=1` in your shell rc, and publish with tools/publish-artifact.sh.
# `fix` removes enableArtifact / disableArtifact / env.CLAUDE_CODE_DISABLE_ARTIFACT from ~/.claude/settings.json:
# nothing overrides those keys, running sessions hot-reload them ("off" reaches live sessions, "on" never does),
# and they block the side-session publisher.
S=${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
case ${1:-status} in
  status)
    python3 - "$S" <<'PY'
import json,os,sys
p=sys.argv[1];s=json.load(open(p)) if os.path.exists(p) else {}
bad=[k for k in ('enableArtifact','disableArtifact') if k in s]+(['env.CLAUDE_CODE_DISABLE_ARTIFACT'] if 'CLAUDE_CODE_DISABLE_ARTIFACT' in s.get('env',{}) else [])
print('settings.json:', ('has '+', '.join(bad)+' (run: artifacts.sh fix)') if bad else 'clean')
PY
    echo "this shell: CLAUDE_CODE_DISABLE_ARTIFACT=${CLAUDE_CODE_DISABLE_ARTIFACT:-unset} ($([ "${CLAUDE_CODE_DISABLE_ARTIFACT:-}" = 1 ] && echo 'new sessions start without Artifact' || echo 'new sessions get Artifact, ~12-16k more start tokens'))";;
  fix)
    cp "$S" "$S.bak.$(date +%s)"
    python3 - "$S" <<'PY'
import json,sys
p=sys.argv[1];s=json.load(open(p))
for k in ('enableArtifact','disableArtifact'): s.pop(k,None)
s.get('env',{}).pop('CLAUDE_CODE_DISABLE_ARTIFACT',None)
json.dump(s,open(p,'w'),indent=2);open(p,'a').write('\n');print('cleaned',p)
PY
    echo 'Now add to your shell rc:  export CLAUDE_CODE_DISABLE_ARTIFACT=1';;
  *) sed -n 2,6p "$0";;
esac
