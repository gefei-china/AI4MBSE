# -*- coding: utf-8 -*-
"""P1-4 沙箱 A/B 评估引擎：影子配置跑门禁套件，差分判回归。

═══════════════════════════════════════════════════════════════
动机（2026-10-19《自进化 AI 平台调研评估》路线图 P1-4）
═══════════════════════════════════════════════════════════════
P1-3 提案器提议改配置（如 llm.retry_times 2→3），P1-4 回答两个问题：
  ① 这个变更**会不会改坏系统**？—— 对照组（活配置）vs 实验组（影子配置）
     各跑一遍 CI 门禁集，差分出新失败（回归）；
  ② 怎么安全地让它生效？—— 结论 pass ⇒ 人发起晋升 ⇒ HIL 批准 ⇒ 才真改
     （core/config_promotion.py），全程留痕可回滚。

═══════════════════════════════════════════════════════════════
设计纪律（每条都对应一个真实风险）
═══════════════════════════════════════════════════════════════
1. **评估期间零生产副作用**：门禁子进程的 MBSE_DB_PATH 指向**每臂独立的
   临时库**（从干净 init_db 快照复制），MBSE_CONFIG_PATH 指向**每臂独立
   的配置副本**——有门禁会 save_override 写真配置（verify_hyde_heading /
   verify_intent_llm_review 实测如此），不隔离就会改到用户真配置。
   影子配置只经 MBSE_SHADOW_CONFIG 进**实验组**子进程（core.config 影子层，
   生产进程不设该变量 ⇒ 行为零变化）。
2. **评估集 = CI 口径**：取 ci.yml **执行行**的门禁及其**命令行参数**
   （如 verify_orchestration_e2e.py --static-only——裸跑会进 live 模式真调
   LLM 烧钱）。CI 豁免门禁（需活服务/真 key）不在集内：它们在双臂同样
   跑不起来，纳入只稀释信号。这使「沙箱通过」的含义精确可陈述：
   *与每次合并同一道回归闸，在影子配置下不新红*。
3. **差分判定，不要求套件全绿**：环境性失败（无活服务等）在双臂同败 ⇒
   stable_fail/infra_error（中性）。判红只认「对照组过、实验组败」。
   双臂 error 占比超 infra_error_ratio ⇒ verdict=error（评估环境坏了，
   结论无效——比没有评估更坏的是假结论）。
4. **fail-closed 的回归口径**：对照组 pass、实验组 error（如超时）也算
   回归信号——宁可让人重跑，不拿模糊信号放行。报告里 fail/error 分开列，
   人可裁决。
4b. **源码变异检测 + 双臂顺序**（均为实测踩到后立规）：
   · 变异：评估跑到一半 llm/providers/openai_compat.py 被外部编辑改坏
     （SyntaxError），对照组（改坏前）过、实验组（改坏后）败 ⇒ 假回归。
     ⇒ 跑前跑后各对**全部跟踪文件内容**做一次 sha1（不用 porcelain：
     它只反映脏/干净，dirty→clean 翻转时前后都"干净"，实测漏过），
     不一致 ⇒ 结论无效、作业失败可重试。
   · 顺序：门禁工作目录是 ROOT/tmp/verify_* 固定路径、两臂共享，并行
     跑同一门禁会互删对方目录（实测差分出假回归）⇒ 双臂只能顺序跑
     （CI 口径本来就是顺序消费者，不发明并发）。
5. **纯函数核心**：differential_verdict / categorize / run_matches_proposal
   / gates_referencing 全部无 I/O，门禁逐条打；I/O 只发生在 evaluate。
6. **配置驱动**：口径走 core.config 的 sandbox 组（五键），模块内不写死
   阈值；门禁用变异自证口径真被消费。
7. **只读失败轨迹、只写自己的表**：本引擎写且仅写 sandbox_runs（经
   repositories/sandbox_repo.py）；**绝不写配置**（晋升在
   config_promotion，且须过人）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional

# ── 产品契约常量（API/门禁/前端三方共用，勿在调用处另写字面量）─────────────
VERDICT_PASS = "pass"
VERDICT_REGRESSION = "regression"
VERDICT_ERROR = "error"
VERDICTS = (VERDICT_PASS, VERDICT_REGRESSION, VERDICT_ERROR)

SCOPE_FULL = "full"              # 全 CI 门禁集
SCOPE_REFERENCED = "referenced"  # 仅静态引用目标配置的门禁（快速预检）
SCOPES = (SCOPE_FULL, SCOPE_REFERENCED)

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_STATUSES = (RUN_QUEUED, RUN_RUNNING, RUN_DONE, RUN_FAILED)

#: 单臂内门禁结果状态（子进程退出码/超时/拉起失败）
G_PASS = "pass"
G_FAIL = "fail"
G_ERROR = "error"

#: 差分类别（categorize 的返回值；报告与计数共用）
CAT_STABLE_PASS = "stable_pass"
CAT_REGRESSION = "regression"
CAT_IMPROVEMENT = "improvement"
CAT_STABLE_FAIL = "stable_fail"
CAT_INFRA_ERROR = "infra_error"
CAT_MIXED = "mixed"

#: 配置默认值（与 core.config DEFAULT_CONFIG.sandbox 一致；此处仅兜底，
#: 正常路径全部走配置——门禁变异自证）
_SETTINGS_DEFAULTS = {
    "enabled": True,
    "gate_timeout_s": 150,
    "default_scope": SCOPE_FULL,
    "infra_error_ratio": 0.3,
    "max_report_gates": 400,
}

_ROOT_CACHE: dict = {}


def _root() -> str:
    """仓库根（含 .github/workflows/ci.yml 与 tools/verify/）。"""
    if _ROOT_CACHE.get("root"):
        return _ROOT_CACHE["root"]
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(d, ".github", "workflows", "ci.yml")) \
                and os.path.isdir(os.path.join(d, "tools", "verify")):
            _ROOT_CACHE["root"] = d
            return d
        d = os.path.dirname(d)
    raise RuntimeError("找不到仓库根（.github/workflows/ci.yml）")


def get_settings() -> dict:
    """沙箱评估口径。缺配置时回落默认值（与 DEFAULT_CONFIG 一致，不是第二套真值）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        if key == "enabled":
            out[key] = _config.as_bool("sandbox", key, bool(default))
            continue
        try:
            out[key] = type(default)(_config.get("sandbox", key, default))
        except Exception:
            out[key] = default
    if out.get("default_scope") not in SCOPES:
        out["default_scope"] = SCOPE_FULL
    return out


