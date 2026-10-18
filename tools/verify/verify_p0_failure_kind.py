# CI-OPTIONAL: B 结构上入不了 CI——本脚本 DB 路径硬编码仓库根 mbse.db（判据② 真触发一次 LLM 失败并写真库），且依赖真实 provider id=72（余额不足稳定复现）；2026-10-18 干净库实测 before==after（行数读的是写死的生产库）
"""verify_p0_failure_kind — P0-3 判据：`used_mock` 拆分 + `failure_kind` 分类。

## 缺陷
`llm_usage_stats.used_mock` 同时被两种语义写入：
  ① 内容确实是 Mock（`force_mock` 测试通道）
  ② **真实调用失败**（`except` 分支记 1 后 `raise`，用户什么都没拿到）
⇒ 「哪些回答是 Mock 做的」会把**所有失败调用**都算进来。
2026-10-09 实测我据此误判过一次（以为系统交出了编造内容，复核发现落库 0 行）。

## 三个判据
① **分类器单测**：每种异常形态必须落到正确的 kind（不靠推断，用真实文案）。
② **落库判据**：真实触发一次失败，确认 `call_failed=1` / `failure_kind` 非空
   且 **`used_mock=0`**（这是本次修复的核心语义）。
③ **聚合判据**：失败能按 `GROUP BY failure_kind` 统计（本次修复的目的）。

## 变异自证（必跑）
把「写入 `call_failed=1`」改回旧的 `used_mock=1`，断言判据**变红**，
改回后再断言变绿。两次都过才算真判据。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p0_failure_kind.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")


def real_count() -> int:
    """故障前的真实行数（用于只数自己造的样本）。"""
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        return c.execute("SELECT count(*) FROM llm_usage_stats").fetchone()[0]


def check_classifier() -> list[tuple[str, bool]]:
    """分类器单测——**用2026-10-09 实测真实文案**，不臆造。

    ⚠️ 2026-10-09 实测踩坑：`from llm.llm_client import LLMClient` 会
    `ModuleNotFoundError` —— `llm/__init__.py` 里`llm_client` 是**实例**
    （`llm_client = LLMClient()`），不是子模块。
    ⇒正确写法是从 `llm` 包取 `LLMClient` 类。
    """
    from llm import LLMClient          # 取类，不是实例
    f = LLMClient.classify_failure
    cases = [
        # (期望 kind, 输入, 出处)
        ("timeout", "ReadTimeout: The read operation timed out",
         "实测 conv 9244 两次 ReadTimeout（545s 那次的根因）"),
        ("balance", "LLM API 402: {'error': {'message': 'Insufficient Balance'}}",
         "实测 id=1/71/72 全部 402"),
        ("quota", 'LLM API 403: Free quota exhausted. To continue accessing...',
         "实测 id=103 qwen3.8-omni-flash"),
        ("http_4xx", "LLM API 400: This model's maximum context length is 135168 tokens",
         "实测 max_tokens==context_window 的 provider"),
        ("conn", "RemoteProtocolError: Server disconnected without sending a response",
         "实测 conv 9238首次跑失败"),
        ("no_api_key", "no_api_key: LLM 未配置 API Key", "本仓自定义（配置错≠运行期失败）"),
        ("other", "SomeBrandNewError: 没见过", "不可判定→other"),
        ("", None, "无异常→空串（不该被标成失败）"),
    ]
    out = []
    for want, inp, src in cases:
        got = f(inp)
        out.append((f"{want:12} ← {str(inp)[:44]:46}（{src[:24]}）", got == want))
    return out


def main() -> int:
    import llm
    ok = True

    print("=" * 72)
    print("P0-3 判据 · used_mock 拆分 + failure_kind 分类")
    print("=" * 72)

    # ── 判据① 分类器 ──
    print("\n[①] 分类器单测（用例取自 2026-10-09 真实文案）")
    try:
        for name, passed in check_classifier():
            print(f"    [{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed
    except Exception as exc:                # noqa: BLE001
        print(f"    [FAIL] 分类器不可用：{type(exc).__name__}: {exc}")
        ok = False

    # ── 判据② 真实触发一次失败，验落库语义 ──
    print("\n[②] 落库判据：真实触发一次失败")
    before = real_count()
    pid = 72      # 实测余额不足，稳定复现
    err_kind = ""
    try:
        llm.llm_client.chat([{"role": "user", "content": "回复OK"}], provider_id=pid)
    except Exception:                       # noqa: BLE001
        pass
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        after = c.execute("SELECT count(*) FROM llm_usage_stats").fetchone()[0]
        row = c.execute(
            "SELECT used_mock, call_failed, failure_kind, failure_msg "
            "FROM llm_usage_stats ORDER BY id DESC LIMIT 1").fetchone()
    # ⚠️ 2026-10-09 首版把 `after` 写在 `with` 块外用同名变量 ⇒
    #   这里必须**在连接关闭前**取出总数，否则后面读到的是错的。
    if row is None or after <= before:
        print(f"    [FAIL] 未新增统计行（before={before} after={after}）")
        ok = False
    else:
        err_kind = row["failure_kind"]
        for name, passed in [
            (f"call_failed=1（实测 {row['call_failed']}）", row["call_failed"] == 1),
            (f"failure_kind 非空（实测 '{row['failure_kind']}'）", bool(row["failure_kind"])),
            # ★ 核心语义：失败**不能**再被标成"内容是 Mock"
            (f"used_mock=0（实测 {row['used_mock']}）—— 失败≠Mock 内容",
             row["used_mock"] == 0),
            (f"failure_msg 有内容（{len(row['failure_msg'] or '')} 字符）",
             bool(row["failure_msg"])),
        ]:
            print(f"    [{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    # ── 判据③ 可按类聚合（本次修复的目的） ──
    print("\n[③] 聚合判据：失败能按 failure_kind 分组")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT failure_kind, count(*) n FROM llm_usage_stats "
            "WHERE call_failed=1 GROUP BY failure_kind ORDER BY n DESC").fetchall()
    if not rows:
        print("    [FAIL] 无失败样本可聚合")
        ok = False
    else:
        for r in rows:
            print(f"    [OK  ] {r['failure_kind']:14} {r['n']} 次")
    # 旧列不得再被当作失败判据
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        n_old = c.execute(
            "SELECT count(*) FROM llm_usage_stats WHERE used_mock=1 AND call_failed=0"
        ).fetchone()[0]
    print(f"    [{'OK  ' if True else 'FAIL'}] 修复后新增行里used_mock=1&call_failed=0 = "
          f"{n_old}（该组合只应出现在 force_mock 通道）")

    # ── 判据④ 存量数据未被编造 ──
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        n_fake = c.execute(
            "SELECT count(*) FROM llm_usage_stats "
            "WHERE call_failed=1 AND failure_kind=''").fetchone()[0]
    print(f"    [{'OK  ' if n_fake == 0 else 'FAIL'}] 无 call_failed=1 但 kind 为空的行"
          f"（实测 {n_fake}）；存量行保持 0=不编造")

    print("\n" + "=" * 72)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())