# -*- coding: utf-8 -*-
"""实体归一消歧链路的常驻自检（2026-10-11 首次建立）。

**为什么建这个脚本**：本工程的 `tools/verify/` 有 200+ 个自检脚本，但**没有任何一个覆盖
`entity_resolver.py` 主干**（消歧检测 / 合并 / 别名写入）。最接近的两个在
`tests/manual_verify/`，且 `AGENTS.md:411` 记录着它们的存量失败（24/26）。
结果就是一条**主干长期假绿**的链路，直到 2026-10-11 才被实机取证实锤：

    detect_and_save_candidates 返回 {'auto_merged': 122}，
    但库内 entity_merges 仍 8 行、实体数 183→183 **零变化**。

根因（三层）：
  ① SELECT 漏查 branch 列 —— 下游按 branch 分组，却从没读到过 branch；
  ② entities 主键是复合键 (id, branch)，真库有 61 组同 id 跨三分支，
     分组键不含 branch ⇒ 把「同一实体的三个分支副本」当成「三个重复实体」；
  ③ merge_entities 默认 branch='personal'，取不到行时返回 {"ok":False} 而**不抛异常**，
     而调用侧 auto_merged += 1 **不在成功判断内** ⇒ 计数撒谎。

本脚本把这三条钉死，并加**变异自证**证明断言有牙齿。

**本脚本不依赖服务、不写生产库**（只读打开 mbse.db + 临时库夹具），随时可跑：
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_entity_resolver.py

分段：
  [1] 分支口径完整性（**防 P0-1 回退**）：SELECT 含 branch / 分组键含 branch /
      canopy 桶键可隔离分支 / merge 调用透传 branch / seen 键含 branch
  [2] 计数诚实性（**防 P0-2 回退**）：auto_merged == entity_merges 实际增量（真库 + 夹具）
  [3] 跨分支不误合并（**核心行为判据**）：夹具 5 场景，含真库 61 组形态
  [4] 自动合并总开关（**防 P1-1 回退**）：关 = 不合并但**必须留候选**（线索不丢）
  [5] 阈值配置化：走 core/config 的 entity_merge 分组，不是裸字面量
  [6] 术语库接线（**防 P0-3 回退**）：读现役 glossary_terms/concepts，产出真实别名对
  [7] 权限门（**防 P1-3 回退**）：detect 端点必须有写权限门
  [8] 变异自证：把各修复还原 → 对应断言必须 FAIL（证明断言不是空转）
  [9] 编号机械自检（O 号连续递增）

⚠️ 换行符：本仓库混用 CRLF/LF。[8] 段变异写入按文件原约定归一化，
   直接用 LF 锚点去字节匹配 CRLF 文件会「锚点不存在」，极易误判成脚本自身写错。

⚠️ 口径纪律：**真库金标断言与夹具断言必须分开标注**。
   真库当前没有待消歧对象（同分支内归一化重名 = 0 组），
   所以「修完 auto_merged 从 122 变 0」**不能证明引擎正确** ——
   必须靠 [3] 的夹具证明「有该合的能合上」。二者缺一不可。
"""

# ── CI 豁免（2026-10-11 定性：B 类，真库金标门禁）──────────────
# 本脚本 [2] 的真库部分断言的是当前真库数值（entity_merges=8 等），
# 换库后数字会变 → CI 上应作为「非阻断」项。
# CI-OPTIONAL: B 真库金标门禁 —— [2]/[6] 断言依赖 mbse.db 现值

import argparse
import atexit
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
DB_PATH = os.path.join(BASE, "mbse.db")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    tag = "[PASS]" if cond else "[FAIL]"
    line = f"{tag} {name}"
    if detail:
        line += f"  | {detail}"
    print(line, flush=True)
    return cond


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


_TMP_COPY = None


def _tmp_copy() -> str:
    """真库 → 临时副本（夹具用；生产库绝不写入）。

    ⚠️ 2026-10-11 修正（曾把磁盘写满）：
      原实现文件名带 `os.getpid()`，而 [8] 变异段的每个子进程 PID 都不同
      ⇒ 每跑一次变异就新建一个 **374 MB** 副本且**永不回收**。
      单轮 8 个变异 × 多轮 = 22 个副本 = **7.7 GB**，最终 `database or disk is full`
      把所有回归脚本一起打挂（表象与代码缺陷无关，极易误判）。
      现改为**固定名**（同名即覆盖）＋ 进程退出时统一清理。
    """
    global _TMP_COPY
    tmp = os.path.join(tempfile.gettempdir(), "verify_entity_resolver.db")
    _TMP_COPY = tmp
    for suf in ("", "-wal", "-shm"):
        p = tmp + suf
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    s = sqlite3.connect(DB_PATH)
    d = sqlite3.connect(tmp)
    s.backup(d)
    d.close()
    s.close()
    return tmp


def _cleanup_tmp_copy() -> None:
    """删除临时副本（含 -wal/-shm）。**必须在任何退出路径上调用**，否则 374MB 泄漏一次。"""
    global _TMP_COPY
    if not _TMP_COPY:
        return
    for suf in ("", "-wal", "-shm"):
        p = _TMP_COPY + suf
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    _TMP_COPY = None


# 进程退出时兜底清理（即使主流程异常中断也不留 374MB）
atexit.register(_cleanup_tmp_copy)


def _clean_entities(c: sqlite3.Connection) -> None:
    for t in ("entity_aliases", "entity_merges", "entity_dup_candidates"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.execute("DELETE FROM entities")


def _add_entity(c, eid, name, etype="转发器", branch="personal"):
    c.execute(
        "INSERT INTO entities (id,name,entity_type,properties,status,branch,created_at) "
        "VALUES (?,?,?,'{}','reviewed',?,CURRENT_TIMESTAMP)", (eid, name, etype, branch))


def _counts(c):
    g = lambda q: c.execute(q).fetchone()[0]
    return {
        "entities": g("SELECT COUNT(*) FROM entities WHERE status!='deprecated'"),
        "merges": g("SELECT COUNT(*) FROM entity_merges"),
        "pending": g("SELECT COUNT(*) FROM entity_dup_candidates WHERE status='pending'"),
        "aliases": g("SELECT COUNT(*) FROM entity_aliases"),
        "deprecated": g("SELECT COUNT(*) FROM entities WHERE status='deprecated'"),
    }


def _disk_src(fname: str) -> str:
    """从**磁盘**读源码（不用 inspect.getsource）。

    为什么不用 inspect.getsource：它按模块 `__file__` + 行号去读文件，
    遇到 .pyc 缓存时行号可能对不上变异后的版本 ⇒ 变异自证会「打不中靶子」
    （踩过一次：M3 变异已写进磁盘，但 getsource 仍返回旧片段，E9 判不出红）。
    变异自证必须以磁盘为准 —— 变异改的就是磁盘。
    """
    with open(os.path.join(BASE, fname), encoding="utf-8") as f:
        return f.read()


def _func_src(fname: str, func_name: str) -> str:
    """从磁盘源码里截出指定函数体（按 def 缩进切，不依赖 import）。"""
    src = _disk_src(fname)
    key = f"def {func_name}("
    i = src.find(key)
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 1)
    j2 = src.find("\nclass ", i + 1)
    for cand in (j, j2):
        if cand > 0 and (j < 0 or j2 < 0 or cand < max(j, j2)):
            j = cand if j > 0 else j2
            break
    return src[i:j if j > 0 else len(src)]