# ══════════════════════════════════════════════════════════════
# 纯函数区（门禁 A 段逐条打）
# ══════════════════════════════════════════════════════════════
def categorize(a: str, b: str) -> str:
    """单门禁双臂结果 → 差分类别。

    (对照 pass, 实验 fail/error) ⇒ regression（唯一判红类别）；
    (对照 fail/error, 实验 pass) ⇒ improvement；
    双臂同 fail ⇒ stable_fail（存量，中性）；双臂同 error ⇒ infra_error；
    fail↔error 互换 ⇒ mixed（对照本就坏，既不算回归也不算改善）。
    """
    a, b = a or "", b or ""
    if a == G_PASS and b == G_PASS:
        return CAT_STABLE_PASS
    if a == G_PASS and b in (G_FAIL, G_ERROR):
        return CAT_REGRESSION
    if a in (G_FAIL, G_ERROR) and b == G_PASS:
        return CAT_IMPROVEMENT
    if a == G_FAIL and b == G_FAIL:
        return CAT_STABLE_FAIL
    if a == G_ERROR and b == G_ERROR:
        return CAT_INFRA_ERROR
    return CAT_MIXED


def differential_verdict(results: list, infra_error_ratio: float = 0.3) -> dict:
    """双臂结果 → 总判定（纯函数）。

    判定序（fail-closed）：
      1. 零门禁 ⇒ error（评估没跑起来，比回归更严重的信号缺失）；
      2. infra_error 占比 > infra_error_ratio ⇒ error（环境坏，差分无效）；
      3. 有任一 regression ⇒ regression；
      4. 否则 pass（存量失败/环境失败中性，improvement 只记录不改变判定）。
    """
    counts: dict = {}
    for r in results or []:
        cat = categorize(r.get("a", ""), r.get("b", ""))
        counts[cat] = counts.get(cat, 0) + 1
        r["category"] = cat
    n = len(results or [])
    verdict = VERDICT_PASS
    reason = ""
    if n == 0:
        verdict, reason = VERDICT_ERROR, "评估集为空（没有门禁可跑）"
    elif counts.get(CAT_INFRA_ERROR, 0) / n > float(infra_error_ratio):
        verdict, reason = VERDICT_ERROR, (
            "双臂 infra_error 占比 %.0f%% > %.0f%%（评估环境可疑，结论无效）"
            % (100.0 * counts[CAT_INFRA_ERROR] / n, 100.0 * float(infra_error_ratio)))
    elif counts.get(CAT_REGRESSION, 0) > 0:
        verdict = VERDICT_REGRESSION
        reason = "%d 个门禁对照组过、实验组败" % counts[CAT_REGRESSION]
    return {"verdict": verdict, "reason": reason, "counts": counts, "total": n}


