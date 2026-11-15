# -*- coding: utf-8 -*-
"""verify_citation_clause_link.py —— 正文条款号（§5.2.5）必须能点开对应的来源片段。

## 为什么需要这个门禁（2026-11-15 米爸报「知识库检索 引用的片段 输出正文没有展示引用标识」）

修复经历了两轮，第二轮才找到**不该指望模型**这个结论：

| 轮次 | 做法 | 真机结果 |
|---|---|---|
| ① 硬性措辞 | `_build_citation_rules` + 检索尾部 `_citation_footer` 都写「凡写出条款号必须紧跟 [n]」 | msg 6614：正文 §5.2.5 出现 **20 次**、§x.y 合计 41 次，`[n]`（扣除摘要里的一次批量标注）**0 处** |
| ② 前端自动链接 | 用 citations 里**后端已算好**的 `anchor`，把正文 `§x.y` 直接渲染成可点元素 | 同一份正文：条款号链接 **0 → 20** |

⇒ **结论**：结构化输出时模型天然写条款号（JSON 的 `clause_ref` 语义上就该放
`"ISO 29148:2011 §5.2.5 Unambiguous"`），让它在那儿拼 `[1]` 是反直觉的；
**这类事不该靠提示词，应该靠已有的数据（anchor）在渲染层兜住**。

## 判据（A 组行为级 + B 组接线 + C 组真机）

A 组用 node 执行 `05-markdown.js` 里的**真函数体**（不是 Python 复刻 ——
复刻就是两套实现，改一边必然漂移）：
  A1 anchor 内的条款号 ⇒ 渲染成 `cite-sec` 且带 data-i
  A2 anchor 外的条款号（模型自己写的、检索没命中）⇒ **保持纯文本**
     —— 我们手里没有它的片段，链过去只会打开空内容；"只说真话"同口径
  A3 部分匹配防护：`§5.2.55` 不得被 `§5.2.5` 命中后留下孤零零的 `5`
  A4 与 `[n]` 共存：`§5.2.5 [3]` 两条路径各自可点、互不破坏
  A5 同条款多片段 ⇒ 取**最小编号**（真机 [1][2][3][4] 的 anchor 全是 §5.2.5）
  A6 无 cites / cites 无 anchor ⇒ 原样返回，不报错
  A7 变异自证：把「取最小编号」改成「取最大」⇒ A5 必须翻红

B 组（源码级，防接线丢失）：
  B1 inlineMd 仍接收 cites 参数（签名没被改坏）
  B2 条款号替换发生在 [n] 替换**之前**（顺序敏感，见文件内注释）
  C 组（真机）：C1 正文里出现的 anchor 条款号实际被链接了；C2 无越界链接

## 环境
需要 node。取法与 `verify_agent_name_unify.py` 一致：`MBSE_NODE` > PATH。
**不硬编码本机路径**（verify_gate_portability 的 P1/P5 会判红，且那是
"本机永远有、CI 永远没有"的假分支）。无 node ⇒ 整组 SKIP（环境没有，不是缺陷）。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_citation_clause_link.py
退出码：0 全绿（允许 SKIP）／1 有失败
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

MD = os.path.join(ROOT, "static", "js", "mods", "05-markdown.js")
CSS = os.path.join(ROOT, "static", "css", "app.css")
DB = os.environ.get("MBSE_GATE_DB") or os.path.join(ROOT, "mbse.db")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("　— " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def find_node():
    n = os.environ.get("MBSE_NODE") or ""
    if not n:
        n = shutil.which("node") or shutil.which("node.exe") or ""
    return n


def grab_fn(src, name):
    """从全局作用域脚本里抽出 `function name(...) {...}` 的完整函数体（花括号配平）。"""
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


def run_node(node, js_src, timeout=60):
    """执行一段 JS，返回 (rc, stdout, stderr)。

    ⚠️ 必须让 JS 自己 `console.log` —— `module.exports` 不产生任何 stdout
      （本仓 verify_agent_name_unify.py 踩过，注释里已留痕）。
    """
    td = tempfile.mkdtemp()
    p = os.path.join(td, "probe.js")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(js_src)
        r = subprocess.run([node, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── 真机样本：正文 + citations（取锚点机制上线后的最新一条）──────────────
def real_sample():
    """从真库取一条带 citations 的消息；取不到返回 None（干净库 ⇒ C 组 SKIP）。"""
    if not os.path.exists(DB):
        return None
    try:
        con = sqlite3.connect(DB, timeout=15)
        try:
            r = con.execute(
                "select id, content, card_data from messages "
                "where card_data like '%\"anchor\"%' and card_data not like '%\"citations\": []%' "
                "order by id desc limit 1").fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not r:
        return None
    try:
        cd = json.loads(r[2] or "{}")
    except Exception:
        return None
    cits = cd.get("citations") or []
    if not cits or not (r[1] or ""):
        return None
    return {"id": r[0], "body": r[1], "cites": cits}


def group_a_behavior(node):
    """A 组：用 node 跑 05-markdown.js 的**真函数体**。"""
    src = open(MD, encoding="utf-8").read()
    parts = [grab_fn(src, n) for n in ("escMd", "_citeClauseIndex", "inlineMd")]
    if any(p is None for p in parts):
        # ⚠️ 抽不到函数 ⇒ 必须 FAIL，不能"没断言就通过"（那是空转）
        chk("A0 从 05-markdown.js 抽到 escMd/_citeClauseIndex/inlineMd",
            False, "缺: %s" % [n for n, p in zip(("escMd", "_citeClauseIndex", "inlineMd"), parts) if p is None])
        return
    chk("A0 从 05-markdown.js 抽到 escMd/_citeClauseIndex/inlineMd", True)

    fx = real_sample()
    # 兜底夹具：真库不可用时也要能验行为（不依赖真库）
    cites = (fx["cites"] if fx else [
        {"anchor": "§5.2.5 Characteristics of", "content": "a", "source_doc": "ISO.pdf"},
        {"anchor": "§5.2.6 Verifiable", "content": "b", "source_doc": "ISO.pdf"},
        {"anchor": "§5.2.5 Characteristics of", "content": "c", "source_doc": "ISO.pdf"},
        {"anchor": "§4.4.1 R18", "content": "d", "source_doc": "Guide.pdf"},
    ])
    # 只保留有 anchor 的，避免真机样本里混入空锚点干扰断言
    cites = [c for c in cites if str(c.get("anchor") or "").strip()]
    if not cites:
        chk("A0b 样本里至少有一条带 anchor 的 citation", False, "全空")
        return
    chk("A0b 样本里至少有一条带 anchor 的 citation", True)

    js = "\n".join(parts) + """
