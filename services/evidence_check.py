# -*- coding: utf-8 -*-
"""引用可回查：`basis=verified` / `claims.kind=fact` 必须**指得到受管元素**（P0-4，2026-10-30）。

## 为什么需要它（问题不是"模型会撒谎"，而是"没有任何机制发现它指了个不存在的 id"）
`services/subtask_protocol.py` 的能力边界写得很清楚：
`basis` / `kind` 都是**模型自报**，服务端只做枚举校验与截断，**不验证真实性**。
这个边界本身是对的 —— 我们确实无法判断"模型是否真的查过图谱"。

但它把**另一件事**也一起放弃了：**引用能不能在库里被找到**。
这两件事完全不同：

| 问题 | 能不能机械化判定 | 本模块 |
|---|---|---|
| 模型是否**真的**查过图谱？ | ❌ 不能 | 不判（保持原边界） |
| 它引用的 `#LM-P10` / `REQ-042` / 那个附件路径**在库里存在吗**？ | ✅ 能 | **判** |

对标 ACM MODELS'26 的 "epistemic adequacy"：要求结论**指得到**受管元素（point to），
而不是要求"结论为真"。前者是工程可判的，后者不是。
真库实测的失效形态：模型给出 `basis=verified` + `ref="E-P10-001"`，而 `entities` 里没有这个 id ——
下游（评审、汇总、前端徽标）把这个"已验证"当成事实采信。

## 判据（只降不升，与全仓"保守兜底"一致）
1. `evidence[i].basis == 'verified'` 且 `ref` **回查不到** ⇒ 降级为 `asserted`，并记一条 warning。
2. 该子任务里 `claims[j].kind == 'fact'` 但**没有任何一条可回查的 evidence** ⇒ 降级为 `to_verify`。
   —— "fact" 的定义是"可被第三方独立复核"，若连一条能回查的依据都没有，
      它按定义就不是 fact（这与"结论对不对"无关，是**自洽性**问题）。

## 为什么"降级"而不是"丢弃 / 判失败"
丢弃会让一段本来有信息量的产出凭空消失（信息损失）；判失败会让整批降级（连带损失）。
降级只改**信任标记**，内容一字不动 —— 读者看到的是"这条依据没查到，请自行核对"，
而不是"这段话不见了"。方向永远是**往更不可靠的一档**降（见 `BASIS_VALUES` / `CLAIM_KINDS` 的顺序）。
"""
import os
import re

__all__ = ["resolve_ref", "enforce", "STATS_KEYS"]

STATS_KEYS = ("checked", "resolved", "downgraded_basis", "downgraded_claim", "unresolved")

# ref 前缀 → 查询。刻意**只认明确可控的形态**，不做模糊猜测：
#   · 纯数字 / `ent:` / `E-` 开头 → 实体 id 或名字
#   · `rel:` → 关系 id
#   · `doc:` → 文档 id
#   · 以 `.` 结尾或含 `/` 的路径 → 附件/文件（校验存在性）
_SQL_BY_KIND = {
    "entity": "SELECT 1 FROM entities WHERE id=? OR name=? LIMIT 1",
    # ⚠️ relations 的列名是 **`source_id`**，不是 `src_id`。
    #   第一版写成 `src_id` ⇒ 每次查询都抛 `no such column` ⇒
    #   被 fail-open 吞成"跳过" ⇒ **关系通道整体失效且看起来是 OK**
    #   （判别信号：体检输出里全是"查询失败（跳过）"而不是"命中/未命中"）。
    #   教训与"只传一个绑定参数"同族：**fail-open 的分支必须能在日志里被看见**。
    "relation": "SELECT 1 FROM relations WHERE id=? OR source_id=? LIMIT 1",
    "doc": "SELECT 1 FROM documents WHERE id=? OR filename=? LIMIT 1",
}


def _looks_like_path(ref: str) -> bool:
    return ("/" in ref or "\\" in ref) and ("." in os.path.basename(ref))


# ── 文档引用的归一（真库主形态，占不可回查样本的绝大多数）─────────────────
# 实测不可回查的 ref 长这样（都是**合理引用**，只是不指向实体 id）：
#     "OMG Systems Modeling Language™ V2 (1).pdf §7.1"
#     "知识库_运行视角_视图规范元素要求 §3：需求定义/需求使用/doc/id 四要素"
#     "知识库_运行视角_视图间关联关系 第17节（…）"
# 若把它们一律判成"不可回查"，就会把**正确的引用**降级 —— 那是假阴性，
# 比漏判更有害（会让模型学会不引用，而不是学会引用得准）。
#
# 归一规则（只做能确定的剥离，不做模糊猜测）：
#   ① 去掉商标符号/全角空格；② 去扩展名；③ 去末尾的 "(1)"/"（1）" 副本标记；
#   ④ 只取 `§`/`第…节`/`：`/`:` 之前的**文档名部分**。
_TAIL_SPLIT = ("§", "第", "：", ":", " - ", " – ")


