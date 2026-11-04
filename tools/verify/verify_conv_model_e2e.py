# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
"""端到端：同一会话内切模型，真实请求的 payload 必须各用各的设置（2026-11-01）。

为什么接口层隔离还不够（本轮必须做这一层）：
  设置存在 `by_model` 里 ≠ 执行时真的按当前模型取桶。
  上一轮就踩过同族问题：provider 层不读覆盖，导致"设置存进去了、payload 没变"。
  ⇒ 唯一能算数的证据：**发真实请求，把发给上游的 JSON 截下来，逐字段比对**。

判据（同一会话，模型 A 设 关闭+8192，模型 B 设 高+131072）：
  E1 用 A 发 ⇒ **主对话调用**必须 thinking:disabled 且 max_tokens 被夹到 8192
  E2 用 B 发 ⇒ 主对话调用必须"开思考"（qwen 走 enable_thinking，不是 thinking）
  E3 两次的主对话 payload 必须不同 —— "不是一套设置套所有模型"的硬证据
  E4 未设置的模型 C 发 ⇒ 主对话调用不带任何会话覆盖

  ⚠️ 判据必须限定在「**主对话调用**」上，不能要求"所有调用都带覆盖"：
     实测一次用户消息的调用里，`#0` 是**意图分类器**（intent_detect），
     它跑在 `effective_provider` 确定**之前**，用 DB 默认模型与默认 max_tokens，
     **不受会话设置管辖**（会话设置是"用哪个模型"确定之后才读得到的）。
     第一版判据写成"所有调用 ≤8192 / 所有调用都要有思考字段"⇒ 2 项假红，
     那是**判据错**，不是实现错（本任务已第 N 次踩这个坑，别再犯）。
     主对话调用的判据：payload 里 `model` 等于目标 provider 的 model_name。

  ⚠️ 思考字段有三形态，判据必须全覆盖：
     deepseek/anthropic → `thinking`；qwen → `enable_thinking`+`thinking_budget`；
     openai o系 → `reasoning_effort`。只查 `thinking` 会把 qwen 的开启误判成"没开"。

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_conv_model_e2e.py
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import httpx  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")
CAP = []
_orig = httpx.post


class _R:
    status_code = 200

    def __init__(self, p, url=""):
        self._p = p
        self._u = url

    def json(self):
        if "/embeddings" in self._u:
            return {"data": [{"embedding": [0.01] * 8, "index": 0}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1}}
        return {"id": "f", "choices": [{"index": 0,
                                        "message": {"role": "assistant", "content": "可以"},
                                        "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8,
                          "completion_tokens_details": {"reasoning_tokens": 0}}}

    def raise_for_status(self):
        return None

    @property
    def text(self):
        return "{}"


def fake_post(url, json=None, headers=None, timeout=None, **kw):
    if "/chat/completions" in str(url):
        CAP.append(dict(json or {}))
    return _R(json, str(url))


def think_fields(b):
    """抽出思考相关字段（三形态全覆盖）。只查 `thinking` 会漏掉 qwen/openai。"""
    return {k: b.get(k) for k in ("thinking", "enable_thinking", "thinking_budget", "reasoning_effort")
            if k in b}


def is_thinking_off(tf):
    return (tf.get("thinking") or {}).get("type") == "disabled" or tf.get("enable_thinking") is False


def is_thinking_on(tf):
    return (tf.get("thinking") or {}).get("type") == "enabled" or tf.get("enable_thinking") is True


def main():
    from fastapi.testclient import TestClient
    import main as app_main

    A, B, C = 71, 104, 1     # C=DeepSeek-V3（不设任何会话参数）
    ok = fail = 0

    def chk(desc, cond, extra=""):
        nonlocal ok, fail
        print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
        if cond:
            ok += 1
        else:
            fail += 1

    # 主对话调用 = payload.model 等于目标 provider 的 model_name
    def model_name_of(pid):
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        r = con.execute("SELECT model_name FROM llm_providers WHERE id=?", (pid,)).fetchone()
        con.close()
        return (r[0] if r else "") or ""

    def main_calls(pid):
        mn = model_name_of(pid)
        return [b for b in CAP if str(b.get("model")) == mn]

    httpx.post = fake_post
    c = TestClient(app_main.app)
    cid = (c.post("/api/conversations", json={"title": "__cme2e__"}).json() or {}).get("id")
    print("\n会话=%s ｜ A=%s(关/8K) B=%s(高/128K) C=%s(未设置)" % (cid, A, B, C))
    print("模型名：A=%s B=%s C=%s" % (model_name_of(A), model_name_of(B), model_name_of(C)))

    def send(pid):
        CAP.clear()
        r = c.post("/api/conversations/%s/chat" % cid,
                   json={"message": "只回复两个字 可以", "provider_id": pid})
        return r.status_code, list(CAP)

    try:
        # 先写入两套设置
        c.put("/api/conversations/%s/llm-settings" % cid,
              json={"provider_id": A, "thinking_level": "关闭", "context_window": 8192})
        c.put("/api/conversations/%s/llm-settings" % cid,
              json={"provider_id": B, "thinking_level": "高", "context_window": 131072})
        print("设置已写入：A=关闭/8192  B=高/131072\n")

        st_a, calls_a = send(A)
        mc_a = [b for b in calls_a if str(b.get("model")) == model_name_of(A)]
        tf_a = [think_fields(b) for b in mc_a]
        mt_a = [b.get("max_tokens") for b in mc_a if b.get("max_tokens")]
        print("[E1] A 主对话调用 %d 次 → HTTP %s ｜ 思考=%s ｜ max_tokens=%s"
              % (len(mc_a), st_a, tf_a, sorted(set(mt_a))))
        chk("E1-0 找到 A 的主对话调用（判据前置，防「没找到也判绿」）", len(mc_a) >= 1, "n=%d" % len(mc_a))
        chk("E1-1 用 A 发，主对话**关闭思考**",
            any(is_thinking_off(t) for t in tf_a), str(tf_a))
        chk("E1-2 用 A 发，max_tokens 被会话设置夹到 8192",
            any(m == 8192 for m in mt_a), str(sorted(set(mt_a))))

        st_b, calls_b = send(B)
        mc_b = [b for b in calls_b if str(b.get("model")) == model_name_of(B)]
        tf_b = [think_fields(b) for b in mc_b]
        mt_b = [b.get("max_tokens") for b in mc_b if b.get("max_tokens")]
        print("\n[E2] B 主对话调用 %d 次 → HTTP %s ｜ 思考=%s ｜ max_tokens=%s"
              % (len(mc_b), st_b, tf_b, sorted(set(mt_b))))
        chk("E2-0 找到 B 的主对话调用", len(mc_b) >= 1, "n=%d" % len(mc_b))
        chk("E2-1 用 B 发，主对话**打开思考**（三形态全覆盖判据）",
            any(is_thinking_on(t) for t in tf_b) and not any(is_thinking_off(t) for t in tf_b), str(tf_b))
        chk("E2-2 用 B 发，max_tokens **不是** A 的 8192（用自己的档）",
            all(m != 8192 for m in mt_b) if mt_b else False, str(sorted(set(mt_b))))

        # E3：两次的主对话 payload 必须不同
        print("\n[E3] 对照：A 思考=%s vs B 思考=%s" % (tf_a, tf_b))
        chk("E3 两个模型的**主对话 payload 不同**（不是一套设置套所有模型）",
            json.dumps(tf_a, ensure_ascii=False) != json.dumps(tf_b, ensure_ascii=False),
            "A=%s B=%s" % (tf_a, tf_b))

        st_c, calls_c = send(C)
        mc_c = [b for b in calls_c if str(b.get("model")) == model_name_of(C)]
        tf_c = [think_fields(b) for b in mc_c]
        mt_c = [b.get("max_tokens") for b in mc_c if b.get("max_tokens")]
        print("\n[E4] C 主对话调用 %d 次 → HTTP %s ｜ 思考=%s ｜ max_tokens=%s"
              % (len(mc_c), st_c, tf_c, sorted(set(mt_c))))
        chk("E4 未设置的模型不带会话覆盖（不出现 A 的 8192 特征值）",
            not any(m == 8192 for m in mt_c), str(sorted(set(mt_c))))

        print("\n%d 项通过，%d 项失败" % (ok, fail))
        return 1 if fail else 0
    finally:
        httpx.post = _orig
        try:
            con = sqlite3.connect(DB)
            con.execute("DELETE FROM messages WHERE conversation_id IN "
                        "(SELECT id FROM conversations WHERE title='__cme2e__')")
            con.execute("DELETE FROM conversations WHERE title='__cme2e__'")
            con.commit()
            con.close()
            print("（已清理测试会话）")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