# ════════════════[1] 分支口径完整性（防 P0-1 回退）════════════════
def section1():
    print("\n[1] 分支口径完整性（防 P0-1 回退）")
    import entity_resolver as ER
    from text_normalize import _canopy_key

    # ★ 一律读磁盘源码（变异自证的前提，见 _disk_src 注释）
    src = _func_src("entity_resolver.py", "detect_and_save_candidates")

    # E1 SELECT 必须含 branch 列 —— 这是整条链路的缺陷源头
    m = re_sel = [l for l in src.splitlines() if "SELECT id, name, entity_type" in l]
    check("E1 detect 的 SELECT 含 branch 列",
          bool(m) and all("branch" in l for l in m),
          (m[0].strip()[:90] if m else "未找到 SELECT"))

    # E2 强规则分组键必须含 branch。
    #    ⚠️ 判据必须**收紧到 setdefault 的第一个实参**（踩过一次假牙齿）：
    #    早先用「setdefault 之后 220 字符窗口内是否出现 branch」判红，但该窗口会扫到
    #    紧随其后的 `merged_dups`/日志行（其中含 r["branch"]）⇒ 去掉分组键的 branch 也照样 PASS。
    #    现在只取 setdefault 到下一个 `)` 之前的实参文本，精准判定。
    i_grp = src.find("by_full.setdefault")
    grp_block = ""
    if i_grp >= 0:
        i_close = src.find(").append", i_grp)
        grp_block = src[i_grp:i_close] if i_close > i_grp else src[i_grp:i_grp + 160]
    check("E2 强规则分组键含 branch",
          i_grp >= 0 and 'r["branch"]' in grp_block,
          grp_block.strip().replace("\n", " ")[:110])

    # E3 canopy 桶键必须能隔离分支
    k_dev = _canopy_key("转发器", "转发器", branch="dev")
    k_rel = _canopy_key("转发器", "转发器", branch="release")
    check("E3 canopy 桶键隔离分支", k_dev != k_rel, f"{k_dev!r} != {k_rel!r}")

    # E4 canopy 默认调用（不传 branch）必须与历史键一致 —— 向后兼容不许被破坏
    check("E4 canopy 不传 branch 时向后兼容",
          _canopy_key("转发器", "转发器") == "#转发器#转发器",
          _canopy_key("转发器", "转发器"))

    # E5 merge 调用必须透传 branch（取强规则那次调用的实参段，勿用宽窗口）
    i_merge_call = src.find('score=1.0, method="norm_exact"')
    call_block = ""
    if i_merge_call >= 0:
        i_end = src.find("\n", i_merge_call)
        call_block = src[max(0, i_merge_call - 160):i_end if i_end > 0 else i_merge_call + 160]
    check("E5 强规则 merge 调用透传 branch=branch", "branch=branch" in call_block,
          call_block.strip().replace("\n", " ")[-110:])

    # E6 seen 去重键必须含 branch（否则跨分支对会被误判为已处理）
    seen_lines = [l for l in src.splitlines() if 'key = (' in l and 'sorted' in l]
    check("E6 seen 去重键含 branch",
          bool(seen_lines) and "branch" in seen_lines[0],
          (seen_lines[0].strip()[:90] if seen_lines else "未找到"))

    # E7 桶内配对必须显式跳过跨分支
    check("E7 桶内配对跳过跨分支",
          'a["branch"] != b["branch"]' in src)

    # E8 run_rule_preprocess 的分组键（含 branch，作为同文件口径一致的对照）
    src2 = _func_src("entity_resolver.py", "run_rule_preprocess")
    g2 = [l for l in src2.splitlines() if "by_norm.setdefault" in l]
    check("E8 run_rule_preprocess 分组键含 branch（两函数口径一致）",
          bool(g2) and "branch" in g2[0])


