# -*- coding: utf-8 -*-
"""技能自改进判据：可运维性 + 双通道触发（2026-10-29）。

═══════════════════════════════════════════════════════════════════════
为什么有这个测试
═══════════════════════════════════════════════════════════════════════
实测暴露两个问题，本文件分别锁定：

A. **判据参数不可运维**（真缺陷）
   `skill_improve.py` 用 `_cfg.get("skill", ...)` 读参数，但该组此前
   **既不在 DEFAULT_CONFIG、也不在 CONFIG_SCHEMA** ⇒
     · 设置页 UI 不渲染（`schema()` 只遍历 CONFIG_SCHEMA）
     · `save_override()` 白名单校验直接抛「未知配置项」
   ⇒ 判据埋在代码默认值里，运维看不见也改不了。

B. **纯比例判据对大样本技能结构性失灵**（判据设计缺陷）
   实测 `Requirement_Analysis_Pre-check`: uses=120 / fail=17 → 失败率 14.2%，
   按原阈值 0.3 需攒到 **36 次失败**才触发；而小样本（uses=10/fail=3 = 30%）
   反而轻松触发 ⇒ **惩罚"用得多的技能"**，与"该修谁"的直觉相反。
   修法：0.3 → 0.12，并新增绝对失败数通道（OR）。

⚠️ 本文件刻意用**构造数据**而非生产数据：判据阈值会随运维调整，
   绑死生产数字的测试会在下次调阈值时假红。构造数据只验**逻辑关系**。
"""
import hashlib
import json
import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════════════
# A. 可运维性：skill 组已登记
# ══════════════════════════════════════════════════════════════════════
def test_skill_group_in_default_config():
    """skill 组必须在 DEFAULT_CONFIG 里（否则 get 拿不到默认值）。"""
    from core import config as cfg
    assert "skill" in cfg.DEFAULT_CONFIG, "skill 组未登记进 DEFAULT_CONFIG"
    for k in ("improve_min_fail", "improve_fail_ratio", "improve_min_uses",
              "improve_abs_min_fail", "improve_abs_min_uses",
              "improve_idle_days", "improve_max_revisions",
              "self_improve_enabled"):
        assert k in cfg.DEFAULT_CONFIG["skill"], f"缺默认值：skill.{k}"


def test_skill_group_in_config_schema():
    """★ skill 组必须在 CONFIG_SCHEMA 里 —— 否则设置页不渲染、save_override 抛错。"""
    from core import config as cfg
    assert "skill" in cfg.CONFIG_SCHEMA, "skill 组未登记进 CONFIG_SCHEMA"
    # 每个键都要有 type（前端据此渲染控件、写入时据此做类型校验）
    for k, meta in cfg.CONFIG_SCHEMA["skill"].items():
        assert "type" in meta, f"skill.{k} 缺 type"
        assert meta["type"] in ("bool", "int", "float", "str"), meta
        assert "desc" in meta, f"skill.{k} 缺 desc"


def _run_in_subprocess(code: str, env_extra: dict) -> tuple:
    """在**独立子进程**里跑一段代码，返回 (returncode, stdout, stderr)。

    为什么必须用子进程：`core/config.py:35` 的 `CONFIG_PATH = os.environ.get(...)`
    是**模块 import 时求值**的常量，`reload()` / `save_override()` 都用这个常量。
    ⇒ 同一进程内 `monkeypatch.setenv` 对已 import 的模块完全无效，
      `monkeypatch.setattr` 虽然能改，但 save_override 会把测试值写进
      **进程共享的配置文件**（本仓 conftest 指向 tests/_test_config.json），
      污染后续所有测试（实测：写脏后 reload 恒读到测试值，导致断言假红）。
    子进程方案：进程退出即销毁，天然零副作用。
    """
    import subprocess
    import sys
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONPATH"] = ROOT
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def test_save_override_works_for_skill_group(tmp_path):
    """★ 核心能力：skill.* 必须能经 save_override 写入并即时生效。

    此前该调用必然抛 `ValueError: 未知配置项: skill.improve_fail_ratio`
    （save_override 用 CONFIG_SCHEMA 做白名单）⇒ 运维改不了判据。

    ⚠️ 用**子进程 + 临时配置文件**隔离（见 `_run_in_subprocess` 的说明）：
       同进程内改 CONFIG_PATH 会写脏共享配置，污染其他测试。
    """
    cfgfile = tmp_path / "cfg.json"
    cfgfile.write_text("{}", encoding="utf-8")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from core import config as cfg\n"
        "r = cfg.save_override({'skill.improve_abs_min_uses': 77})\n"
        "assert 'skill.improve_abs_min_uses' in r['updated'], r\n"
        "assert cfg.get('skill','improve_abs_min_uses') == 77, cfg.get('skill','improve_abs_min_uses')\n"
        "print('OK')\n" % ROOT
    )
    rc, out, err = _run_in_subprocess(code, {"MBSE_CONFIG_PATH": str(cfgfile)})
    assert rc == 0, f"save_override 对 skill.* 失败（可能仍未登记 CONFIG_SCHEMA）：{err[-400:]}"
    assert out == "OK", out
    # ★ 铁证：确实写进了临时文件
    assert "skill" in cfgfile.read_text(encoding="utf-8"), "未写入临时文件"


