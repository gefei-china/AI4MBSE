# -*- coding: utf-8 -*-
"""verify_view_intent_extraction.py —— 用户要一个视图就只给一个视图，且给具体名称。

## 真机事故（conv 999999065，米爸 2026-11-16）

用户原话（631 字）明确只要「黑盒状态视图」，产物面板却展示 **3 个视图**（包图/参数图/状态机图）。
同一条消息还被抱怨：① 代码 tab 显示数字「2」；② 视图只有笼统描述、没有具体名称。

### 根因一：视图类型抽取器把普通词当成视图类型

`CardMixin._extract_view_types` 用**大小写不敏感的子串**匹配 alias：

| alias | 原话里的误命中 | 结果 |
|---|---|---|
| `"包": "PKG"`（单字词） | 「**包**含以下状态（State）」 | 误抽 PKG |
| `"PAR"`（无词边界） | 「实例化定义（**Par**t）」「**Park**ed」 ×4 | 误抽 PAR |
| `"状态机"` | 「在实例上绑定**状态机**」 ✅ | 正确抽到 STM |

⇒ 抽成 `['PKG','PAR','STM']` ⇒ 前端展示 3 个视图。
而该函数 docstring 明明写着「避免普通词被误判」—— 实现与契约自相矛盾。

修法：删单字词「包」；英文缩写走词边界；补「状态视图」「状态机视图」→ STM。

### 根因二：代码 tab 的「2」来自数所有 ``` 围栏

`codeCnt = 正文里 ``` 数 / 2` —— 把 A 方案两段式输出里的 **json 结论块**
和 **sysml 代码块**都算成代码；但代码 pane 只有 `extractSysmlCode()` 那一段。
⇒ 数字与内容对不上。既然只有一段，就不显示数字。

### 根因三：视图只有图类型名，没有具体名

`view.name` 是「状态机图」（所有状态机视图都一样），真正标识的是
`nodes[0].name`（真机：「驾驶员生命周期状态机」，与代码里的 `state '…'` 一致）。
⇒ 新增 `svmViewSubject()`，缩略图/tab 栏/大图标题都带上具体名。

## 判据
A 组：抽取器行为（含两条变异自证）
B 组：用真机代码离线投影，确认只有一个视图且有具体名
C 组：前端真函数体（node 执行，不复刻）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_view_intent_extraction.py
退出码：0 全绿（无 node 时 C 组 SKIP）／1 有失败
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

CARDS = os.path.join(ROOT, "static", "js", "mods", "06-cards.js")
SVIEW = os.path.join(ROOT, "static", "js", "mods", "08-sysmlview.js")
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def find_node():
    n = os.environ.get("MBSE_NODE") or ""
    return n if n and os.path.isfile(n) else (shutil.which("node") or shutil.which("node.exe") or "")


def grab(src, name):
    i = src.find("function " + name)
    if i < 0:
        return None
    j = src.find("{", i)
    if j < 0:
        return None
    d = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            d += 1
        elif src[k] == "}":
            d -= 1
            if d == 0:
                return src[i:k + 1]
    return None


def real_sample():
    """取真机 conv 999999065 的用户原话 + assistant 代码；取不到返回 None。"""
    if not os.path.exists(DB):
        return None
    try:
        con = sqlite3.connect(DB, timeout=15)
        con.row_factory = sqlite3.Row
        u = con.execute("SELECT content FROM messages WHERE id=6675").fetchone()
        a = con.execute("SELECT content, card_data FROM messages WHERE id=6676").fetchone()
        con.close()
    except Exception:
        return None
    if not (u and a):
        return None
    body = a["content"] or ""
    codes = re.findall(r"```(?:sysml|kerml|SysML|sysmlv2)\s*\n([\s\S]*?)```", body, re.I)
    code = "\n\n".join(x.strip() for x in codes if x.strip())
    return {"user": u["content"] or "", "code": code}


def group_a(sample):
    from agent.pipeline_parts.cards import CardMixin
    m = CardMixin()
    text = (sample["user"] if sample else
            "为X建立黑盒状态视图。实例化定义（Part）：包含以下状态：Parked ...")
    got = m._extract_view_types(text)
    chk("A1 真机原话（631 字）只抽出 STM（不是 PKG/PAR/STM）",
        got == ["STM"], str(got))

    # A2/A3：逐条锁住两个误命中源
    chk("A2「包含」不误命中包图（删掉单字词「包」）",
        m._extract_view_types("生成视图：包含以下状态") is None
        or "PKG" not in (m._extract_view_types("生成视图：包含以下状态") or []),
        str(m._extract_view_types("生成视图：包含以下状态")))
    t = m._extract_view_types("生成状态视图：实例化定义（Part），Parked 后熄火")
    chk("A3「Part」「Parked」不误命中 PAR（英文缩写词边界）",
        t == ["STM"] if t else False, str(t))

    # A4：正向兼容（不能为了消除误判把正常说法也砍掉）
    for s, want in (("生成 BDD 视图", ["BDD"]), ("只看状态机图", ["STM"]),
                    ("生成状态视图", ["STM"]), ("画一个用例图", ["UC"]),
                    ("生成需求图", ["REQ"])):
        chk("A4 正向「%s」⇒ %s" % (s, want), m._extract_view_types(s) == want,
            str(m._extract_view_types(s)))

    # ── A5 变异自证：把词边界改回子串 ⇒ 旧误判必须回来 ──
    #    用等价的旧匹配逻辑在**内存里**复现（不改磁盘），证明 A3 不是空转
    alias_old = ["PAR", "包"]
    low = text.lower()
    hit_old = [k for k in alias_old if k.lower() in low]
    chk("A5 变异自证：旧子串匹配仍会命中 PAR/包（证明 A2/A3 有牙齿）",
        bool(hit_old), "旧命中=%s" % hit_old)


def group_b(sample):
    if not sample or not sample["code"]:
        skip("B1 真机代码投影只生成 STM，且带具体名", "无真机样本（干净库）")
        return
    from view_generator import generate_views_from_sysml
    from agent.pipeline_parts.cards import CardMixin
    types = CardMixin()._extract_view_types(sample["user"])
    res = generate_views_from_sysml(sample["code"], view_types=types, intent="view_expansion")
    views = res.get("views") or {}
    chk("B1 真机代码用修复后的 view_types 投影 ⇒ 只有 STM",
        types == ["STM"] and list(views.keys()) == ["STM"],
        "types=%s views=%s" % (types, list(views.keys())))
    ns = (views.get("STM") or {}).get("nodes") or []
    first = str((ns[0].get("name") if ns else "") or "")
    chk("B2 STM 视图带具体名「驾驶员生命周期状态机」",
        first == "驾驶员生命周期状态机", first)


def group_c():
    node = find_node()
    cards = open(CARDS, encoding="utf-8").read()
    sv = open(SVIEW, encoding="utf-8").read()

    tab = grab(cards, "cardSysmlTabs")
    subject = grab(cards, "svmViewSubject")
    thumbs = grab(cards, "svmThumbsHtml")
    if any(x is None for x in (tab, subject, thumbs)):
        chk("C0 抽到 cardSysmlTabs/svmViewSubject/svmThumbsHtml 真函数体", False, "有函数缺失")
        return
    chk("C0 抽到 cardSysmlTabs/svmViewSubject/svmThumbsHtml 真函数体", True)

    # ① 代码 tab 不显示数字（只查执行语句，注释里说明旧行为不算）
    chk("C1 代码 tab 不再计算 codeCnt（数所有围栏的旧算法）",
        not re.search(r"\bconst\s+codeCnt\b", tab), "")
    chk("C2 代码 tab 没有 sv-cnt 数字徽标",
        not re.search(r"代码<[^>]*sv-cnt", tab), "")
    chk("C3 tab 栏不再出现「按…意图投影」的笼统描述（只查渲染属性）",
        not re.search(r'title="按', tab), "")

    if not node:
        for n in ("C4", "C5", "C6"):
            skip("%s 前端真函数体行为" % n, "环境无 node（MBSE_NODE / PATH 都取不到）")
        return

    vm = {"view": {"name": "状态机图", "v1": "State Machine Diagram"},
          "nodes": [{"name": "驾驶员生命周期状态机"}, {"name": "Outside"}],
          "edges": [], "warnings": []}
    vm_empty = {"view": {"name": "参数图", "v1": "Parametric Diagram"},
                "nodes": [], "edges": [], "warnings": []}
    js = subject + "\n" + thumbs + "\n" + """
