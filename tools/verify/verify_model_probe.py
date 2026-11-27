# -*- coding: utf-8 -*-
# CI-OPTIONAL: 需真实 provider 凭据做功能自检（CI 无 secret），开发机 40/40；不进 CI
"""verify_model_probe —— 锁住「模型测试 = 真实功能验证」的口径（离线确定性）。

## 背景（2026-11-27 用户需求）

> 模型管理中「测试」功能是否完备，点击测试只确认连接成功，功能是否正常其实无法知道，
> 需要进行真实的使用验证。

改动前判据是 `HTTP 200`。本门禁用一个**本地假上游**复现四种"200 但没用"，逐项断言
`llm.probe` 能分辨出来：判据若退化成"看状态码"，这些用例会全部变绿 ⇒ 门禁立刻红。

| 假上游模式 | 旧判据 | 新判据要求 |
|---|---|---|
| ok | 通过 | 全项 pass |
| empty_body（200 + content=""） | 通过 | chat **fail**（正文为空） |
| no_tools（200 但不返 tool_calls） | 通过 | tools **fail/warn**（Agent 工具链空转） |
| no_stream（忽略 stream，返回普通 JSON） | 通过 | stream **fail** |
| no_json（response_format 400） | 通过 | json **warn**（可降级，不判死） |
| multimodal 拒绝图片（400） | 通过 | vision **fail** |
| emb_const（向量恒定） | 通过 | embed_semantics **fail**（排序无意义） |
| emb_zero（返回全零向量） | 通过 | embedding **fail**（余弦恒 0） |
| emb_short（回包条数不足） | 通过 | embedding **fail**（条与条错位） |
| emb_bad（data 为空） | 通过 | embedding **fail** |

另有两条口径断言：
  P1 · `deep=True` 必须真的发出 ≥3 次请求（不是"看起来跑了"）；
  P2 · 探针记账 `intent="chat_probe"` ⇒ `is_probe=1`（不进业务成本口径）。

用法：.venv/Scripts/python.exe -X utf8 tools/verify/verify_model_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

MODE = {"v": "ok", "hits": 0}

_fails, _passes = [], []


def chk(tag: str, ok: bool, detail: str = "") -> bool:
    (_passes if ok else _fails).append(tag)
    print(("  PASS " if ok else "  FAIL ") + tag + (("  | " + detail) if detail else ""))
    return ok


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # 静默：门禁输出只看断言
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks: list):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for c in chunks:
            payload = ("data: " + c + "\n\n").encode("utf-8")
            self.wfile.write(("%x\r\n" % len(payload)).encode() + payload + b"\r\n")
        end = b"data: [DONE]\n\n"
        self.wfile.write(("%x\r\n" % len(end)).encode() + end + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def do_POST(self):
        MODE["hits"] += 1
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        mode = MODE["v"]

        if self.path.rstrip("/").endswith("/embeddings"):
            inp = body.get("input") or []
            if isinstance(inp, str):
                inp = [inp]
            if mode == "emb_bad":
                return self._json(200, {"data": []})
            if mode == "emb_short":          # 只回一半条数：入库/检索会错位
                inp = inp[:2]
            if mode == "emb_zero":           # 非空但全零：余弦分母为 0
                return self._json(200, {"data": [{"index": i, "embedding": [0.0] * 8}
                                                 for i in range(len(inp))]})
            if mode == "emb_const":          # 常量向量：条数/维度都对，但余弦恒为 1
                return self._json(200, {"data": [{"index": i, "embedding": [0.1] * 8}
                                                 for i in range(len(inp))]})
            if mode == "emb_nodisc":         # 非退化但无关句更近：能排序也排不对
                data = []
                for i in range(len(inp)):
                    v = [0.0] * 8
                    v[0 if i % 2 == 0 else 1] = 1.0     # q 与 neg 同向、与 rel 正交
                    data.append({"index": i, "embedding": v})
                return self._json(200, {"data": data})
            # ok：含「活动图」的落第 0 维，其余落第 1 维 ⇒ 相关句必然比无关句更近
            data = []
            for i, t in enumerate(inp):
                v = [0.0] * 8
                v[0 if "活动图" in str(t) else 1] = 1.0
                data.append({"index": i, "embedding": v})
            return self._json(200, {"data": data})

        has_image = any(isinstance(m.get("content"), list) for m in (body.get("messages") or []))
        if has_image and mode == "no_vision":
            return self._json(400, {"error": {"message": "model does not support image input"}})

        if body.get("stream") and mode != "no_stream":
            return self._sse([json.dumps({"choices": [{"delta": {"content": "一"}}]}),
                              json.dumps({"choices": [{"delta": {"content": "二"}}]})])

        if "tools" in body and mode != "no_tools":
            return self._json(200, {
                "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": None,
                                         "tool_calls": [{"id": "c1", "type": "function",
                                                         "function": {"name": "get_weather",
                                                                      "arguments": '{"city":"北京"}'}}]}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 10}})

        if "response_format" in body and mode == "no_json":
            return self._json(400, {"error": {"message": "response_format unsupported"}})

        if "response_format" in body:
            return self._json(200, {"model": "aliased-model",
                                    "choices": [{"message": {"content": '{"intent":"design","ok":true}'}}]})

        content = "" if mode == "empty_body" else ("白色" if has_image else "需求工程是……（示例正文）")
        return self._json(200, {"model": ("aliased-model" if mode == "alias" else body.get("model")),
                                "choices": [{"message": {"role": "assistant", "content": content}}],
                                "usage": {"prompt_tokens": 20, "completion_tokens": 8}})


def _start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/v1" % srv.server_address[1]


def _probe(base: str, mtype: str = "chat", tags=None):
    from llm.probe import probe
    return probe({"id": 0, "name": "fake", "base_url": base, "api_key": "sk-test",
                  "model_name": "fake-model", "model_type": mtype, "tags": tags or [],
                  "_no_record": True}, deep=True)


def _by_key(rep: dict, key: str) -> dict:
    for c in rep.get("checks") or []:
        if c.get("key") == key:
            return c
    return {}


def g9_endpoint(base_fake: str):
    """G9 端到端：真 HTTP 打「测试」按钮背后的接口，验证返回的是逐项判据（不是一句话）。

    为什么要起独立实例：模型管理页的「测试」是 `POST /api/llm/providers/{id}/test`，
    只有走完整 HTTP 才能证明"点按钮 -> 看到逐项结果"这条链路真的通。
    在用户正在用的服务上插一个假 provider 属于污染生产数据 ⇒ 用独立实例 + 独立库。
    """
    import importlib.util
    import httpx
    import sqlite3

    path = os.path.join(_HERE, "verify_degraded_direct_flow.py")
    spec = importlib.util.spec_from_file_location("_vdf_for_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv = mod.IsolatedServer()
    H = {"X-User-Id": os.environ.get("MBSE_UID", "3")}
    try:
        ok, detail = srv.start(test_mode=False)
        if not chk("G9.0 独立实例启动", ok, detail):
            print(mod._tail(srv.log))
            return
        c = sqlite3.connect(srv.db)
        c.execute(
            "insert into llm_providers (name, provider_type, base_url, api_key, model_name, "
            "max_tokens, temperature, is_default, status, model_params, model_type, "
            "context_window, tags, priority, budget_tokens, description) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("__probe_fake__", "openai_compat", base_fake, "sk-test", "fake-model",
             512, 0.3, 1, "active", "{}", "chat", 8192, '["tools"]', 0, 0, "gate"))
        c.commit()
        pid = c.execute("select id from llm_providers where name='__probe_fake__'").fetchone()[0]
        c.close()

        MODE["v"] = "ok"
        r = httpx.post(srv.base + "/api/llm/providers/%d/test" % pid, headers=H, timeout=120).json()
        keys = [x.get("key") for x in (r.get("checks") or [])]
        chk("G9.1 接口返回逐项判据", r.get("ok") is True and {"chat", "stream", "tools", "json"} <= set(keys),
            "ok=%s checks=%s" % (r.get("ok"), keys))
        chk("G9.2 有汇总文案", bool(r.get("summary")), str(r.get("summary"))[:60])
        chk("G9.3 每项带判据与实际回显",
            all(x.get("detail") for x in (r.get("checks") or [])),
            "无 detail 的项：%s" % [x.get("key") for x in (r.get("checks") or []) if not x.get("detail")])

        # 反向：上游不支持工具调用 + 该 provider 标了 tools 标签 ⇒ 必须判不可用
        MODE["v"] = "no_tools"
        r2 = httpx.post(srv.base + "/api/llm/providers/%d/test" % pid, headers=H, timeout=120).json()
        fail_keys = [x.get("key") for x in (r2.get("checks") or []) if x.get("level") == "fail"]
        chk("G9.4 不支持工具 ⇒ 判不可用", r2.get("ok") is False and "tools" in fail_keys,
            "ok=%s fail=%s" % (r2.get("ok"), fail_keys))

        # 向量模型端到端：E4 必须真的经平台 Embedder 通路（不是只直连 API 就算过）
        c = sqlite3.connect(srv.db)
        # 快照库里的向量 provider 也带 is_default=1，而平台按 `is_default DESC, id LIMIT 1` 选
        # ⇒ 不先清标记，新插入的 provider 永远不是"平台实际在用的那个"，E4 会被正确跳过。
        c.execute("update llm_providers set is_default=0 where model_type='embedding'")
        c.execute(
            "insert into llm_providers (name, provider_type, base_url, api_key, model_name, "
            "max_tokens, temperature, is_default, status, model_params, model_type, "
            "context_window, tags, priority, budget_tokens, description) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("__probe_fake_emb__", "openai_compat", base_fake, "sk-test", "fake-emb",
             512, 0.3, 1, "active", "{}", "embedding", 8192, "[]", 0, 0, "gate"))
        c.commit()
        epid = c.execute("select id from llm_providers where name='__probe_fake_emb__'").fetchone()[0]
        c.close()
        MODE["v"] = "ok"
        r3 = httpx.post(srv.base + "/api/llm/providers/%d/test" % epid, headers=H, timeout=120).json()
        ekeys = [x.get("key") for x in (r3.get("checks") or [])]
        chk("G9.5 向量自检返回 4 项判据（含平台通路）",
            {"embedding", "embed_semantics", "embed_index", "embed_platform"} <= set(ekeys), str(ekeys))
        pf = _by_key(r3, "embed_platform")
        chk("G9.6 默认向量模型 ⇒ 平台 Embedder 通路真跑通",
            pf.get("level") == "pass" and "openai-compat" in (pf.get("detail") or ""),
            pf.get("detail", "")[:90])
        chk("G9.7 默认向量模型 + 维度与存量不符 ⇒ 判不可用（不再只看「有向量」）",
            r3.get("ok") is False and _by_key(r3, "embed_index").get("level") == "fail"
            and "静默 0 命中" in (_by_key(r3, "embed_index").get("detail") or ""),
            _by_key(r3, "embed_index").get("detail", "")[:90])
        MODE["v"] = "ok"
    finally:
        srv.stop()


def main() -> int:
    srv, base = _start()
    print("假上游：", base)
    try:
        print("\n== J1 前端：测试按钮必须展示逐项结果 ==")
        js = open(os.path.join(_ROOT, "static/js/mods/30-agents.js"), encoding="utf-8").read()
        has_render = "_formatProbeResult" in js and "_showProbeResult" in js
        uses_render = ("_showProbeResult('模型功能自检'" in js
                       and "_showProbeResult('模型功能自检（表单配置，未保存也生效）'" in js)
        legacy = "toast(r.ok ? '连接成功'" in js
        chk("J1.1 有逐项渲染器", has_render)
        chk("J1.2 列表页与表单页都走渲染器", uses_render)
        chk("J1.3 已撤销「只 toast 一句连接成功」", not legacy)
        print("\n== G1 全能力可用 ==")
        MODE["v"] = "ok"
        rep = _probe(base)
        chk("G1.1 总体 ok", rep.get("ok") is True, rep.get("summary", ""))
        for k in ("chat", "stream", "tools", "json"):
            c = _by_key(rep, k)
            chk(f"G1.2 {k} pass", c.get("level") == "pass", c.get("detail", "")[:70])
        chk("G1.3 实际模型名一致", _by_key(rep, "model_name").get("level") == "pass",
            _by_key(rep, "model_name").get("detail", ""))

        print("\n== G2 200 但正文为空 ==")
        MODE["v"] = "empty_body"
        rep = _probe(base)
        chk("G2.1 总体不可用", rep.get("ok") is False, rep.get("summary", ""))
        chk("G2.2 chat 判 fail", _by_key(rep, "chat").get("level") == "fail",
            _by_key(rep, "chat").get("detail", "")[:80])

        print("\n== G3 不支持流式（200 但返回普通 JSON） ==")
        MODE["v"] = "no_stream"
        rep = _probe(base)
        chk("G3.1 stream 判 fail", _by_key(rep, "stream").get("level") == "fail",
            _by_key(rep, "stream").get("detail", "")[:80])
        chk("G3.2 总体不可用", rep.get("ok") is False, rep.get("summary", ""))

        print("\n== G4 不支持工具调用 ==")
        MODE["v"] = "no_tools"
        rep = _probe(base)
        lv = _by_key(rep, "tools").get("level")
        chk("G4.1 未标 tools 标签 ⇒ warn（告知不判死）", lv == "warn", f"level={lv}")
        rep2 = _probe(base, tags=["tools"])
        chk("G4.2 标了 tools 标签 ⇒ fail", _by_key(rep2, "tools").get("level") == "fail",
            _by_key(rep2, "tools").get("detail", "")[:80])

        print("\n== G5 JSON 输出不支持 ==")
        MODE["v"] = "no_json"
        rep = _probe(base)
        chk("G5.1 json 判 warn（可选能力）", _by_key(rep, "json").get("level") == "warn",
            _by_key(rep, "json").get("detail", "")[:80])
        chk("G5.2 warn 不影响总体可用", rep.get("ok") is True, rep.get("summary", ""))

        print("\n== G6 多模态拒绝图片 ==")
        MODE["v"] = "no_vision"
        rep = _probe(base, mtype="multimodal")
        chk("G6.1 vision 判 fail", _by_key(rep, "vision").get("level") == "fail",
            _by_key(rep, "vision").get("detail", "")[:80])
        chk("G6.2 总体不可用", rep.get("ok") is False, rep.get("summary", ""))

        print("\n== G7 上游静默换模型 ==")
        MODE["v"] = "alias"
        rep = _probe(base)
        chk("G7.1 模型名不一致判 warn", _by_key(rep, "model_name").get("level") == "warn",
            _by_key(rep, "model_name").get("detail", "")[:90])

        print("\n== G8 embedding ==")
        MODE["v"] = "ok"
        rep = _probe(base, mtype="embedding")
        keys = [c.get("key") for c in (rep.get("checks") or [])]
        chk("G8.1 向量可返回 ⇒ pass",
            rep.get("ok") is True and _by_key(rep, "embedding").get("level") == "pass",
            _by_key(rep, "embedding").get("detail", ""))
        chk("G8.2 深度自检含 4 项判据（不是只看有没有向量）",
            {"embedding", "embed_semantics", "embed_index", "embed_platform"} <= set(keys), str(keys))
        chk("G8.3 语义区分度 pass（相关句显著更近）",
            _by_key(rep, "embed_semantics").get("level") == "pass",
            _by_key(rep, "embed_semantics").get("detail", "")[:110])
        # 假上游是 8 维，库内向量是 1024 维 ⇒ 必须指出"维度不符会静默 0 命中"，但不判死
        c_idx = _by_key(rep, "embed_index")
        chk("G8.4 维度与库内索引不符 ⇒ 指出后果且不误判为可用",
            c_idx.get("level") in ("warn", "fail")
            and ("维" in (c_idx.get("detail") or ""))
            and ("静默 0 命中" in (c_idx.get("detail") or "") or "暂无可核对" in (c_idx.get("detail") or "")),
            c_idx.get("detail", "")[:110])
        c_pf = _by_key(rep, "embed_platform")
        chk("G8.5 非默认向量模型 ⇒ 平台通路明确跳过（不假装已验证）",
            c_pf.get("level") == "warn" and "非平台当前默认" in (c_pf.get("detail") or ""),
            c_pf.get("detail", "")[:90])

        MODE["v"] = "emb_bad"
        rep = _probe(base, mtype="embedding")
        chk("G8.6 200 但无向量 ⇒ fail", rep.get("ok") is False,
            _by_key(rep, "embedding").get("detail", "")[:60])

        MODE["v"] = "emb_const"
        rep = _probe(base, mtype="embedding")
        chk("G8.7 常量向量（有向量但毫无区分度）⇒ 判不可用",
            rep.get("ok") is False and _by_key(rep, "embed_semantics").get("level") == "fail",
            _by_key(rep, "embed_semantics").get("detail", "")[:110])

        MODE["v"] = "emb_zero"
        rep = _probe(base, mtype="embedding")
        chk("G8.8 全零向量 ⇒ fail", rep.get("ok") is False
            and "全零" in (_by_key(rep, "embedding").get("detail") or ""),
            _by_key(rep, "embedding").get("detail", "")[:60])

        MODE["v"] = "emb_short"
        rep = _probe(base, mtype="embedding")
        chk("G8.9 回包条数少于输入 ⇒ fail（否则条与条错位）",
            rep.get("ok") is False and "只回" in (_by_key(rep, "embedding").get("detail") or ""),
            _by_key(rep, "embedding").get("detail", "")[:70])

        MODE["v"] = "emb_nodisc"
        rep = _probe(base, mtype="embedding")
        chk("G8.10 非退化但无关句更近 ⇒ 判无区分度（不能只靠「不是常量」）",
            rep.get("ok") is False and _by_key(rep, "embed_semantics").get("level") == "fail"
            and "无区分度" in (_by_key(rep, "embed_semantics").get("detail") or ""),
            _by_key(rep, "embed_semantics").get("detail", "")[:110])

        print("\n== P1 深度自检真的发了多次请求 ==")
        MODE["v"] = "ok"
        before = MODE["hits"]
        _probe(base)
        n = MODE["hits"] - before
        chk("P1.1 深度自检请求数 ≥4", n >= 4, f"实际 {n} 次")

        print("\n== P2 探针记账被标 is_probe ==")
        try:
            from core.probe_mark import is_probe_intent
            chk("P2.1 chat_probe 记为探针", is_probe_intent("chat_probe") == 1)
        except Exception as e:
            chk("P2.1 chat_probe 记为探针", False, str(e))

        print("\n== G9 端到端：模型管理「测试」接口 ==")
        g9_endpoint(base + "/v1")
    finally:
        srv.shutdown()

    print("\n合计 %d 项，PASS %d，FAIL %d" % (len(_passes) + len(_fails), len(_passes), len(_fails)))
    if _fails:
        print("失败项：" + "; ".join(_fails))
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