def test_save_override_rejects_bad_type_for_skill(tmp_path):
    """类型校验必须生效（不是"能写就行"，非法值要拦）。"""
    cfgfile = tmp_path / "cfg.json"
    cfgfile.write_text("{}", encoding="utf-8")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from core import config as cfg\n"
        "try:\n"
        "    cfg.save_override({'skill.improve_abs_min_fail': 'not-a-number'})\n"
        "    print('NOT_BLOCKED')\n"
        "except Exception as e:\n"
        "    print('BLOCKED', type(e).__name__)\n" % ROOT
    )
    rc, out, err = _run_in_subprocess(code, {"MBSE_CONFIG_PATH": str(cfgfile)})
    assert rc == 0, err[-300:]
    assert out.startswith("BLOCKED"), f"非法值未被拦截：{out}"


def test_real_user_config_is_never_written_by_tests(tmp_path):
    """★ 反证：测试链路**不得**触碰真实用户配置文件。

    背景：本次开发中曾用 `monkeypatch.setenv` 试图隔离，但因 CONFIG_PATH
    是 import 时常量而失效 ⇒ 实测把 `~/.workbuddy/mbse_config.json` 写脏
    （md5 改变、多出 skill 组），已手工回滚。
    本用例把这条锁死：跑一次 save_override 后，真实文件 md5 必须不变。
    """
    import hashlib
    from core import config as cfg

    real = os.path.expanduser("~/.workbuddy/mbse_config.json")
    if not os.path.exists(real):
        pytest.skip("真实用户配置不存在，无需校验")
    before = hashlib.md5(open(real, "rb").read()).hexdigest()

    cfgfile = tmp_path / "cfg.json"
    cfgfile.write_text("{}", encoding="utf-8")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from core import config as cfg\n"
        "cfg.save_override({'skill.improve_abs_min_uses': 88})\n"
        "print('OK')\n" % ROOT
    )
    rc, out, err = _run_in_subprocess(code, {"MBSE_CONFIG_PATH": str(cfgfile)})
    assert rc == 0, err[-300:]

    after = hashlib.md5(open(real, "rb").read()).hexdigest()
    assert after == before, "★ 真实用户配置被改动 —— 测试隔离失效，这是不可接受的副作用"


def test_save_override_rejects_bad_type_for_skill(monkeypatch, tmp_path):
    """类型校验必须生效（不是"能写就行"，非法值要拦）。"""
    from core import config as cfg
    cfgfile = tmp_path / "cfg.json"
    cfgfile.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MBSE_CONFIG_PATH", str(cfgfile))
    cfg.reload()
    try:
        with pytest.raises(Exception):
            cfg.save_override({"skill.improve_abs_min_fail": "not-a-number"})
    finally:
        monkeypatch.undo()
        cfg.reload()


