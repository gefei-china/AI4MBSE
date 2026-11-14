# -*- coding: utf-8 -*-
# CI-OPTIONAL: 需真实上游与真实服务（直连 DeepSeek 探测 penalty 接受度 + PATCH→DB→调用），
#               且会临时改写 provider 的 model_params（脚本内已 try/finally 还原）；
#               CI 无 secret、也无外网额度，故不进 CI。
"""真机验证：上游是否接受 frequency_penalty / presence_penalty（2026-11-13）。

为什么必须真机：本地 mock 永远发现不了"厂商不认这个键 ⇒ 400 / 静默忽略"。
本脚本分三段：
  Part 1  直连上游：基线 / +frequency_penalty / +frequency+presence 三组对照
  Part 2  走平台链路：HTTP PATCH /api/llm/providers/{id}/params → 读回 DB 确认落库
                     → llm_client.chat 真实调用 → 判定上游接受与否
  Part 3  还原：把 model_params 恢复成原值（**必须**，否则污染用户配置）

判据口径：HTTP 状态码 + 真实响应体，不是"没抛异常"。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402

from llm import resolve_provider_cfg  # noqa: E402

PID = 71
API = "http://127.0.0.1:8000"
H = {"X-User-Id": "111", "Content-Type": "application/json"}

cfg = resolve_provider_cfg(PID)
if not cfg or not cfg.get("base_url"):
    print("!! 读不到 provider %s 配置" % PID)
    sys.exit(2)
url = str(cfg["base_url"]).rstrip("/") + "/chat/completions"
headers = {"Authorization": "Bearer %s" % cfg.get("api_key", ""),
           "Content-Type": "application/json"}
model = cfg.get("model_name")
print("上游：%s ｜ model=%s" % (cfg.get("base_url"), model))
print("=" * 72)

MSGS = [{"role": "user", "content": "用两句话说明 SysML v2 的需求视图是什么。"}]

CASES = [
    ("基线（不带 penalty）", {}),
    ("+frequency_penalty=0.5", {"frequency_penalty": 0.5}),
    ("+frequency=0.5 +presence=0.3（DeepSeek 不支持 presence 的极端测试）",
     {"frequency_penalty": 0.5, "presence_penalty": 0.3}),
]

results = {}
for name, extra in CASES:
    payload = {"model": model, "messages": MSGS, "max_tokens": 64, "temperature": 0.3}
    payload.update(extra)
    t0 = time.time()
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=90)
        dt = time.time() - t0
        ok = r.status_code == 200
        results[name] = (r.status_code, dt, ok)
        print("  [%s] %s -> HTTP %s  %.1fs" % ("200" if ok else "ERR", name, r.status_code, dt))
        if not ok:
            print("        body: %s" % r.text[:300].replace("\n", " "))
        else:
            try:
                ch = (r.json().get("choices") or [{}])[0]
                txt = (ch.get("message") or {}).get("content") or ""
                print("        输出片段: %s" % txt[:60].replace("\n", " "))
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        dt = time.time() - t0
        results[name] = ("EXC", dt, False)
        print("  [ERR] %s -> 异常 %.1fs: %s" % (name, dt, e))

print("=" * 72)
print("Part 2：走平台链路（HTTP PATCH → DB → llm_client.chat）")
import sqlite3  # noqa: E402

DB = os.path.join(ROOT, "mbse.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
orig_mp = con.execute("SELECT model_params FROM llm_providers WHERE id=?", (PID,)).fetchone()[0]
print("  原 model_params = %s" % orig_mp)
try:
    new_mp = {"top_p": None, "top_k": None, "thinking_mode": "模型默认",
              "frequency_penalty": 0.5, "presence_penalty": 0.3,
              "penalty_support": "all"}
    r = httpx.patch("%s/api/llm/providers/%d/params" % (API, PID),
                    json={"model_params": new_mp}, headers=H, timeout=30)
    print("  PATCH /params -> HTTP %s" % r.status_code)
    back = con.execute("SELECT model_params FROM llm_providers WHERE id=?", (PID,)).fetchone()[0]
    print("  DB 回读 model_params = %s" % back)
    stored = json.loads(back or "{}")
    print("  落库判定: frequency_penalty=%r presence_penalty=%r penalty_support=%r"
          % (stored.get("frequency_penalty"), stored.get("presence_penalty"),
             stored.get("penalty_support")))

    from llm import llm_client  # noqa: E402
    t0 = time.time()
    try:
        resp = llm_client.chat(MSGS, provider_id=PID, _intent="verify_penalty_upstream")
        txt = (resp["choices"][0]["message"].get("content") or "")
        print("  [200] 平台链路调用成功 %.1fs  输出片段: %s"
              % (time.time() - t0, txt[:60].replace("\n", " ")))
        print("  ⇒ 上游接受该 payload（含 frequency_penalty + presence_penalty）")
    except Exception as e:  # noqa: BLE001
        print("  [ERR] 平台链路调用失败: %s: %s" % (type(e).__name__, e))
finally:
    r = httpx.patch("%s/api/llm/providers/%d/params" % (API, PID),
                    json={"model_params": json.loads(orig_mp or "{}")}, headers=H, timeout=30)
    now = con.execute("SELECT model_params FROM llm_providers WHERE id=?", (PID,)).fetchone()[0]
    print("=" * 72)
    print("已还原 model_params：%s（restore HTTP %s）" % (now, r.status_code))
    print("还原一致: %s" % (now == orig_mp))
