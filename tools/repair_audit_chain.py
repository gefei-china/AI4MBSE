# -*- coding: utf-8 -*-
"""审计哈希链修复 CLI（2026-11-24 P2，配套 core/audit.py::repair_chain）。

用法（在项目根目录）：
    python tools/repair_audit_chain.py            # dry-run：只诊断，不动库
    python tools/repair_audit_chain.py --apply    # 实际修复（必须在服务停机窗口执行）

背景：见 core/audit.py::repair_chain 文档。实测缺陷：id 15106 处 prev_hash 不衔接
（历史双写者竞争），其后的 ~300 行整段被判 broken ⇒ 「审计链破损」critical 告警
持续触发（hit_count 1100+）且永不闭合。

安全语义（与 repair_chain 一致）：
- 存在「内容与摘要不符」（篡改型）断点 → 拒绝修复，只输出诊断；
- 未入链行（hash 为空，历史降级写入）保持原样；
- 修复后回写一条 audit_chain_repair 审计事件（入新链尾，留痕）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.audit import verify_chain, repair_chain, audit      # noqa: E402
from database import db_conn                                   # noqa: E402


def main():
    apply = "--apply" in sys.argv
    with db_conn() as conn:
        print("== 修复前校验 ==")
        v = verify_chain(conn=conn)
        print({k: v[k] for k in ("total", "chained", "unchained", "broken_count", "ok")})
        for b in (v.get("broken") or [])[:5]:
            print("  broken:", b)

        print("== 修复计划 ==")
        r = repair_chain(conn=conn, dry_run=not apply)
        print({k: r.get(k) for k in ("ok", "broken_at", "would_relink", "action", "relinked")})
        if not r.get("ok"):
            print("  拒绝/失败：", r.get("error"))
            return 1

        if not apply:
            print("== dry-run 结束（未动库）。确认无误后加 --apply 执行 ==")
            return 0

        print("== 已重链，回写修复审计事件 ==")
        audit("系统", "audit_chain_repair",
              f"审计哈希链修复：断点 id={r.get('broken_at')}，重链 {r.get('relinked')} 行"
              f"（tools/repair_audit_chain.py，服务停机窗口执行）",
              conn=conn)

        print("== 修复后校验 ==")
        v2 = verify_chain(conn=conn)
        print({k: v2[k] for k in ("total", "chained", "unchained", "broken_count", "ok")})
        for b in (v2.get("broken") or [])[:5]:
            print("  broken:", b)
        return 0 if v2.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
