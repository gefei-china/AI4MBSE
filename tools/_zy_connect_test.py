# -*- coding: utf-8 -*-
"""写入完整 token → project_list → tree → gen 实测"""
import json, os, sys

TOKEN = ("eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VySW5mbyI6IntcImlkXCI6XCIyMDM2NjMwOTU4NjEzNzg2NjI0XCI"
         "sXCJ1c2VybmFtZVwiOlwiZ2VmZWlcIixcImRpc3BsYXlOYW1lXCI6XCJnZWZlaVwiLFwidXNlcklkZW50aXR5XCI6MCxcImlz"
         "QWN0aXZlXCI6dHJ1ZSxcIm5lZWRVcGRhdGVQYXNzd29yZFwiOmZhbHNlLFwicGVyc29ubmVsU2VjcmV0S2V5XCI6XCJJTlRF"
         "Uk5BTF9DT05UUk9MTEFCTEVcIn0iLCJzdWIiOiJnZWZlaSIsImV4cCI6MTc4OTIxMDE5NCwidXNlcklkIjoyMDM2NjMwOTU4"
         "NjEzNzg2NjI0LCJpYXQiOjE3ODg4NTAxOTQsImp0aSI6ImEwNzgzOTc2MGVmODQwN2RhODU3NmFlMjU3MTVlZDU2In0."
         "o8t3Hz2WhFiU-0dr7dmdRMMAUShKTaR8dU3576U3MX0")

p = os.path.expanduser('~/.workbuddy/mbse_config.json')
cfg = json.load(open(p, encoding='utf-8'))
cfg['zhiyuan']['token'] = TOKEN
print('token 段数:', TOKEN.count('.')+1, '长度:', len(TOKEN))
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('1) 配置已更新')

sys.path.insert(0, '.')
from zhiyuan_client import ZhiyuanClient
client = ZhiyuanClient(base_url='http://192.168.100.69:8012', token=TOKEN, timeout=30)

print('\n2) project_list:')
pl = client.project_list('')
print(json.dumps(pl, ensure_ascii=False)[:800])

sys.path.insert(0, '.')
from services.knowledge_service import _zhiyuan_first_vc
vc = _zhiyuan_first_vc(pl)
print('\n3) 解析 vc:', vc)

print('\n4) project_tree:')
try:
    tr = client.project_tree(vc)
    print(json.dumps(tr, ensure_ascii=False)[:800])
except Exception as e:
    print('  失败:', str(e)[:200])

print('\n5) sysmlv2_gen（全工程）:')
try:
    gen = client.sysmlv2_gen(vc)
    from services.knowledge_service import _zhiyuan_extract_text
    text = _zhiyuan_extract_text(gen)
    print('  提取文本长度:', len(text or ''))
    print('  预览:', (text or '')[:300].replace('\n', '\\n'))
    open('tools/_zy_gen_output.sysml', 'w', encoding='utf-8').write(text or '')
    print('  全文已存 tools/_zy_gen_output.sysml')
except Exception as e:
    print('  失败:', str(e)[:300])
