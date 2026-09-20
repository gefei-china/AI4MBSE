"""E2E: SysML XML 导入（SysML2 + XMI 两种格式）"""
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

sysml2 = """<model xmlns="http://www.omg.org/spec/SysML/20241001">
  <partDefinition id="p-1" declaredName="宽带载荷"/>
  <partUsage id="pu-1" declaredName="载荷A" type="#p-1"/>
  <partUsage id="pu-2" declaredName="转发器" type="#p-1"/>
  <requirementUsage id="ru-1" declaredName="吞吐量需求" type="#r-1"><reqText>吞吐量大于2Gbps</reqText></requirementUsage>
  <connectionUsage id="c-1" declaredName="载荷到转发器">
    <connectorEnd id="ce-1" type="#pu-1"/><connectorEnd id="ce-2" type="#pu-2"/>
  </connectionUsage>
</model>"""
r = api('/api/knowledge/sysml/import', {'source':'xml', 'content': sysml2, 'model_name':'XML测试模型'})
print('1) SysML2 XML 导入:', json.dumps(r, ensure_ascii=False)[:280])

xmi = """<?xml version="1.0"?>
<xmi:XMI xmlns:xmi="http://www.omg.org/XMI" xmlns:uml="http://www.omg.org/spec/UML/20131001">
  <uml:Model xmi:id="m1" name="XMI模型">
    <packagedElement xmi:type="uml:Class" xmi:id="c1" name="卫星"/>
    <packagedElement xmi:type="uml:Class" xmi:id="c2" name="载荷"/>
    <packagedElement xmi:type="uml:Association" xmi:id="a1" name="包含">
      <memberEnd xmi:idref="e1"/><memberEnd xmi:idref="e2"/>
    </packagedElement>
    <packagedElement xmi:type="uml:Property" xmi:id="e1" name="src" type="c1"/>
    <packagedElement xmi:type="uml:Property" xmi:id="e2" name="tgt" type="c2"/>
  </uml:Model>
</xmi:XMI>"""
r2 = api('/api/knowledge/sysml/import', {'source':'xml', 'content': xmi, 'model_name':'XMI测试模型'})
print('2) SysML1 XMI 导入:', json.dumps(r2, ensure_ascii=False)[:280])

g = api('/api/knowledge/graph', method='GET')
print('3) 图谱实例:', len(g.get('entities',[])), 'entities /', len(g.get('relations',[])), 'relations')

import sqlite3
conn = sqlite3.connect('mbse.db'); c = conn.cursor()
c.execute("DELETE FROM relations WHERE source_id LIKE 'S-%' OR target_id LIKE 'S-%'")
c.execute("DELETE FROM entities WHERE id LIKE 'S-%'")
c.execute("DELETE FROM sysml_sync")
c.execute("DELETE FROM sysml_imports WHERE model_name LIKE '%测试模型%'")
conn.commit()
print('4) 已清理: entities', c.execute("SELECT COUNT(*) FROM entities").fetchone()[0], '/ relations', c.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
sys.exit(0 if r.get('entity_count',0)>=3 and r2.get('entity_count',0)>=2 else 1)