# ════════════════[2] 计数诚实性（防 P0-2 回退）════════════════
def section2(tmp):
    print("\n[2] 计数诚实性（防 P0-2 回退）")
    import entity_resolver as ER  # noqa: F401  (确保可导入=模块存在)

    src = _func_src("entity_resolver.py", "detect_and_save_candidates")

    # E9 **每一处** `auto_merged += 1` 都必须在 res.get("ok") 的紧邻守卫内。
    #    ⚠️ 必须逐处检查（不能只看第一处）：函数里有强规则与桶内两处累加，
    #    只查第一处会让另一处失去牙齿（踩过一次假牙齿）。
    lines = src.splitlines()
    bad_sites = []
    for idx, line in enumerate(lines):
        if "auto_merged += 1" not in line:
            continue
        # 向上找最近的、非纯注释的 if 守卫
        guard = ""
        for j in range(idx - 1, -1, -1):
            t = lines[j].strip()
            if t.startswith("if "):
                guard = t
                break
            if t and not t.startswith("#"):
                break
        if 'res.get("ok")' not in guard:
            bad_sites.append(f"L{idx + 1} 守卫={guard!r}")
    check("E9 每处 auto_merged 累加都受 res.get('ok') 紧邻守卫",
          not bad_sites, f"违规 {len(bad_sites)} 处: " + "; ".join(bad_sites[:3]))

    # E10 必须返回 merge_failed
    check("E10 返回值含 merge_failed", '"merge_failed"' in src)

    # E11 真库实跑：auto_merged 必须等于 merges 增量（★核心判据）
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    before = _counts(c)
    r = ER.detect_and_save_candidates(c)
    after = _counts(c)
    delta_m = after["merges"] - before["merges"]
    delta_d = after["deprecated"] - before["deprecated"]
    check("E11 ★真库 auto_merged == entity_merges 实际增量",
          r["auto_merged"] == delta_m,
          f"auto_merged={r['auto_merged']} mergesΔ={delta_m} deprecatedΔ={delta_d}"
          f" merge_failed={r.get('merge_failed')}")
    check("E12 ★真库 auto_merged == deprecated 增量（实体确被软删）",
          r["auto_merged"] == delta_d, f"auto_merged={r['auto_merged']} deprecatedΔ={delta_d}")

    # E13 本次运行不得新增跨分支合并
    #    ⚠️ 口径修正（2026-10-11）：真库**存量**有 6 条 2026-09 的跨分支合并（entity_merges #5/#6/#8，
    #    method='norm_exact'/''，keep=personal|release、dup=dev），那是修复前的历史脏数据，
    #    **不是本次产生的**。因此判据必须比对「增量」而非「总量」，否则会把历史脏数据
    #    误判成修复失败 —— 这正是「存量基线污染验收判据」的典型坑。
    #    遗留脏数据的清理属独立议题（P2），不在本脚本断言范围内。
    cross = c.execute(
        "SELECT COUNT(*) FROM entity_merges m "
        "JOIN entities k ON k.id=m.keep_id JOIN entities d ON d.id=m.dup_id "
        "WHERE k.branch<>d.branch").fetchone()[0]
    cross_before = cross - (delta_m if delta_m else 0)
    check("E13 ★本次运行未新增跨分支合并（增量判据，容忍存量脏数据）",
          cross == cross_before, f"cross_total={cross} 存量={cross_before} 本次新增={cross - cross_before}")

    # E13b 存量跨分支脏数据的存在性记录（信息项，不判红；清理见 P2）
    hist = c.execute(
        "SELECT COUNT(*) FROM entity_merges m "
        "JOIN entities k ON k.id=m.keep_id JOIN entities d ON d.id=m.dup_id "
        "WHERE k.branch<>d.branch AND m.created_at < '2026-10-01'").fetchone()[0]
    print(f"       [信息] 真库存量跨分支合并 {hist} 条（2026-10-01 前，属修复前历史数据，未判红）")
    # E14 自合并（同 id 互为 keep/dup）——存量 + 本次都必须为 0
    selfm = c.execute(
        "SELECT COUNT(*) FROM entity_merges m "
        "JOIN entities k ON k.id=m.keep_id JOIN entities d ON d.id=m.dup_id "
        "WHERE k.id=d.id").fetchone()[0]
    check("E14 真库无 keep_id==dup_id 自合并", selfm == 0, f"self={selfm}")
    # E15 幂等：连跑两次第二次不得再变更
    r2 = ER.detect_and_save_candidates(c)
    check("E15 幂等：第二次运行零变更",
          r2["auto_merged"] == 0 and r2["merge_failed"] == 0,
          f"第二次 auto_merged={r2['auto_merged']} merge_failed={r2['merge_failed']}")
    c.close()


# ════════════════[3] 跨分支不误合并（核心行为，夹具驱动）════════════════
def section3(tmp):
    print("\n[3] 跨分支不误合并（核心行为判据，夹具驱动）")
    import entity_resolver as ER

    # E16 同分支+同类型+归一全等 → 必须真合并（证明「修完不是因无事可做」）
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    _clean_entities(c)
    _add_entity(c, "T-A1", "转发器")
    _add_entity(c, "T-A2", "转发器")
    r = ER.detect_and_save_candidates(c)
    a = _counts(c)
    check("E16 同分支同名真合并（实体 2→1 + 1 条审计）",
          r["auto_merged"] == 1 and a["entities"] == 1 and a["merges"] == 1,
          f"auto_merged={r['auto_merged']} 实体={a['entities']} merges={a['merges']}")
    c.close()

    # E17 跨分支同 id（真库 61 组形态）→ 必须零变更
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    _clean_entities(c)
    for br in ("dev", "personal", "release"):
        _add_entity(c, "T-B1", "转发器", branch=br)
    r = ER.detect_and_save_candidates(c)
    b = _counts(c)
    check("E17 跨分支同 id 零变更（真库 61 组形态）",
          r["auto_merged"] == 0 and b["entities"] == 3 and b["merges"] == 0,
          f"auto_merged={r['auto_merged']} 实体={b['entities']} merges={b['merges']}")
    c.close()

    # E18 跨分支 + 归一全等（修前的核心 bug 形态）→ 零变更
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    _clean_entities(c)
    _add_entity(c, "T-C1", "宽带载荷", etype="分系统", branch="dev")
    _add_entity(c, "T-C1", "宽带载荷", etype="分系统", branch="release")
    r = ER.detect_and_save_candidates(c)
    cc = _counts(c)
    check("E18 跨分支+归一全等 零变更（修前核心 bug）",
          r["auto_merged"] == 0 and cc["merges"] == 0,
          f"auto_merged={r['auto_merged']} merges={cc['merges']}")
    c.close()

    # E19 跨分支异名同类型（桶内配对路径也要隔离）
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    _clean_entities(c)
    _add_entity(c, "T-E1", "转发器甲", branch="dev")
    _add_entity(c, "T-E2", "转发器乙", branch="release")
    r = ER.detect_and_save_candidates(c)
    ee = _counts(c)
    check("E19 跨分支异名 零变更（桶内配对路径）",
          r["auto_merged"] == 0 and ee["merges"] == 0,
          f"auto_merged={r['auto_merged']} merges={ee['merges']}")
    c.close()


# ════════════════[4] 自动合并总开关（防 P1-1 回退）════════════════
def section4(tmp):
    print("\n[4] 自动合并总开关（防 P1-1 回退）")
    import entity_resolver as ER
    from core.config import get as cfg_get

    # E20 config 必须有 entity_merge 分组与三键
    sec = cfg_get("entity_merge") or {}
    check("E20 config 含 entity_merge 分组三键",
          all(k in sec for k in ("auto_threshold", "flag_threshold", "auto_merge_enabled")),
          json.dumps(sec, ensure_ascii=False))

    # E21 开关关闭：强规则路径不合并但必须留候选（线索不丢）
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    _clean_entities(c)
    _add_entity(c, "T-D1", "转发器")
    _add_entity(c, "T-D2", "转发器")
    orig = sec.get("auto_merge_enabled")
    try:
        sec["auto_merge_enabled"] = False
        r = ER.detect_and_save_candidates(c)
        d = _counts(c)
        check("E21 开关关闭=不合并",
              d["merges"] == 0 and r["auto_merged"] == 0,
              f"merges={d['merges']} auto_merged={r['auto_merged']}")
        check("E22 ★开关关闭仍留待审候选（线索不丢）",
              d["pending"] >= 1, f"pending={d['pending']}")
    finally:
        if orig is not None:
            sec["auto_merge_enabled"] = orig
    c.close()

    # E23 阈值健壮性：flag 必须 < auto
    th = ER._entity_merge_thresholds(None)
    check("E23 flag 阈值 < auto 阈值（决策树不退化）",
          th["flag"] < th["auto"], f"auto={th['auto']} flag={th['flag']}")


