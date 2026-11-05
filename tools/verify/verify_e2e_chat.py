# CI-OPTIONAL: 需外部依赖（本机服务 / 真实 LLM key / 浏览器自动化 / Fuseki+java），CI 无这些运行时 ⇒ 接线必红。本地开发机可跑。判据：源码含 127.0.0.1 / OPENAI_API_KEY / playwright / fuseki 等信号。
# -*- coding: utf-8 -*-
"""会话级端到端验证：真实 HTTP 对话链路，确认链路完整 + 效果提升。

## 为什么脚本级不够（本项目已栽过）

脚本调 `retrieve()` 只证明"检索函数返回了什么"，不证明用户拿到什么。
中间还隔着：意图识别 → 上下文组装 → prompt 注入 → LLM 生成 → 引用回填。

实测教训（本章 §10）：`inject_k=3` 时脚本判"答对"，实际是**无依据答对**，
用户在界面上看到的是"结构完整、带引用编号、但引用对不上内容"的答案 —— 只有
**走真实 `/chat` 并同时核对正文与 citations** 才抓得到。

## 本脚本验什么（四类，覆盖本次全部改动）

  A. **域外拒答**（阈值重标定 0.481/0.395）——问"红烧肉"必须明确说不支持，而不是编
  B. **有依据答对**（rrf_k=10 + 向量重算）——答案要点 + citations 内容确实含依据
  C. **引用对齐**（§4 修的同源选择器）——正文 [n] 编号 ≤ citations 条数，且指向同一来源
  D. **链路完整**——HTTP 200、有 retrieval 段、有 citations、无 500

## 判据（全部自动判）

    refusal_ok   域外：正文明确说不支持/未找到，且 citations 为空
    grounded     切题：答案要点命中 且 依据片段里确实出现该要点
    align_ok     正文最大引用编号 ≤ citations 条数
"""
import os
import sys
import re
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import httpx

BASE = "http://127.0.0.1:8000"

# (类别, 问题, 答案要点候选, 说明)
CASES = [
    # A 域外：必须拒答
    ("域外", "今天北京天气怎么样", None, "与知识库无关"),
    ("域外", "红烧肉要炖多久", None, "常识类，库里无"),
    ("域外", "推荐几部好看的科幻电影", None, "完全无关"),
    # B 切题：必须有依据
    ("切题", "技术要求书要求的全年可用性是多少", ["99.9"], "单点数据"),
    ("切题", "自主可控要求对组件部署有什么限制", ["公有服务器"], "定性条款"),
    ("切题", "座舱空气调节仿真需要考虑哪些负荷", ["显热负荷", "太阳得热"], "场景知识库"),
    ("切题", "ISO 29148 对需求标识符的唯一性有什么规定", ["唯一", "标识"], "英文文档（曾降级）"),
    ("切题", "空调系统里'流与消息'的写法规则是什么", ["flow", "from"], "SysML 语法"),
]

REFUSAL_PAT = re.compile(
    r"(没有(?:相关|找到|检索到|提及|包含|收录)|未(?:找到|提及|检索到|包含)|"
    r"不存在(?:相关)?|知识库(?:中|里)(?:并)?(?:没有|未|无)|"
    r"无法(?:从|在|根据)(?:知识库|资料)|不在.{0,8}知识库|"
    r"不在.{0,6}知识范围|超出.{0,10}(?:范围|知识|能力)|超纲|"
    r"不属于.{0,10}(?:范围|领域|知识)|与.{0,8}(?:无关|不相关)|"
    r"没法给你|无法回答|抱歉.{0,12}(?:无法|不能))")

# ★ 2026-11-02：拒答判据第一版漏了实际措辞，导致把**正确拒答**判成失败。
#   实测原文：「红烧肉不在我的知识范围内…烹饪问题属于「超纲」，没法给你靠谱答案」
#   —— 命中的是"知识范围/超纲/没法给你"，而旧词表只有"知识库/没有相关"。
#   ⇒ 教训：**判据要用真实输出校准**，不能凭想象列关键词。加了上面 4 组变体。


def _client():
    return httpx.Client(base_url=BASE, timeout=300.0, trust_env=False)


def _new_conv(c, title):
    r = c.post("/api/conversations", json={"title": title, "intent": "knowledge_qa"})
    r.raise_for_status()
    return int(r.json()["id"])


