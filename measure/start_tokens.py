#!/usr/bin/env python3
"""Real start tokens of Claude Code sessions, read from the session jsonl (no /context needed).
start = first assistant usage: input + cache_creation + cache_read. Blocks ~= attachment chars / 4.
Usage: start_tokens.py [FILE.jsonl ...] [--dir PROJECT_DIR] [--last N] [--all-entrypoints] [--json]
Rows are keyed by session START time (file mtime moves when a session is resumed). Default: newest 5 interactive (entrypoint=cli) sessions across ~/.claude/projects."""
import json,sys,os,glob,argparse,datetime
ap=argparse.ArgumentParser();ap.add_argument('files',nargs='*');ap.add_argument('--dir');ap.add_argument('--last',type=int,default=5)
ap.add_argument('--all-entrypoints',action='store_true',help='include -p/sdk sessions (they undercount by ~21k)');ap.add_argument('--json',action='store_true');a=ap.parse_args()
BLOCKS=['skill_listing','instructions','mcp_instructions_delta','deferred_tools_delta','agent_listing_delta','output_style_instructions','prompt_snapshot']
def parse(p):
    att={};ep=None;prompt='';model=None;ts=None
    try:
        for l in open(p,errors='ignore'):
            try: r=json.loads(l)
            except Exception: continue
            t=r.get('type');ts=ts or r.get('timestamp')
            if t=='user' and ep is None:
                ep=r.get('entrypoint');c=r.get('message',{}).get('content');prompt=(c if isinstance(c,str) else json.dumps(c))[:60]
            elif t=='attachment':
                x=r.get('attachment',{});k=x.get('type')
                if k in BLOCKS: att[k]=att.get(k,0)+len(json.dumps(x))
            elif t=='assistant':
                u=r.get('message',{}).get('usage') or {};model=r.get('message',{}).get('model')
                tot=u.get('input_tokens',0)+u.get('cache_creation_input_tokens',0)+u.get('cache_read_input_tokens',0)
                if not tot: continue
                return dict(file=p,when=(datetime.datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone() if ts else datetime.datetime.fromtimestamp(os.path.getmtime(p)).astimezone()).strftime('%m-%d %H:%M'),entrypoint=ep,model=model,
                            start=tot,blocks={k:round(v/4) for k,v in att.items()},prompt=prompt.replace('\n',' '))
    except OSError: pass
    return None
files=a.files or sorted(glob.glob(os.path.expanduser(a.dir or '~/.claude/projects')+('/*.jsonl' if a.dir else '/*/*.jsonl')),key=os.path.getmtime,reverse=True)
out=[]
for f in files:
    r=parse(f)
    if not r or (not a.files and not a.all_entrypoints and r['entrypoint']!='cli'): continue
    out.append(r)
    if not a.files and len(out)>=a.last*3: break
out=sorted(out,key=lambda r:r['when'],reverse=True)[:len(out) if a.files else a.last]
if a.json: print(json.dumps(out,indent=1));sys.exit()
short={'skill_listing':'skills','instructions':'CLAUDE.md','mcp_instructions_delta':'mcp','deferred_tools_delta':'deferred','agent_listing_delta':'agents','output_style_instructions':'style','prompt_snapshot':'snapshot'}
print('| When | Entry | Start | '+' | '.join(short[b] for b in BLOCKS)+' | First prompt |');print('|---'*(len(BLOCKS)+4)+'|')
for r in out: print(f"| {r['when']} | {r['entrypoint']} | {r['start']:,} | "+' | '.join(f"{r['blocks'].get(b,0)/1000:.1f}k" for b in BLOCKS)+f" | {r['prompt']} |")
print('\nStart is exact. Block columns are ~chars/4 estimates of logged attachments; the rest (system prompt + tool schemas) is not logged.')