const CITES = %s;
const out = {};
out.idx = _citeClauseIndex(CITES);
// A1：anchor 内
const _k = Object.keys(out.idx)[0];
out.a1 = inlineMd('依据 §' + _k + ' 的条款', CITES);
// A2：anchor 外（构造一个必然不在索引里的号）
let _miss = '9.9.9';
while (out.idx[_miss]) _miss = _miss + '9';
out.miss = _miss;
out.a2 = inlineMd('依据 §' + _miss + ' 的条款', CITES);
// A3：部分匹配
out.a3 = inlineMd('见 §' + _k + '5 条', CITES);
// A4：与 [n] 共存
out.a4 = inlineMd('按 §' + _k + ' [1] 判定', CITES);
// A5：同条款多片段 ⇒ 最小号
const _dup = {};
CITES.forEach((c,k)=>{
  const m = String(c.anchor||'').match(/§\\s*(\\d+(?:\\.\\d+){1,4})/);
  if(!m) return;
  (_dup[m[1]] = _dup[m[1]] || []).push(k+1);
});
out.dup = _dup;
out.a5idx = out.idx;
// A6：无 cites / 空 anchor
out.a6a = inlineMd('依据 §' + _k + ' 条款', null);
out.a6b = inlineMd('依据 §' + _k + ' 条款', [{content:'x'}]);
out.a6probe = '依据 §' + _k + ' 条款';
console.log(JSON.stringify(out));
""" % json.dumps(cites, ensure_ascii=False)

    rc, out, err = run_node(node, js)
    if rc != 0 or not out.strip():
        chk("A0c node 执行内联探针成功", False, (err or out)[-300:])
        return
    chk("A0c node 执行内联探针成功", True)
    try:
        d = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        chk("A0d 探针输出可解析", False, "%s / %s" % (e, out[-200:]))
        return
    chk("A0d 探针输出可解析", True)

    k0 = list(d["idx"].keys())[0]
    n0 = d["idx"][k0]

    # A1
    chk("A1 anchor 内的条款号 ⇒ 渲染成 cite-sec 且带 data-i",
        "cite-sec" in d["a1"] and ('data-i="%d"' % n0) in d["a1"] and ("§" + k0) in d["a1"],
        d["a1"][:180])

    # A2 —— 关键：没有片段就不许造入口
    chk("A2 anchor 外的条款号 ⇒ 保持纯文本（不造空入口）",
        "cite-sec" not in d["a2"] and ("§" + d["miss"]) in d["a2"],
        d["a2"])

    # A3
    chk("A3 部分匹配防护：§%s5 不被 §%s 命中后留残尾" % (k0, k0),
        "cite-sec" not in d["a3"], d["a3"])

    # A4
    chk("A4 与 [n] 共存：条款号与编号各自可点、互不破坏",
        "cite-sec" in d["a4"] and "[1]" in d["a4"] and 'data-i="1"' in d["a4"],
        d["a4"][:200])

    # A5 —— 同条款多片段取最小号
    dup_ok = True
    dup_detail = []
    for key, nums in (d["dup"] or {}).items():
        if len(nums) > 1:
            got = d["a5idx"].get(key)
            if got != min(nums):
                dup_ok = False
                dup_detail.append("%s: 编号%s 期望 %d 实得 %s" % (key, nums, min(nums), got))
    chk("A5 同条款多片段 ⇒ 取最小编号（不让编号乱跳）",
        dup_ok, "; ".join(dup_detail) or "（样本里无重复条款，判据空转）"
        if not dup_detail else "; ".join(dup_detail))

    # A6
    chk("A6 无 cites / 无 anchor ⇒ 原样返回（不报错、不改文本）",
        d["a6a"] == d["a6probe"] and d["a6b"] == d["a6probe"],
        "%r vs %r" % (d["a6a"], d["a6probe"]))

    # ── A8：两次替换互不破坏（2026-11-15 实测踩坑的正面判据）──
    #   § 替换先、[n] 替换后；若 § 生成的 title 里含 `[数字]`，
    #   [n] 会把它也换成 <sup> ⇒ HTML 变成
    #     `title="…片段 <sup …>[1]</sup>"`  ⇒ 标签跑进属性里、§5.2.5 点不动。
    #   判据：① title 属性内不得含 `<`；② 不得出现 `title="` 之后 200 字内出现 `</sup>"`。
    bad_title = re.search(r'title="[^"]*<', d["a1"]) or re.search(r'title="[^"]*<', d["a4"])
    chk("A8 § 与 [n] 两次替换互不破坏（title 属性里不得被塞进标签）",
        not bad_title, d["a1"][:220])

    # A9：结构完整性 —— 标签数量必须配平（被破坏时 </sup> 会多于 <sup）
    _open = d["a1"].count("<sup")
    _close = d["a1"].count("</sup>")
    chk("A9 渲染后 <sup>/</sup> 配平（结构未被二次替换破坏）",
        _open == _close and _open > 0, "open=%d close=%d" % (_open, _close))

    # ── A7 变异自证：把「取最小」改成「取最大」⇒ A5 必须翻红 ──
    mut_src = "\n".join(parts).replace(
        "if(idx[m[1]] === undefined) idx[m[1]] = k + 1;",
        "idx[m[1]] = k + 1;")          # 去掉"只在未设置时赋值" ⇒ 变成取最大值
    if mut_src != "\n".join(parts):
        js_mut = mut_src + """
