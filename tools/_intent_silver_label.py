# -*- coding: utf-8 -*-
"""意图评测集扩容：双视图独立标注 → 只取一致（银标），P0-3，2026-10-30。

## 为什么需要这一步（口径问题，不是"想多攒点数据"）
真库现状：`intent_samples` 里 **confirmed 只有 48 条**。而 47/48 = 97.92% 的
Wilson 95% 置信区间是 **[89.1%, 99.6%]，宽 10.5 个百分点** ——
"0.9792" 与 "0.891" 在统计上**无法区分**，门禁阈值 0.85 落在区间内。
要检出 5 个百分点的回归需要 **269 条以上**。所以"评测集 300+"不是洁癖，是**判据能否成立的前提**。

## 为什么不直接把 suggested 转成 confirmed
`suggested` 行的 `intent` 字段来自**路由器自己的预测**（`hit_intent`）——
拿它当答案就是"用被测者自己的答案考它"，准确率必然接近 100%，属**循环论证**。
所以新增标签必须来自**与路由器不同的判定过程**。

## 本脚本的标注协议（银标，不是金标）
两个**视图不同**的标注器，各自独立调 LLM，**只有两者一致才入库**：

| | 视图 | 提问方式 | 输出 |
|---|---|---|---|
| L1 | **分类视图** | "这段输入属于哪一类任务？先判行为动词（提问/生成/变更/检查/汇报/管理），再给类" | `{"intent": "..."}` |
| L2 | **分派视图** | "这段输入应该交给哪个 Agent 处理？" | `{"agent": "..."}` → 映射回 intent |

- **一致** ⇒ 写 `intent` + `created_by='dual_label_v1'`，`status` 保持 `suggested`
  （**不**改成 `confirmed` —— 那是"人工确认"的语义，被污染后就再也说不清哪些是真人的判定）。
- **不一致** ⇒ 不写，只在报告里计数（这幅"分歧率"本身是有用指标：分歧高说明意图边界模糊）。

## ⚠️ 如实声明的局限（引用银标数字时必须带上）
1. **银标与路由器同源（都用 LLM）** ⇒ 绝对准确率会被**系统性高估**；
   它的正当用途是**同集合上的前后对比（回归检测）**，不是"我们的准确率是 X%"。
2. 两个标注器共享同一份意图目录（但推理视图不同）⇒ **不是完全独立**，
   只能消除"单次采样噪声 + 视图相关的系统性偏差"，消不掉"模型整体的共同偏见"。
3. 因此门禁口径必须**双集并列**：金标看绝对水平（n 小、区间宽，只做粗判），
   银标看回归（n 大、区间窄，做细判）。**任何一方单独报数都不合格。**

## 用法
    python tools/_intent_silver_label.py --limit 340            # 标注（带缓存，可重跑）
    python tools/_intent_silver_label.py --limit 340 --apply    # 写库
    python tools/_intent_silver_label.py --report               # 只看现有分布
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CACHE = os.path.join(ROOT, "tmp", "p0_3_evalset", "label_cache.json")
_CREATOR = "dual_label_v1"
_LOCK = threading.Lock()

# 与 agent/intent.py 的 `_db_intents` 同源：意图名 = active sub agent 的 name。
# 手工写死会漂移，故运行时从库里读（本脚本只做标注，不依赖路由器实现）。


def _catalog(conn):
    """意图目录：{intent: (展示名, 一句定义)}。定义取 agents.description 首句。"""
    out = {}
    for r in conn.execute(
            "SELECT name, display_name, COALESCE(description,'') FROM agents "
            "WHERE status='active' AND agent_role='sub' ORDER BY name"):
        name, disp, desc = r[0], (r[1] or r[0]), (r[2] or "")
        first = re.split(r"[。\n]", desc.strip())[0][:110] if desc.strip() else ""
        out[name] = (disp, first)
    return out


_SYS_L1 = """你是 MBSE 平台的**意图分类器**。给定一句用户输入，判断它属于下面哪一类任务。