def shadow_from_targets(section: str, key: str, value) -> dict:
    """提案目标 → 影子配置 dict（一次评估只含一个键：一个提案一个处理）。"""
    return {str(section): {str(key): value}}


def run_matches_proposal(run: dict, proposal: dict) -> bool:
    """沙箱结论是否与提案**当前**目标一致（ staleness 判据，纯函数）。

    影子配置必须恰好是 {target_section: {target_key: proposed_value}}：
    现场配置变过 ⇒ 提案 proposed 变 ⇒ 不匹配 ⇒ 必须重跑（防拿旧结论
    晋升新值）。两侧都转字符串比——提案表里 proposed_value 是 TEXT，
    影子经 _coerce 后可能是 int/bool。
    """
    try:
        shadow = json.loads(run.get("shadow_json") or "{}")
    except Exception:
        return False
    if not isinstance(shadow, dict):
        return False
    want = shadow_from_targets(proposal.get("target_section", ""),
                               proposal.get("target_key", ""),
                               proposal.get("proposed_value", ""))
    if set(shadow.keys()) != set(want.keys()):
        return False
    for sec, items in want.items():
        got = shadow.get(sec)
        if not isinstance(got, dict) or set(got.keys()) != set(items.keys()):
            return False
        for k, v in items.items():
            if str(got.get(k)) != str(v):
                return False
    return True


def gates_referencing(section: str, key: str, root: Optional[str] = None) -> list:
    """静态引用检测：CI 门禁集里源码引用该 section/key 的门禁（保守下限）。

    判据（命中其一即算引用）：
      · `get("section"` / `as_bool("section"` —— 直接读该分组；
      · `"section.key"` 点路径字面量；
      · `\bkey\b` 键名整词（如 retry_times）。
    ⚠️ 这是**下限**：经 import 链间接读配置的门禁（如跑整条管道的门禁）
    不会被标为 referenced——那正是 full 口径存在的理由。referenced 只用于
    快速预检，晋升前的正式结论建议 full。
    """
    root = root or _root()
    out = []
    sec_pat = re.compile(r"""['"]%s['"]""" % re.escape(section))
    dotted_pat = re.compile(r"""['"]%s\.%s['"]""" % (re.escape(section), re.escape(key)))
    key_pat = re.compile(r"\b%s\b" % re.escape(key))
    for name, path in _gate_universe(root):
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        if dotted_pat.search(src) or key_pat.search(src) or sec_pat.search(src):
            out.append(name)
    return sorted(out)