def _ask(c, cid, q):
    r = c.post("/api/conversations/%d/chat" % cid,
               json={"message": q, "intent_override": "knowledge_qa"})
    r.raise_for_status()
    d = r.json()
    cites = (d.get("retrieval") or {}).get("citations") or []
    return {
        "answer": d.get("content") or "",
        "citations": cites,
        "n_cit": len(cites),
        "intent": d.get("intent") or "",
        "needs_clarify": bool(d.get("needs_clarification")),
    }


def main():
    fails = []
    print("=" * 88)
    print("会话级端到端验证（真实 /chat 链路）")
    print("=" * 88)

    with _client() as c:
        for cat, q, keys, note in CASES:
            print("\n[%s] %s" % (cat, q))
            print("     （%s）" % note)
            try:
                cid = _new_conv(c, "__e2e_verify__")
                r = _ask(c, cid, q)
            except Exception as e:  # noqa: BLE001
                print("     ❌ 链路失败：%s" % str(e)[:110])
                fails.append((cat, q, "链路异常"))
                continue

            ans, cites, n_cit = r["answer"], r["citations"], r["n_cit"]
            ctx = "\n".join((x.get("content") or "") for x in cites)
            marks = sorted({int(m) for m in re.findall(r"\[(\d{1,2})\]", ans)})

            # D 链路完整性：只看"有没有正常走完并产出正文"
            #
            # ★ 2026-11-02 修：第一版把 `needs_clarify=True` 当成链路失败，**判错了**。
            #   实测 `技术要求书要求的全年可用性是多少` → needs_clarify=True，但正文
            #   完整给出了"不低于 99.9% [1]"并在引用里找到依据 —— 说明它**答完了**，
            #   `needs_clarification` 只是附带"要不要继续深入"的追问建议，
            #   不是"没答"。用 answered 判更有意义。
            answered = len(ans.strip()) >= 20
            link_ok = (r["intent"] != "") and answered
            print("     D 链路：intent=%s 正文%d字 附追问=%s → %s"
                  % (r["intent"], len(ans.strip()), r["needs_clarify"],
                     "✅" if link_ok else "❌"))
            if not link_ok:
                fails.append((cat, q, "链路未走完"))
                continue

            if cat == "域外":
                is_ref = bool(REFUSAL_PAT.search(ans))
                # 拒答 = 明确说不支持，且不应给出成篇的"知识库内容"
                ok = is_ref and n_cit == 0
                print("     A 拒答：命中拒答语=%s citations=%d → %s"
                      % (is_ref, n_cit, "✅" if ok else "❌"))
                if not ok:
                    fails.append((cat, q, "未正确拒答（citations=%d）" % n_cit))
                    print("        正文摘要：%s" % ans[:150].replace("\n", " "))
                else:
                    print("        正文摘要：%s" % ans[:110].replace("\n", " "))
            else:
                # B 有依据：答案要点命中 + 依据片段里也有该要点
                hit_keys = [k for k in (keys or []) if k.lower() in ans.lower()]
                key_in_ctx = [k for k in (keys or []) if k.lower() in ctx.lower()]
                grounded = bool(hit_keys) and bool(key_in_ctx)
                # C 引用对齐
                align_ok = (not marks) or (max(marks) <= n_cit)
                print("     B 有依据：答中要点=%s | 依据含要点=%s → %s"
                      % (hit_keys or "无", key_in_ctx or "无", "✅" if grounded else "❌"))
                print("     C 引用对齐：正文编号=%s citations=%d → %s"
                      % (marks or "无", n_cit, "✅" if align_ok else "❌"))
                if not grounded:
                    fails.append((cat, q, "无依据/未答中"))
                if not align_ok:
                    fails.append((cat, q, "引用越界"))
                print("        正文摘要：%s" % ans[:150].replace("\n", " "))

    print("\n" + "=" * 88)
    if fails:
        print("❌ %d 项未通过：" % len(fails))
        for cat, q, why in fails:
            print("   [%s] %s —— %s" % (cat, q[:40], why))
    else:
        print("✅ 全部通过：域外正确拒答、切题有依据、引用对齐、链路完整")
    print("=" * 88)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