def _norm_doc(s: str) -> str:
    t = str(s or "").strip()
    for ch in ("™", "®", "©"):
        t = t.replace(ch, "")
    t = t.replace("\u3000", " ").strip()
    # ★ 去掉"来源N / 【来源N】/ [来源N]"前缀 —— 模型常把 RAG 的引用编号写进 ref，
    #   实测形态："来源1 视图规范元素要求 §4：…" 而库里文档名是
    #   "知识库_运行视角_视图规范元素要求(1).md" ⇒ 不去前缀会误判成"文档未命中"。
    t = re.sub(r"^[\[【（(]?\s*来源\s*\d+\s*[\]】）)]?\s*", "", t)
    t = re.sub(r"^参考\s*[:：]?\s*", "", t)
    for ext in (".pdf", ".PDF", ".md", ".docx", ".doc", ".txt", ".xlsx"):
        if t.endswith(ext):
            t = t[: -len(ext)]
            break
    # 去副本标记 "(1)" / "（1）"
    if len(t) > 3 and t[-1] in ")）":
        for op in ("(", "（"):
            if op in t and t.rindex(op) > len(t) - 6:
                t = t[: t.rindex(op)]
                break
    t = " ".join(t.split())
    return t.lower()


def _doc_exists(conn, ref: str) -> tuple:
    """ref 里**能识别出的文档名**是否存在于 `documents`？返回 (ok, 说明)。

    只判"文档存在"，**不校验章节号是否存在**（那要解析文档结构，属另一条线）——
    如实声明这个粒度：本判据的意思是"你引的这份资料在库里"，不是"第 7.1 节确有这句话"。
    """
    head = ref
    for sp in _TAIL_SPLIT:
        if sp in head:
            head = head.split(sp, 1)[0]
    cand = _norm_doc(head) or _norm_doc(ref)
    if len(cand) < 4:
        return False, "文档名过短，无法判定"
    try:
        rows = conn.execute("SELECT filename FROM documents").fetchall()
    except Exception as e:      # noqa: BLE001
        return True, "documents 表不可用（跳过）：%s" % str(e)[:50]
    for r in rows:
        fn = _norm_doc(r[0] if not hasattr(r, "keys") else r["filename"])
        if not fn:
            continue
        # 双向包含：ref 常省略扩展名/副本号，而库里带；反之也可能 ref 更全
        if cand in fn or fn in cand:
            return True, "文档命中：%s" % fn[:40]
    return False, "文档未命中：%s" % cand[:40]


def resolve_ref(conn, source: str, ref: str) -> tuple:
    """引用能否在当前库里回查？返回 (ok, 说明)。

    **只验存在性**：查不到不等于模型在编（可能是历史数据、别的分支、已删除），
    因此调用方只应据此**降级信任标记**，不得据此判失败。

    解析顺序（先便宜后昂贵，且**任一路径命中即算可回查** —— 引用只要能落到
    某个受管元素上就满足"可回查"的定义）：
      ① 显式前缀 `ent:` / `rel:` / `doc:` → 对应通道（精确）
      ② 路径形态 → 文件是否存在
      ③ 文档引用（`… .pdf §7.1` / `… 第17节`）→ `documents` 里是否有这份资料
      ④ 实体 id / 名称精确匹配
    """
    r = str(ref or "").strip().strip("`'\"")
    if not r:
        return False, "ref 为空"
    if conn is None:
        return True, "无连接（跳过，不降级）"

    low = r.lower()
    # ① 显式前缀：只走指定通道，不做兜底（写错了就该被发现）
    for pfx, kd in (("rel:", "relation"), ("relation:", "relation"),
                    ("doc:", "doc"), ("document:", "doc"),
                    ("ent:", "entity"), ("entity:", "entity")):
        if low.startswith(pfx):
            val = r.split(":", 1)[1].strip()
            st, why = _by_sql(conn, kd, val)
            # 显式前缀下 fail-open **仍生效**（老库缺表时不该把引用全判死）
            return (st != "miss"), why

    # ② 路径
    if _looks_like_path(r):
        try:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p = r if os.path.isabs(r) else os.path.join(root, r)
            if os.path.exists(p):
                return True, "文件存在"
        except Exception:
            return True, "路径校验异常（跳过）"

    # ③ 文档引用（真库主形态）
    ok, why = _doc_exists(conn, r)
    if ok:
        return True, why

    # ④ 实体/关系精确匹配（**兜底**）
    #   ⚠️ 这里绝不能把 `skip` 当命中：第一版就是因此把"关系表列名写错"
    #      掩盖成"引用可回查"（门禁 A2/A9 同时红才暴露）。
    #      兜底路径的语义是"试着匹配一下"，匹配不成就该继续往下走/如实报未命中。
    for kd in ("entity", "relation"):
        st, why2 = _by_sql(conn, kd, r)
        if st == "hit":
            return True, why2

    return False, why or "未命中任何通道"


