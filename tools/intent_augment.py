"""intent_augment.py —— 意图语料泛化（P0-4）：用 LLM 把已确认种子扩成同义变体，写回样本池。

定位（**不是提分工具，是度量基础设施**）：
  主评测 n=44，逐类分辨率只有 1/44 ≈ 2.3%；`requirement_analysis` / `requirement_quality` /
  `system_mgmt` 各仅 2 条 → 逐类 P/R **不具统计意义**。本脚本把种子扩成可人工确认的候选，
  让后续每条改动的验收具备统计分辨率。

硬边界（越界即污染评测，见方案 §3 P0-4）：
  ① 产物一律 `status='suggested'` + `source='aug'`，**绝不自动 confirmed**（人工确认才是标签）；
  ② **只写样本池**，不写 `intent_rules`、不改词表、不进语义索引竞答名单
     （`routers/intent_samples.py` 的模块纪律：改标注 ≠ 改路由）；
  ③ 幂等：`intent_samples.text` 有 UNIQUE，重复说法只累加 `seen_count`。

用法：
  # 先看要生成什么（不写库）
  <repo>\\.venv\\Scripts\\python.exe -X utf8 tools\\intent_augment.py --dry-run
  # 首批：扩种子最少的 3 类，每类 20 条
  <repo>\\.venv\\Scripts\\python.exe -X utf8 tools\\intent_augment.py \\
      --intents requirement_analysis,requirement_quality,system_mgmt --per-intent 20
  # 全量
  <repo>\\.venv\\Scripts\\python.exe -X utf8 tools\\intent_augment.py --per-intent 20
"""
import argparse
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STYLES = [
    ("直接命令式", "短、祈使句，直接下令，可省略主语，例如「做个需求分析」「生成结构树」"),
    ("询问式", "疑问句，用户在问能不能/怎么做/有没有，例如「能帮我分析下需求吗」「需求追溯怎么建」"),
    ("描述式", "带一点背景或目标描述，但仍是一句话，例如「我要把这个系统的需求理一遍再建模」"),
    ("口语化省略", "工程师日常口语、能省则省，可有语气词，例如「帮我看下这需求」「这玩意儿怎么改」"),
]

SYSTEM = (
    "你是 MBSE（SysML v2 系统建模）智能助手的**语料工程师**。"
    "下面给你某个意图的真实用户说法样例，请仿写**同一意图**的更多不同表达。"
    "要求：①贴近真实工程师口吻，不要客服腔；②不得引入其它意图的话题；"
    "③可用同义术语替换（SysML v2 ⇄ 系统建模语言、BDD ⇄ 块定义图、IBD ⇄ 内部块图、"
    "需求追溯 ⇄ 需求链路追溯）；④每条 4~40 字，无编号、无引号、无解释；"
    '⑤只输出 JSON：{"queries": ["...", "..."]}，不要任何其它文字。'
)
USER_TMPL = (
    "意图名：{intent}\n"
    "风格要求（本批必须全部符合）：{style_desc}\n"
    "参考样例（真实用户原话）：\n{seeds}\n\n"
    "请生成 {n} 条**新**表达（不要与参考样例重复）："
)

MIN_LEN, MAX_LEN = 4, 60


def parse_queries(content: str) -> list:
    """从 LLM 返回里抠出字符串数组。容错：整体 JSON → 正则兜底。"""
    content = (content or "").strip()
    # 去掉 ```json fences
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    try:
        obj = json.loads(content)
        arr = obj.get("queries") if isinstance(obj, dict) else obj
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if str(x).strip()]
    except Exception:
        pass
    m = re.search(r"\[(.*)\]", content, re.S)
    if m:
        try:
            arr = json.loads("[" + m.group(1) + "]")
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    # 最兜底：按行取
    out = []
    for ln in content.splitlines():
        ln = re.sub(r'^\s*[\d\-\*\.\)、]+\s*', "", ln).strip().strip('",')
        if MIN_LEN <= len(ln) <= MAX_LEN:
            out.append(ln)
    return out


