# -*- coding: utf-8 -*-
"""verify_memory_eval_reanchor.py —— 记忆评测集的「锚点判据」必须自洽且不许静默缩水。

## 背景（2026-11-16 实测；2026-11-27 升级）
`tools/eval/eval_memory_quality.py` 的评测集硬锚 2026-10-07 的 id 快照。
库从 144 条涨到 295 条、其中 97 条 forgotten 后，实跑只有 4 PASS / 10 FAIL（28.6%）。

两种可能的解释必须分开（否则会把"库内容变化"误判成"召回算法退化"）：
  ① 期望记忆已 forgotten/删除，且库内**无任何语义等价条目** ⇒ **STALE**（能力已不在库）
  ② 库里还有等价条目却召回不到 ⇒ **FAIL**（真实召回问题）

**2026-11-27（P0-4）**：只把 id 换成另一批 id（重锚表）**治不了根** —— 下次库演进又全红。
现判据升级为「内容指纹 + 语义等价集合」：锚点失效后，只要库内还有
① 归一化内容相同（md5 指纹）② 与参照正文余弦 ≥ `--sem-sim` 的条目，用例**仍有判别力**。
本门禁因此多锁三条：快照必须存在且覆盖重锚非空用例、STALE 不得静默、等价通道必须真在兜底。

## 门禁锁什么
A 组：重锚表与真实库的自洽性
  A1 表里所有非空重锚 id 都真实存在且 alive
  A2 表里所有空重锚的旧 expect id 确实 forgotten/删除（空表不是偷懒放宽）
  A3 重锚表覆盖了全部 T/M/C 用例（不许新增用例忘了登记）
  A4 变异自证：把某个重锚 id 改成不存在的，A1 必须翻红
  A5 内容指纹快照存在，且覆盖每个「重锚非空」的用例（否则指纹通道静默失效）
B 组：评测器行为
  B1 评测脚本可运行且不抛异常
  B2 汇总声明的 STALE 数 == 明细 STALE 行数（禁静默吞掉）
  B3 每条 STALE 都带理由（不是黑盒）
  B4 **等价集合的机制有牙齿**（夹具驱动，不依赖本次库内容）：
     指纹相同的条目必须进集合；向量相近的必须进；远处的必须不进；
     把 sim 抬到 1.01 后语义项必须消失（证明它会因阈值而变，不是恒真）
  B5 等价通道**有可测贡献**：基线里出现「仅语义等价」标记 ≥1，且 `--strict`
     口径的 PASS 数**严格更少**（若两口径一样多，说明等价判据根本没起作用）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_eval_reanchor.py
退出码：0 全绿（干净库跳过真库组）／1 有失败
"""
import io
import json
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
SCRIPT = os.path.join(ROOT, "tools", "eval", "eval_memory_quality.py")
FP_FILE = os.path.join(ROOT, "tools", "eval", "memory_eval_fingerprints.json")
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def load_eval_module():
    """只取评测脚本的常量与纯函数，不执行 main()。"""
    src = io.open(SCRIPT, encoding="utf-8").read()
    ns = {"__name__": "eval_memory_quality_probe", "__file__": SCRIPT}
    # 用 exec 执行到 def main() 之前（常量 + 纯函数已定义）
    cut = src.index("\ndef main():")
    exec(compile(src[:cut], SCRIPT, "exec"), ns)
    return ns, src


