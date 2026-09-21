# -*- coding: utf-8 -*-
"""清掉系统级配置覆盖文件里的「纯镜像项」（值 == 出厂默认值 → 不含信息）。

为什么必须清：`PUT /api/system/config/static` 由配置页提交，而页面前端会把**整张表单**
（所有非空字段）发上去 → `save_override` 忠实地把当前值全量写盘。
后果：`core/config.py` 的 DEFAULT_CONFIG **任何改动都不会生效，且看不出原因**
（本会话已因此踩过两次）。判据：某项的值若与默认值完全相同，删掉它**不改变任何生效值**，
只是把「默认值语义」还给默认值来源。

保留：① 值 ≠ 默认值的真实偏离；② 文件里既有但 DEFAULT_CONFIG/CONFIG_SCHEMA 都不认识的键
（`save_override` 的设计声明是「保留文件内既有未知键，不覆盖」）。
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]   # 本脚本直接位于 tools/ 下（不在 tools/xxx/ 子目录）
os.chdir(REPO)
sys.path.insert(0, str(REPO))

from core.config import CONFIG_PATH, DEFAULT_CONFIG as D, _load_file  # noqa: E402

path = Path(CONFIG_PATH)
before_bytes = path.read_bytes()
ov = _load_file(CONFIG_PATH)

# 显式删除：其值等于「改动前的出厂默认」的镜像项。
# 为什么需要单独列：本轮刚把 rag.route_threshold 的默认值 0.75 → 0.70（标定结论，见
# core/config.py 注释）。改完后 override 里的 0.75 在机械比较下"变成"了真实偏离而被保留 ——
# 但它本来就是旧默认值的镜像（没人手工选过 0.75），保留它只会让标定结论失效。
# 故此处显式列出，并在自证中**只允许这一处**生效值差异。
FORCE_DROP = {
    "rag.route_threshold": "0.75 = 改动前的出厂默认（本轮标定为 0.70）",
}

dropped, kept, unknown, forced = [], [], [], []
out = {}
for sec, items in ov.items():
    if not isinstance(items, dict):
        out[sec] = items
        continue
    for k, v in items.items():
        dotted = "%s.%s" % (sec, k)
        d = (D.get(sec) or {}).get(k, "__MISSING__")
        if dotted in FORCE_DROP:
            forced.append("%s = %s（%s）" % (dotted, v, FORCE_DROP[dotted]))
        elif d == "__MISSING__":
            out.setdefault(sec, {})[k] = v          # 未知键：按设计保留
            unknown.append(dotted)
        elif d == v or str(d) == str(v):
            dropped.append("%s.%s = %s" % (sec, k, v))   # 纯镜像：删
        else:
            out.setdefault(sec, {})[k] = v
            kept.append("%s.%s = %s（默认 %s）" % (sec, k, v, d))

# 空分组不留壳
out = {s: v for s, v in out.items() if not (isinstance(v, dict) and not v)}

print("删除前字节数：%d" % len(before_bytes))
print("\n将删除（纯镜像，%d 项）：" % len(dropped))
for x in dropped:
    print("   - " + x)
if forced:
    print("\n显式删除（旧默认值的镜像，%d 项）：" % len(forced))
    for x in forced:
        print("   ! " + x)
print("\n保留（真实偏离，%d 项）：" % len(kept))
for x in kept:
    print("   ✓ " + x)
print("\n保留（未知键，%d 项）：%s" % (len(unknown), unknown))

if "--apply" not in sys.argv:
    print("\n（预演模式，未写盘。加 --apply 执行）")
    sys.exit(0)

# ── 取「清理前」生效配置：必须在写盘之前读（否则两次读同一份 = 空转自证）──
import core.config as C                                                      # noqa: E402
before_eff = json.dumps(C.reload(), ensure_ascii=False, sort_keys=True, default=str)

bak = path.with_name(path.name + ".bak-before-mirror-cleanup-" + time.strftime("%Y%m%d_%H%M%S"))
shutil.copy2(path, bak)
path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n备份：%s" % bak.name)
print("已写：%s" % path)

# ── 自证：除显式项外，清理前后「生效配置」必须逐键等价 ────────────────────
after_eff = json.dumps(C.reload(), ensure_ascii=False, sort_keys=True, default=str)
a = json.loads(before_eff)
b = json.loads(after_eff)
diff = {}
for sec in set(a) | set(b):
    sa, sb = a.get(sec) or {}, b.get(sec) or {}
    if not isinstance(sa, dict) or not isinstance(sb, dict):
        if sa != sb:
            diff[sec] = (sa, sb)
        continue
    for k in set(sa) | set(sb):
        if sa.get(k) != sb.get(k):
            diff["%s.%s" % (sec, k)] = (sa.get(k), sb.get(k))

print("\n[自证] 清理前后生效配置差异：%s" % (diff if diff else "无差异"))
allowed = set(FORCE_DROP)
unexpected = {k: v for k, v in diff.items() if k not in allowed}
if unexpected:
    print("⛔ 出现预期外差异 —— 说明删的不是纯镜像，请用备份还原：" + bak.name)
    for k, v in unexpected.items():
        print("     %s: %s → %s" % (k, v[0], v[1]))
    sys.exit(1)
for k in sorted(allowed):
    if k in diff:
        print("   ✓ 显式删除项已生效：%s: %s → %s" % (k, diff[k][0], diff[k][1]))
    else:
        print("   · 显式删除项 %s 本就等于新默认值，无差异" % k)
print("   → 除上述显式项外，生效配置逐键等价（删掉的确都是不含信息的镜像）")