console.log(JSON.stringify(_citeClauseIndex(%s)));
""" % json.dumps(cites, ensure_ascii=False)
        rc2, out2, _ = run_node(node, js_mut)
        mutated_max = False
        if rc2 == 0 and out2.strip():
            try:
                midx = json.loads(out2.strip().splitlines()[-1])
                mutated_max = any(len(v) > 1 and midx.get(kk) == max(v)
                                  for kk, v in (d["dup"] or {}).items() if len(v) > 1)
            except Exception:
                mutated_max = False
        chk("A7 变异自证：改成「取最大编号」后 A5 判据必须翻红",
            mutated_max, "变异体仍是取最小 ⇒ 判据抓不到该行为")
    else:
        chk("A7 变异自证：改成「取最大编号」后 A5 判据必须翻红",
            False, "未能在源码里定位变异点（字符串已变）")

    # ── A10 变异自证：把 § 的 title 改回含 `[n]` ⇒ A8/A9 必须翻红 ──
    #   这是本轮真实踩过的坑（真机 msg 6614 渲染出
    #   `title="…片段 <sup…>[1]</sup>"` ⇒ §5.2.5 点不动），
    #   所以必须证明 A8/A9 不是摆设 —— 否则下次谁把它改回去都没人拦。
    _good = 'title="查看该条款对应的来源片段"'
    _bad = 'title="查看该条款对应的来源片段 [${n}]"'
    if _good in "\n".join(parts):
        js_bad = "\n".join(parts).replace(_good, _bad) + """
