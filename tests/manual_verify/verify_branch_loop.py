"""验证分支闭环：创建分支 → 分支上建模 → 合并请求(冲突检测) → 审批 → 真正合并数据"""
import urllib.request, json, os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
def api(path, body=None, method='GET'):
    req = urllib.request.Request('http://127.0.0.1:8000'+path, method=method)
    if body is not None:
        req.add_header('Content-Type','application/json')
        req.data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return json.loads(raw)
        except: return {"error": raw[:200]}

total = passed = 0
def chk(name, cond, extra=''):
    global total, passed
    total += 1
    if cond: passed += 1
    print(('PASS' if cond else 'FAIL') + ' | ' + name + (' | ' + extra if extra else ''))

# 1) 创建功能分支
r = api('/api/branches', {'name':'dev/闭环测试', 'branch_type':'dev', 'parent_branch':'dev/main', 'description':'闭环验证'}, 'POST')
chk('创建分支 dev/闭环测试', r.get('ok') is True)

# 2) 在该分支上创建实体（带 branch）
r = api('/api/knowledge/graph/nodes', {'name':'闭环测试节点', 'entity_type':'部件', 'branch':'dev/闭环测试', 'x':10, 'y':10}, 'POST')
print('  创建实体响应:', json.dumps(r, ensure_ascii=False)[:120])
chk('分支上创建实体', r.get('id') or r.get('ok'))
ent_id = r.get('id') or (r.get('node',{}) or {}).get('id')

# 3) 分支元素数体现
bs = api('/api/branches')
count = next((b['entity_count'] for b in bs if b['name']=='dev/闭环测试'), None)
chk('分支列表显示元素数', count and count > 0, f"count={count}")

# 4) 创建合并请求 dev/闭环测试 → dev/main（冲突检测）
r = api('/api/branches/merge-requests', {'source_branch':'dev/闭环测试', 'target_branch':'dev/main'}, 'POST')
print('  合并请求:', json.dumps(r, ensure_ascii=False)[:120])
chk('创建合并请求', r.get('ok') is True)
mr = api('/api/branches/merge-requests')
mr_id = mr[0]['id'] if mr else None

# 5) 审批通过 → 真正合并
r = api(f'/api/branches/merge-requests/{mr_id}/resolve', {'action':'approve'}, 'POST')
chk('审批通过', r.get('ok') is True)
mrs = api('/api/branches/merge-requests')
m0 = mrs[0]
detail = json.loads(m0.get('merge_detail') or '{}')
print('  合并详情:', json.dumps(detail, ensure_ascii=False))
chk('合并执行（merge_detail 有数据）', m0.get('status')=='approved' and detail.get('moved',0) > 0,
    f"moved={detail.get('moved')} updated={detail.get('updated')}")

# 6) 目标分支元素数增加
bs2 = api('/api/branches')
count_tgt = next((b['entity_count'] for b in bs2 if b['name']=='dev/main'), None)
chk('目标分支元素数增加', count_tgt and count_tgt > 0)

# 7) 合并后目标分支可查到该实体
g = api('/api/knowledge/graph?branch=' + 'dev/main')
found = any(e.get('name')=='闭环测试节点' for e in g.get('entities',[]))
chk('合并后目标分支图谱含新节点', found)

# 清理
import sqlite3
conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mbse.db'))
c = conn.cursor()
c.execute("DELETE FROM entities WHERE name='闭环测试节点'")
c.execute("DELETE FROM merge_requests WHERE source_branch='dev/闭环测试'")
c.execute("DELETE FROM branches WHERE name='dev/闭环测试'")
conn.commit()
print('\n已清理测试数据')

print(f'\n结果: {passed}/{total} PASS')
sys.exit(0 if passed == total else 1)