# ══════════════════════════════════════════════════════════════
# 评估集（CI 口径：ci.yml 执行行 + 各自参数）
# ══════════════════════════════════════════════════════════════
def _gate_universe(root: Optional[str] = None) -> list:
    """全部门禁 [(name, path)]——单一事实源：verify_gate_wiring.scan_gates。

    不另写 glob：门禁宇宙的判定规则（verify_/audit_ 前缀）由元门禁掌管，
    复制一份就会漂移。importlib 按文件路径加载（tools/verify 不是包）。
    """
    root = root or _root()
    gw_path = os.path.join(root, "tools", "verify", "verify_gate_wiring.py")
    spec = importlib.util.spec_from_file_location("_mbse_verify_gate_wiring", gw_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 顶层只定义函数 + sys.path.insert，无副作用
    return mod.scan_gates(root)


def ci_gate_commands(root: Optional[str] = None) -> list:
    """ci.yml 执行行里的门禁命令 → [(name, [argv...])]。

    必须带**原命令行参数**：verify_orchestration_e2e 在 CI 里是
    `--static-only`，裸跑会进 live 模式真调 LLM（烧钱 + 慢）。
    只认 `python tools/verify/X.py [args]` / `python tools/audit_X.py` 形态；
    pytest / init_db 等非门禁行自然不匹配。
    """
    root = root or _root()
    gw_path = os.path.join(root, "tools", "verify", "verify_gate_wiring.py")
    spec = importlib.util.spec_from_file_location("_mbse_verify_gate_wiring", gw_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out, seen = [], set()
    for line in mod.ci_commands(root):
        m = re.search(r"python\s+(tools/(?:verify|audit_)\S+\.py)(.*)", line)
        if not m:
            continue
        rel, argstr = m.group(1), m.group(2).strip()
        name = os.path.basename(rel)[:-3]
        if name in seen:
            continue
        seen.add(name)
        try:
            args = shlex.split(argstr)
        except ValueError:
            args = argstr.split()
        out.append((name, [rel] + args))
    return out


def select_gates(scope: str, section: str = "", key: str = "",
                 root: Optional[str] = None) -> list:
    """按 scope 选评估集 → [(name, argv)]。scope 非法 ⇒ ValueError（fail-loud）。"""
    if scope not in SCOPES:
        raise ValueError("未知评估范围: %r（可选: %s）" % (scope, "/".join(SCOPES)))
    cmds = ci_gate_commands(root)
    if scope == SCOPE_FULL:
        return cmds
    ref = set(gates_referencing(section, key, root))
    return [(n, a) for n, a in cmds if n in ref]


# ══════════════════════════════════════════════════════════════
# I/O 区（子进程执行；生产零副作用）
# ══════════════════════════════════════════════════════════════
def _gate_env(arm_db: str, arm_cfg: str, shadow: Optional[dict],
              base_env: dict) -> dict:
    """构造门禁子进程环境：每臂独立库 + 独立配置副本；影子只进实验组。"""
    env = dict(base_env or os.environ)
    env["MBSE_DB_PATH"] = arm_db
    env["MBSE_CONFIG_PATH"] = arm_cfg
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("MBSE_SHADOW_CONFIG", None)       # 对照组显式剥净（防继承污染）
    if shadow is not None:
        env["MBSE_SHADOW_CONFIG"] = json.dumps(shadow, ensure_ascii=False)
    return env


def _source_fingerprint() -> dict:
    """工作树内容指纹（git 跟踪文件的 sha1）——检测**评估期间源码被外部修改**。

    实测踩到两次（2026-10-19 首跑）：评估进行到一半，
    llm/providers/openai_compat.py 被外部编辑改坏（`_think_cap = 0` →
    `0 = 0`，SyntaxError），对照组门禁在改坏前跑（过）、实验组同门禁在
    改坏后跑（败）⇒ 差分出**假回归**。源码静态是差分有效性的前提。

    为什么不用 `git status --porcelain` 比对：它只反映"脏/干净"状态，
    文件在快照间隙里 dirty→clean→dirty 翻转（外部编辑写坏又改回）时
    前后两份 porcelain 可能都是"干净"——实测正是漏过了这种翻转。
    ⇒ 直接对**全部跟踪文件内容**做 sha1（20MB/1051 文件 ≈0.2s，
    一次评估跑分钟级，成本可忽略）。git 不可用 ⇒ available=False，
    跳过检测并在 summary 注明（诚实降级，不假装检过）。
    """
    try:
        root = _root()
        r = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return {"available": False}
        h = hashlib.sha1()
        for rel in (r.stdout or b"").decode("utf-8", "replace").split("\0"):
            if not rel:
                continue
            p = os.path.join(root, rel)
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                data = b"<unreadable>"
            h.update(rel.encode("utf-8"))
            h.update(str(len(data)).encode("ascii"))
            h.update(data)
        return {"available": True, "sha1": h.hexdigest()}
    except Exception:
        return {"available": False}


def _init_clean_db(path: str, base_env: dict) -> None:
    """子进程里建干净库（schema + seeds；CI 同口径：init_db 后门禁才跑）。

    必须在子进程：core.config.DB_PATH 是 import 期绑定的，引擎进程内
    已绑定生产库，改不了。
    """
    code = ("import os;os.environ['MBSE_DB_PATH']=%r;"
            "from database import init_db;init_db()" % path)
    env = dict(base_env or os.environ)
    env.pop("MBSE_SHADOW_CONFIG", None)
    r = subprocess.run([sys.executable, "-c", code], cwd=_root(), env=env,
                       capture_output=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(path):
        raise RuntimeError("干净库初始化失败: %s"
                           % (r.stderr or b"")[-300:].decode("utf-8", "replace"))


def _run_one_gate(name: str, argv: list, env: dict, timeout_s: int) -> dict:
    """跑一个门禁子进程 → {gate, status, ms, detail}。退出码即判定（CI 同口径）。"""
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable] + argv, cwd=_root(), env=env,
                           capture_output=True, timeout=max(5, int(timeout_s)))
        ok = r.returncode == 0
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        detail = out[-700:]
        if not ok:
            fail_lines = [ln for ln in out.splitlines() if "FAIL" in ln]
            detail = ("\n".join(fail_lines[-6:]) if fail_lines else detail)
            if err.strip():
                detail += "\n[stderr] " + err[-300:]
        return {"gate": name, "status": G_PASS if ok else G_FAIL,
                "ms": int((time.time() - t0) * 1000), "detail": detail[-900:]}
    except subprocess.TimeoutExpired:
        return {"gate": name, "status": G_ERROR, "ms": int((time.time() - t0) * 1000),
                "detail": "timeout >%ss" % int(timeout_s)}
    except Exception as e:                      # noqa: BLE001  拉起失败也算 error
        return {"gate": name, "status": G_ERROR, "ms": int((time.time() - t0) * 1000),
                "detail": "spawn failed: %s: %s" % (type(e).__name__, e)}


def _prepare_arms(tmpdir: str, base_db: str, base_env: dict) -> dict:
    """每臂：独立库副本 + 独立配置副本（有门禁 save_override 写真配置）。"""
    from core import config as _config
    arms = {}
    for tag in ("a", "b"):
        db = os.path.join(tmpdir, "arm_%s.db" % tag)
        shutil.copyfile(base_db, db)
        cfg = os.path.join(tmpdir, "arm_%s_config.json" % tag)
        src_cfg = _config.CONFIG_PATH
        if src_cfg and os.path.exists(src_cfg):
            shutil.copyfile(src_cfg, cfg)
        arms[tag] = {"db": db, "cfg": cfg}
    return arms


def _run_arm(tag: str, gates: list, env: dict, timeout_s: int,
             results: dict, done_box: list, total: int,
             progress: Optional[Callable]) -> None:
    """单臂：顺序跑全部门禁（CI 口径：同库顺序执行，门禁间允许有状态继承）。

    ★ 为什么双臂只能**顺序**跑（2026-10-19 实测踩坑）：门禁的工作目录是
    `ROOT/tmp/verify_*`——**固定路径、两臂共享**。并行跑同一门禁时两臂
    互相删对方的目录（verify_attribution / verify_config_proposal 的
    B' 段"删库重建"实测把对方搞失败，差分出假回归）。门禁按 CI 口径
    就是顺序消费者，这里不发明并发。
    """
    for name, argv in gates:
        res = _run_one_gate(name, argv, env, timeout_s)
        results.setdefault(name, {})[tag] = res
        done_box[0] += 1
        if progress:
            try:
                progress(min(99, int(100 * done_box[0] / max(1, total * 2))),
                         "门禁 %s（%s 臂）%s" % (name, "实验" if tag == "b" else "对照",
                                               res["status"]))
            except Exception:
                pass


# ── 运行记录生命周期 ────────────────────────────────────────────────
def start_run(conn, proposal_id: int, scope: str = "", *, settings: Optional[dict] = None) -> dict:
    """创建沙箱运行记录（status=queued）。只写 sandbox_runs，不跑门禁。

    校验（fail-closed）：沙箱开关、提案存在且 pending、目标是已登记配置键。
    scope 空 ⇒ 配置默认口径。影子配置由提案当前 target/proposed 生成
    （评估的永远是「这个提案现在想改成的值」）。
    """
    from repositories.proposal_repo import ProposalRepo
    from repositories.sandbox_repo import SandboxRepo
    from core import config_promotion as _promo
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "沙箱评估已停用（sandbox.enabled=False）"}
    scope = (scope or cfg.get("default_scope") or SCOPE_FULL).strip()
    if scope not in SCOPES:
        return {"ok": False, "error": "未知评估范围: %r（可选: %s）"
                % (scope, "/".join(SCOPES))}
    row = ProposalRepo(conn).get_proposal(int(proposal_id))
    if not row:
        return {"ok": False, "error": "提案不存在"}
    from core import config_proposal as _cp
    if not _cp.can_decide(row.get("status")):
        return {"ok": False, "error": "提案已决（%s），终态不重开" % row.get("status")}
    section, key = row.get("target_section", ""), row.get("target_key", "")
    if not _promo.is_config_target(section, key):
        return {"ok": False, "error":
                "目标 %s.%s 不是已登记配置键（非配置目标没有可评估的配置变更）"
                % (section, key)}
    shadow = shadow_from_targets(section, key, row.get("proposed_value", ""))
    repo = SandboxRepo(conn)
    run = repo.insert_run(proposal_id=int(proposal_id), scope=scope,
                          shadow_json=json.dumps(shadow, ensure_ascii=False))
    return {"ok": True, "run": run}