# ════════════════[5] 阈值配置化（防裸字面量回退）════════════════
def section5():
    print("\n[5] 阈值配置化（防裸字面量回退）")
    import entity_resolver as ER  # noqa: F401  (确保可导入=模块存在)

    src = _func_src("entity_resolver.py", "detect_and_save_candidates")
    # E24 决策树不得出现裸 0.92 / 0.62
    hard = [l.strip() for l in src.splitlines()
            if ("score >= 0.92" in l or "score >= 0.62" in l)]
    check("E24 决策树无裸阈值字面量（0.92/0.62）", not hard, "; ".join(hard)[:80])

    # E25 必须调用阈值读取函数
    check("E25 调用 _entity_merge_thresholds(conn)",
          "_entity_merge_thresholds(conn)" in src)

    # E26 阈值读取须在强规则合并之前（强规则也受开关约束）
    i_th = src.find("_entity_merge_thresholds(conn)")
    i_merge = src.find('method="norm_exact"')
    check("E26 阈值读取早于强规则合并", 0 < i_th < i_merge,
          f"阈值@{i_th} 强规则@{i_merge}")

    # E27 get_fuse_config 真实存在于 staging_fuse（防引用不存在的函数）
    try:
        from staging_fuse import get_fuse_config  # noqa: F401
        ok = True
    except Exception as e:
        ok = False
    check("E27 staging_fuse.get_fuse_config 可导入", ok)


# ════════════════[6] 术语库接线（防 P0-3 回退）════════════════
def section6(tmp):
    print("\n[6] 术语库接线（防 P0-3 回退）")
    import entity_resolver as ER  # noqa: F401  (确保可导入=模块存在)

    src = _func_src("entity_resolver.py", "_normalize_mentions")
    # E29/E30 必须查现役两表（收紧到 SQL 实参行，勿用全源码包含——
    # 全源码里若留有注释提及表名，即使 SQL 已回退也会 PASS）
    #
    # ⚠️ P5（2026-10-12）：取材范围从「函数体」扩到「函数体 ∪ 模块级 SQL 常量」。
    #   原因：`load_cross_ling_index` 与 `_normalize_mentions` 必须共用同一段
    #   `FROM glossary_terms ...` 字符串，否则 M4/M8 变异锚点不唯一、
    #   `replace(..., 1)` 会打到新函数上 ⇒ E29/E32 **假性失效**（实测踩过）。
    #   为此 SQL 已收口到 `_GLOSSARY_ALIAS_FROM/_GLOSSARY_ALIAS_WHERE`，
    #   函数体内不再有 `FROM ` 字面量 ⇒ 原取材范围会误判红。
    #   **断言意图不变**：仍要证明"读的是现役两表、不是已废弃的旧表 glossary"。
    sql_lines = [l for l in src.splitlines() if "FROM " in l]
    mod_src = _read(os.path.join(BASE, "entity_resolver.py")).decode("utf-8", "replace")
    sql_lines += [l for l in mod_src.splitlines()
                  if "FROM " in l and ("glossary_terms" in l or "glossary_concepts" in l)]
    sql_blob = " ".join(sql_lines)
    check("E29 读现役 glossary_terms（SQL 实参）", "FROM glossary_terms" in sql_blob)
    check("E30 读现役 glossary_concepts（SQL 实参）", "glossary_concepts" in sql_blob)

    # 真库：现役表须能产出别名对（否则读了也是空）
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    n = c.execute(
        "SELECT COUNT(*) FROM glossary_terms t JOIN glossary_concepts c "
        "ON c.concept_id=t.concept_id WHERE t.term<>c.pref_label "
        "AND t.term_status='approved' AND c.concept_status='approved'").fetchone()[0]
    check("E31 真库现役术语表可产出别名对", n > 0, f"alias_pairs={n}")

    # 真库金标：归一必须真发生映射（修前恒 0）
    res = ER._normalize_mentions(c, ["ibd", "bdd", "LM"])
    mapped = {k: v for k, v in res.items() if v and v != k}
    check("E32 ★真库归一真发生别名映射（修前恒 0）",
          len(mapped) >= 2, json.dumps(res, ensure_ascii=False))
    c.close()


# ════════════════[7] 权限门（防 P1-3 回退）════════════════
def section7():
    print("\n[7] 权限门（防 P1-3 回退）")
    import routers.knowledge_parts.pipeline as PL  # noqa: F401  (确保可导入=端点存在)

    src = _func_src("routers/knowledge_parts/pipeline.py", "entity_dup_detect")
    check("E33 /entity-dups/detect 有写权限门",
          "require_any_permission" in src or "require_permission" in src)

    # 对照：review 端点本来就有门，两处口径必须一致
    src2 = _func_src("routers/knowledge_parts/pipeline.py", "entity_dup_review")
    check("E34 对照 review 端点权限门在位（口径一致）",
          "require_permission" in src2)

    # audit 留痕必须记录 merge_failed（否则失败静默）
    check("E35 audit 留痕含 merge_failed", "merge_failed" in src)


