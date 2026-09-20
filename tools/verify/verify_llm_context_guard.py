# -*- coding: utf-8 -*-
"""LLM 上下文额度守卫 / 默认 provider 确定性 的自检脚本（2026-09-20）。

背景（遗留项 7.1「provider context_window 是假天花板」）：
  `llm/providers/openai_compat.py` 原有一句**静默**截断（`mt` 一旦超过 `cw` 就赋成 `cw`），
  把 DB `llm_providers.context_window` 当硬上限。但实测该值可能只是**保守配置而非模型真实
  上限**（id=1 配 cw=8192，上游在 in=6575 + out=5841 = 12416 时仍 200 返回）→ 后果是
  **静默压低输出上限，且现象上与「模型本来就写不长」无法区分**。
  另：`core/token_counter.input_budget`（ctx - mt - safety）此前**全仓无生产调用点**（死代码）。
  另：默认 provider 查询无 `ORDER BY` → 多行 is_default=1 时取值不确定。

本次改动把上述三处收敛为「可配置 + 留痕 + 接线」：见 `llm.context_window_guard`。

本脚本**不依赖 git ref、不依赖服务、不联网、不写业务库**（httpx 被猴补为离线探针）：
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_llm_context_guard.py

口径提示：
- [1][2][3] 为**行为级**断言（离线探针捕获真实发出的 payload + 捕获 logger 记录）；
- [4] 为**夹具驱动**（临时库 + 猴补 database.get_db），断言的是「机制」，与本次部署的
  真实 provider 配置无关 —— 故改 DB 里的 priority / is_default **不会**影响通过数；
- 断言**不得空转**：[2.1] 同时断言告警文案里出现由 `input_budget(8192, 8192, 512)` 算出的
  `1024` —— 这既证明「留痕真的发生了」，也证明 `input_budget` **真的被生产链路调用**（不再死代码）。
  变异测试（把截断改回静默）必须让 [2.1] FAIL，否则本脚本无效。
"""
import inspect
import logging
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_n_pass = 0
_n_fail = 0


def chk(name, cond, extra=""):
    global _n_pass, _n_fail
    if cond:
        _n_pass += 1
        print("  PASS %s%s" % (name, ("  | " + str(extra)) if extra else ""))
    else:
        _n_fail += 1
        print("  FAIL %s%s" % (name, ("  | " + str(extra)) if extra else ""))
    return bool(cond)


# ── 离线探针：替换 httpx.post，捕获真正发出去的 payload（不联网）────────────────
import httpx  # noqa: E402

_captured = {}


class _FakeResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}


def _cap_post(url, json=None, headers=None, timeout=None):
    _captured.clear()
    _captured.update(json or {})
    return _FakeResp()


_orig_post = httpx.post
httpx.post = _cap_post

# ── logger 捕获：挂在 mbse.llm（本模块的日志通道）上 ────────────────────────────
from llm.providers import openai_compat as _oac  # noqa: E402
from llm.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from core import config as _cfg  # noqa: E402


