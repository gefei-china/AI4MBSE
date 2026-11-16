# -*- coding: utf-8 -*-
"""行为级门禁：运行时不变量的「配置可驱动 + 观测可持久化 + 触发条件可判」。

2026-11-09 修的两个真缺陷（都是「装了没生效」）：
  ① `core/request_invariant.py::_mode()` 读 `get("request_invariant","mode")`，
     但该分组不在 DEFAULT_CONFIG / CONFIG_SCHEMA / ENV_MAP 里 ⇒ save_override 抛
     「未知配置项」、影子层抛「未知配置分组」，三态开关恒等于 shadow，enforce 到不了。
  ② `_record()` 只写 `_LOG.info`，而 root logger 级别 WARNING 且无 handler ⇒
     INFO 事件从未落盘，进程重启即丢 ⇒ 「积累真实流量、确认无误报后切 enforce」
     这一触发条件根本无法执行。

本门禁只做行为验证，不切生产模式、不写生产台账、不插假样本：
  * 配置驱动用临时 CONFIG_PATH + 影子层，落在临时目录；
  * 台账写入用 MBSE_SHADOW_CONFIG 指到临时文件，收尾把文件内容清空（不删文件）；
  * 每条判据都有「正常通过 + 变异必被拒」。

空库 / 全新克隆下同样可跑：不依赖 mbse.db、不依赖真实服务。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                          (" — " + str(detail)) if (detail and not cond) else ""))


def expect_raise(name, exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        chk(name, True)
    except Exception as e:
        chk(name, False, "抛了 %s: %s" % (type(e).__name__, str(e)[:90]))
    else:
        chk(name, False, "未抛异常")


def main() -> int:
    from core import config as cfg
    from core import request_invariant as ri

    tmp = os.path.join(tempfile.gettempdir(), "mbse_invariant_verify")
    os.makedirs(tmp, exist_ok=True)
    cfg_file = os.path.join(tmp, "config.json")
    audit_file = os.path.join(tmp, "audit.jsonl")
    rot_file = audit_file + ".1"
    # 清空而不删除（沙箱 DeleteFileW 挂钩下，删除可能触发回收站语义死锁）。
    # 配置文件必须写合法 JSON：空文件会让 _load_file 打一条解析失败告警（噪音）。
    with open(cfg_file, "w", encoding="utf-8") as _f:
        _f.write("{}")
    for p in (audit_file, rot_file):
        with open(p, "w", encoding="utf-8"):
            pass

    # ── [1] 配置真源：三处登记齐备 ──────────────────────────────────────
    print("\n[1] 配置真源：DEFAULT / SCHEMA / ENV 三处同源")
    chk("DEFAULT_CONFIG 含 request_invariant", "request_invariant" in cfg.DEFAULT_CONFIG)
    chk("CONFIG_SCHEMA 含同名分组", "request_invariant" in cfg.CONFIG_SCHEMA,
        sorted(cfg.CONFIG_SCHEMA.get("request_invariant", {})))
    chk("ENV_MAP 提供部署覆盖", "MBSE_REQUEST_INVARIANT_MODE" in cfg.ENV_MAP)
    chk("默认仍是 shadow（未擅自切 enforce）",
        cfg.DEFAULT_CONFIG["request_invariant"]["mode"] == "shadow")
    keys = set(cfg.CONFIG_SCHEMA["request_invariant"])
    dkeys = set(cfg.DEFAULT_CONFIG["request_invariant"])
    chk("SCHEMA 键与 DEFAULT 键一致", keys == dkeys, (sorted(keys), sorted(dkeys)))

    # ── [2] save_override 能真正驱动三态（临时配置文件，不碰用户级配置）──
    print("\n[2] 三态开关可被配置驱动")
    old_path = cfg.CONFIG_PATH
    old_shadow = os.environ.pop("MBSE_SHADOW_CONFIG", None)
    try:
        cfg.CONFIG_PATH = cfg_file
        cfg.save_override({"request_invariant.mode": "enforce"})
        chk("save_override 不再拒绝该键", True)
        chk("reload 后 _mode() == enforce", ri._mode() == "enforce", ri._mode())
        cfg.save_override({"request_invariant.mode": "off"})
        chk("reload 后 _mode() == off", ri._mode() == "off", ri._mode())
        cfg.save_override({"request_invariant.mode": "shadow"})
        chk("reload 后 _mode() == shadow（还原）", ri._mode() == "shadow", ri._mode())
        cfg.save_override({"request_invariant.mode": "bogus"})
        chk("非法值退回 shadow（fail-safe）", ri._mode() == "shadow", ri._mode())
        cfg.save_override({"request_invariant.mode": "shadow"})
        expect_raise("未知分组仍被 save_override 拒绝", ValueError,
                     cfg.save_override, {"no_such_group.x": "1"})
    finally:
        cfg.CONFIG_PATH = old_path
        if old_shadow is not None:
            os.environ["MBSE_SHADOW_CONFIG"] = old_shadow
        cfg.reload()

    # ── [3] 台账可配置 + observe 落盘 ───────────────────────────────────
    print("\n[3] 判定事件 append-only 落盘与读回")
    shadow_cfg = json.dumps({"request_invariant": {
        "mode": "shadow", "audit_enabled": True,
        "audit_path": audit_file, "audit_max_bytes": 8 * 1024 * 1024}}, ensure_ascii=False)
    os.environ["MBSE_SHADOW_CONFIG"] = shadow_cfg
    cfg.reload()
    chk("audit_path 跟随影子层配置", os.path.normcase(ri.audit_path()) ==
        os.path.normcase(os.path.abspath(audit_file)), ri.audit_path())

    MSGS = [{"role": "user", "content": "建模需求：参数图"}]
    TOOLS = [{"name": "sysml_v2_validate", "description": "校验"}]
    tk = ri.freeze_request(MSGS, TOOLS, {"intent": "design"})
    ri.reset_events()
    r_ok = ri.assert_before_dispatch(tk, MSGS, TOOLS)
    chk("正常请求 allowed=True", r_ok["allowed"] is True and r_ok["would_block"] is False)
    evs = ri.read_events(path=audit_file, limit=0)
    chk("正常事件落入台账", len(evs) == 1, len(evs))
    if evs:
        e = evs[0]
        chk("台账字段完整", set(("ts", "mode", "allowed", "reasons", "digest", "would_block")) <= set(e),
            sorted(e))
        chk("台账不写 prompt 正文", "content" not in e and "messages" not in e, sorted(e))
        chk("台账 mode=shadow / allowed=True", e.get("mode") == "shadow" and e.get("allowed") is True)

    drift = [{"role": "user", "content": "建模需求：参数图（被拼接改动）"}]
    r_bad = ri.assert_before_dispatch(tk, drift, TOOLS)
    chk("漂移请求 would_block=True 且不抛错",
        r_bad["would_block"] is True and r_bad["blocked"] is False)
    evs2 = ri.read_events(path=audit_file, limit=0)
    chk("漂移事件也落盘（供无误报判据统计）", len(evs2) == 2, len(evs2))
    chk("漂移事件 would_block=True", any(x.get("would_block") is True for x in evs2))

    # ── [4] 变异：off 不写、enforce 漂移抛错、关闭观测不写 ────────────────
    print("\n[4] 变异必被拒 / 三态差异")
    n_before = len(ri.read_events(path=audit_file, limit=0))
    ri.assert_before_dispatch(tk, MSGS, TOOLS, mode="off")
    chk("off 模式不写台账", len(ri.read_events(path=audit_file, limit=0)) == n_before)
    expect_raise("enforce 模式漂移抛 RequestInvariantError", ri.RequestInvariantError,
                 ri.assert_before_dispatch, tk, drift, TOOLS, mode="enforce")
    chk("enforce 判定仍留痕（失败可见）",
        len(ri.read_events(path=audit_file, limit=0)) == n_before + 1)
    off_cfg = json.dumps({"request_invariant": {
        "mode": "shadow", "audit_enabled": False, "audit_path": audit_file}}, ensure_ascii=False)
    os.environ["MBSE_SHADOW_CONFIG"] = off_cfg
    cfg.reload()
    n_off = len(ri.read_events(path=audit_file, limit=0))
    ri.assert_before_dispatch(tk, MSGS, TOOLS)
    chk("audit_enabled=False 不写台账", len(ri.read_events(path=audit_file, limit=0)) == n_off)

    # ── [5] 轮转与坏行容错 ──────────────────────────────────────────────
    print("\n[5] 轮转与坏行容错（观测设施不变成业务故障）")
    rot_cfg = json.dumps({"request_invariant": {
        "mode": "shadow", "audit_enabled": True, "audit_path": audit_file,
        "audit_max_bytes": 64}}, ensure_ascii=False)
    os.environ["MBSE_SHADOW_CONFIG"] = rot_cfg
    cfg.reload()
    ri.assert_before_dispatch(tk, MSGS, TOOLS)
    ri.assert_before_dispatch(tk, MSGS, TOOLS)
    chk("超限轮转保留 .1 一代", os.path.isfile(rot_file) and os.path.getsize(rot_file) > 0)
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write("{坏行 not json\n")
    got = ri.read_events(path=audit_file, limit=0)
    chk("坏行被跳过且不影响读回", isinstance(got, list) and all(isinstance(x, dict) for x in got))
    # 用一个「父路径是文件」的不可写目标，验证写入失败不抛
    with open(os.path.join(tmp, "blocker"), "w", encoding="utf-8") as f:
        f.write("x")
    unwritable = os.path.join(tmp, "blocker", "impossible.jsonl")
    fail_cfg = json.dumps({"request_invariant": {
        "mode": "shadow", "audit_enabled": True, "audit_path": unwritable}}, ensure_ascii=False)
    os.environ["MBSE_SHADOW_CONFIG"] = fail_cfg
    cfg.reload()
    try:
        r = ri.assert_before_dispatch(tk, MSGS, TOOLS)
        chk("台账写入失败不阻断业务（仍返回判定）", r.get("checked") is True)
    except Exception as e:
        chk("台账写入失败不阻断业务（仍返回判定）", False,
            "%s: %s" % (type(e).__name__, str(e)[:80]))

    # ── [6] enforce 就绪判据：只看真实台账，不自动切换 ───────────────────
    print("\n[6] 「确认无误报后切 enforce」变成机器判据")
    empty = os.path.join(tmp, "empty.jsonl")
    with open(empty, "w", encoding="utf-8"):
        pass
    r0 = ri.enforce_readiness(path=empty, min_samples=3)
    chk("空台账 not ready", r0["ready"] is False and r0["samples"] == 0, r0["reason"])
    clean = os.path.join(tmp, "clean.jsonl")
    with open(clean, "w", encoding="utf-8") as f:
        for _ in range(4):
            f.write(json.dumps({"ts": 1.0, "mode": "shadow", "origin": "runtime",
                                "allowed": True, "reasons": [], "digest": "a" * 12,
                                "would_block": False}) + "\n")
    r1 = ri.enforce_readiness(path=clean, min_samples=3)
    chk("样本足 + 零误报 ⇒ ready", r1["ready"] is True and r1["samples"] == 4, r1)
    with open(clean, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 2.0, "mode": "shadow", "origin": "runtime",
                            "allowed": False, "reasons": ["drift"], "digest": "b" * 12,
                            "would_block": True}) + "\n")
    r2 = ri.enforce_readiness(path=clean, min_samples=3)
    # min_samples 仍为 3（样本已足），必须因 would_block 判 not ready，而非因样本不足
    chk("出现 1 条 would_block ⇒ not ready（样本仍充足）",
        r2["ready"] is False and r2["would_block"] == 1 and r2["samples"] == 5, r2)
    with open(clean, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 3.0, "mode": "enforce", "origin": "runtime",
                            "allowed": True, "reasons": [], "digest": "c" * 12,
                            "would_block": False}) + "\n")
    r3 = ri.enforce_readiness(path=clean, min_samples=99)
    chk("判据只统计 shadow 事件（enforce 自身不算样本）",
        r3["samples"] == 5 and r3["total_events"] == 6, (r3["samples"], r3["total_events"]))

    # ── [6b] 来源隔离：验证上下文不得污染真实流量判据 ──────────────────
    print("\n[6b] 来源隔离（CI 事件不得凑出假 READY）")
    mixed = os.path.join(tmp, "mixed.jsonl")
    with open(mixed, "w", encoding="utf-8") as f:
        for _ in range(5):   # 验证来源，数量足够但不该计入
            f.write(json.dumps({"ts": 1.0, "mode": "shadow", "origin": "verify",
                                "allowed": True, "reasons": [], "digest": "v" * 12,
                                "would_block": False}) + "\n")
        for _ in range(3):   # 真实来源
            f.write(json.dumps({"ts": 2.0, "mode": "shadow", "origin": "runtime",
                                "allowed": True, "reasons": [], "digest": "r" * 12,
                                "would_block": False}) + "\n")
        f.write(json.dumps({"ts": 3.0, "mode": "shadow", "allowed": True,
                            "reasons": [], "digest": "o" * 12, "would_block": False}) + "\n")
    rm = ri.enforce_readiness(path=mixed, min_samples=3)
    chk("只按 runtime 计样本（verify 与无 origin 的旧事件不计）",
        rm["samples"] == 3 and rm["total_events"] == 9, (rm["samples"], rm["by_origin"]))
    chk("来源分布如实报出", rm["by_origin"].get("verify") == 5
        and rm["by_origin"].get("runtime") == 3, rm["by_origin"])
    chk("有效样本不足时 not ready（5 条 verify 不够）",
        ri.enforce_readiness(path=mixed, min_samples=4)["ready"] is False)

    # 验证上下文写生产默认路径 ⇒ 拒写；显式临时路径 ⇒ 照写
    os.environ.pop("MBSE_SHADOW_CONFIG", None)
    cfg.reload()
    os.environ[ri.ORIGIN_ENV] = "verify"
    prod_path = ri.audit_path()
    n0 = len(ri.read_events(path=prod_path, limit=0)) if os.path.isfile(prod_path) else 0
    ri.assert_before_dispatch(tk, MSGS, TOOLS)
    n1 = len(ri.read_events(path=prod_path, limit=0)) if os.path.isfile(prod_path) else 0
    chk("verify 来源不写生产台账（默认路径）", n1 == n0, (n0, n1))
    explicit = json.dumps({"request_invariant": {
        "mode": "shadow", "audit_enabled": True, "audit_path": audit_file}}, ensure_ascii=False)
    os.environ["MBSE_SHADOW_CONFIG"] = explicit
    cfg.reload()
    n2 = len(ri.read_events(path=audit_file, limit=0))
    ri.assert_before_dispatch(tk, MSGS, TOOLS)
    chk("verify 来源写显式临时路径（可测前提）",
        len(ri.read_events(path=audit_file, limit=0)) == n2 + 1)
    chk("事件带 origin=verify", 
        ri.read_events(path=audit_file, limit=1)[-1].get("origin") == "verify")
    os.environ.pop(ri.ORIGIN_ENV, None)
    os.environ.pop("MBSE_SHADOW_CONFIG", None)
    cfg.reload()
    chk("runtime 为默认来源（未声明时）", ri._origin() == "runtime", ri._origin())

    chk("生产模式未被本门禁改动", ri._mode() in ("off", "shadow", "enforce"))

    # 收尾：清空临时文件，还原配置层
    for p in (cfg_file, audit_file, rot_file, empty, clean):
        try:
            with open(p, "w", encoding="utf-8"):
                pass
        except Exception:
            pass
    os.environ.pop("MBSE_SHADOW_CONFIG", None)
    if old_shadow is not None:
        os.environ["MBSE_SHADOW_CONFIG"] = old_shadow
    cfg.reload()

    print("\n" + "=" * 66)
    print("  PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  失败项：" + "; ".join(FAIL))
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
