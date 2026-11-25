# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：P0-4 配置审计防漂移门禁：重扫四组清单与 core/config_audit.py + 清单 JSON 逐项对拍，守 audit_flags 注入生效、schema()/save_override/前端零回归、importlib 独立加载降级不崩（含 9 项变异自证）。静态扫描 + 只读，不调 LLM、不写库；需仓库全量源码故归 local，不占用 broken/backlog 预算。2026-11-15 新增
"""P0-4 常驻门禁：配置审计清单防漂移 + `audit_flags` 注入真的生效 + 零回归。

判「配置标注有没有做成」只看这一节，别再人肉 grep：

1. **清单防漂移**：调用 `tools/_p0_config_audit.py::compute()`（**唯一真源**）重扫当前磁盘，
   与 `core/config_audit.py` 四组常量 + `docs/评测/config_audit_p0-20261115.json` 逐项对拍。
   ⚠️ 只比**集合**，不比「扫描文件数」—— 任何人新增一个 .py 都会让文件数变化，
   把它设成硬断言等于让门禁天天误报。文件数只作为带口径的观测值输出。
2. **注入生效**：正常 `import core.config` 路径下，`CONFIG_SCHEMA` 里带 `audit_flags` 的键集合
   必须与 `config_audit.flags()` 推导出的期望集合**完全一致**（不多不少、值逐个相等）。
3. **零回归**：`schema()` 的 `default` 字段仍等于 `DEFAULT_CONFIG` / meta 内声明值；
   `save_override` 只读 `meta.type`、前端 `35-ctxconfig.js` 只读 `meta.current/default`，
   多出来的 meta 键不会被误当成配置项或表单字段。
4. **降级路径不崩**：少数脚本用 importlib 独立加载 `core/config.py`（无包上下文），
   相对导入会失败；`except` 必须让它安静跳过而不是把配置模块弄崩（子进程实测）。

5. **变异自证（强制）**：所有判据都抽成纯函数，喂篡改输入必须翻红 —— 否则就是假绿。
   变异全部发生在内存里，**磁盘文件一字不动**（§6.2 的孪生体纪律）。

跑法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p0_config_audit.py
口径：2026-11-15 P0-4；集合口径见 tools/_p0_config_audit.py 顶部 docstring。
"""
import ast
import copy
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

AUDIT_JSON = REPO / "docs" / "评测" / "config_audit_p0-20261115.json"
GEN_PY = REPO / "tools" / "_p0_config_audit.py"
CFG_PY = REPO / "core" / "config.py"
FE_JS = REPO / "static" / "js" / "mods" / "35-ctxconfig.js"
SELF_REL = "tools/verify/verify_p0_config_audit.py"
GEN_REL = "tools/_p0_config_audit.py"

GROUPS = (("schema_only", "SCHEMA_ONLY_KEYS"),
          ("no_business_consumer", "NO_BUSINESS_CONSUMER_KEYS"),
          ("dead_config_suspect", "DEAD_CONFIG_SUSPECT_KEYS"),
          ("suspect", "SUSPECT_KEYS"))

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


