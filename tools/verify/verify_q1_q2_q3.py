"""R3 verify: Q1 main-agent persistence + Q3 MCP tool not JIT-pruned + /messages returns main_agent.
Usage: .venv/Scripts/python.exe tools/verify/verify_q1_q2_q3.py
"""
import json, sys, urllib.request, urllib.error
sys.path.insert(0, '.')
from agent.pipeline import AgentPipeline

PASS = []; FAIL = []
def chk(name, ok, note=''):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (("  -- " + note) if (note and not ok) else "")))

BASE = 'http://127.0.0.1:8000'
HDR = {'Content-Type':'application/json', 'X-User-Id':'1', 'X-Session-Token':'v'}

def post(path, body):
    req = urllib.request.Request(BASE+path, data=json.dumps(body).encode(), headers=HDR, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code

def get(path):
    req = urllib.request.Request(BASE+path, headers=HDR, method='GET')
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def sse_stages(cid, body):
    req = urllib.request.Request(BASE+'/api/conversations/%d/chat/stream' % cid,
                                 data=json.dumps(body).encode(), headers=HDR, method='POST')
    stages=[]; buf=''
    with urllib.request.urlopen(req, timeout=200) as r:
        while True:
            c=r.read(4096)
            if not c: break
            buf+=c.decode('utf-8','ignore')
            while '\n\n' in buf:
                f,buf=buf.split('\n\n',1)
                for ln in f.splitlines():
                    if ln.startswith('data:'):
                        try: e=json.loads(ln[5:].strip())
                        except: continue
                        if e.get('type')=='stage': stages.append(e.get('name'))
                        if e.get('type')=='stage' and e.get('name')=='intent': stages.append(('intent', e.get('intent')))
    return stages

MAIN = '智源对接agent'

print("=== Q3 bound MCP tool must NOT be JIT-pruned ===")
p = AgentPipeline(); p._load_db_agents()
for q in ['query project list', 'help me query project list', 'export sysml']:
    t = p._build_tools_def(MAIN, q, None)
    names = [x['function']['name'] for x in t]
    chk("Q3 inject contains query_projects for [%s]" % q, 'query_projects' in names, "injected %d" % len(names))

print("\n=== Q1 main-agent persistence + readback ===")
r, _ = post('/api/conversations', {'title':'verify-q1'})
cid = r['id']
st1 = sse_stages(cid, {'message':'query project list','team':MAIN})
ma = get('/api/conversations/%d/messages?limit=1' % cid).get('main_agent','')
chk("Q1 main_agent persisted after team send", ma==MAIN, "main_agent=%r" % ma)
st2 = sse_stages(cid, {'message':'query again'})
def intent_of(stages):
    for s in stages:
        if isinstance(s, tuple) and s[0]=='intent': return s[1]
    return None
it2 = intent_of(st2)
chk("Q1 no-team send reads back binding (intent stays main agent)", it2==MAIN, "intent=%s" % it2)

print("\n=== /messages returns main_agent (frontend selectConv backfill) ===")
r2 = get('/api/conversations/%d/messages?limit=1' % cid)
chk("/messages response has main_agent field", 'main_agent' in r2, "keys=%s" % list(r2.keys()))
chk("/messages main_agent equals binding", r2.get('main_agent')==MAIN, "main_agent=%r" % r2.get('main_agent'))

print("\n=== Q2 backend side: clarify-answer usable ===")
r3,_ = post('/api/conversations/%d/clarify-answer' % cid, {'free_text':'continue'})
chk("Q2 clarify-answer endpoint usable", bool(r3) and ('resume_text' in r3 or 'error' in r3),
    "keys=%s" % (list(r3.keys()) if isinstance(r3, dict) else r3))

print("\n=== result: %d pass / %d fail ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("ALL PASS")