function esc(x){ return String(x==null?'':x); }
let _svmVmSeq = 0;
const _svmVmStore = new Map();
function requestAnimationFrame(){}
const document = { querySelectorAll: () => [] };
const _svmThumbEnsureObs = () => ({ observe(){} });
const out = {
  subj: svmViewSubject(%s),
  fallback: svmViewSubject(%s),
  html: svmThumbsHtml({ views: { STM: %s } })
};
console.log(JSON.stringify(out));
""" % (json.dumps(vm, ensure_ascii=False), json.dumps(vm_empty, ensure_ascii=False),
       json.dumps(vm, ensure_ascii=False))
    td = tempfile.mkdtemp()
    p = os.path.join(td, "probe.js")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run([node, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            chk("C4 node 执行前端真函数体成功", False, (r.stderr or out)[-300:])
            return
        d = json.loads(out.splitlines()[-1])
        chk("C4 svmViewSubject 从 nodes 提取具体名",
            d["subj"] == "驾驶员生命周期状态机", d["subj"])
        chk("C5 无节点时退回图类型名", d["fallback"] == "参数图", d["fallback"])
        chk("C6 缩略图显示「状态机图 · 驾驶员生命周期状态机」",
            "状态机图 · 驾驶员生命周期状态机" in d["html"], d["html"][:250])
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # 大图标题（源码级：确认它真的调用了 svmViewSubject）
    chk("C7 大图预览标题带具体名（调用 svmViewSubject）",
        "svmViewSubject(vm)" in sv and "subj !== vname" in sv, "")


def main():
    print("=" * 78)
    print("门禁：视图意图抽取（要一个就给一个）+ 代码/视图卡的具体名称")
    print("=" * 78)
    sample = real_sample()
    print("\n--- GROUP_A ---")
    group_a(sample)
    print("\n--- GROUP_B ---")
    group_b(sample)
    print("\n--- GROUP_C ---")
    group_c()

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