# ════════════════[7b] 拉丁名 fuzzy + 包含判据（防 P4 回退）════════════════
def section7b():
    """P4（2026-10-11）常驻断言。

    两个被钉死的缺陷形态：
      ① `_bigrams` 对**无空格连写的拉丁名**退化为整 token ⇒ fuzzy 恒 0
         （真库 v2g 候选 297 个去重名中 253 个 / 85.2% 受影响）
      ② 「包含」判据在短名**非前缀**时用 `_long[len(_short):]` 算出无意义切片
         ⇒ has_agg 偶然、行为不可预测（同一结构「X+转发器」时对时错）
    """
    print("\n[7b] 拉丁名 fuzzy + 包含判据（防 P4 回退）")
    from text_normalize import fuzzy_score
    from vector2graph import _containment_status
    from staging_fuse import _fuzzy_same_entity

    # ── ① 拉丁名 fuzzy（修复的正向判据）──
    check("E41 拉丁驼峰名 fuzzy 不再恒 0（无空格连写名须能比出相似度）",
          fuzzy_score("VehiclePowerManagement", "VehiclePowerManagementReq") >= 0.90,
          "实际 %s" % fuzzy_score("VehiclePowerManagement", "VehiclePowerManagementReq"))
    check("E42 无关拉丁名仍应低分（防过度敏感）",
          fuzzy_score("Driver", "Server") < 0.60,
          "实际 %s" % fuzzy_score("Driver", "Server"))

    # ── ② 中文 fuzzy 零回归（防后人误把中文 token 也拆字符）──
    # ⚠️ 若拆中文 token，会引入「器a」这类跨界 bigram，使本值由 0.6667 掉到 0.5000
    check("E43 中文 fuzzy 语义未变（转发器A⇄转发器B 仍 ≈0.6667）",
          abs(fuzzy_score("转发器A", "转发器B") - 0.6667) < 0.01,
          "实际 %s" % fuzzy_score("转发器A", "转发器B"))
    check("E44 中文层级样例未变（宽带载荷⊂宽带载荷分系统 ≈0.75）",
          abs(fuzzy_score("宽带载荷", "宽带载荷分系统") - 0.75) < 0.01,
          "实际 %s" % fuzzy_score("宽带载荷", "宽带载荷分系统"))

    # ── ③ 包含判据：非前缀 ⇒ related（层级），前缀+聚合词 ⇒ dup_suspect ──
    check("E45 包含但非前缀 ⇒ related（EV热管理⊃热管理：层级不是重复）",
          _containment_status("EVThermalManagementSystem", "ThermalManagement", True)
          == "related",
          _containment_status("EVThermalManagementSystem", "ThermalManagement", True))
    check("E46 前缀+聚合词后缀仍判 dup_suspect（收紧不得过度，交人工）",
          _containment_status("转发器", "转发器分系统", True) == "dup_suspect",
          _containment_status("转发器", "转发器分系统", True))
    check("E47 不过 floor 的前缀+聚合词 ⇒ related（层级兜底）",
          _containment_status("转发器", "有效载荷转发器", False) == "related",
          _containment_status("转发器", "有效载荷转发器", False))

    # ── ④ 质量闸判官：fuzzy 修复后必须否决包含关系，否则会放行错误自动合并 ──
    check("E48 quality_gate 判官否决包含关系（含关系 fuzzy 可达 0.95，须靠长度差拦）",
          _fuzzy_same_entity("VehiclePowerManagement", "VehiclePowerManagementReq") is False,
          "长度差 %d" % abs(len("VehiclePowerManagement") -
                             len("VehiclePowerManagementReq")))
    # ⚠️ E49 记录一个**实测局限**（不是理想期望）：`_bigrams` 会把整串 token 也放进集合
    #   （「转发器A」这类正是靠它保住中文语义），于是**任何一个字符的拼写差异**都会让
    #   两侧各有一个不匹配项 ⇒ fuzzy 被压到 0.91 左右，达不到判官的 0.95。
    #   ⇒ 当前 `quality_gate` 对英文名几乎只认 normalize_full 全等 —— **偏严**。
    #   方向上是安全的（宁可整批驳回，也不误合并），故此处钉住**当前真实契约**；
    #   是否把阈值 0.95 放宽属产品决策，需米爸拍板，不在本轮擅改。
    check("E49 quality_gate 对英文拼写变体当前不放行（fuzzy≈0.91 < 0.95，偏严但安全）",
          _fuzzy_same_entity("ProbeVehicle", "ProbeVehicl") is False,
          "fuzzy=%.4f lendiff=%d" % (fuzzy_score("ProbeVehicle", "ProbeVehicl"),
                                    abs(len("ProbeVehicle") - len("ProbeVehicl"))))


# ════════════════[7c] 跨语言消歧（防 P5 回退）════════════════
def section7c():
    """P5（2026-10-12）：跨语言消歧的常驻断言。

    被钉死的**断点形态**：`_normalize_mentions` 已能把英文名折成中文规范名，
    但结果只进报告展示字段；`_disambiguate_many` 独立重算且**不查词库**
    ⇒ 图库实体全中文、AI 候选全英文时，两侧 bigram 无交集 ⇒ 恒不命中。
    """
    print("\n[7c] 跨语言消歧（防 P5 回退）")
    from entity_resolver import load_cross_ling_index
    from vector2graph import _disambiguate_many

    real = _open_real()
    if real is None:
        print("     [SKIP] E50~E54：真库不可达，判据面不存在")
        return
    try:
        idx = load_cross_ling_index(real)
        check("E50 跨语言索引已产出（含硬编码常量的卫星术语 + 种子词条）",
              isinstance(idx, dict) and len(idx) > 0,
              "索引概念数=%d" % len(idx))
        # 同语言不得入索引（否则会破坏 P4 的层级判定：转发器 ⊂ 有效载荷转发器）
        same_lang = [k for k, v in idx.items() if any(_zh(s) == _zh(k) for s in v)]
        check("E51 同语言词条未被灌入跨语言索引（防层级误配）",
              not same_lang, "同语言混入=%s" % same_lang[:3])

        seeds = real.execute(
            "SELECT COUNT(*) FROM glossary_terms WHERE source='seed' AND lang='en' "
            "AND term_status='approved'").fetchone()[0]
        if not seeds:
            print("     [SKIP] E52~E54：真库未导入 P5 种子词条（判据面不存在）"
                  " → 跑 tools/seeds/seed_mbse_entity_terms.py --commit")
            return
        print("     （真库金标：种子英文词条 %d 条 / 跨语言索引概念 %d）" % (seeds, len(idx)))
        names = {r["name"]: r["id"] for r in real.execute(
            "SELECT id,name FROM entities WHERE branch='personal' AND status!='deprecated'")}
        # ★ 真库金标：AI 生成的驼峰英文名应命中图库中文实体
        CASES = [("WidebandPayload", "宽带载荷"), ("PowerSubsystem", "电源分系统"),
                 ("AttitudeAndOrbitControlSubsystem", "姿轨控分系统"),
                 ("SolarArray", "太阳翼"), ("BatteryPack", "蓄电池组"),
                 ("TT&CStation", "测控站"), ("DataRelayFunction", "数据转发功能")]
        on = _disambiguate_many(real, [n for n, _ in CASES], branch="personal",
                                cross_ling=idx)
        off = _disambiguate_many(real, [n for n, _ in CASES], branch="personal")
        miss = [(n, cn) for n, cn in CASES
                if on.get(n, ("", ""))[1] != names.get(cn)]
        check("E52 英文驼峰候选名命中中文图库实体（真库金标）",
              not miss, "未命中=%s" % miss[:3])
        # ② 断点反证：不传 cross_ling 必须命不中，否则说明"新能力"其实是既有行为
        already = [n for n, _ in CASES if off.get(n, ("", ""))[0] == "dup_high"]
        check("E53 不传 cross_ling 时这批候选全不命中（证明该参数是必需的）",
              not already, "未传即命中=%s（改前应全为 none）" % already[:3])
        # ③ 不得误配：含公共词的复合名不得判重复
        NEG = ["ForwardingPowerUnit", "PowerModeManagement", "TransponderUnit"]
        bad = [n for n in NEG
               if on.get(n, ("", ""))[0] in ("dup_high", "dup_suspect")]
        check("E54 不得误配（无对应实体 / 含公共词的复合名不得判重复）",
              not bad, "误配=%s" % bad)
    finally:
        real.close()