def _by_sql(conn, kind: str, val: str) -> tuple:
    """按通道精确查一条。返回 (state, 说明)，state ∈ {"hit","miss","skip"}。

    `skip` = 查不了（老库缺表/列名漂移）。**调用方必须区分它与 `miss`**：
    显式前缀下 skip 可以 fail-open 放行，但兜底匹配里 skip 不能算命中。
    """
    # ⚠️ 每条 SQL 有**两个**占位符（id OR name/filename）—— 必须传两个参数。
    #   第一版只传一个 ⇒ sqlite3 抛 "Incorrect number of bindings" ⇒
    #   被下面 except 吞成"跳过" ⇒ **降级从不发生**，而表面上一切正常
    #   （门禁 A2/B1 就是这么抓出来的：断言红、日志无异常）。
    try:
        row = conn.execute(_SQL_BY_KIND[kind], (val, val)).fetchone()
        return ("hit", "%s 命中" % kind) if row else ("miss", "%s 未命中" % kind)
    except Exception as e:      # noqa: BLE001
        return "skip", "查询失败（跳过）：%s" % str(e)[:60]


def enforce(conn, summary, *, stat: dict | None = None):
    """对一份已 `validate` 过的 summary 做引用回查 + 降级。**原地返回**同一对象。

    :param conn: 数据库连接（None 时整体跳过）
    :param stat: 可选的累计计数器（调用方可在整批结束后汇总）
    """
    if not isinstance(summary, dict):
        return summary
    st = stat if isinstance(stat, dict) else {}
    for k in STATS_KEYS:
        st.setdefault(k, 0)

    evs = summary.get("evidence") or []
    resolved_any = False
    unresolved = []
    for i, e in enumerate(evs):
        if not isinstance(e, dict):
            continue
        basis = str(e.get("basis") or "asserted")
        st["checked"] += 1
        ok, why = resolve_ref(conn, str(e.get("source") or ""), str(e.get("ref") or ""))
        if ok:
            st["resolved"] += 1
            if basis == "verified":
                resolved_any = True
            continue
        unresolved.append({"i": i, "ref": str(e.get("ref") or "")[:60], "why": why})
        st["unresolved_name"] = st.get("unresolved_name", 0)
        if basis == "verified":
            # ★ 只降不升：verified → asserted（不是删掉，也不是判失败）
            e["basis"] = "asserted"
            e["ref_unresolved"] = why          # 留痕：读者能看到"为什么降到这一档"
            st["downgraded_basis"] += 1
        elif basis == "derived":
            # derived 依赖已验证事实 ⇒ 依据回查不到时它也不成立，但比 verified 轻一档：
            # 不降级，只标注（保守与信息量的折中，见模块头"只降不升"的边界）。
            e["ref_unresolved"] = why

    # claims：声称 fact 却没有任何可回查依据 ⇒ 按定义不是 fact
    for j, c in enumerate(summary.get("claims") or []):
        if not isinstance(c, dict):
            continue
        if str(c.get("kind") or "") == "fact" and (evs and not resolved_any):
            c["kind"] = "to_verify"
            c["demoted_reason"] = "无可回查依据"
            st["downgraded_claim"] += 1

    if unresolved:
        st["unresolved"] += len(unresolved)
        try:
            summary.setdefault("meta", {})["evidence_unresolved"] = unresolved[:5]
        except Exception:
            pass
    if st.get("downgraded_basis") or st.get("downgraded_claim"):
        try:
            summary.setdefault("meta", {})["evidence_downgraded"] = {
                "basis": st.get("downgraded_basis", 0),
                "claim": st.get("downgraded_claim", 0),
            }
        except Exception:
            pass
    return summary