def evaluate(conn, run_id: int, *, job: Optional[dict] = None,
             progress: Optional[Callable] = None,
             settings: Optional[dict] = None,
             gates: Optional[list] = None) -> dict:
    """执行一次沙箱 A/B（重活：分钟级；由作业队列 handler 调用）。

    流程：选评估集 → 干净库快照 → 每臂独立库/配置副本 → 双臂跑门禁
    （双臂顺序）→ 源码变异检测 → 差分判定 → 落 sandbox_runs。任何一步
    异常 ⇒ status=failed + error（不伪装成某种 verdict）。

    gates：评估集注入（[(name, argv)]）——**门禁/测试缝**，传入则跳过
    select_gates（生产路径一律不传，走 scope 口径）。没有它，验证脚本
    为了跑端到端就得真跑 158 个门禁。
    """
    from repositories.sandbox_repo import SandboxRepo
    cfg = settings or get_settings()
    repo = SandboxRepo(conn)
    run = repo.get_run(int(run_id))
    if not run:
        raise ValueError("沙箱运行不存在: #%s" % run_id)
    if run.get("status") in (RUN_DONE, RUN_FAILED):
        return run                      # 终态不重跑（作业重试安全）
    if job:
        repo.stamp_job(int(run_id), int(job.get("id") or 0))
    shadow = json.loads(run.get("shadow_json") or "{}")
    # 被评估的键以运行记录里的影子配置为准（提案可能已被现场配置变化改写）
    section = next(iter(shadow.keys()), "")
    key = next(iter((shadow.get(section) or {}).keys()), "")
    repo.mark_running(int(run_id))
    tmpdir = ""
    snap_before = _source_fingerprint()
    try:
        if progress:
            progress(2, "选择评估集（%s 口径）…" % run.get("scope"))
        if gates is None:      # 显式 None 才走 scope 口径；注入 [] = 测试缝传空 ⇒ 空集检查负责判红
            gates = select_gates(run.get("scope") or SCOPE_FULL, section, key)
        if not gates:
            raise RuntimeError("评估集为空（scope=%s, section=%s, key=%s）"
                               % (run.get("scope"), section, key))
        tmpdir = tempfile.mkdtemp(prefix="mbse_sandbox_")
        base_db = os.path.join(tmpdir, "base.db")
        _init_clean_db(base_db, os.environ)
        arms = _prepare_arms(tmpdir, base_db, os.environ)
        env_a = _gate_env(arms["a"]["db"], arms["a"]["cfg"], None, os.environ)
        env_b = _gate_env(arms["b"]["db"], arms["b"]["cfg"], shadow, os.environ)
        if progress:
            progress(5, "评估集 %d 个门禁，开始双臂运行（顺序：门禁共享 ROOT/tmp）…"
                    % len(gates))
        results: dict = {}
        done_box = [0]
        t0 = time.time()
        _run_arm("a", gates, env_a, int(cfg["gate_timeout_s"]), results,
                 done_box, len(gates), progress)
        _run_arm("b", gates, env_b, int(cfg["gate_timeout_s"]), results,
                 done_box, len(gates), progress)
        # ★ 源码变异检测：跑到一半被外部编辑 ⇒ 差分污染 ⇒ 结论无效（实测踩到两次）
        snap_after = _source_fingerprint()
        if (snap_before.get("available") and snap_after.get("available")
                and snap_before.get("sha1") != snap_after.get("sha1")):
            raise RuntimeError(
                "源码/工作树在评估期间被修改（外部编辑）——差分结论无效，请重跑。"
                "评估期间请勿改动仓库（变更前 sha1=%s…；变更后 sha1=%s…）"
                % (snap_before.get("sha1", "")[:12], snap_after.get("sha1", "")[:12]))
        merged = []
        for name, _argv in gates:
            ra = (results.get(name) or {}).get("a") or {"status": G_ERROR, "detail": "未执行"}
            rb = (results.get(name) or {}).get("b") or {"status": G_ERROR, "detail": "未执行"}
            merged.append({"gate": name, "a": ra.get("status"), "b": rb.get("status"),
                           "a_ms": ra.get("ms", 0), "b_ms": rb.get("ms", 0),
                           "a_detail": ra.get("detail", "")[-300:],
                           "b_detail": rb.get("detail", "")[-300:]})
        verdict = differential_verdict(merged, cfg.get("infra_error_ratio", 0.3))
        duration = int(time.time() - t0)
        # 报告按严重度排序后截断（回归永远在报告里，stable_pass 让位）
        order = {CAT_REGRESSION: 0, CAT_IMPROVEMENT: 1, CAT_MIXED: 2,
                 CAT_INFRA_ERROR: 3, CAT_STABLE_FAIL: 4, CAT_STABLE_PASS: 5}
        merged.sort(key=lambda r: (order.get(r.get("category"), 9), r["gate"]))
        cap = int(cfg.get("max_report_gates") or 400)
        summary = {"scope": run.get("scope"), "gates_total": len(gates),
                   "verdict": verdict["verdict"], "reason": verdict["reason"],
                   "counts": verdict["counts"], "duration_s": duration,
                   "shadow": shadow, "section": section, "key": key,
                   "mutation_check": ("clean" if snap_before.get("available")
                                      else "unavailable"),
                   "gates": merged[:cap],
                   "truncated": max(0, len(merged) - cap)}
        repo.finish_run(int(run_id), status=RUN_DONE,
                        verdict=verdict["verdict"],
                        summary_json=json.dumps(summary, ensure_ascii=False))
        if progress:
            progress(100, "完成：%s（%s）" % (verdict["verdict"], verdict["reason"] or "无回归"))
        return repo.get_run(int(run_id))
    except Exception as e:                              # noqa: BLE001
        repo.finish_run(int(run_id), status=RUN_FAILED, verdict="",
                        summary_json=json.dumps(
                            {"error": "%s: %s" % (type(e).__name__, str(e)[:400])},
                            ensure_ascii=False))
        raise
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def get_run(conn, run_id: int) -> Optional[dict]:
    from repositories.sandbox_repo import SandboxRepo
    return SandboxRepo(conn).get_run(int(run_id))


def list_runs(conn, proposal_id: int = 0, limit: int = 50) -> list:
    from repositories.sandbox_repo import SandboxRepo
    return SandboxRepo(conn).list_runs(proposal_id=int(proposal_id or 0),
                                       limit=limit)


def latest_passing_run(conn, proposal_id: int) -> Optional[dict]:
    """该提案最新一条 verdict=pass 的运行（晋升前置③用）。"""
    for r in list_runs(conn, proposal_id=proposal_id, limit=50):
        if r.get("verdict") == VERDICT_PASS and r.get("status") == RUN_DONE:
            return r
    return None