# ════════════════[7d] 消歧可见性解耦 + hiddenLabel 通道（防 P6 回退）════════════════
def section7d():
    """P6（2026-10-12）：① `use_in_matching` 把「消歧可用性」从 `term_status` 拆出；
    ② SKOS `skos:hiddenLabel`（只检索不展示的拼写变体）打通导入/导出。"""
    print("\n[7d] 消歧可见性解耦 + hiddenLabel 通道（防 P6 回退）")
    from entity_resolver import _matching_gate

    # ── ① 三态语义：NULL 沿用旧规则 / 1 强制参与 / 0 强制不参与 ──
    import sqlite3 as _s
    empty = _s.connect(":memory:")
    gate_no_col = _matching_gate(empty)          # 老库（无该列）⇒ 旧条件
    check("E55 老库（无 use_in_matching 列）回落到 term_status 旧规则",
          gate_no_col.strip() == "AND t.term_status = 'approved'", gate_no_col.strip())
    real = _open_real()
    if real is None:
        print("     [SKIP] E56：真库不可达")
        return
    try:
        cols = {r[1] for r in real.execute("PRAGMA table_info(glossary_terms)")}
        if "use_in_matching" not in cols:
            print("     [SKIP] E56：真库未迁移 use_in_matching 列")
        else:
            g = _matching_gate(real)
            check("E56 迁移后消歧条件显式区分「显式提权」与「沿用旧规则」",
                  "use_in_matching = 1" in g and "IS NULL" in g, g.strip())
            # 真实数据：导入通道写的词条必须对消歧可见
            n_uim = real.execute(
                "SELECT COUNT(*) FROM glossary_terms WHERE use_in_matching=1").fetchone()[0]
            print("     （真库 use_in_matching=1 的词条数 = %d）" % n_uim)
    finally:
        real.close()

    # ── ② hiddenLabel 通道：导入侧写入 + 导出侧还原 ──
    import routers.glossary_io as G
    csv_bytes = ("术语ID,名称,英文,隐藏词条,定义,域\n"
                 "C-ZZ-HID,探测车,ProbeVehicle,ProbeVehicl,探测车,satellite_comms\n").encode()
    rows = G._parse_file(csv_bytes, "z.csv")
    check("E57 「隐藏词条」列可被解析（CSV 表头已含该列）",
          "隐藏词条" in (rows[0] if rows else {}),
          "解析列=%s" % (list(rows[0].keys())[:5] if rows else []))
    import io as _io
    src = _io.open(os.path.join(BASE, "routers/glossary_io.py"), encoding="utf-8").read()
    check("E58 _commit 导入的词条一律置 use_in_matching=1（否则导入成功但消歧不可见）",
          '"admitted", "import", 6, 1)' in src and '"preferred", "import", 7, 1)' in src)
    check("E59 导出侧回显 hiddenLabel 且不与「英文」列重复",
          '"隐藏词条"' in src and 't["term_kind"] not in ("abbr", "hidden")' in src
          and "skos:hiddenLabel" in src)
    # E60 单独钉住 hidden 的**采集**语句（M16 的变异靶点）
    check("E60 导出侧确实采集 term_kind='hidden' 的词条",
          'hidden = [t["term"] for t in terms if t["term_kind"] == "hidden"]' in src)


def _zh(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in (s or ""))


def _open_real():
    """真库**只读**连接；取不到返回 None（判据面不存在 ⇒ 上层 SKIP，绝不伪装成 PASS）。"""
    import os
    import sqlite3 as _sq
    db = os.path.join(BASE, "mbse.db")
    if not os.path.isfile(db):
        return None
    try:
        c = _sq.connect("file:%s?mode=ro" % db, uri=True)
        c.row_factory = _sq.Row
        return c
    except Exception:
        return None