def clean(q: str) -> str:
    q = re.sub(r"\s+", " ", (q or "").strip())
    q = q.strip('"“”\'‘’ 　')
    return q


def gen_batch(intent: str, seeds: list, style_desc: str, n: int) -> list:
    from llm import llm_client
    seed_txt = "\n".join("- " + s for s in seeds[:12])
    user = USER_TMPL.format(intent=intent, style_desc=style_desc, seeds=seed_txt, n=n)
    resp = llm_client.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        _intent="intent_augment",
    )
    msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
    return parse_queries(msg.get("content") or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="意图语料泛化（P0-4）")
    ap.add_argument("--intents", default="", help="逗号分隔；缺省=全部有 confirmed 种子的意图")
    ap.add_argument("--per-intent", type=int, default=20, help="每意图目标新增条数（默认 20，对齐对标文建议的 20~30 下限）")
    ap.add_argument("--min-seeds", type=int, default=2, help="种子数少于该值的意图跳过（默认 2=不跳）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不调 LLM、不写库")
    ap.add_argument("--no-llm", action="store_true", help="只统计种子与现有池子，不调用 LLM")
    args = ap.parse_args()

    from database import get_db
    from repositories.intent_sample_repo import IntentSampleRepo

    conn = get_db()
    try:
        repo = IntentSampleRepo(conn)
        rows = conn.execute(
            "SELECT intent, text FROM intent_samples WHERE status='confirmed' AND intent<>'' ORDER BY id"
        ).fetchall()
        by = {}
        for r in rows:
            by.setdefault(r["intent"], []).append(r["text"])
        existing = {r["text"] for r in conn.execute("SELECT text FROM intent_samples").fetchall()}

        targets = ([x.strip() for x in args.intents.split(",") if x.strip()]
                   if args.intents else sorted(by, key=lambda k: len(by[k])))
        targets = [t for t in targets if len(by.get(t, [])) >= args.min_seeds]

        print("=" * 78)
        print("P0-4 意图语料泛化")
        print("=" * 78)
        print(f"池中 confirmed 种子：{len(rows)} 条 / {len(by)} 个意图；表中已有说法 {len(existing)} 条")
        print(f"目标意图（{len(targets)}）：")
        for t in targets:
            print(f"  {t:<24} 种子 {len(by.get(t, []))} 条 → 目标 +{args.per_intent}")
        if args.dry_run or args.no_llm:
            print("\n（--dry-run/--no-llm：不调 LLM、不写库）")
            return 0

        total_new, total_gen = 0, 0
        for intent in targets:
            seeds = by[intent]
            per_style = max(1, math.ceil(args.per_intent / len(STYLES)))
            got_all = []
            for si, (sname, sdesc) in enumerate(STYLES, 1):
                try:
                    qs = gen_batch(intent, seeds, sdesc, per_style)
                except Exception as e:
                    print(f"  [{intent}/{sname}] LLM 调用失败：{str(e)[:80]}")
                    continue
                total_gen += len(qs)
                kept = []
                for q in qs:
                    q = clean(q)
                    if not (MIN_LEN <= len(q) <= MAX_LEN):
                        continue
                    if q in existing or q in got_all:
                        continue
                    got_all.append(q)
                    kept.append(q)
                print(f"  [{intent}/{sname}] 产出 {len(qs)} → 去重保留 {len(kept)}")

            ins = 0
            for q in got_all:
                r = repo.upsert(q, intent=intent, status="suggested", source="aug",
                                note=f"LLM 泛化(P0-4) {intent}")
                if r.get("ok") and not r.get("updated"):
                    ins += 1
                existing.add(q)
            total_new += ins
            print(f"  → {intent}：新增 {ins} 条（suggested / source=aug）")

        print("\n" + "=" * 78)
        print(f"合计：LLM 产出 {total_gen} 条 → 去重后新增入库 {total_new} 条")
        print("状态一律 suggested，**需人工确认后才进评测集**（confirmed）")
        print("=" * 78)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