class _Cap(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.msgs = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


_cap = _Cap()
_lg = logging.getLogger("mbse.llm")
_lg.addHandler(_cap)
_lg.setLevel(logging.WARNING)

_ORIG_GUARD = _cfg.get("llm", "context_window_guard", None)


def _set_guard(v):
    _cfg._CONFIG.setdefault("llm", {})["context_window_guard"] = v


def _reset():
    _cap.msgs[:] = []
    _oac._ctx_warned.clear()


def _cfg_for(cw, mt):
    return {"id": 7001, "name": "probe-ctx", "base_url": "http://probe.local/v1",
            "api_key": "k", "model_name": "probe-model", "max_tokens": mt,
            "context_window": cw, "temperature": 0.3}


MSG = [{"role": "user", "content": "x"}]

try:
    print("\n[1] 三档守卫（行为级：以探针实收的 max_tokens 为准）")
    _reset(); _set_guard("clamp")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    p.chat(MSG, max_tokens=999999)
    chk("1.1 guard=clamp（默认）→ 超窗截到 cw（= 改动前行为）", _captured.get("max_tokens") == 8192,
        "实收=%s" % _captured.get("max_tokens"))

    _reset(); _set_guard("clamp")
    p = OpenAICompatProvider(_cfg_for(65536, 8192))
    p.chat(MSG, max_tokens=999999)
    chk("1.2 guard=clamp + cw=65536（解锁后真实值）→ 实收 65536", _captured.get("max_tokens") == 65536,
        "实收=%s" % _captured.get("max_tokens"))

    _reset(); _set_guard("warn")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    p.chat(MSG, max_tokens=999999)
    chk("1.3 guard=warn → **不截断**（放开假天花板）", _captured.get("max_tokens") == 999999,
        "实收=%s" % _captured.get("max_tokens"))

    _reset(); _set_guard("off")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    p.chat(MSG, max_tokens=999999)
    chk("1.4 guard=off → 不介入（静默）", _captured.get("max_tokens") == 999999,
        "实收=%s" % _captured.get("max_tokens"))

    _reset(); _set_guard("不是合法值")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    p.chat(MSG, max_tokens=999999)
    chk("1.5 guard 取到非法值 → 回落 clamp（保守，不放行）", _captured.get("max_tokens") == 8192,
        "实收=%s" % _captured.get("max_tokens"))

    print("\n[2] 留痕可见（关键：不得静默）")
    _reset(); _set_guard("clamp")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    p.chat(MSG, max_tokens=999999)
    _hit = [m for m in _cap.msgs if "max_tokens 超过 context_window" in m]
    chk("2.1 clamp 超窗 → 有 WARNING 留痕", len(_hit) == 1, "条数=%d" % len(_hit))
    chk("2.1b 留痕含真实 context_window / 生效 max_tokens",
        bool(_hit) and "context_window=8192" in _hit[0] and "max_tokens生效=8192" in _hit[0],
        _hit[0][:110] if _hit else "-")
    # ↓ 这条同时证明 input_budget 真的被生产链路调用（不再死代码）：
    #   input_budget(8192, 8192, 512) = max(8192-8192-512, 1024) = 1024（触到保底）
    chk("2.1c 留痕里的「输入余量」== input_budget(8192,8192,512) == 1024（证明 input_budget 已接线）",
        bool(_hit) and "输入余量=1024" in _hit[0], _hit[0][-90:] if _hit else "-")
    chk("2.1d 留痕带估算输入（证明 count_messages_tokens 已接线）",
        bool(_hit) and "估算输入=" in _hit[0], "")
    chk("2.1e 留痕给出放开办法（guard=warn/off）", bool(_hit) and "warn/off" in _hit[0], "")

    _before = len(_cap.msgs)
    p.chat(MSG, max_tokens=999999)
    chk("2.2 同 (provider, cw, guard) 第二次调用不重复告警（去重生效）",
        len(_cap.msgs) == _before, "增量=%d" % (len(_cap.msgs) - _before))

    _reset(); _set_guard("warn")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    p.chat(MSG)  # 不传 max_tokens → 取 DB max_tokens=8192；估输入 + 8192 > 8192 → 必然超窗
    _hit2 = [m for m in _cap.msgs if "估算输入 + max_tokens 超过 context_window" in m]
    chk("2.3 warn 且「估算输入+输出」必然超窗 → 有留痕（这一情形原先完全不可见）",
        len(_hit2) == 1, "条数=%d" % len(_hit2))

    _reset(); _set_guard("off")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    p.chat(MSG, max_tokens=999999)
    chk("2.4 guard=off → 一条日志都不产生", len(_cap.msgs) == 0, "条数=%d" % len(_cap.msgs))

    print("\n[3] _meta 额度留痕（响应侧可审计）")
    _reset(); _set_guard("clamp")
    p = OpenAICompatProvider(_cfg_for(8192, 8192))
    r = p.chat(MSG, max_tokens=999999)
    m = r.get("_meta") or {}
    chk("3.1 _meta 含 context_window / max_tokens_effective / est_input_tokens / context_guard",
        all(k in m for k in ("context_window", "max_tokens_effective", "est_input_tokens", "context_guard")),
        sorted(m.keys()))
    chk("3.2 _meta.max_tokens_effective 与实收一致",
        m.get("max_tokens_effective") == _captured.get("max_tokens") == 8192,
        "%s vs %s" % (m.get("max_tokens_effective"), _captured.get("max_tokens")))
    chk("3.3 _meta 既有键未被破坏（provider/model/used_mock/latency_ms）",
        all(k in m for k in ("provider", "model", "used_mock", "latency_ms")), "")

    print("\n[4] 默认 provider 选取的确定性（夹具驱动；与本次部署真实配置无关）")
    import database

    _DDL = ("CREATE TABLE llm_providers (id INTEGER PRIMARY KEY, name TEXT, provider_type TEXT, "
            "base_url TEXT, api_key TEXT, model_name TEXT, max_tokens INTEGER, temperature REAL, "
            "is_default INTEGER, model_type TEXT, context_window INTEGER, tags TEXT, "
            "priority INTEGER, budget_tokens INTEGER, status TEXT)")

    def _mkdb(rows):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        c = sqlite3.connect(path)
        c.execute(_DDL)
        c.executemany("INSERT INTO llm_providers (id,name,provider_type,model_type,is_default,"
                      "priority,status,tags) VALUES (?,?,?,?,?,?,?,?)", rows)
        c.commit()
        c.close()
        return path

    def _patch(path):
        def _mk():
            cc = sqlite3.connect(path)
            cc.row_factory = sqlite3.Row
            return cc
        database.get_db = _mk

    _orig_get_db = database.get_db
    from llm import LLMClient, _load_provider_cfg
    _cli = LLMClient()

    try:
        # id=1 priority=10 / id=2 priority=90：rowid 序会给出 1，priority 序给出 2
        pth = _mkdb([(1, "A", "openai", "chat", 1, 10, "active", "[]"),
                     (2, "B", "openai", "chat", 1, 90, "active", "[]")])
        _patch(pth)
        got = _cli.get_default_provider()
        chk("4.1 两行 is_default=1 → 取 priority 最高者（不是 rowid 最小者）",
            got is not None and got["id"] == 2, "得 id=%s" % (got or {}).get("id"))

        # 把 priority 对调 → 必须换成另一行。两条合起来才证明判据是 priority，而非 id/rowid
        pth2 = _mkdb([(1, "A", "openai", "chat", 1, 90, "active", "[]"),
                      (2, "B", "openai", "chat", 1, 10, "active", "[]")])
        _patch(pth2)
        got2 = _cli.get_default_provider()
        chk("4.2 priority 对调后选中另一行（证明判据确为 priority）",
            got2 is not None and got2["id"] == 1, "得 id=%s" % (got2 or {}).get("id"))

        pth3 = _mkdb([(1, "A", "openai", "chat", 1, 10, "active", "[]"),
                      (2, "B", "openai", "chat", 0, 90, "active", "[]")])
        _patch(pth3)
        got3 = _cli.get_default_provider()
        chk("4.3 只有一行 default 时，priority 更高的非 default 行**不得**被选中（零回归）",
            got3 is not None and got3["id"] == 1, "得 id=%s" % (got3 or {}).get("id"))

        cc = sqlite3.connect(pth3)
        cc.row_factory = sqlite3.Row
        row = _load_provider_cfg(cc, None)
        cc.close()
        chk("4.4 _load_provider_cfg(None) 与 get_default_provider 同源同选",
            row.get("id") == 1, "得 id=%s" % row.get("id"))

        gc = sqlite3.connect(pth)
        gc.row_factory = sqlite3.Row
        n = 0
        for _ in range(5):
            n = (_load_provider_cfg(gc, None) or {}).get("id") or n
        gc.close()
        chk("4.5 重复 5 次取值稳定（无 ORDER BY 时该断言会随机挂）", n == 2, "得 id=%s" % n)
    finally:
        database.get_db = _orig_get_db
        for _p in (locals().get("pth"), locals().get("pth2"), locals().get("pth3")):
            try:
                if _p:
                    os.remove(_p)
            except Exception:
                pass

    print("\n[5] 静态哨兵：防「改回静默」（射程精确到该分支，不扫全文件）")
    _src = inspect.getsource(OpenAICompatProvider.chat)
    if "if mt > cw:" in _src and "payload = {" in _src:
        _seg = _src.split("if mt > cw:")[1].split("payload = {")[0]
        chk("5.1 `if mt > cw:` 分支内必须调用 _warn_context_once（留痕不可省）",
            "_warn_context_once(" in _seg, "")
    else:
        chk("5.1 源码结构可比对", False, "未找到 `if mt > cw:` / `payload = {`")
    _mod_src = inspect.getsource(_oac)
    chk("5.2 守卫读取入口 _ctx_guard 仍在（配置化未被打回硬编码）", "_ctx_guard(" in _mod_src, "")
    chk("5.3 input_budget 已被本模块引用（死代码已接线）", "input_budget" in _mod_src, "")

    print("\n[6] 配置键登记")
    chk("6.1 DEFAULT_CONFIG.llm.context_window_guard == 'clamp'（默认零回归）",
        _cfg.DEFAULT_CONFIG.get("llm", {}).get("context_window_guard") == "clamp",
        _cfg.DEFAULT_CONFIG.get("llm", {}).get("context_window_guard"))
    chk("6.2 CONFIG_SCHEMA 有该键（否则配置面板无法暴露）",
        "context_window_guard" in _cfg.CONFIG_SCHEMA.get("llm", {}), "")
finally:
    httpx.post = _orig_post
    _lg.removeHandler(_cap)
    if _ORIG_GUARD is None:
        _cfg._CONFIG.setdefault("llm", {}).pop("context_window_guard", None)
    else:
        _set_guard(_ORIG_GUARD)

print("\nSUMMARY %d/%d PASS" % (_n_pass, _n_pass + _n_fail))
raise SystemExit(0 if _n_fail == 0 else 1)
