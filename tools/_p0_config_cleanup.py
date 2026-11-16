"""P0-4：清理用户 override（`~/.workbuddy/mbse_config.json`）里的镜像项与 legacy 键。

复用 tools/_config_mirror_cleanup.py 的「清理前后逐键等价自证」范式，但有三点不同：

1. **显式 FORCE_DROP**（工程 §1k 翻转陷阱）：`rag.rerank_enabled=true` 与默认值
   `False` 不相等，机械判据会把它当「真实偏离」保留。它是被 `rag.rerank_mode='auto'`
   取代的 legacy 键（`core/config.py:2376`、`services/rag_rerank.py:81-101`），
   留着只制造误判。删除**必须显式列出**，不能靠「值等于默认值」反推。
2. **override-only 键一律保留**：8 个 `agent.*` 键在 DEFAULT_CONFIG 里没有条目，
   override 是它们**唯一生效来源**；按镜像逻辑删掉会让 `get()` 返回 None（真回归）。
3. **行为级自证**：删 legacy 键会让 `get("rag","rerank_enabled")` 从 True→False，
   所以逐键等价自证要允许这一项差异，并额外断言 `_rerank_mode()` 前后都是 'auto'
   —— 证明「生效档位没变」，而不是假装零差异。

默认 dry-run；--apply 才写盘（写盘前再备份一次到 backups/）。
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)
sys.path.insert(0, str(REPO))

import core.config as C  # noqa: E402
from core.config import CONFIG_PATH, DEFAULT_CONFIG as D, _load_file  # noqa: E402

# 显式冻结判据：不依赖「当前默认值」，默认值改动不会让判据失效
FORCE_DROP = {
    "rag.rerank_enabled": "legacy：已被 rag.rerank_mode='auto' 取代（config.py:2376 / rag_rerank.py:81-101）",
}
ALLOWED_DIFF = set(FORCE_DROP)


def _rerank_mode():
    from services.rag_rerank import _rerank_mode
    return _rerank_mode()


def main():
    path = Path(CONFIG_PATH)
    ov = _load_file(CONFIG_PATH)
    before_keys = {(s, k) for s, items in ov.items() if isinstance(items, dict)
                   for k in items}
    out, dropped, forced, kept, override_only = {}, [], [], [], []
    for sec, items in ov.items():
        if not isinstance(items, dict):
            out[sec] = items
            continue
        for k, v in items.items():
            dotted = "%s.%s" % (sec, k)
            d = (D.get(sec) or {}).get(k, "__MISSING__")
            if dotted in FORCE_DROP:
                forced.append("%s = %r（%s）" % (dotted, v, FORCE_DROP[dotted]))
            elif d == "__MISSING__":
                out.setdefault(sec, {})[k] = v       # override-only：唯一生效来源，必须保留
                override_only.append(dotted)
            elif d == v or str(d) == str(v):
                dropped.append("%s = %r" % (dotted, v))
            else:
                out.setdefault(sec, {})[k] = v
                kept.append("%s = %r（默认 %r）" % (dotted, v, d))
    out = {s: v for s, v in out.items() if not (isinstance(v, dict) and not v)}

    print("BEFORE keys=%d  FORCE_DROP=%d  pure_mirror=%d  real_diff=%d  override_only=%d  AFTER=%d"
          % (len(before_keys), len(forced), len(dropped), len(kept),
             len(override_only), sum(len(v) for v in out.values() if isinstance(v, dict))))
    print("[FORCE_DROP]", forced)
    print("[PURE_MIRROR]", dropped)
    print("[KEEP real diff]", kept)
    print("[KEEP override-only]", override_only)

    if "--apply" not in sys.argv:
        print("DRY-RUN（未写盘）；加 --apply 执行")
        return 0

    before_eff = json.loads(json.dumps(C.reload(), ensure_ascii=False,
                                       sort_keys=True, default=str))
    before_mode = _rerank_mode()
    bak = REPO / "backups" / ("mbse_config.json.bak-before-p0-cleanup-" +
                              time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(path, bak)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    after_eff = json.loads(json.dumps(C.reload(), ensure_ascii=False,
                                      sort_keys=True, default=str))
    after_mode = _rerank_mode()

    diff = {}
    for sec in set(before_eff) | set(after_eff):
        sa, sb = before_eff.get(sec) or {}, after_eff.get(sec) or {}
        if not isinstance(sa, dict) or not isinstance(sb, dict):
            if sa != sb:
                diff[sec] = (sa, sb)
            continue
        for k in set(sa) | set(sb):
            if sa.get(k) != sb.get(k):
                diff["%s.%s" % (sec, k)] = (sa.get(k), sb.get(k))
    unexpected = {k: v for k, v in diff.items() if k not in ALLOWED_DIFF}
    print("\n[自证] 生效配置差异 =", diff)
    print("[自证] _rerank_mode: %s -> %s" % (before_mode, after_mode))
    print("[自证] 备份 =", bak.name)
    problems = []
    if unexpected:
        problems.append("出现预期外的生效值差异：%s" % unexpected)
    if set(diff) - ALLOWED_DIFF:
        problems.append("差异集合越界")
    if before_mode != "auto" or after_mode != "auto":
        problems.append("重排生效档位发生变化（必须都是 auto）")
    for k in override_only:
        s, key = k.split(".", 1)
        if after_eff.get(s, {}).get(key) is None:
            problems.append("override-only 键被删：%s" % k)
    if problems:
        print("⛔ 自证失败，请用备份还原：", bak)
        for p in problems:
            print("   -", p)
        return 2
    print("[自证] OK：除 %s 外零差异，且生效档位仍是 auto" % list(ALLOWED_DIFF))
    return 0


if __name__ == "__main__":
    sys.exit(main())
