"""测试：未与本映射的 SysML XML 能否上传"""
import urllib.request, json, os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
def api(path, body=None, method='POST'):
    req = urllib.request.Request('http://127.0.0.1:8000'+path, method=method)
    if body is not None:
        req.add_header('Content-Type','application/json')
        req.data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(req) as r: return json.loads(r.read().decode())
    except urllib.error.HTTPError as e: return json.loads(e.read().decode() or '{}')

# XML 里含本体【不存在】的类型词：陀螺仪(新部件)、姿态控制(新功能)、自定义关系 links_to
xml = """<model xmlns="http://www.omg.org/spec/SysML/20241001">
  <partDefinition id="p-1" declaredName="陀螺仪"/>
  <partDefinition id="p-2" declaredName="星敏感器"/>
  <requirementUsage id="ru-1" declaredName="姿态精度需求"/>
  <connectionUsage id="c-1" declaredName="c1">
    <connectorEnd id="ce-1" type="#p-1"/><connectorEnd id="ce-2" type="#p-2"/>
  </connectionUsage>
  <satisfactionUsage id="s-1" declaredName="s1">
    <type href="#ru-1"/><target href="#p-1"/>
  </satisfactionUsage>
</model>"""
r = api('/api/knowledge/sysml/import', {'source':'xml', 'content': xml, 'model_name':'未映射类型测试'})
print('导入结果:', json.dumps(r, ensure_ascii=False, indent=1)[:600])

# 清理
import sqlite3
conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mbse.db'))
c = conn.cursor()
c.execute("DELETE FROM relations WHERE source_id LIKE 'S-%' OR target_id LIKE 'S-%'")
c.execute("DELETE FROM entities WHERE id LIKE 'S-%'")
c.execute("DELETE FROM sysml_sync")
c.execute("DELETE FROM sysml_imports WHERE model_name='未映射类型测试'")
conn.commit()
print('\n已清理')
sys.exit(0 if r.get('status') in ('done','partial') else 1)