判定方法（按顺序）：
1. 先看**行为动词**：提问/解释/是什么 → 知识类；生成/建模/设计/输出视图或代码 → 建模类；
   变更/影响/波及/删了会怎样 → 影响类；检查/评审/校验/打分 → 评审类；
   总结/汇总/出报告 → 汇报类；增删改配置/管理操作 → 管理类；寒暄/闲聊/问你是谁 → 闲聊类。
2. 再看**对象**：明确指向"需求条目/需求文档"→ 需求类；指向"模型/视图/SysML 代码"→ 建模类。
3. 只输出一个类名，不要解释。

可选类（键名必须逐字一致）：
%s

只输出 JSON：{"intent": "<类名>"}"""

_SYS_L2 = """你是 MBSE 平台的**任务分派器**。给定一句用户输入，选择**最适合接手它的 Agent**。

原则：
- 选"能直接开始干活"的那一个，不要选"看起来相关"的。
- 若输入只是打招呼、问你身份、或与建模无关 → 选通用问答 Agent。
- 若输入要求改配置/管理账号/系统运维 → 选系统管理类 Agent。

可选 Agent（键名必须逐字一致）：
%s

只输出 JSON：{"agent": "<Agent 名>"}"""


def _llm_json(system, user, tries=2):
    from llm import llm_client
    last = ""
    for _ in range(tries):
        try:
            r = llm_client.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                _intent="intent_silver_label", max_tokens=1024, temperature=0)
            txt = (((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                return json.loads(m.group(0))
            last = "无 JSON: " + txt[:80]
        except Exception as e:      # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, str(e)[:100])
    return {"_err": last}


def _load_cache():
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def label_one(text, cat, agents_desc, cache):
    """双视图标注一条文本 → (l1, l2, agree, err)。结果进缓存（按文本 sha1）。"""
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    with _LOCK:
        if h in cache:
            d = cache[h]
            return d.get("l1", ""), d.get("l2", ""), bool(d.get("agree")), d.get("err", "")

    cat_txt = "\n".join("- %s：%s%s" % (k, v[0], ("。" + v[1]) if v[1] else "")
                        for k, v in sorted(cat.items()))
    a1 = _llm_json(_SYS_L1 % cat_txt, text)
    l1 = str(a1.get("intent") or "").strip()

    ag_txt = "\n".join("- %s（%s）%s" % (k, v[0], ("：" + v[1]) if v[1] else "")
                       for k, v in sorted(agents_desc.items()))
    a2 = _llm_json(_SYS_L2 % ag_txt, text)
    l2 = str(a2.get("agent") or "").strip()

    err = "; ".join(x for x in (a1.get("_err"), a2.get("_err")) if x)
    # 归一：只保留目录内的键（模型偶尔会带上书名号/空格）
    l1 = l1.strip("「」\"' ")
    l2 = l2.strip("「」\"' ")
    ok1, ok2 = l1 in cat, l2 in agents_desc
    agree = bool(ok1 and ok2 and l1 == l2)
    with _LOCK:
        cache[h] = {"l1": l1, "l2": l2, "agree": agree, "err": err,
                    "ok1": ok1, "ok2": ok2}
    return l1, l2, agree, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=340, help="最多标注多少条候选")
    ap.add_argument("--apply", action="store_true", help="写库（默认只报告）")
    ap.add_argument("--report", action="store_true", help="只报告现有分布，不调 LLM")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    from database import db_conn
    with db_conn() as conn:
        cat = _catalog(conn)
        agents_desc = dict(cat)   # 分派视图用同一目录（Agent 名 == 意图名）
        # 系统管理 / 闲聊 不在 sub agent 里，但路由器会产出它们，必须进目录
        for extra in ("system_mgmt", "chat"):
            cat.setdefault(extra, (extra, ""))
            agents_desc.setdefault(extra, (extra, ""))

        if a.report:
            for row in conn.execute(
                    "SELECT COALESCE(created_by,'') c, COUNT(*) FROM intent_samples "
                    "WHERE intent<>'' GROUP BY c ORDER BY 2 DESC"):
                print("  %-22s %d" % (row[0] or "(空)", row[1]))
            return 0

        cands = [dict(r) for r in conn.execute(
            "SELECT id, text, COALESCE(intent,'') AS old_intent, COALESCE(created_by,'') AS cb "
            "FROM intent_samples "
            "WHERE status='suggested' AND COALESCE(text,'')<>'' "
            "  AND COALESCE(created_by,'')<>? "
            "  AND NOT EXISTS (SELECT 1 FROM intent_samples s2 "
            "                  WHERE s2.created_by=? AND s2.text=intent_samples.text) "
            "ORDER BY id", (_CREATOR, _CREATOR))]
        print("候选 %d 条（status=suggested 且未被本标注器处理过）" % len(cands))

        # 去重（同文本只标一次；写库时全部共享同一结论）
        uniq, seen = [], {}
        for r in cands:
            t = r["text"].strip()
            if t in seen:
                seen[t].append(r["id"])
                continue
            seen[t] = [r["id"]]
            uniq.append((t, r["id"]))
        print("去重后 %d 条唯一文本" % len(uniq))

        todo = uniq[:max(0, a.limit)]
        cache = _load_cache()
        print("开始标注 %d 条（缓存命中 %d 条）…" % (
            len(todo), sum(1 for t, _ in todo
                           if hashlib.sha1(t.encode("utf-8")).hexdigest()[:16] in cache)))

        results, errs = [], 0
        def _job(pair):
            t, _i = pair
            return (t,) + label_one(t, cat, agents_desc, cache)

        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            for n, (t, l1, l2, agree, err) in enumerate(ex.map(_job, todo), 1):
                if err:
                    errs += 1
                results.append((t, l1, l2, agree, err))
                if n % 40 == 0:
                    _save_cache(cache)
                    print("  … 已完成 %d/%d（一致 %d）" % (
                        n, len(todo), sum(1 for x in results if x[3])))
        _save_cache(cache)

        agree_rows = [x for x in results if x[3]]
        print("\n标注完成：%d 条，两视图一致 %d 条（%.1f%%），出错 %d 条" % (
            len(results), len(agree_rows), 100.0 * len(agree_rows) / max(len(results), 1), errs))

        dist = collections.Counter(x[1] for x in agree_rows)
        print("\n一致样本的类别分布：")
        for k, v in dist.most_common():
            print("  %-24s %d" % (k, v))
        dis = collections.Counter((x[1], x[2]) for x in results if not x[3])
        print("\n分歧 top8（L1 分类 vs L2 分派）：")
        for (k1, k2), v in dis.most_common(8):
            print("  %-22s vs %-22s %d" % (k1 or "(空)", k2 or "(空)", v))

        if not a.apply:
            print("\n[预演] 未写库。加 --apply 写入（status 保持 suggested，created_by=%s）。" % _CREATOR)
            return 0

        n = 0
        for t, l1, l2, agree, _err in agree_rows:
            ids = seen.get(t) or []
            for i in ids:
                n += conn.execute(
                    "UPDATE intent_samples SET intent=?, created_by=?, "
                    "  note=COALESCE(NULLIF(note,''),'')||? , updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND COALESCE(created_by,'')<>?",
                    (l1, _CREATOR, "[双视图一致 %s/%s] " % (l1, l2), i, _CREATOR)).rowcount
        print("\n已写入 %d 行（银标，status 仍为 suggested）" % n)
        tot = conn.execute("SELECT COUNT(*) FROM intent_samples WHERE created_by=?",
                           (_CREATOR,)).fetchone()[0]
        gold = conn.execute("SELECT COUNT(*) FROM intent_samples "
                            "WHERE status='confirmed' AND intent<>''").fetchone()[0]
        print("现规模：金标 %d + 银标 %d = %d" % (gold, tot, gold + tot))
        return 0


if __name__ == "__main__":
    sys.exit(main())
