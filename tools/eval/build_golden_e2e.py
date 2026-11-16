"""P0-1：从真实业务语料派生「端到端 Golden Set v0」数据集。

口径（与 tools/eval/build_evalset.py 同源）：标注来自真库派生，不是人工臆造；
固定 case 清单 + 固定 conv_id ⇒ 可重复、可审计、可 diff。
默认 dry-run 只打印；--apply 才写 docs/评测/datasets/golden_e2e_v0.json。

⚠️ 关键设计：不把几千字的原始用户输入塞进数据集。
   用例只存 input_preview（前 240 字）+ input_source（conv_id / message_id）+ 长度指纹；
   live 重放时由 run_arch_ab.py 从真库按 message_id 取**完整原文**回放。
   offline 回放则完全不需要输入文本（判据作用在已落库的交付物上）。

用法（工程 venv）：
  ./.venv/Scripts/python.exe -X utf8 tools/eval/build_golden_e2e.py
  ./.venv/Scripts/python.exe -X utf8 tools/eval/build_golden_e2e.py --apply
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
OUT = os.path.join(ROOT, "docs", "评测", "datasets", "golden_e2e_v0.json")
DB = os.path.join(ROOT, "mbse.db")

SCHEMA = "p0/golden-e2e@1"
VERSION = "v0"

# ── 用例清单：chain 三条链路必须都覆盖；规则是 MBSE 平台硬约束，不照搬文章 ──
# checks 的 type 必须与 golden_e2e.py::CHECKERS 的键一致（verify_arch_ab.py 会钉死这点）
CASES = [
    # ① 单 Agent 直答（视图生成，已由 Skill 承载）—— 4 例
    dict(id="G01", conv_id=990712, chain="single_agent", intent="design",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["只输出指定视图的 SysML v2 代码", "标识符不得含中文", "不得伪造校验通过"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "sysml_code_nonempty"},
                 {"type": "assistant_answer_nonempty"}],
         note="活动视图；含异常分支约束"),
    dict(id="G02", conv_id=990713, chain="single_agent", intent="design",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["端口与连接器必须显式定义", "不得伪造校验通过"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "sysml_code_nonempty"},
                 {"type": "assistant_answer_nonempty"}],
         note="IBD 内部块图"),
    dict(id="G03", conv_id=990717, chain="single_agent", intent="design",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["用例必须有 subject 且置于最前", "不得伪造校验通过"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "sysml_code_nonempty"},
                 {"type": "assistant_answer_nonempty"}],
         note="用例视图"),
    dict(id="G04", conv_id=990719, chain="single_agent", intent="design",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["结构视图必须含 part def", "不得伪造校验通过"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "sysml_code_nonempty"},
                 {"type": "check_verdict_in", "values": ["pass", "report"]},
                 {"type": "assistant_answer_nonempty"}],
         note="结构视图 BDD；带本地校验器结论"),

    # ② 编排链路 —— 4 例（其中 1 例是「部分完成」的历史真实形态，用作回归敏感例）
    dict(id="G05", conv_id=514, chain="orchestrated", intent="design",
         expected_deliverable_kind=["sysml", "code", "document"],
         inviolable_rules=["子任务不得看到父会话历史（fresh context）",
                           "未完成子任务必须进入聚合状态，不得报 full"],
         checks=[{"type": "orchestration_done_rate_min", "min": 0.8},
                 {"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "assistant_answer_nonempty"}],
         note="18 个任务的需求建模批次"),
    dict(id="G06", conv_id=9360, chain="orchestrated", intent="requirement_analysis",
         expected_deliverable_kind=["sysml", "code", "document"],
         inviolable_rules=["上游交付物必须显式回灌下游子任务", "不得报 full 而无交付物"],
         checks=[{"type": "orchestration_done_rate_min", "min": 0.8},
                 {"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "assistant_answer_nonempty"}],
         note="8 个任务的需求视图生成批次"),
    dict(id="G07", conv_id=9565, chain="orchestrated", intent="knowledge_qa",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["不得虚构生命周期阶段之外的实体", "交付物必须自包含"],
         checks=[{"type": "orchestration_done_rate_min", "min": 0.8},
                 {"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "assistant_answer_nonempty"}],
         note="生命周期上下文建模批次"),
    dict(id="G08", conv_id=9571, chain="orchestrated", intent="report_generation",
         expected_deliverable_kind=["sysml", "code", "report"],
         inviolable_rules=["存在失败子任务时聚合状态不得为 full", "不得把门禁失败报成内容合格"],
         checks=[{"type": "orchestration_done_rate_min", "min": 0.5},
                 {"type": "artifact_kind_present", "kind": "report"},
                 {"type": "assistant_answer_nonempty"}],
         note="含 failed 子任务的历史批次；回归敏感例"),

    # ③ 建模 + 校验闭环 —— 4 例
    dict(id="G09", conv_id=9610, chain="model_and_validate", intent="view_expansion",
         expected_deliverable_kind=["sysml"],
         inviolable_rules=["必须真实调用 sysml_v2_validate", "不得跳过校验直接交付"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "check_verdict_in", "values": ["pass", "report"]},
                 {"type": "assistant_answer_nonempty"}],
         note="明确要求调用校验工具"),
    dict(id="G10", conv_id=9586, chain="model_and_validate", intent="view_expansion",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["诊断必须按词法/语法/语义三路分类", "不得臆造 v2 关键字"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "sysml_code_nonempty"},
                 {"type": "assistant_answer_nonempty"}],
         note="校验现有 V2 代码"),
    dict(id="G11", conv_id=9999, chain="model_and_validate", intent="chat",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["修复后必须复核", "不得把解析失败报成质量不合格"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "assistant_answer_nonempty"}],
         note="分析代码无法通过校验的原因"),
    dict(id="G12", conv_id=9004, chain="model_and_validate", intent="requirement_analysis",
         expected_deliverable_kind=["sysml", "code"],
         inviolable_rules=["需求条目必须有稳定 ID", "不得在编排交付物里塞父会话历史"],
         checks=[{"type": "artifact_kind_present", "kind": "sysml"},
                 {"type": "sysml_code_nonempty"},
                 {"type": "assistant_answer_nonempty"}],
         note="整车需求导入 + 4 子任务"),
]


def _conn():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def _one(c, sql, args=()):
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else {}


def derive(c):
    out = []
    for spec in CASES:
        cid = spec["conv_id"]
        u = _one(c, "SELECT id, content FROM messages WHERE conversation_id=? "
                    "AND role='user' ORDER BY id LIMIT 1", (cid,))
        raw = str(u.get("content") or "")
        kinds = [r[0] for r in c.execute(
            "SELECT DISTINCT kind FROM artifacts WHERE conversation_id=?", (cid,))]
        n_tasks = c.execute("SELECT COUNT(*) FROM agent_tasks WHERE conversation_id=?", (cid,)).fetchone()[0]
        n_done = c.execute("SELECT COUNT(*) FROM agent_tasks WHERE conversation_id=? AND status='done'", (cid,)).fetchone()[0]
        n_ver = c.execute("SELECT COUNT(*) FROM sysml_versions WHERE conversation_id=?", (cid,)).fetchone()[0]
        asst = c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant' "
                         "AND LENGTH(COALESCE(content,''))>0", (cid,)).fetchone()[0]
        verdicts = []
        for r in c.execute("SELECT element_summary FROM sysml_versions WHERE conversation_id=?", (cid,)):
            try:
                v = (json.loads(r[0] or "{}").get("check") or {}).get("verdict")
                if v:
                    verdicts.append(v)
            except Exception:
                pass
        item = dict(spec)
        item["input_source"] = {"table": "messages", "message_id": int(u.get("id") or 0),
                                "conversation_id": cid, "note": "live 重放按 message_id 取完整原文"}
        item["input_preview"] = raw[:240].replace("\r", " ")
        item["input_len"] = len(raw)
        item["input_sha1"] = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        item["evidence_at_derivation"] = {
            "artifact_kinds": sorted(kinds), "n_artifacts_kinds": len(kinds),
            "agent_tasks": n_tasks, "agent_tasks_done": n_done,
            "sysml_versions": n_ver, "assistant_nonempty": asst,
            "check_verdicts": sorted(set(verdicts)),
        }
        out.append(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写盘（默认只打印）")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    c = _conn()
    try:
        cases = derive(c)
        payload = {
            "schema": SCHEMA, "version": VERSION,
            "derived_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "source_db": "mbse.db (只读，含 WAL)", "derivation": "tools/eval/build_golden_e2e.py",
            "case_count": len(cases),
            "coverage": {k: sum(1 for x in cases if x["chain"] == k)
                         for k in ("single_agent", "orchestrated", "model_and_validate")},
            "cases": cases,
        }
        # ★ 语料指纹只依赖 cases：把 derived_at_utc 算进去会让「同一份用例」
        #   每次派生得到不同 id，语料漂移拦截就失效（§1l 的纪律）。
        #   库内交付物/证据变化 ⇒ cases 变 ⇒ 指纹变 ⇒ 必须重建基线，这正是目的。
        body = json.dumps(cases, ensure_ascii=False, sort_keys=True)
        payload["dataset_id"] = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
        for x in cases:
            ev = x["evidence_at_derivation"]
            print("%s chain=%-20s conv=%-7s kinds=%-30s tasks=%d/%d ver=%d verdicts=%s" %
                  (x["id"], x["chain"], x["conv_id"], ",".join(ev["artifact_kinds"]),
                   ev["agent_tasks_done"], ev["agent_tasks"], ev["sysml_versions"],
                   ",".join(ev["check_verdicts"]) or "-"))
        print("coverage=", payload["coverage"], "dataset_id=", payload["dataset_id"])
        missing = [x["id"] for x in cases if not x["input_source"]["message_id"]]
        if missing:
            print("!! 缺首条 user 消息，拒绝落盘：", missing)
            return 2
        if args.apply:
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            with open(args.out, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print("WROTE", args.out, os.path.getsize(args.out), "bytes")
        else:
            print("DRY-RUN（未写盘）；加 --apply 落盘")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