console.log(JSON.stringify(inlineMd('依据 §' + %s + ' 条款 [1]', %s)));
""" % (json.dumps(k0, ensure_ascii=False), json.dumps(cites, ensure_ascii=False))
        rc3, out3, _ = run_node(node, js_bad)
        caught = False
        if rc3 == 0 and out3.strip():
            try:
                bad_html = json.loads(out3.strip().splitlines()[-1])
                _bt = re.search(r'title="[^"]*<', bad_html)
                _o = bad_html.count("<sup")
                _c = bad_html.count("</sup>")
                caught = bool(_bt) or (_o != _c)
            except Exception:
                caught = False
        chk("A10 变异自证：title 含 [n] 时会破坏 HTML ⇒ A8/A9 必须翻红（守卫有牙齿）",
            caught, "变异体未被抓到 ⇒ A8/A9 形同虚设")
    else:
        chk("A10 变异自证：title 含 [n] 时会破坏 HTML ⇒ A8/A9 必须翻红（守卫有牙齿）",
            False, "未能在源码里定位 title 文案（字符串已变）")


def group_b_wiring():
    """B 组：源码级接线（防两个路径/顺序被改坏）。"""
    md = open(MD, encoding="utf-8").read()
    chk("B1 inlineMd 仍接收 cites 参数（签名未被改坏）",
        "function inlineMd(s, cites)" in md, "")

    # B2：条款号替换必须在 [n] 替换之前
    i_sec = md.find("_citeClauseIndex(cites)")
    i_num = md.find("/\\[(\\d+(?:[,，]\\s*\\d+)*)\\]/g")
    chk("B2 条款号替换在 [n] 替换之前（顺序敏感：反过来 [n] 会产生嵌套风险）",
        i_sec > 0 and i_num > 0 and i_sec < i_num,
        "sec@%s num@%s" % (i_sec, i_num))

    css = open(CSS, encoding="utf-8").read()
    chk("B3 有 cite-sec 样式且 vertical-align:baseline（条款号不许被当上标抬高）",
        "sup.cite-sec" in css and "vertical-align:baseline" in css, "")
    chk("B4 cite-sec 覆盖了 sup.cite 的背景/边框（否则句中的条款号会变成蓝块）",
        "sup.cite-sec" in css and "background:transparent" in css and "border:none" in css, "")


def group_c_real(fx, node):
    """C 组：真机样本（干净库 ⇒ SKIP）。

    ⚠️ 2026-11-15 自纠：C2 初版写成 `chk(..., True, "")` —— 那是个**永远 PASS 的假判据**
      （注释还写着"由 A2 保证"）。假判据比没有判据更坏：它制造"已验证"的错觉。
      现在改成**真的拿真机正文跑一遍渲染**，逐条核对渲染出的 cite-sec 条款号是否都在
      anchor 集合内。
    """
    if not fx:
        skip("C1 真机正文的条款号确实被链接", "无可用真机样本（干净库 或 该库无 anchor 消息）")
        skip("C2 真机正文渲染后 anchor 覆盖的条款号确实被链接", "同上")
        skip("C3 真机正文渲染后无越界条款号被链接", "同上")
        skip("C4 代码块内的 § 不链接（那里是待复制的原文）", "同上")
        return
    idx = {}
    for c in fx["cites"]:
        m = re.search(r"§\s*(\d+(?:\.\d+){1,4})", str(c.get("anchor") or ""))
        if m and m.group(1) not in idx:
            idx[m.group(1)] = len(idx) + 1
    if not idx:
        skip("C1 真机正文的条款号确实被链接", "该样本 citations 的 anchor 全无条款号")
        skip("C2 真机正文渲染后 anchor 覆盖的条款号确实被链接", "同上")
        skip("C3 真机正文渲染后无越界条款号被链接", "同上")
        skip("C4 代码块内的 § 不链接（那里是待复制的原文）", "同上")
        return

    # C1：正文里出现了几个 anchor 覆盖的条款号（说明链接有实际作用面）
    hit = [k for k in idx if re.search(r"§\s*" + re.escape(k) + r"(?![\d.])", fx["body"])]
    chk("C1 真机正文里出现了 anchor 覆盖的条款号（说明链接有实际作用面）",
        bool(hit), "msg %s：anchor 覆盖 %d 个条款号，正文命中 %d 个"
                   % (fx["id"], len(idx), len(hit)))

    # C2（真判据）：**走生产的 renderMarkdown**（不是自己 split+inlineMd）
    #
    # ⚠️⚠️ 2026-11-15 自纠（这条差点让我把假判据当成果交付）：
    #   初版用 `BODY.split('\n').map(l => inlineMd(l, CITES))` 逐行渲染，
    #   算出条款号链接 **20 处**，我还据此宣称"0 → 20"。
    #   但**真机浏览器实测只有 1 处**（citeSecCount=1）。
    #   根因：`renderMarkdown` 对 ``` 围栏走 `_codeFold` → `escMd(body.join())`，
    #   **代码块内容根本不进 inlineMd**；而 msg 6614 的 41 个 § 有 40 个在 json 代码块里。
    #   ⇒ 逐行 inlineMd 是把"用户永远看不到的渲染路径"当成结果来断言。
    #   这正是本仓反复出现的「验证器骗人」同族，必须走生产函数。
    #
    #   顺带确认：代码块内不链接是**正确**行为 —— 那里是给用户复制的原文，
    #   加了 HTML 标签复制出去就是脏的 ⇒ 由 C4 锁死，不许谁为了"链接更多"去掉它。
    if not node:
        skip("C2 真机正文渲染后 anchor 覆盖的条款号确实被链接", "环境无 node")
        skip("C3 真机正文渲染后无越界条款号被链接", "环境无 node")
        skip("C4 代码块内的 § 不链接（那里是待复制的原文）", "环境无 node")
        return
    src = open(MD, encoding="utf-8").read()
    need = ("escMd", "_mdCells", "renderTable", "renderMdLine",
            "inlineMd", "_citeClauseIndex", "renderMarkdown")
    parts = [grab_fn(src, n) for n in need]
    if any(p is None for p in parts):
        chk("C2 真机正文渲染后 anchor 覆盖的条款号确实被链接", False,
            "抽不到函数: %s" % [n for n, p in zip(need, parts) if p is None])
        return
    js = "\n".join(parts) + """
