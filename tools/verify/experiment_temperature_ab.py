# -*- coding: utf-8 -*-
"""P0 真机 A/B：temperature 对 SysML v2 产码质量的影响（2026-11-13，第 2 版）。

**第 1 版为何作废**（实测留证，勿重犯）：
  第 1 版 max_tokens=8192 ⇒ 思考与正文共享配额被吃光 ⇒
  6 次里 4 次正文为空（sha=e3b0c44…=空串）、2 次被中途截断（末行未闭合）⇒
  **测到的是配额问题，不是温度问题**。第 1 版结论全部作废。

**第 2 版受控条件**：
  · 唯一变量 = temperature（0.3 = 平台当前默认 / 0.0 = 贪心解码）
  · 两组共用：同一 provider、同一 prompt、同一 max_tokens=32768、**显式关闭思考**
    （思考随机性会污染温度对照，故隔离掉）
  · REPEAT=3 每组

判据五条：
  ① 复现性：同组 3 次输出 sha256 的唯一值数（1 = 完全可复现）
  ② 正确性：checker.jar 实测 词法/语法/语义 错误数
  ③ 截断率：finish_reason == 'length' 的次数
  ④ 要素齐全率：package/stakeholder/requirement def/part def/satisfy 等是否齐备
  ⑤ 错误模式：具体错在哪（便于决定要不要回灌技能提示词）

产物落盘 outputs/temp_ab/，逐份可复核（不靠摘要下结论）。
"""
import hashlib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "outputs", "temp_ab")
os.makedirs(OUT, exist_ok=True)

from llm import resolve_provider_cfg  # noqa: E402
from llm.providers.openai_compat import OpenAICompatProvider  # noqa: E402
import sysml_v2_check as chk  # noqa: E402

PID = 71
PROMPT = """请生成 SysML v2 的**需求视图**模型代码，只输出代码本身，不要任何解释文字。

场景：车载儿童遗留保护系统（LBP）。

需求条目：
1. 系统应在车辆熄火后 2 分钟内检测到后排遗留的儿童，并发出声光警报。
   涉众：车辆安全工程师。验证方式：测试。
2. 系统应记录每次警报的触发时间与环境温度传感器读数，并可供事后追溯。

代码要求（SysML v2 文本表示）：
- 用 package 承载模型，包名 RequirementModel
- 声明 stakeholder（车辆安全工程师）
- 用 requirement def 给出每条需求（带唯一 id 与描述）
- 用 requirement usage 引用这些需求
- 建立 satisfy 关系把需求满足到某个 part

严格遵守 SysML v2 语法（kernel 语义 + 标准库用法）。"""

GROUPS = [("A_temp0.3", 0.3), ("B_temp0.0", 0.0)]
REPEAT = 3
MAX_TOKENS = 32768
# 两组共用：显式关闭思考（隔离思考随机性；实测上游接受该字段）
MP = {"thinking_mode": "关闭"}

cfg = resolve_provider_cfg(PID)
if not cfg:
    print("!! 取不到 provider %s" % PID)
    sys.exit(2)
print("provider=%s model=%s" % (cfg.get("name"), cfg.get("model_name")))
print("checker 可用: %s ｜ max_tokens=%d ｜ thinking=关闭" % (chk.available(), MAX_TOKENS))
print("=" * 78)

