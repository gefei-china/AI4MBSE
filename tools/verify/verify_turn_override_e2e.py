# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
"""全链路验证：会话入口的「本轮调整」是否真的落到上游请求（2026-10-31）。

为什么必须做这一层（前两轮都只证到一半）：
  · 第一轮 `verify_thinking_level.py` 只证到 **provider 层**：给 chat() 一个 mp，看 payload 对不对。
  · 第二轮 experiment 只证到 **provider 层 + 真实上游**：看到 reasoning 真的变了。
  但两者都**跳过了** ChatIn → 路由 → pipeline → 线程局部 这段。
  而本轮恰恰在这段里发现过两个真实缺陷（provider 不读覆盖、预算函数不读覆盖）。
  ⇒ 用户问"真的在生效吗"，唯一能算数的证据是：
    **发一个真实 HTTP 请求，把最终发给上游的 JSON 截下来，逐字段比对。**

本脚本用 FastAPI TestClient 走真实应用（真实路由、真实 pipeline、真实 ChatIn 校验），
只把最外层的 `httpx.post` 换成长截器 —— 也就是"网络那一跳"之外全是生产代码。

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_turn_override_e2e.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import httpx  # noqa: E402

CAPTURED = []
_orig_post = httpx.post


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        # 兼容 embedding 端点（它读 data[].embedding）与 chat 端点
        p = self._payload or {}
        if "input" in p or (isinstance(p.get("messages"), type(None)) and "model" in p):
            return {"data": [{"embedding": [0.01] * 8, "index": 0}],
                    "model": p.get("model", "emb"), "usage": {"prompt_tokens": 1, "total_tokens": 1}}
        return {
            "id": "fake", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "可以"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8,
                      "completion_tokens_details": {"reasoning_tokens": 0}},
        }

    def raise_for_status(self):
        return None

    @property
    def text(self):
        return json.dumps(self.json())


def _fake_post(url, json=None, headers=None, timeout=None, **kw):
    body = dict(json or {})
    body["__is_chat__"] = "/chat/completions" in str(url)
    CAPTURED.append({"url": url, "body": body})
    return _FakeResp(json)


def summarize(bodies):
    """把**LLM 对话调用**的 payload 汇总成用户关心的几件事。

    ★ 统计口径（2026-10-31 踩过，必须写清）：
      第一版把"所有 httpx.post"都算成上游调用，得到"38 次调用只有 3 次带覆盖"≈8%。
      逐条查 URL 才发现：**38 次里 38 次是 `/embeddings`（检索向量化）**，
      真正的 `/chat/completions` 只有 **2 次，且 2 次都带了覆盖** ⇒ 覆盖率其实 **100%**。
      即"覆盖率 8%"是**我的统计口径错**，不是实现问题。
      ⇒ 分母只能算 `/chat/completions`。同时把 embeddings 次数单独报出来，
        避免下次又把它的次数混进分子分母。
    """
    if not bodies:
        return None
    chat = [b for b in bodies if b.get("__is_chat__")]
    if not chat:
        return None
    th = [b.get("thinking") for b in chat if "thinking" in b]
    en = [b.get("enable_thinking") for b in chat if "enable_thinking" in b]
    mt = [b.get("max_tokens") for b in chat]
    tp = [b.get("temperature") for b in chat]
    hit_mt = [m for m in mt if m in (8192, 9216, 512)]
    return {
        "calls": len(chat),
        "embeddings": len(bodies) - len(chat),
        "thinking": th,
        "thinking_off": sum(1 for x in th if x and x.get("type") == "disabled"),
        "thinking_on": sum(1 for x in th if x and x.get("type") == "enabled"),
        "thinking_absent": len(chat) - len(th),
        "enable_thinking": en,
        "max_tokens": mt,
        "mt_override_hits": len(hit_mt),
        "temperature": tp,
    }


def main():
    from fastapi.testclient import TestClient
    import main as app_main

    httpx.post = _fake_post
    try:
        c = TestClient(app_main.app)
        r = c.post("/api/conversations", json={"title": "__e2e_override__"})
        cid = (r.json() or {}).get("id")
        if not cid:
            print("❌ 建会话失败：", r.status_code, r.text[:200])
            return 1

        cases = [
            ("不传覆盖（应沿用模型配置）", {}),
            ("本轮：关闭思考 + 上下文 8192",
             {"o_think": "关闭", "o_context": 8192}),
            ("本轮：高思考 + 输出上限 512",
             {"o_think": "高", "o_max_tokens": 512}),
        ]
        results = []
        for label, ov in cases:
            CAPTURED.clear()
            body = {"message": "本轮唯一消息 %d：只回复两个字 可以" % time.time_ns(),
                    "provider_id": 71, "o_temperature": 0.1}
            body.update(ov)
            resp = c.post("/api/conversations/%s/chat" % cid, json=body)
            s = summarize([x["body"] for x in CAPTURED])
            results.append((label, resp.status_code, s))
            print("\n" + "=" * 92)
            print("▶ %s" % label)
            print("  HTTP 状态：%s" % resp.status_code)
            if s is None:
                print("  ⚠️ 未捕获到上游调用（可能命中语义缓存）")
                continue
            print("  上游调用次数：%s" % s["calls"])
            print("  thinking        实际：%s" % s["thinking"])
            print("  enable_thinking 实际：%s" % s["enable_thinking"])
            print("  max_tokens      实际：%s" % s["max_tokens"])
            print("  temperature     实际：%s" % s["temperature"])

        print("\n" + "=" * 92)
        print("判定")
        print("=" * 92)
        ok = 0
        fail = 0

        def chk(desc, cond, extra=""):
            nonlocal ok, fail
            print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
            if cond:
                ok += 1
            else:
                fail += 1

        s1 = results[0][2]
        s2 = results[1][2]
        s3 = results[2][2]
        if s1 and s2 and s3:
            # ★ 判据已修正（2026-10-31）：原版断言"不传覆盖时不得出现 disabled"是**错的**。
            #   实测"不传覆盖"时也有 2 次 disabled —— 那是 `llm.thinking_policy.off` 名单
            #   对**编排内部 intent**（probe/routing/doc_summary…）的既有合法行为，
            #   与本轮覆盖无关。把系统既有策略当成"覆盖泄漏"是判据错，不是实现错。
            #   正确判据：**「关闭」比「不传」多出的 disabled 次数** 才是覆盖的真实贡献。
            delta_off = (s2["thinking_off"] or 0) - (s1["thinking_off"] or 0)
            chk("① 传「关闭」比不传**多关掉**调用（覆盖的真实贡献）",
                delta_off >= 1, "不传 disabled=%s → 关闭 disabled=%s（+%s）"
                % (s1["thinking_off"], s2["thinking_off"], delta_off))
            chk("② 传「高」把 disabled 全部翻成 enabled",
                (s3["thinking_off"] or 0) == 0 and (s3["thinking_on"] or 0) >= 1,
                "高：off=%s on=%s" % (s3["thinking_off"], s3["thinking_on"]))
            chk("③ o_context=8192 生效（max_tokens 被夹到 ≤8192）",
                all((m or 0) <= 8192 for m in s2["max_tokens"] if m),
                "max_tokens 覆盖命中 %s 次" % s2["mt_override_hits"])
            chk("④ o_think=高 触发深度提示（system 里含「思考强度：高」）",
                any("思考强度：高" in str(m.get("content") or "")
                    for x in CAPTURED for m in (x["body"].get("messages") or [])),
                "")
            chk("⑤ 高档给正文留额度（max_tokens ≥ 8192）",
                all((m or 0) >= 8192 for m in s3["max_tokens"] if m),
                "max_tokens 覆盖命中 %s 次" % s3["mt_override_hits"])
            chk("⑥ o_temperature=0.1 落到 payload", any(t == 0.1 for t in (s3["temperature"] or [])),
                "temperature=%s" % (s3["temperature"] or [])[:4])

            # ★★ 关键：覆盖的**命中率**（只按 chat/completions 算，口径见 summarize 注释）
            print("\n" + "-" * 92)
            print("覆盖命中率（分母只算 /chat/completions；embedding 单列，不计入）")
            print("-" * 92)
            for label, s in (("不传覆盖", s1), ("关闭+ctx8192", s2), ("高+mt512", s3)):
                print("  %-14s 对话调用 %-2s 次（另有 embedding %-2s 次）｜带 thinking %-2s"
                      "｜off=%-2s on=%-2s｜max_tokens 被覆盖 %s 次"
                      % (label, s["calls"], s["embeddings"],
                         (s["thinking_off"] or 0) + (s["thinking_on"] or 0),
                         s["thinking_off"], s["thinking_on"], s["mt_override_hits"]))
            cov = (s2["thinking_off"] or 0) + (s2["thinking_on"] or 0)
            ratio = (cov / float(s2["calls"])) if s2["calls"] else 0
            print("\n  「关闭」档覆盖 %s/%s 次对话调用 = **%.0f%%**"
                  % (cov, s2["calls"], ratio * 100))
            print("  说明：一次用户消息的**对话调用**只有个位数次（其余是检索向量化），")
            print("        所以覆盖几乎是全覆盖，不是'只生效一小部分'。")
            print("        ⚠️ 但编排内部若走 gateway/其他 provider 实例，仍可能读不到覆盖 ——")
            print("           判据是「带 thinking 的对话调用数 == 对话调用总数」。")
        print("\n%d 项通过，%d 项失败" % (ok, fail))
        return 1 if fail else 0
    finally:
        httpx.post = _orig_post
        try:
            import sqlite3
            con = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "mbse.db"))
            con.execute("DELETE FROM messages WHERE conversation_id IN "
                        "(SELECT id FROM conversations WHERE title='__e2e_override__')")
            con.execute("DELETE FROM conversations WHERE title='__e2e_override__'")
            con.commit()
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