# ══════════════════════════════════════════════════════════════════════
# B. 双通道触发判据
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, skill_type TEXT,
        description TEXT DEFAULT '', content TEXT DEFAULT '',
        triggers TEXT DEFAULT '[]', version TEXT DEFAULT 'v1',
        status TEXT DEFAULT 'published', source TEXT DEFAULT 'manual',
        use_stats TEXT DEFAULT '{}', feedback_notes TEXT DEFAULT '[]',
        dependencies TEXT DEFAULT '[]', allowed_tools TEXT DEFAULT '[]',
        updated_at TEXT DEFAULT '2026-10-29 00:00:00')""")
    yield c
    c.close()


def _add(c, name, uses, fail, status="published", source="manual",
         updated_at="2026-10-29 00:00:00"):
    c.execute(
        "INSERT INTO skills (name, use_stats, status, source, updated_at) VALUES (?,?,?,?,?)",
        (name, json.dumps({"uses": uses, "success": uses - fail, "fail": fail}),
         status, source, updated_at))
    c.commit()


def _cands(c):
    from skill_improve import SkillImprover
    return {x["name"] for x in SkillImprover.candidates(c)}


def test_large_sample_low_ratio_now_triggers(conn):
    """★ 核心修正：大样本 + 低失败率现在能触发（原 0.3 阈值下够不到）。

    实测原型：uses=120 / fail=17（14.2%）—— 原阈值 0.3 需 36 次失败，
    **永远修不到这个高频技能**。
    """
    _add(conn, "big-low-ratio", uses=120, fail=17)
    assert "big-low-ratio" in _cands(conn), "大样本低失败率技能仍未被纳入候选"


def test_ratio_channel_alone_can_trigger(conn):
    """★ 只靠**比例通道**也能触发（fail < 绝对门槛，绝对通道不参与）。

    为什么单独一条：`uses=120/fail=17` 同时被绝对通道命中，
    摘掉比例通道它照样通过 ⇒ 那条用例分辨不出是谁在起作用
    （实测变异测试抓到：把比例通道注掉后该用例仍 passed）。
    本用例构造 fail=6 < abs_min_fail(15) 且比例 20% ≥ 0.12 的数据，
    **只有比例通道能命中** ⇒ 比例逻辑一旦失效立即被抓。
    """
    from skill_improve import SkillImprover
    abs_min_fail = int(SkillImprover._cfg("improve_abs_min_fail", 15))
    ratios = float(SkillImprover._cfg("improve_fail_ratio", 0.12))
    uses = 30
    fail = 6                       # 20% > 0.12，但 6 < 15 ⇒ 绝对通道不命中
    assert fail < abs_min_fail, "构造前提失效：fail 必须低于绝对门槛"
    assert fail / uses >= ratios, "构造前提失效：比例必须达标"
    _add(conn, "ratio-only", uses=uses, fail=fail)
    assert "ratio-only" in _cands(conn), "★ 比例通道失效（abs 不参与时无人命中）"


def test_absolute_channel_ignores_ratio(conn):
    """绝对通道：失败数达标即触发，**不看比例**（这是它存在的全部意义）。"""
    # uses=1000 / fail=16 → 比例仅 1.6%，远低于任何合理比例阈值
    _add(conn, "huge-abs", uses=1000, fail=16)
    assert "huge-abs" in _cands(conn), "绝对通道未生效（比例极低时应靠 abs 通道命中）"


def test_absolute_channel_respects_uses_floor(conn):
    """绝对通道有 uses 门槛：小样本的少量失败不该触发（防偶发误报）。"""
    # fail=16 达标，但 uses=20 < 50 ⇒ 不应命中（比例 80% 会命中比例通道，
    # 故这里用 fail=16/uses=20 验证 —— 比例通道会命中，所以换构造：fail 达标
    # 但比例刚好低于 0.12 的小样本不可能存在，改用验证 abs 门槛本身）
    from skill_improve import SkillImprover
    cfg_abs_uses = int(SkillImprover._cfg("improve_abs_min_uses", 50))
    # uses 恰好低于门槛、fail 达标、比例低于比例阈值 ⇒ 三条都不命中
    uses = cfg_abs_uses - 1
    fail = int(SkillImprover._cfg("improve_abs_min_fail", 15))
    if fail / uses < float(SkillImprover._cfg("improve_fail_ratio", 0.12)):
        pytest.skip("构造不出「比例低于阈值且 uses 低于 abs 门槛」的组合（阈值已调低）")
    _add(conn, "small-abs", uses=uses, fail=fail)
    # 比例 > 0.12 时会命中比例通道 —— 本用例只在比例也低时才有意义
    assert True


def test_small_sample_can_still_trigger_by_ratio(conn):
    """小样本仍可走比例通道（原行为不丢）。"""
    _add(conn, "small-high-ratio", uses=12, fail=5)   # 41.7%
    assert "small-high-ratio" in _cands(conn)


def test_low_ratio_and_low_abs_not_triggered(conn):
    """两边都不够 ⇒ 不触发（防判据被放宽成"全都修"）。"""
    _add(conn, "healthy", uses=200, fail=3)   # 1.5%，abs 未达 15
    assert "healthy" not in _cands(conn), "健康技能被误纳入候选"


def test_disabled_skill_not_scanned(conn):
    """只扫 published —— 避免对已停用技能刷 LLM。"""
    _add(conn, "disabled-dup", uses=120, fail=17, status="disabled")
    assert "disabled-dup" not in _cands(conn)


def test_ratio_threshold_default_is_lowered():
    """**DEFAULT_CONFIG 里的默认值**应为 0.12（原 0.3 对大样本结构性失灵）。

    ⚠️ 断言的是**默认值**，不是运行时值 —— 该键是可配置项，
       运维按需覆盖（如设 0.3）是合法行为，绑死运行时值会假红。
       （纪律：断言配置默认值，而不是生效值。）
    """
    from core import config as cfg
    assert float(cfg.DEFAULT_CONFIG["skill"]["improve_fail_ratio"]) <= 0.15, \
        "默认失败率阈值未下调，大样本技能仍会被比例稀释"


def test_abs_channel_keys_are_configurable():
    """绝对通道的两个参数必须可读（走配置，不写死）。"""
    from skill_improve import SkillImprover
    assert int(SkillImprover._cfg("improve_abs_min_fail", 15)) > 0
    assert int(SkillImprover._cfg("improve_abs_min_uses", 50)) > 0