rows = []
for gname, temp in GROUPS:
    for i in range(1, REPEAT + 1):
        c = dict(cfg)
        c["model_params"] = json.dumps(MP, ensure_ascii=False)
        t0 = time.time()
        p = OpenAICompatProvider(c)
        try:
            resp = p.chat([{"role": "user", "content": PROMPT}],
                          temperature=temp, max_tokens=MAX_TOKENS, _intent="verify_temp_ab")
            msg = (resp["choices"][0] or {}).get("message") or {}
            txt = msg.get("content") or ""
            finish = resp["choices"][0].get("finish_reason") or ""
            usage = resp.get("usage") or {}
        except Exception as e:  # noqa: BLE001
            print("  [%s#%d] 调用失败: %s" % (gname, i, e))
            rows.append({"group": gname, "i": i, "temp": temp, "error": str(e)})
            continue
        dt_gen = time.time() - t0
        fn = os.path.join(OUT, "%s_%d.sysml" % (gname, i))
        with open(fn, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        sha = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:16]

        t1 = time.time()
        res = chk.check_code(txt, use_cache=False)
        dt_chk = time.time() - t1

        code = txt
        m = code.find("```")
        if m >= 0:
            e2 = code.find("```", m + 3)
            if e2 > m:
                code = code[m + 3:e2]
        feat = {
            "package": "package" in code,
            "stakeholder": "stakeholder" in code,
            "requirement_def": "requirement def" in code,
            "requirement_usage": bool(("requirement usage" in code)
                                      or re.search(r"^\s*requirement\s+\w+\s*:", code, re.M)),
            "satisfy": "satisfy" in code,
            "part_def": "part def" in code,
        }
        # 实测到的真实错误模式（SysML v2 里 stakeholder 不可 usage 化）
        misuse = []
        if re.search(r"^\s*stakeholder\s+\w+\s*:", code, re.M):
            misuse.append("stakeholder_usage(非法)")
        if re.search(r"^\s*import\s", code, re.M):
            misuse.append("import_in_package(非法层级)")
        diags = chk.diagnostics(res, max_items=6) if res.get("n_error") else []
        msgs = []
        for d in diags:
            try:
                msgs.append("L%s %s: %s" % (d.get("line"), (d.get("messages") or [""])[0][:70], d.get("source", "")[:40]))
            except Exception:
                pass
        row = {
            "group": gname, "i": i, "temp": temp, "sha": sha, "chars": len(txt),
            "finish": finish, "gen_s": round(dt_gen, 1), "chk_s": round(dt_chk, 1),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "n_error": res.get("n_error"), "n_lexical": res.get("n_lexical"),
            "n_syntax": res.get("n_syntax"), "n_semantic": res.get("n_semantic"),
            "n_warn": res.get("n_warn"), "verdict": res.get("verdict"),
            "feat": feat, "misuse": misuse, "diag": msgs, "file": fn,
        }
        rows.append(row)
        print("  [%s#%d] temp=%s sha=%s chars=%d finish=%s gen=%.1fs chk=%.1fs "
              "ERROR=%s(词%s/语%s/义%s) feat=%s%s"
              % (gname, i, temp, sha, len(txt), finish, dt_gen, dt_chk,
                 res.get("n_error"), res.get("n_lexical"), res.get("n_syntax"),
                 res.get("n_semantic"),
                 "".join(k[0].upper() if v else "-" for k, v in feat.items()),
                 (" 误用:" + ",".join(misuse)) if misuse else ""))
        for msg in msgs:
            print("        · %s" % msg)

print("=" * 78)
print("汇总")
for gname, temp in GROUPS:
    rs = [r for r in rows if r["group"] == gname and "sha" in r]
    if not rs:
        continue
    shas = {r["sha"] for r in rs}
    errs = [r["n_error"] for r in rs]
    syn = [r["n_syntax"] for r in rs]
    lex = [r["n_lexical"] for r in rs]
    sem = [r["n_semantic"] for r in rs]
    chars = [r["chars"] for r in rs]
    gens = [r["gen_s"] for r in rs]
    trunc = sum(1 for r in rs if r.get("finish") == "length")
    empty = sum(1 for r in rs if not r.get("chars"))
    ok_feat = sum(1 for r in rs if all(r["feat"].values()))
    misuse = {}
    for r in rs:
        for m in r.get("misuse", []):
            misuse[m] = misuse.get(m, 0) + 1
    print("  %s temp=%s" % (gname, temp))
    print("     复现性  : %d 次 → %d 个唯一 sha %s"
          % (len(rs), len(shas), "（完全可复现）" if len(shas) == 1 else "（有分化）"))
    print("     错误数  : ERROR %s ｜ 词法 %s ｜ 语法 %s ｜ 语义 %s" % (errs, lex, syn, sem))
    print("     空输出/截断: 空 %d 次 ｜ 截断 %d 次" % (empty, trunc))
    print("     长度/耗时: %s ｜ %s" % (chars, gens))
    print("     要素齐全(6/6): %d/%d ｜ 误用模式: %s" % (ok_feat, len(rs), misuse or "无"))

with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
print("产物目录：%s" % OUT)
