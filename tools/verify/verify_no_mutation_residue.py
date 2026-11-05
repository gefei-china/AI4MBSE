# -*- coding: utf-8 -*-
"""门禁：产品文件**不得**处于「变异体」状态（变异门禁被中断的残留）。

## 为什么需要这个门禁（2026-10-26 实测事故）

`tools/verify/verify_p1_project_attribution_mutate.py` 为了做**变异自证**，会
**原地改写产品源文件**（备份到 `tmp/_mut_bak_p1/`，跑完再还原）。它的还原做得对：
`try/finally` + 还原后逐字节校验 + 不一致就判红。

**但运行窗口内，产品文件就是坏的。** 2026-10-26 实测踩中：
全量回归正在跑该门禁时，我执行了 `git add -A && git commit` ⇒
**把变异体（`routers/sysml_versions.py` 少了 `v.project_id` 优先的 COALESCE）
提交进了仓库**——提交里存的是"闸门只看会话当前归属"的错版本。

⇒ 症状：提交内容看起来完全正常（只有一行 SQL 变了），但**语义错了**，
而且没人会注意到——这与"验证对象也会错"同族：**你以为你在提交产品改动，
其实提交的是某个门禁的临时变异体。**

## 本门禁守什么

① **签名检查（精确）**：对每个已知的"原地变异"定义，若产品文件里
   **注入串在、而原锚点不在** ⇒ 文件仍是变异体 ⇒ 判红。
   判据来源直接取变异门禁自己的 `MUTATIONS` 列表（`import` 它是**无副作用**的：
   模块级只建目录 + 定义常量，执行体在 `if __name__ == "__main__"` 里），
   ⇒ 变异定义改了，本门禁自动跟着改，**不会漏检**。
② **备份比对（提示）**：把 `tmp/_mut_bak_p1/` 里的备份与当前产品文件逐个比对，
   不一致的列出来（**仅提示**：可能是"跑完之后又正常改过"，不是判据）。

## 判据为什么不能用"内容等于备份"

`shutil.copy2` 保留 mtime ⇒ 正常跑完后「源文件 mtime == 备份 mtime」；
但**跑完之后再正常改一次**（合法改动）也会让两者不一致 ⇒ 无法区分
"残留"与"改过" ⇒ 只能靠 ① 的**签名**判别（合法改动不会恰好引入注入串）。

## 用法

    .venv/Scripts/python.exe -X utf8 tools/verify/verify_no_mutation_residue.py
退出码：0 = 无残留；1 = 有残留（附还原指引）。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
VERIFY_DIR = os.path.join(ROOT, "tools", "verify")
if VERIFY_DIR not in sys.path:
    sys.path.insert(0, VERIFY_DIR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  ← " + str(detail)) if detail else ""))
    return bool(cond)


def load_mutations():
    """从变异门禁里取 MUTATIONS 定义（无副作用 import）。"""
    path = os.path.join(VERIFY_DIR, "verify_p1_project_attribution_mutate.py")
    if not os.path.exists(path):
        return []
    spec = importlib.util.spec_from_file_location("_mut_gate_src", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)          # 模块级只建目录 + 定义常量
    except Exception as e:                    # noqa: BLE001
        print("  [warn] 变异门禁 import 失败：%s" % e)
        return []
    return getattr(mod, "MUTATIONS", []) or []


# ── 其它"原地改写产品文件"的探针（模块级改写，非 verify_* 门禁）──────────
EXTRA_SIGNATURES = [
    ("artifact_tools.py", "if False:",
     "probe_save_artifact.py 把 _at_path 改成 if False:"),
    ("agent/auto_continue.py", "if (False",
     "probe_auto_continue.py 把 _s_path 的门禁改成 if (False"),
]


def main() -> int:
    print("=== 产品文件变异残留检查 ===")
    muts = load_mutations()
    print("  已知原地变异定义：%d 条（来自 verify_p1_project_attribution_mutate）" % len(muts))

    residue = []
    for m in muts:
        f = m.get("file")
        anchor = m.get("anchor") or ""
        inject = m.get("inject") or ""
        if not f or not inject:
            continue
        src = os.path.join(ROOT, f)
        if not os.path.exists(src):
            continue
        try:
            text = open(src, encoding="utf-8").read()
        except Exception as e:                  # noqa: BLE001
            residue.append("%s：读取失败 %s" % (f, e))
            continue
        has_inject = inject in text
        has_anchor = (anchor in text) if anchor else True
        if has_inject and not has_anchor:
            residue.append("%s：仍是变异体（注入串在、锚点不在）— %s" % (f, m.get("tag")))

    for f, marker, why in EXTRA_SIGNATURES:
        src = os.path.join(ROOT, f)
        if not os.path.exists(src):
            continue
        try:
            text = open(src, encoding="utf-8").read()
        except Exception:
            continue
        if marker in text:
            residue.append("%s：含探针注入标记 %r（%s）" % (f, marker, why))

    check("A1 产品文件无变异残留（%d 条已知变异全部还原）" % len(muts),
          not residue, "；".join(residue[:4]))

    # ── A2：已知「门禁自注入」目标：注释块必须恰好一份 ────────────────────
    # 2026-10-26 实测：`verify_quality_review_wiring.py` 每次运行都往
    # `workflows/refine.py` 插一段观测口径提醒（幂等判断写错 ⇒ 永远命中），
    # HEAD 里已积到 6 份（历史人工清过两次都没修根因）⇒ 这里钉住"恰好一份"。
    for _f, _mark, _label in (
            ("workflows/refine.py", "观测口径提醒（2026-10-13 复核）", "plan_eval 观测口径提醒"),
    ):
        _p = os.path.join(ROOT, _f)
        if not os.path.exists(_p):
            continue
        _t = open(_p, encoding="utf-8", errors="ignore").read()
        _n = _t.count(_mark)
        check("A2 %s 的%s**恰好一份**（重复=门禁自注入残留）" % (_f, _label),
              _n == 1, "出现 %d 份" % _n)

    # ── A3：通用扫描 —— 同一文件里**相同的长注释块**出现多次 ⇒ 自注入残留 ──
    _scan_dirs = ("agent", "routers", "core", "services", "workflows", "database",
                  "repositories", "llm", "knowledge_pipeline", "tools")
    _dups = []
    for _d in _scan_dirs:
        _dd = os.path.join(ROOT, _d)
        if not os.path.isdir(_dd):
            continue
        for _dp, _dn, _fn in os.walk(_dd):
            if "__pycache__" in _dp or "verify" in _dp.split(os.sep)[-1:]:
                continue
            for _f in _fn:
                if not _f.endswith(".py"):
                    continue
                _p = os.path.join(_dp, _f)
                try:
                    _lines = open(_p, encoding="utf-8", errors="ignore").read().split("\n")
                except Exception:
                    continue
                _runs, _cur = [], []
                for _ln in _lines:
                    _s = _ln.strip()
                    if _s.startswith("#") and len(_s) > 3:
                        _cur.append(_s)
                    else:
                        if len(_cur) >= 5:
                            _runs.append(_cur)
                        _cur = []
                if len(_cur) >= 5:
                    _runs.append(_cur)
                # ⚠️ 必须用**滑窗**而不是"整段比对"：自注入常把副本插在**紧邻**位置
                #   ⇒ 两份会合并成一段 10 行注释 ⇒ 整段比对看不见重复（实测踩到）。
                #   滑窗（5 行窗口）⇒ 相邻/分离的副本都能抓到。
                _win = {}
                for _r in _runs:
                    for _i in range(len(_r) - 4):
                        _w = tuple(_r[_i:_i + 5])
                        _win[_w] = _win.get(_w, 0) + 1
                for _w, _c in _win.items():
                    if _c > 1:
                        _dups.append("%s：同一 5 行注释窗口重复 %d 次（首行：%s）"
                                     % (os.path.relpath(_p, ROOT), _c, _w[0][:40]))
                        break
    check("A3 产品文件内无**重复的长注释块**（自注入残留的通用特征）",
          not _dups, "；".join(_dups[:4]))

    # ② 备份比对（仅提示，不判红）：不一致可能是"跑完后又正常改过"
    bak_dir = os.path.join(ROOT, "tmp", "_mut_bak_p1")
    if os.path.isdir(bak_dir):
        diff = []
        for fn in sorted(os.listdir(bak_dir)):
            bak = os.path.join(bak_dir, fn)
            if not os.path.isfile(bak):
                continue
            # 备份名 = 路径里的 / 与 \ 换成 _
            rel = fn.replace("_", "/", 1) if fn.startswith(("agent_", "routers_", "repositories_",
                                                           "database_", "m6_database_")) else fn
            cand = os.path.join(ROOT, rel)
            if not os.path.exists(cand):
                for p in ("agent/pipeline_parts/tools.py", "routers/sysml_versions.py",
                          "routers/reports.py", "repositories/artifact_repo.py",
                          "database/migrations/columns.py", "agent/auto_continue.py",
                          "artifact_tools.py"):
                    if os.path.basename(p).replace(".py", "").replace("/", "_") in fn:
                        cand = os.path.join(ROOT, p)
                        break
            if not os.path.exists(cand):
                continue
            try:
                if open(bak, encoding="utf-8", errors="ignore").read() != \
                        open(cand, encoding="utf-8", errors="ignore").read():
                    diff.append(cand)
            except Exception:
                continue
        print("  [提示] 与 tmp/_mut_bak_p1 备份不一致的文件 %d 个%s"
              % (len(diff), ("：%s" % diff[:5]) if diff else ""))

    print()
    if FAIL:
        print("结论：有变异残留 ⇒ 某个变异门禁被中断，产品文件仍是坏版本")
        print("      还原：把对应文件从 tmp/_mut_bak_p1/ 拷回（备份名 = 路径的 / 换成 _）")
        print("      然后重跑该变异门禁确认「还原校验」通过。")
        print("      ⚠️ 若你是**故意**改的这些文件，先确认改动语义再还原。")
        return 1
    print("结论：无残留（PASS %d / FAIL %d）" % (len(PASS), len(FAIL)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