# ════════════════[8] 变异自证 ════════════════
MUTATIONS = [
    # (编号, 文件, 原文锚点, 变异替换, 应被击穿的断言, 需要执行的段)
    # ── 结构类变异（跑 section1，断言是"代码里有没有写对"）──
    ("M1", "SELECT id, name, entity_type, properties, status, branch FROM entities WHERE status!='deprecated'",
     "SELECT id, name, entity_type, properties, status FROM entities WHERE status!='deprecated'",
     "E1", "section1"),
    ("M2", '        by_full.setdefault(\n            (r["branch"], r["entity_type"], normalize_full(r["name"])), []).append(dict(r))',
     '        by_full.setdefault(\n            (r["entity_type"], normalize_full(r["name"])), []).append(dict(r))',
     "E2", "section1"),
    # M3 只变异强规则那一处；E9（section2）逐处检查 ⇒ 这一处无守卫即判红
    #    ⚠️ 目标段必须与断言所在段一致（踩过一次：M3 配了 section1，
    #    而 E9 实际在 section2 ⇒ 段跑完没这条断言，误判成"未击穿"）。
    ("M3", '            if res.get("ok"):\n                auto_merged += 1\n                merged_dups.add(dup["id"])',
     '            if True:\n                auto_merged += 1\n                merged_dups.add(dup["id"])',
     "E9", "section2"),
    ("M4", '"FROM glossary_terms t JOIN glossary_concepts c ON c.concept_id = t.concept_id "',
     '"FROM glossary g LEFT JOIN glossary_terms t ON 1=0 LEFT JOIN glossary_concepts c ON 1=0 "',
     "E29", "section6"),
    ("M5", '                if a["branch"] != b["branch"]:\n                    continue',
     '                if False:\n                    continue',
     "E7", "section1"),
    # ── 行为类变异（跑真跑段，证明断言不是"只看代码文本"而是真钉住行为）──
    ("M6", "SELECT id, name, entity_type, properties, status, branch FROM entities WHERE status!='deprecated'",
     "SELECT id, name, entity_type, properties, status FROM entities WHERE status!='deprecated'",
     "E16", "section3"),
    ("M7", '        by_full.setdefault(\n            (r["branch"], r["entity_type"], normalize_full(r["name"])), []).append(dict(r))',
     '        by_full.setdefault(\n            (r["entity_type"], normalize_full(r["name"])), []).append(dict(r))',
     "E18", "section3"),
    ("M8", '"FROM glossary_terms t JOIN glossary_concepts c ON c.concept_id = t.concept_id "',
     '"FROM glossary g LEFT JOIN glossary_terms t ON 1=0 LEFT JOIN glossary_concepts c ON 1=0 "',
     "E32", "section6"),
    # ── P4 类变异（第 6 元组 = 目标文件名，缺省为 entity_resolver.py）──
    # M9：还原 `_bigrams` 的拉丁字符 bigram（去掉补字符循环）⇒ 拉丁名 fuzzy 回到恒 0
    ("M9",
     "    for tok in en:\n        if len(tok) > 2 and _LATIN_ONLY.match(tok):\n"
     "            for i in range(len(tok) - 1):\n                bigrams.add(tok[i:i + 2])\n",
     "    for tok in []:\n        if len(tok) > 2 and _LATIN_ONLY.match(tok):\n"
     "            for i in range(len(tok) - 1):\n                bigrams.add(tok[i:i + 2])\n",
     "E41", "section7b", "text_normalize.py"),
    # M10：去掉「短名非前缀 ⇒ related」⇒ EV热管理⊃热管理 会被升级成 dup_suspect
    ("M10",
     "    if not long_.startswith(short):\n        return \"related\"          # P4：中缀/后缀包含 = 复合术语\n",
     "    if False:\n        return \"related\"\n",
     "E45", "section7b", "vector2graph.py"),
    # M11：去掉质量闸的长度差约束 ⇒ fuzzy 修复后包含关系会被判官放行（错误自动合并）
    ("M11",
     "    return abs(len(str(a)) - len(str(b))) <= 2\n",
     "    return True\n",
     "E48", "section7b", "staging_fuse.py"),
    # M12：把拉丁规则放宽到「所有 token」⇒ 中文 token 也被拆字符，中文 fuzzy 回归
    ("M12", "        if len(tok) > 2 and _LATIN_ONLY.match(tok):",
     "        if len(tok) > 2:",
     "E43", "section7b", "text_normalize.py"),
    # M13：让同语言词条也进跨语言索引 ⇒ 层级关系被当成同一实体的别名（P4 判定被破坏）
    ("M13",
     '        return bool(_ZH_CH.search(canon)) != bool(_ZH_CH.search(alias))\n',
     '        return True\n',
     "E51", "section7c", "entity_resolver.py"),
    # M14：不再把跨语言别名灌进消歧索引 ⇒ 英文候选名彻底命不中中文实体（P5 能力消失）
    ("M14",
     "            for _alt in cross_ling.get(_own, ()) or ():\n"
     "                if normalize_name(str(_alt).strip()) != _own:\n"
     "                    _index_one(_alt, r[\"id\"])\n",
     "            for _alt in []:\n"
     "                if normalize_name(str(_alt).strip()) != _own:\n"
     "                    _index_one(_alt, r[\"id\"])\n",
     "E52", "section7c", "vector2graph.py"),
    # M15：`_commit` 不再置 use_in_matching ⇒ 导入词条对消歧重新"隐形的成功"（P6 第1项回退）
    ("M15", 'VALUES (?,?,?,?,?,?,?,?)",\n                     (cid, label, _lang(label), "preferred", "preferred", "import", 7, 1))',
     'VALUES (?,?,?,?,?,?,?,?)",\n                     (cid, label, _lang(label), "preferred", "preferred", "import", 7, None))',
     "E58", "section7d", "routers/glossary_io.py"),
    # M16：导出不取 hidden 词条 ⇒ 拼写变体只进库不出库（SKOS hiddenLabel 通道半通）
    ("M16", 'hidden = [t["term"] for t in terms if t["term_kind"] == "hidden"]',
     'hidden = []',
     "E60", "section7d", "routers/glossary_io.py"),
]


def _read(p):
    with open(p, "rb") as f:
        return f.read()


def _write(p, data):
    with open(p, "wb") as f:
        f.write(data)


def _restore(paths, orig=None):
    src = orig if orig is not None else _ORIG
    for p in paths:
        _write(p, src[p])


def V_FAIL_PARSED(out: str) -> list:
    """从子进程输出里取 FAILED 列表。

    ⚠️ 不能用「子进程 stdout 里出现断言号就算击穿」——
       那是"文本命中"，PASS 行里也含断言号（check 打印 [PASS] <断言号>）。
       必须读脚本自己打印的 FAILED 列表，否则变异自证会**恒真**（假牙齿）。
    """
    for line in out.splitlines():
        if line.startswith("FAILED_LIST:"):
            body = line.split(":", 1)[1].strip()
            return [x.strip() for x in body.split(",") if x.strip()]
    return []


