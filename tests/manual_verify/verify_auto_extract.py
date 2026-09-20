"""端到端验证：①上传自动抽取（内嵌）②补抽已有文档 ③候选→确认→图谱"""
import urllib.request, urllib.parse, json, io, uuid, sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

BASE = 'http://127.0.0.1:8000'

def api(path, body=None, method='GET'):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return json.loads(raw)
        except: return {"error": raw[:300]}

def upload_file(filename, content):
    boundary = '----wb' + uuid.uuid4().hex
    body = b''
    body += ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: text/plain\r\n\r\n' % (boundary, filename)).encode()
    body += content.encode('utf-8')
    body += ('\r\n--%s--\r\n' % boundary).encode()
    req = urllib.request.Request(BASE + '/api/documents/upload', data=body, method='POST')
    req.add_header('Content-Type', 'multipart/form-data; boundary=' + boundary)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())

def main():
    total = passed = 0
    def chk(name, cond, extra=''):
        nonlocal total, passed
        total += 1
        if cond: passed += 1
        print(('PASS' if cond else 'FAIL') + ' | ' + name + (' | ' + extra if extra else ''))

    # ── ① 上传自动抽取（内嵌）──
    doc = upload_file('auto_extract_demo.md', '''# 宽带通信卫星
系统由载荷、转发器、天线构成，载荷包含转发器。
载荷使用V频段，吞吐2Gbps，转发器包含TWTA。
载荷满足吞吐需求，天线满足覆盖需求。''')
    ae = doc.get('auto_extract', {})
    print('① 上传 doc', doc.get('id'), '| pipeline 自动抽取:', json.dumps(ae, ensure_ascii=False)[:260])
    chk('上传内嵌自动抽取触发', ae.get('extracted', 0) > 0 or 'error' in ae, f"extracted={ae.get('extracted')}")
    doc_id = doc.get('id')
    # 检查 pipeline_detail 有 extraction
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'mbse.db'))
    pd = conn.execute("SELECT pipeline_detail FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    chk('pipeline_detail 含 extraction 阶段', 'extraction' in pd, pd)

    # ── ② 补抽已有文档（doc 97）──
    back = api('/api/documents/97/extract', None, 'POST')
    print('② 补抽 doc97:', json.dumps(back, ensure_ascii=False)[:200])
    chk('已有文档补抽', back.get('candidates', 0) > 0, f"candidates={back.get('candidates')}")

    # ── ③ 候选 → 确认 → 图谱 ──
    cands = api('/api/knowledge/v2g/candidates?limit=50')
    pending = [c for c in cands if c.get('status') == 'pending' and c.get('entity_type') != '关系候选'][:5]
    chk('候选池有待审候选', len(pending) > 0, f"pending={len(pending)}")
    if pending:
        conf = api('/api/knowledge/v2g/confirm', {'selected_ids': [c['id'] for c in pending]}, 'POST')
        chk('确认入库', conf.get('confirmed', 0) > 0, f"confirmed={conf.get('confirmed')}")
    g = api('/api/knowledge/graph')
    chk('图谱实例增加', len(g.get('entities', [])) > 7, f"entities={len(g.get('entities', []))}")

    print(f'\n结果: {passed}/{total} PASS')
    sys.exit(0 if passed == total else 1)

main()