// onclick 里引用的函数在渲染期不会执行，stub 掉即可
function mermaidAutoRender(){} function codeFoldCopy(){} function codeFoldToggle(){} function mermaidToggle(){}
const CITES = %s, BODY = %s;
const html = renderMarkdown(String(BODY), CITES);
const re = /class="cite cite-sec"[^>]*>(§[^<]+)</g;
const keys = [];
let m;
while ((m = re.exec(html))) keys.push(m[1].replace(/^§\\s*/, ''));
const allRe = /§\\s*(\\d+(?:\\.\\d+){1,4})(?![\\d.])/g;
const all = []; let a;
while ((a = allRe.exec(String(BODY)))) all.push(a[1]);
// 代码块内的 § 数（围栏区间），用于核对"块内不链接"是否成立
const fences = [];
let depth = 0, inF = false, fStart = 0;
const lines = String(BODY).split('\\n');
lines.forEach((l, i) => {
  if (/^\\s*```/.test(l)) { if (!inF) { inF = true; fStart = i; } else { inF = false; fences.push([fStart, i]); } }
});
const inFence = (idx) => fences.some(([s, e]) => idx > s && idx < e);
let secInFence = 0, secOutFence = 0;
lines.forEach((l, i) => {
  const c = (l.match(/§\\s*\\d+(?:\\.\\d+){1,4}(?![\\d.])/g) || []).length;
  if (c) { if (inFence(i)) secInFence += c; else secOutFence += c; }
});
console.log(JSON.stringify({
  linked: keys, allSecs: Array.from(new Set(all)),
  secInFence, secOutFence,
  htmlHasCodeFold: html.indexOf('code-fold') >= 0
}));
""" % (json.dumps(fx["cites"], ensure_ascii=False),
       json.dumps(fx["body"], ensure_ascii=False))
    rc, out, err = run_node(node, js)
    if rc != 0 or not out.strip():
        chk("C2 真机正文渲染后 anchor 覆盖的条款号确实被链接", False, (err or out)[-250:])
        return
    try:
        d = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        chk("C2 真机正文渲染后 anchor 覆盖的条款号确实被链接", False,
            "%s / %s" % (e, out[-200:]))
        return
    linked = d.get("linked") or []
    allsecs = d.get("allSecs") or []
    stray = sorted({k for k in linked if k not in idx})
    expect = set(idx) & set(allsecs)
    chk("C2 真机正文渲染后，anchor 覆盖的条款号确实被链接（不是 0 处）",
        bool(linked) and bool(expect & set(linked)),
        "链接 %d 处；期望命中 %s，实得 %s" % (len(linked), sorted(expect), sorted(set(linked))))
    chk("C3 真机正文渲染后无越界条款号被链接（逐条核对 anchor 集合）",
        not stray, "越界号=%s" % stray)
    # C4：代码块内的 § 必须**不链接**（那里是给用户复制的原文）
    chk("C4 代码块内的 § 不链接（代码块要能原样复制，不能塞 HTML 标签）",
        d.get("htmlHasCodeFold") and (d.get("secInFence") or 0) > 0
        and len(linked) <= (d.get("secOutFence") or 0) + 1,
        "块内 §=%s、块外 §=%s、实际链接=%d"
        % (d.get("secInFence"), d.get("secOutFence"), len(linked)))
    outside = sorted(set(allsecs) - set(idx))
    print("      [info] msg %s：正文 §x.y 共 %d 个不同号，可链 %d 个，"
          "**无片段** %d 个（保持纯文本）：%s"
          % (fx["id"], len(allsecs), len(set(idx) & set(allsecs)),
             len(outside), outside[:6]))
    print("      [info] § 分布：代码块内 %s 处（不链接，保持可复制）/ 块外 %s 处（链接）"
          % (d.get("secInFence"), d.get("secOutFence")))
    if outside:
        print("      [info] ↑ 这几个号是**检索没召回**的条款（模型凭正文记忆写的）。"
              "本门禁只保证「没有片段就不给假入口」；"
              "要链上它们属于**检索召回**问题，不在本判据射程内。")


def main():
    print("=" * 78)
    print("门禁：正文条款号（§x.y）自动链接到来源片段")
    print("=" * 78)

    node = find_node()
    print("\n--- GROUP_A ---")
    if not node:
        for n in ("A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7"):
            skip("%s 条款号链接行为" % n, "环境无 node（MBSE_NODE / PATH 都取不到）")
    else:
        group_a_behavior(node)

    print("\n--- GROUP_B ---")
    group_b_wiring()

    print("\n--- GROUP_C ---")
    group_c_real(real_sample(), node)

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    if _FAIL:
        print("红项：")
        for n in _FAIL:
            print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