def load_generator():
    spec = importlib.util.spec_from_file_location("p0_cfg_audit_gen", GEN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 纯判据（变异测试直接喂篡改输入，证明有牙齿）────────────────────────────
def compare(calc, stored, payload, skip_files):
    """返回问题清单（空 = 一致）。只比集合与计数，不比扫描文件数。"""
    problems = []
    for key, attr in GROUPS:
        a = list(calc[key])
        b = list(getattr(stored, attr, ()) or ())
        if a != b:
            problems.append("重扫 %s 与 config_audit.py 不一致（%d vs %d）" % (key, len(a), len(b)))
        if list(payload.get(key) or []) != a:
            problems.append("清单 JSON 的 %s 与重扫不一致" % key)
    for k, v in calc["counts"].items():
        if payload.get("counts", {}).get(k) != v:
            problems.append("清单 JSON counts.%s 与重扫不一致" % k)
    for rel in (GEN_REL, SELF_REL):
        if rel not in skip_files:
            problems.append("审计工具未被排除出扫描口径，清单会自污染：%s" % rel)
    return problems


def injection_ok(schema, flags_fn):
    """注入判据：标记集合 == 清单推导的期望集合，且每个值都逐字相等。"""
    expect = {(s, k) for s, items in schema.items() for k in items if flags_fn(s, k)}
    marked = {(s, k) for s, items in schema.items() for k, m in items.items()
              if m.get("audit_flags")}
    if not expect or marked != expect:
        return False
    return all(m.get("audit_flags") == flags_fn(s, k)
               for s, items in schema.items() for k, m in items.items()
               if m.get("audit_flags"))


def schema_ok(out, schema):
    """schema() 输出必须把 audit_flags 透传给前端，且值与 CONFIG_SCHEMA 一致。"""
    if not isinstance(out, dict) or not out:
        return False
    n = 0
    for s, items in out.items():
        for k, m in items.items():
            if "audit_flags" in m:
                n += 1
                if m["audit_flags"] != schema.get(s, {}).get(k, {}).get("audit_flags"):
                    return False
    return n > 0


def defaults_ok(out, C):
    """注入只加 meta 键，不许动 default —— 逐键对拍 DEFAULT_CONFIG / meta 声明值。"""
    for s, items in out.items():
        for k, m in items.items():
            meta = C.CONFIG_SCHEMA.get(s, {}).get(k, {})
            want = meta["default"] if "default" in meta else C.DEFAULT_CONFIG.get(s, {}).get(k)
            if m.get("default") != want:
                return False
    return True


def injection_source_ok(src):
    """AST 级：注入块里唯一的赋值目标必须是 `_meta['audit_flags']`，且带 except 兜底。"""
    start = src.find("配置审计标记（只加 meta")
    end = src.find("def schema()", start)
    if start < 0 or end < 0:
        return False
    frag = src[src.rfind("\n", 0, start) + 1:end]
    try:
        tree = ast.parse(frag)
    except SyntaxError:
        return False
    assigns = [t for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets]
    # 只约束「写进 meta 的键」；`_f = flags(...)` 这类临时变量是合法的。
    meta_targets = sorted({ast.unparse(t) for t in assigns if isinstance(t, ast.Subscript)})
    plain_targets = {ast.unparse(t) for t in assigns if not isinstance(t, ast.Subscript)}
    return (meta_targets == ["_meta['audit_flags']"]
            and plain_targets <= {"_f", "_sec", "_items", "_key", "_meta", "_ca"}
            and "except Exception" in frag)


def main():
    from core import config as C
    from core import config_audit as CA

    gen = load_generator()
    calc = gen.compute()          # 唯一真源重扫，不复制第二份扫描逻辑
    payload = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    src = CFG_PY.read_text(encoding="utf-8")
    problems = compare(calc, CA, payload, set(gen.SKIP_FILES))

    print("口径：业务层 %d 文件 / 全仓层 %d 文件（时点=本次运行；仅作观测，不作硬断言）"
          % (calc["scope"]["business_files"], calc["scope"]["whole_repo_files"]))
    print("四组清单：%s" % calc["counts"])

    # ── [C] 清单一致性与单一真源 ──
    ck("C1 重扫四组清单与 config_audit.py / JSON 逐项一致", not problems,
       "; ".join(problems[:3]))
    ck("C2 清单 JSON 与 config_audit.py 同源同时点（GENERATED_AT 对拍）",
       CA.GENERATED_AT == payload.get("generated_at_utc"),
       "%s vs %s" % (CA.GENERATED_AT, payload.get("generated_at_utc")))
    ck("C3 JSON schema 标识与处置口径未被改写",
       payload.get("schema") == "p0/config-audit@1"
       and "显式标注" in str(payload.get("disposition", "")))
    ck("C4 口径定义仍在（审计工具自身已排除，清单不会自污染）",
       GEN_REL in gen.SKIP_FILES and SELF_REL in gen.SKIP_FILES
       and "core/config.py" in gen.SKIP_FILES)

    # ── [I] 注入在正常 import 路径真的生效 ──
    marked = [(s, k, m["audit_flags"]) for s, items in C.CONFIG_SCHEMA.items()
              for k, m in items.items() if "audit_flags" in m]
    ck("I1 正常 import 路径下 CONFIG_SCHEMA 已带 audit_flags（非空）", len(marked) > 0,
       "标记键数=%d / 总键数=%d" % (len(marked), sum(len(v) for v in C.CONFIG_SCHEMA.values())))
    ck("I2 标记集合与单一真源 flags() 完全一致（不多不少、逐值相等）",
       injection_ok(C.CONFIG_SCHEMA, CA.flags))
    ck("I3 每个标记的值只来自三档标志名", all(
       set(v) <= {"schema_only_no_default", "no_business_consumer", "dead_config_suspect"}
       for _, _, v in marked))
    ck("I4 注入块 AST 级只写 audit_flags 一个 meta 键 + 带 except 兜底",
       injection_source_ok(src))

    # ── [Z] 零回归：schema() / save_override / 前端 ──
    out = C.schema()
    ck("Z1 schema() 输出把 audit_flags 透传给前端且值一致", schema_ok(out, C.CONFIG_SCHEMA))
    ck("Z2 schema() 的 default 逐键未变（注入没动生效值）", defaults_ok(out, C))
    ck("Z3 schema() 全量可 JSON 序列化（前端拿到的是纯数据）",
       json.dumps(out, ensure_ascii=False)[:1] == "{")
    sv = inspect.getsource(C.save_override)
    ck("Z4 save_override 只读 meta.type，不遍历全量 meta 键",
       'meta.get("type")' in sv and "for k in meta" not in sv and "for key in meta" not in sv)
    fe = FE_JS.read_text(encoding="utf-8", errors="ignore")
    ck("Z5 前端只读 meta.current/default，未知 meta 键不会变成表单字段",
       "meta.current" in fe and "meta.default" in fe
       and "Object.keys(meta" not in fe and "Object.entries(meta" not in fe)

    # ── [D] 降级路径：importlib 独立加载 config.py 不能崩 ──
    probe = (
        "import importlib.util\n"
        "s = importlib.util.spec_from_file_location('cfg_standalone', r'%s')\n"
        "m = importlib.util.module_from_spec(s)\n"
        "s.loader.exec_module(m)\n"
        "print('SCHEMA_KEYS', sum(len(v) for v in m.CONFIG_SCHEMA.values()))\n"
        % str(CFG_PY))
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", probe],
                       cwd=str(REPO), capture_output=True, text=True)
    n_keys = sum(len(v) for v in C.CONFIG_SCHEMA.values())
    ck("D1 独立加载 config.py（无包上下文）不崩，且键数与正常路径一致",
       r.returncode == 0 and ("SCHEMA_KEYS %d" % n_keys) in r.stdout,
       "rc=%d out=%s err=%s" % (r.returncode, r.stdout.strip()[:80], r.stderr.strip()[:120]))

    # ── [M] 变异自证：每条判据都必须能被篡改输入翻红 ──
    bad_calc = dict(calc)
    bad_calc["schema_only"] = list(calc["schema_only"])[:-1]
    ck("M1 变异（重扫结果少一项）⇒ compare 必须报差异",
       bool(compare(bad_calc, CA, payload, set(gen.SKIP_FILES))))

    bad_payload = dict(payload)
    bad_payload["counts"] = dict(payload["counts"], suspect=payload["counts"]["suspect"] + 1)
    ck("M2 变异（JSON counts 被改）⇒ compare 必须报差异",
       bool(compare(calc, CA, bad_payload, set(gen.SKIP_FILES))))

    bad_skip = set(gen.SKIP_FILES) - {SELF_REL}
    ck("M3 变异（把审计工具放回扫描口径）⇒ compare 必须报差异",
       bool(compare(calc, CA, payload, bad_skip)))

    stripped = copy.deepcopy(C.CONFIG_SCHEMA)
    for items in stripped.values():
        for m in items.values():
            m.pop("audit_flags", None)
    ck("M4 变异（注入没发生：剥掉全部 audit_flags）⇒ injection_ok 必须翻红",
       not injection_ok(stripped, CA.flags))

    ck("M5 变异（flags() 恒空 ⇒ 清单形同虚设）⇒ injection_ok 必须翻红",
       not injection_ok(C.CONFIG_SCHEMA, lambda *_a, **_k: []))

    out_stripped = copy.deepcopy(out)
    for items in out_stripped.values():
        for m in items.values():
            m.pop("audit_flags", None)
    ck("M6 变异（schema() 不透传标记）⇒ schema_ok 必须翻红",
       not schema_ok(out_stripped, C.CONFIG_SCHEMA))

    out_bad_default = copy.deepcopy(out)
    first_sec = next(iter(out_bad_default))
    first_key = next(iter(out_bad_default[first_sec]))
    out_bad_default[first_sec][first_key]["default"] = "__MUTATED__"
    ck("M7 变异（default 被改）⇒ defaults_ok 必须翻红",
       not defaults_ok(out_bad_default, C))

    ck("M8 变异（注入块多写了一个 meta 键）⇒ injection_source_ok 必须翻红",
       not injection_source_ok(src.replace('_meta["audit_flags"] = _f',
                                           '_meta["audit_flags"] = _f; _meta["type"] = _f')))

    # ── 收尾：磁盘文件未被变异污染 ──
    ck("M9 变异全程只在内存，磁盘 config.py / config_audit.py 未被改动",
       CFG_PY.read_text(encoding="utf-8") == src)

    print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
