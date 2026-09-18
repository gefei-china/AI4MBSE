"""验证：NER/RE 标准修复——实体原子词 + 关系三元组关联"""
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

total = passed = 0
def chk(name, cond, extra=''):
    global total, passed
    total += 1
    if cond: passed += 1
    print(('PASS' if cond else 'FAIL') + ' | ' + name + (' | ' + extra if extra else ''))

# 上传含明确三元组模式的文档
doc = upload_file('ner_re_demo.md', '''卫星系统由载荷与转发器构成，载荷包含转发器，转发器包含TWTA。
载荷满足吞吐需求，天线满足覆盖需求。''')
ae = doc.get('auto_extract', {})
print('① auto_extract: candidates =', json.dumps(ae.get('candidates', []), ensure_ascii=False)[:400])

cands = ae.get('candidates', [])
ent_names = [c['name'] for c in cands]
# 断言1：实体名为原子词（无换行/无超长片段）
atomic = all(c['name'] == c['entity_type'] for c in cands if c['entity_type'] != '关系候选')
chk('实体名为原子词（类型词本身）', atomic, json.dumps(ent_names, ensure_ascii=False))

# 断言2：关系候选关联两端对象
rels = [c for c in cands if c['entity_type'] == '关系候选']
print('② 关系候选:', json.dumps([{'rel_type': c.get('rel_type'), 'src': c.get('rel_source'), 'tgt': c.get('rel_target')} for c in rels], ensure_ascii=False))
chk('关系候选关联两端实体（rel_source/rel_target）', all(c.get('rel_source') and c.get('rel_target') for c in rels), f"关系候选数={len(rels)}")

# 断言3：确认入库后关系边真实建边
batch = ae.get('batch_id')
cand_list = api('/api/knowledge/v2g/candidates?batch_id=' + urllib.parse.quote(batch))
ents_pending = [c for c in cand_list if c['entity_type'] != '关系候选' and c['status'] == 'pending']
rels_pending = [c for c in cand_list if c['entity_type'] == '关系候选' and c['status'] == 'pending']
conf = api('/api/knowledge/v2g/confirm', {'selected_ids': [c['id'] for c in ents_pending + rels_pending]}, 'POST')
print('③ 确认入库:', json.dumps({'confirmed': conf.get('confirmed'), 'nodes': len(conf.get('nodes', [])), 'edges': conf.get('edges', [])}, ensure_ascii=False)[:400])
chk('确认入库同时建实体+关系边', conf.get('confirmed', 0) > 0 and len(conf.get('edges', [])) > 0,
    f"confirmed={conf.get('confirmed')} edges={len(conf.get('edges', []))}")

# 断言4：图谱有 V2G 实体之间的边
g = api('/api/knowledge/graph')
v2g_ents = [e['id'] for e in g.get('entities', []) if e.get('id', '').startswith('V2G-')]
v2g_edges = [r for r in g.get('relations', []) if r.get('source_id', '').startswith('V2G-') and r.get('target_id', '').startswith('V2G-')]
chk('图谱 V2G 实体间存在真实关系边', len(v2g_edges) > 0, f"V2G实体={len(v2g_ents)} 边={len(v2g_edges)}")

print(f'\n结果: {passed}/{total} PASS')
sys.exit(0 if passed == total else 1)