def section8():
    print("\n[8] 变异自证：把修复各自还原 → 对应断言必须 FAIL")
    # ⚠️ targets 必须覆盖**所有被变异文件**（P4 起不再只有 entity_resolver.py），
    #    否则 finally 还原时漏还原 ⇒ 变异残留到工作区（会造成"改到一半"的幽灵状态）。
    targets = [os.path.join(BASE, f) for f in
               ("entity_resolver.py", "text_normalize.py", "vector2graph.py",
                "staging_fuse.py", "routers/glossary_io.py")]
    global _ORIG
    _ORIG = {p: _read(p) for p in targets}
    try:
        for entry in MUTATIONS:
            tag, anchor, repl, kills, sec = entry[:5]
            # 第 6 元组 = 目标文件名（P4 起支持跨文件变异），缺省 entity_resolver.py
            fname = entry[5] if len(entry) > 5 else "entity_resolver.py"
            path = os.path.join(BASE, fname)
            raw = _read(path)
            # 换行符归一（仓库混用 CRLF/LF）
            variants = [(anchor.encode("utf-8"), repl.encode("utf-8"))]
            variants.append((anchor.replace("\n", "\r\n").encode("utf-8"),
                             repl.replace("\n", "\r\n").encode("utf-8")))
            done = False
            for anc, rep in variants:
                if anc in raw:
                    _write(path, raw.replace(anc, rep, 1))
                    done = True
                    break
            if not done:
                check(f"E38 变异 {tag} 锚点存在（脚本自身写对）", False,
                      f"未找到锚点前 50 字: {anchor[:50]!r}")
                continue
            # 清掉目标模块的字节码缓存 —— 否则子进程 import 会命中 __pycache__ 读到修复版
            # ⚠️ 必须含**被变异文件本身**（P4 的 M9/M12 变异 text_normalize、
            #    M10 变异 vector2graph），漏掉会导致变异自证静默失效（假牙齿）。
            for mod in ("entity_resolver", "text_normalize", "vector2graph",
                        "staging_fuse", "sysml_importer"):
                for suffix in ("", ".__pycache__"):
                    cache = os.path.join(BASE, mod + suffix)
                    if os.path.isfile(cache):
                        os.remove(cache)
                    elif os.path.isdir(cache):
                        for f in os.listdir(cache):
                            if f.startswith(mod.split(".")[0]):
                                os.remove(os.path.join(cache, f))
            # 子进程重跑目标段（避免模块缓存），检查该断言是否判红。
            # 严格判据：必须出现在 FAILED_LIST 里才算击穿。
            # ⚠️ 绝不允许「子进程崩溃」被当成击穿通过 —— 那是**假牙齿**：
            #   本工程已踩过（2026-10-11 首次跑本脚本时 8 个变异全靠崩溃豁免"通过"，
            #   实际是 -c 代码拼接错误导致段函数根本没执行）。
            #   崩溃必须判红并暴露，绝不豁免。
            # ⚠️ try 块内必须缩进；**且只能给首行加缩进** —— runner 是多行代码块
            #   （含自己的 try/finally），对整块做 replace("\n","\n    ") 会把内部缩进也顶飞，
            #   导致子进程 IndentationError（踩过一次：M6/M7 全"子进程异常"）。
            #   正确做法：把 runner 整体作为一个语句放进 exec 字符串，用 textwrap.indent
            #   只作用于「需要整体右移的行」—— 这里直接构造成 "def _run():\n<runner>" 再调用。
            if sec in ("section1", "section7b", "section7c", "section7d"):   # 无 tmp 参数的段
                runner_body = f"V.{sec}()"
            else:
                runner_body = (
                    "t=V._tmp_copy()\n"
                    "try:\n"
                    f"    V.{sec}(t)\n"
                    "finally:\n"
                    "    [__import__('os').remove(t+s) for s in ('','-wal','-shm') "
                    "if __import__('os').path.exists(t+s)]")
            code = (
                "import sys,os\nsys.path.insert(0,%r)\n"
                "import tools.verify.verify_entity_resolver as V\n"
                "V.PASS.clear(); V.FAIL.clear()\n"
                "def _run():\n"
                "    %s\n"
                "try:\n    _run()\n"
                "except Exception as e:\n"
                "    print('MUTATION_CRASH', type(e).__name__, e)\n"
                "print('FAILED_LIST:', ','.join(V.FAIL))\n"
                % (BASE, runner_body.replace("\n", "\n    "))
            )
            try:
                # ⚠️ 必须用 `-B`（禁 .pyc 缓存）+ **全新子进程**：
                #   同进程内 import 会命中 __pycache__，改文件后 importlib.invalidate_caches()
                #   也**不生效**（实测：改文件后同进程 reload 仍读到旧值）。
                #   若不加 -B，变异自证会「全部未判红」（假牙齿的另一种形态）。
                proc = subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", "-c", code],
                    capture_output=True, text=True, timeout=600, cwd=BASE,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                out = proc.stdout + proc.stderr
                # ⚠️ 变异把代码改成坏形态时，段函数**抛异常**本身就是有效判红
                #   （例：SELECT 去掉 branch 列 → 行内 KeyError 'branch'）。
                #   早先把「段函数抛异常」判成"子进程异常·脚本问题"，结果是 M6/M7 全红，
                #   而实际上它们恰恰**成功击穿了行为断言**（变异让 E16/E18 真失效了）。
                #   正确判据：只要子进程走到了末尾（打印了 FAILED_LIST），
                #   就说明段真跑过；此时断言未出现在 FAILED_LIST 才是"没击穿"。
                marker_printed = "FAILED_LIST:" in out
                failed_list = V_FAIL_PARSED(out)
                hit = any(f.strip().startswith(kills + " ") or f.strip() == kills
                          for f in failed_list)
                seg_crash = "MUTATION_CRASH" in out
                if not marker_printed:
                    check(f"E39 变异 {tag} 击穿断言 {kills}（{sec}）", False,
                          "子进程未走到末尾（脚本/环境问题）rc=%s 尾部: %s"
                          % (proc.returncode,
                             (out.strip().splitlines() or [""])[-1][:110]))
                else:
                    check(f"E39 变异 {tag} 击穿断言 {kills}（{sec}）",
                          hit or seg_crash,
                          ("已判红" if hit else "段函数异常中断")
                          + f"（FAILED_LIST={failed_list}）")
            except Exception as e:
                check(f"E39 变异 {tag} 击穿断言 {kills}", False, f"父进程异常: {e}")
            finally:
                _write(path, raw)
        # 还原完整性
        for p in targets:
            check(f"E40 源文件已逐字节还原 {os.path.basename(p)}",
                  _read(p) == _ORIG[p])
    finally:
        _restore(targets, _ORIG)


# ════════════════[9] 编号机械自检 ════════════════
def section9():
    print("\n[9] 编号机械自检")
    import re as _re
    src = os.path.abspath(__file__)
    with open(src, encoding="utf-8") as f:
        body = f.read()
    nums = [int(m[1:]) for m in _re.findall(r'check\(\s*f?"(E\d+)', body)]
    uniq = sorted(set(nums))
    check("E00 编号无重复", len(uniq) == len(set(uniq)), f"共 {len(uniq)} 个")
    gaps = [n for n in range(1, max(uniq) + 1) if n not in uniq]
    # E00/E28 为占位，允许缺口
    gaps = [g for g in gaps if g not in (28, 36, 37)]
    check("E00b 编号连续（除占位）", not gaps, f"缺口={gaps}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-mutation", action="store_true",
                    help="跳过变异自证段（CI 上可加速）")
    args = ap.parse_args()

    print("=" * 72)
    print("实体归一消歧链路常驻自检")
    print(f"仓库：{BASE}")
    print(f"真库：{DB_PATH}")
    print("=" * 72)

    tmp = _tmp_copy()
    try:
        section1()
        section2(tmp)
        section3(tmp)
        section4(tmp)
        section5()
        section6(tmp)
        section7()
        section7b()
        section7c()
        section7d()
        if not args.skip_mutation:
            section8()
        section9()
    finally:
        _cleanup_tmp_copy()

    print("\n" + "=" * 72)
    print(f"结果：{len(PASS)} pass / {len(FAIL)} fail")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    print("口径提示：E11/E12/E17/E31/E32 为真库金标；E16~E19/E21/E22 为夹具驱动；"
          "E9/E20/E23~E27/E29~E30/E33~E35 为代码结构断言；E38~E40 为变异自证。")
    print("★ 真库当前无待消歧对象（同分支内归一化重名 = 0 组），"
          "故「auto_merged 由 122 变 0」不足以证明引擎正确 —— "
          "必须与 E16（夹具能合上）合看。")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