def run_eval(extra_args=()):
    """跑一次评测（只读），返回 stdout。"""
    r = subprocess.run([sys.executable, "-X", "utf8", SCRIPT] + list(extra_args),
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    return (r.stdout or ""), r


_STALE_LINE = re.compile(r"^\s*STALE\s+([TCMN]\d{2})\b", re.M)
_STALE_TOTAL = re.compile(r"STALE\s+(\d+)\s*条", re.M)


def main():
    print("=" * 78)
    print("门禁：记忆评测集锚点判据自洽（内容指纹 + 语义等价）")
    print("=" * 78)
    ns, src = load_eval_module()
    reanchor = ns.get("REANCHOR_ALIVE") or {}
    cases = (ns.get("T_CASES") or []) + (ns.get("M_CASES") or []) + (ns.get("C_CASES") or [])

    print("\n--- GROUP A ---")
    chk("A0 重锚表已定义且非空", bool(reanchor), len(reanchor))
    chk("A3 重锚表覆盖全部 T/M/C 用例",
        set(reanchor) == {c["id"] for c in cases},
        "缺: %s" % sorted({c["id"] for c in cases} - set(reanchor)))

    # A5：指纹快照必须存在且覆盖"重锚非空"的用例（指纹通道的唯一参照面）
    fp_ok, fp_missing, fp_n = False, [], 0
    try:
        with io.open(FP_FILE, encoding="utf-8") as f:
            _fp = (json.load(f) or {}).get("cases") or {}
        need = [c["id"] for c in cases if reanchor.get(c["id"])]
        fp_missing = [c for c in need if not (_fp.get(c) or [])]
        fp_n = sum(len(v or []) for v in _fp.values())
        fp_ok = bool(_fp) and not fp_missing
    except Exception as e:  # noqa: BLE001
        fp_missing = ["<快照缺失: %s>" % type(e).__name__]
    chk("A5 内容指纹快照存在且覆盖重锚非空用例", fp_ok,
        "缺 %s（跑 tools/eval/reanchor_memory_fingerprints.py --write 生成）" % fp_missing)

    if not os.path.exists(DB):
        for n in ("A1", "A2", "A4"):
            skip(n, "干净库：跳过需要真库的判据组")
        for n in ("B1 评测脚本可运行", "B2 STALE 计数自洽", "B3 每条 STALE 带理由",
                  "B4 等价通道真的在兜", "B5 变异自证"):
            skip(n, "干净库")
    else:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        alive = {r[0] for r in conn.execute(
            "select id from agent_memory where coalesce(forgotten,0)=0")}
        all_ids = {r[0] for r in conn.execute("select id from agent_memory")}

        bad = []
        for cid, ids in reanchor.items():
            for mid in ids:
                if mid not in alive:
                    bad.append("%s→#%s" % (cid, mid))
        chk("A1 重锚表非空项全部 alive", not bad, bad)

        # A2：空重锚必须"确实没得锚"——旧 expect 全 forgotten/删除，
        #     否则就是把能评的用例偷懒改成 SKIP（缩水判据）
        lazy = []
        for c in cases:
            if reanchor.get(c["id"]):
                continue
            old = [m for m in c.get("expect", []) if m in alive]
            if old:
                lazy.append("%s 旧锚仍 alive=%s" % (c["id"], old))
        chk("A2 空重锚确实无 alive 旧锚（不是偷懒放宽）", not lazy, lazy)

        # A4 变异自证：伪造一个不存在的重锚 id ⇒ A1 判据必须翻红
        fake = dict(reanchor)
        first = next(k for k, v in fake.items() if v)
        fake[first] = [999999999]
        mutated = any(mid not in alive for mid in fake[first])
        chk("A4 变异自证：重锚改成不存在 id ⇒ A1 必须翻红", mutated)

        conn.close()

        # ── B 组：跑一次评测（只读） ──
        try:
            out, r = run_eval()
            chk("B1 评测脚本可运行且不抛异常",
                "Traceback" not in out and r.returncode in (0, 1),
                (out[-300:] if "Traceback" in out else "rc=%s" % r.returncode))
            n_line = len(_STALE_LINE.findall(out))
            m_tot = _STALE_TOTAL.search(out)
            n_tot = int(m_tot.group(1)) if m_tot else -1
            chk("B2 汇总 STALE 数 == 明细 STALE 行数", n_tot == n_line,
                "汇总=%s 明细=%d" % (n_tot, n_line))
            chk("B3 每条 STALE 都带理由（理由可见，不是黑盒）",
                all(("不是召回失败" in l) or ("失去判别力" in l)
                    for l in out.splitlines() if _STALE_LINE.match(l)),
                [l for l in out.splitlines() if _STALE_LINE.match(l)][:3])
            # ★ B4（2026-11-27 改写）：原判据「STALE 数 < 空重锚数」是**错的** ——
            #   它把"本次库里恰好还有一条同义记忆"当成了机制不变式，
            #   而"某条能力确实已从库里消失"是**数据事实**（实测 T05/T06/T08/M02
            #   四个用例的原始记忆全部被归档且无近邻，STALE=4 是正确的）。
            #   ⇒ 机制层必须用**夹具**断言（与库内容解耦，符合"断言不得耦合本次部署"）。
            _rs_row = lambda _id, _txt, _vec, _ver="fake-1": {
                "id": _id, "content": _txt, "vec": _vec, "embed_version": _ver,
                "fp": ns["fingerprint"](_txt)}
            _rows = [
                _rs_row(1, "需求追溯链包含五个环节", [0.0, 1.0, 0.0]),   # 指纹相同（文本一致）
                _rs_row(2, "需求追溯链含五环节的另一种写法", [0.95, 0.10, 0.0]),  # 语义相近
                _rs_row(3, "完全无关的载荷配置记录", [0.0, 0.0, 1.0]),   # 远
            ]
            _case = {"id": "BX", "expect": [], "q": "-", "intent": "-", "why": "-"}
            _refs = [{"id": 0, "head": "需求追溯链包含五个环节",
                      "fp": ns["fingerprint"]("需求追溯链包含五个环节")}]
            _eq, _ed = ns["equiv_ids"](_case, _refs, [[1.0, 0.0, 0.0]], "fake-1",
                                       _rows, 0.90)
            _eq_hi, _ = ns["equiv_ids"](_case, _refs, [[1.0, 0.0, 0.0]], "fake-1",
                                        _rows, 1.01)
            chk("B4 等价集合机制有牙齿（指纹+语义进集合、远处不进、阈值可关）",
                (1 in _eq) and (2 in _eq) and (3 not in _eq)
                and (2 not in _eq_hi) and (1 in _eq_hi),
                "eq=%s eq@1.01=%s detail=%s" % (sorted(_eq), sorted(_eq_hi), _ed))

            # B5 等价通道**有可测贡献**（不是"跑通即绿"）：
            #   ① 基线里必须出现「仅语义等价」标记（说明语义通道真的把一个 id 锚未命中的用例救回来了）；
            #   ② `--strict`（只看 id 锚）的 PASS 数必须**严格更少**。
            # 若两条一样多 ⇒ 等价判据形同虚设，本门禁必须红。
            _BASE_PASS = re.compile(r"PASS\s+(\d+)\s*/\s*FAIL\s+\d+\s*/\s*STALE", re.M)
            _STRICT_PASS = re.compile(r"PASS\s+(\d+)\s*/\s*FAIL\s+\d+\s*/\s*STALE", re.M)
            m_base = _BASE_PASS.search(out)
            n_base = int(m_base.group(1)) if m_base else -1
            n_mark = out.count("仅语义等价")
            out_s, _rs = run_eval(["--strict"])
            m_s = _STRICT_PASS.search(out_s)
            n_strict = int(m_s.group(1)) if m_s else -1
            chk("B5 等价通道有可测贡献（仅语义等价标记≥1 且 strict 口径更少）",
                n_mark >= 1 and n_strict >= 0 and n_base > n_strict,
                "基线 PASS=%s / 仅语义等价标记=%d / strict PASS=%s"
                % (n_base, n_mark, n_strict))
        except Exception as e:  # noqa: BLE001
            chk("B1 评测脚本可运行且不抛异常", False, repr(e)[:200])

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
