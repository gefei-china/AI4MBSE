"""灌「计划语义校验（Plan Guard）」的配置规则到 `generate_rules`。

只灌**定义**，不含任何判断逻辑（与 `register_alternatives_tools.py` 同一纪律）。

## 为什么放 `generate_rules` 而不是代码常量

仓库既有范式（`register_sysml_check_tools.py` 文件头）：

> 「策略写在 pipeline 里意味着每次调整都要改代码 + 重启；写在 DB 里则**改文本即生效**」

阶段序、必需阶段、校验档位都是**策略**，不是机制。所以：

- **机制**（怎么比、怎么报）在 `core/plan_guard.py`
- **策略**（比哪些阶段、要不要拦）在本脚本灌进 DB 的那三行里

改阶段序 ⇒ 改 `pipeline_stage_order` 的 JSON；收紧成拦截 ⇒ 改 `pipeline_guard_mode` 为 `block`。
**都不用改代码、不用重启**（每轮编排即时读）。

## 三条规则

| rule_key | 作用 | 默认值 |
|---|---|---|
| `pipeline_stage_order` | 阶段的合法先后（索引越小越早） | 7 个建模阶段 |
| `pipeline_required_stages` | 缺了就一定不算完整建模的阶段 | 需求结构化 + 架构骨架 |
| `pipeline_guard_mode` | `off` / `warn` / `block` | **`warn`** |

`warn` 是刻意的默认：先让违规**可见**（当前状态是完全不可见），跑一段看真实分布
再决定是否收紧到 `block`。`block` 会让整个编排明确失败，不宜贸然开启。

## 用法

    python tools/seed_pipeline_guard_rules.py            # dry-run，只打印将要写入什么
    python tools/seed_pipeline_guard_rules.py --apply    # 真写（幂等：已存在则跳过）

`--force` 可覆盖已存在的值（用于把被改乱的配置拉回默认）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.plan_guard import (  # noqa: E402
    DEFAULT_MODE,
    DEFAULT_REQUIRED_STAGES,
    DEFAULT_STAGE_ORDER,
    RULE_KEY_MODE,
    RULE_KEY_REQUIRED_STAGES,
    RULE_KEY_STAGE_ORDER,
)

#: 待灌规则：(rule_key, rule_value, description)
#:
#: ⚠️ 2026-11-04 修订：**不再灌 `pipeline_stage_order`**。
#:   阶段顺序的权威出处是 `agents.stage_order`（2026-10 已建列并填了 8 个阶段，
#:   含 `change_safety_gate`）。上一版把它灌成 7 项，等于**在表外又造了一份更短的副本** ——
#:   两处不一致时没人知道该信谁。现口径：表是来源，`generate_rules.pipeline_stage_order`
#:   只作**临时覆盖**（想临时改顺序才写它），不写就用表。
RULES = [
    (
        RULE_KEY_REQUIRED_STAGES,
        json.dumps(DEFAULT_REQUIRED_STAGES, ensure_ascii=False),
        "建模流水线的**必需阶段**（JSON 数组）。缺任一 ⇒ 计划校验报"
        "「缺少必需阶段」（对应 Capella DCOM_*）。置空数组可停用该检查。"
        "阶段名单本身以 `agents.stage_order` 为准，本规则只声明「哪些不可省」。",
    ),
    (
        RULE_KEY_MODE,
        DEFAULT_MODE,
        "计划语义校验档位：off=不校验 / warn=只告警不阻断（默认）/ block=有 error 级违规即中止编排。"
        "建议先 warn 观察真实分布，再考虑收紧。",
    ),
]

#: 上一版（2026-11-04 早）误灌的 7 项阶段序 —— 若库里还是这个**逐字节相同**的值，
#: 说明是我们自己灌的、不是管理员手改的，可以安全清掉（幂等）。
_STALE_STAGE_ORDER = json.dumps(
    ["methodology_resolver", "requirement_structuring", "architecture_skeleton",
     "view_expansion", "model_validation_repair", "trace_verification", "model_release"],
    ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="灌计划语义校验规则（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真写数据库（默认只打印）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的规则值")
    args = ap.parse_args()

    from database import get_db

    conn = get_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(generate_rules)")}
        if not {"rule_key", "rule_value", "description"} <= cols:
            print("[seed] ✗ generate_rules 表结构不符，缺少 rule_key/rule_value/description")
            return 1

        existing = {r[0]: r[1] for r in conn.execute(
            "SELECT rule_key, rule_value FROM generate_rules")}

        # ── 一次性清理：上一版误灌的 7 项阶段序 ──
        # 只删**与本脚本当初写入值逐字节相同**的行（证明不是管理员手改的）。
        # 手改过的值一律保留 —— 那是人的决定，不能被脚本悄悄抹掉。
        stale = existing.get(RULE_KEY_STAGE_ORDER)
        if stale is not None and stale.strip() == _STALE_STAGE_ORDER.strip():
            print("[seed] 发现上一版误灌的 pipeline_stage_order（7 项），%s"
                  % ("将删除" if args.apply else "dry-run 未删除"))
            if args.apply:
                conn.execute("DELETE FROM generate_rules WHERE rule_key=?", (RULE_KEY_STAGE_ORDER,))
                conn.commit()
                existing.pop(RULE_KEY_STAGE_ORDER, None)
        elif stale is not None:
            print("[seed] 保留 pipeline_stage_order（值与本脚本旧版不同 ⇒ 视为人工覆盖）：%s"
                  % str(stale)[:80])

        to_insert, to_update, skipped = [], [], []
        for key, val, desc in RULES:
            if key not in existing:
                to_insert.append((key, val, desc))
            elif str(existing[key] or "") == val:
                skipped.append((key, "已一致"))
            elif args.force:
                to_update.append((key, val, desc))
            else:
                skipped.append((key, "已存在且不同（加 --force 覆盖）｜现值=%s" % str(existing[key])[:60]))

        print("[seed] 计划语义校验规则")
        print("       待插入 %d / 待更新 %d / 跳过 %d" % (len(to_insert), len(to_update), len(skipped)))
        for k, v, _ in to_insert:
            print("  + %s = %s" % (k, v[:100]))
        for k, v, _ in to_update:
            print("  ~ %s = %s" % (k, v[:100]))
        for k, why in skipped:
            print("  = %s（%s）" % (k, why))

        if not args.apply:
            print("\n[seed] dry-run：未写入。确认无误后加 --apply")
            return 0

        for k, v, d in to_insert:
            conn.execute(
                "INSERT INTO generate_rules (rule_key, rule_value, description, updated_at) "
                "VALUES (?,?,?,CURRENT_TIMESTAMP)", (k, v, d))
        for k, v, d in to_update:
            conn.execute(
                "UPDATE generate_rules SET rule_value=?, description=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE rule_key=?", (v, d, k))
        conn.commit()

        # 回读确认（不信任"我以为写进去了"）
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT rule_key, rule_value FROM generate_rules WHERE rule_key IN (?,?,?)",
            (RULE_KEY_STAGE_ORDER, RULE_KEY_REQUIRED_STAGES, RULE_KEY_MODE))}
        print("\n[seed] 回读确认：")
        for k, v, _ in RULES:
            print("  %-28s %s" % (k, (rows.get(k) or "**缺失**")[:90]))

        # 生效校验：用真实加载器读一遍，确认它认这三行
        from core.plan_guard import load_pipeline_config
        cfg = load_pipeline_config(conn)
        print("\n[seed] 加载器读到：source=%s mode=%s 阶段数=%d 必需阶段=%s"
              % (cfg.get("source"), cfg.get("mode"),
                 len(cfg.get("stage_order") or []), cfg.get("required_stages")))
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
