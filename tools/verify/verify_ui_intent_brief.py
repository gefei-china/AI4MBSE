# -*- coding: utf-8 -*-
"""门禁：意图理解摘要 + 代码块默认展开 + 执行过程区限高（2026-10-27）

## 这三个断言对应的用户原话

> 「意图识别环节总是匹配到具体的 agent，不能提供更好理解的内容吗，比如对于用户意图的
>   分析与总结 —— 用户到底想要什么，不清楚的可以通过澄清卡确认」
> 「输出的代码块需要支持展开/收起能力，默认展开」
> 「AI 在执行环节，最下面总是输出大量的文本内容，且页面会被拉得很长，要求此区域限制高度，
>   可以滑动查看完整内容」

## 为什么必须有门禁（而不是只改完就算）

三条都属于「**用户可见的默认状态**」，改一次很容易在后续重构里被悄悄改回去：
① 前端把 `ev.brief` 换回 `ev.agent`（看上去更"简洁"）；
② `_codeFold` 的默认 class 从 `open` 改回空（"避免长代码霸屏"是个很自然的冲动）；
③ `.proc` 的 `max-height` 被当成"限制内容"删掉（B5-8 的"正文不许折叠"常被误用到执行区）。

⚠️ 纪律提醒（本仓踩过）：**B5-8 只管正文（`.body`），不管执行过程区（`.proc`）**。
   两者是不同容器、不同诉求：正文要完整展开，执行过程要限高可滚。断言里两边都要锁住，
   否则改一边必然伤另一边。

## 断言组成

- A 组（后端）：brief 结构 / 壳剥离 / 路由翻译覆盖 / 前端契约字段
- B 组（前端静态）：默认展开、默认收起两处字面量、限高规则、正文不设限
- C 组（变异自证）：把每条改动还原成旧写法 ⇒ 对应断言必须 FAIL（防恒绿空转）

用法：`.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_ui_intent_brief.py`
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK  ] {name}" + (f"  ← {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}" + (f"  ← {detail}" if detail else ""))
    return ok


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


def mutate_and_check(rel: str, old: str, new: str, probe, label: str, reload_mod: str = "") -> None:
    """把文件里 old 换成 new（旧写法），断言 probe 必须变红，然后逐字节还原。

    还原用 read_bytes/write_bytes —— 文本模式会动换行符，本仓已因此踩过。

    ⚠️ `reload_mod`：**改盘不等于改内存**。Python 模块一旦 import，类/常量都在内存里，
    改盘后 probe 读到的仍是旧值 ⇒ 变异"恒不翻转" ⇒ 看起来像断言空转，其实是被测对象没换。
    （本门禁第一版就踩了这个：两条 Python 侧变异自证都假绿。）
    ⇒ 凡是被测对象是 Python 模块的，必须传模块名，变异前后各 reload 一次。
    """
    p = ROOT / rel
    bak = p.read_bytes()
    try:
        txt = bak.decode("utf-8")
        if old not in txt:
            check(f"变异点存在：{label}", False, "锚点未命中，变异无法进行")
            return
        p.write_bytes(txt.replace(old, new, 1).encode("utf-8"))
        if reload_mod:
            _reload(reload_mod)
        ok = probe()
        check(f"变异自证：{label} 改回旧写法会变红", not ok, "" if not ok else "变异后仍然通过 = 断言空转")
    finally:
        p.write_bytes(bak)
        if reload_mod:
            _reload(reload_mod)


def _reload(modname: str) -> None:
    import importlib
    mod = sys.modules.get(modname)
    if mod is not None:
        importlib.reload(mod)
    else:
        __import__(modname)
        importlib.reload(sys.modules[modname])


# ────────────────────────── A 组：后端 brief ──────────────────────────
def group_a():
    print("\n=== A 组：意图理解摘要（agent/intent_brief.py）===")
    from agent import intent_brief as ib

    # A1 结构齐全且可直接展示
    b = ib.build("在通信子系统用例包中新增一个名为测试的包", intent="design",
                 agent_name="工程建模方案设计Agent", slots={"goal": "新增测试包"},
                 meta={"route": "rule", "confidence": 0.95}, label="设计/建模")
    need_keys = {"goal", "need", "agent", "basis", "missing", "route", "source", "followup"}
    check("A1 brief 字段齐全", need_keys <= set(b.keys()), str(sorted(set(b) - need_keys)))
    check("A2 goal 取 slots（最精炼）", b["goal"] == "新增测试包", b["goal"])
    check("A3 need 是人类可读名（不是内部 key）", b["need"] == "设计/建模", b["need"])
    check("A4 basis 含判据与把握", any("规则" in x for x in b["basis"]) and any("95%" in x for x in b["basis"]),
          str(b["basis"]))
    check("A5 agent 仍保留（次要信息，不冒充理解结果）", b["agent"] == "工程建模方案设计Agent")

    # A6 无 slots → 忠实复述原话（剥 @/# 控制前缀）
    b2 = ib.build("@智源对接agent #工程数据 导出广汽元模型1010的sysml v2源码",
                  intent="zhiyuan_mgmt", slots={}, meta={"route": "semantic"}, label="智源工程管理")
    check("A6 无 slots 时复述原话（且剥掉 @/# 前缀）",
          b2["goal"].startswith("导出广汽元模型") and "@" not in b2["goal"] and "#" not in b2["goal"],
          b2["goal"])

    # A7 元对话壳：带「原请求：」⇒ 抽出用户本意
    shell_a = ("【澄清补充】用户在待澄清状态下直接补充了以下信息（请据此继续，无需再确认）：\n"
               "问题「测试包挂在哪」→ 回答：父包为通信子系统用例\n\n"
               "原请求：在通信子系统用例包中新增名为测试的包")
    b3 = ib.build(shell_a, intent="design", slots={}, meta={"route": "llm"}, label="设计/建模")
    check("A7 澄清壳 + 原请求段 → 抽出用户本意",
          b3["goal"].startswith("在通信子系统用例包") and "澄清补充" not in b3["goal"], b3["goal"])
    check("A7b 抽出后不算 followup", b3["followup"] is False)

    # A8 元对话壳：没有原请求段 ⇒ goal 留空 + followup（宁可不显示，也不显示错的）
    shell_b = "【澄清补充】用户在待澄清状态下直接补充了以下信息（请据此继续，无需再确认）"
    b4 = ib.build(shell_b, intent="design", slots={"goal": "【澄清补充】用户在待澄清状态下…"},
                  meta={"route": "llm"}, label="设计/建模")
    check("A8 只有澄清壳时 goal 留空（不把壳当理解）", b4["goal"] == "", repr(b4["goal"]))
    check("A8b 标记 followup=True（前端据此如实说明）", b4["followup"] is True)

    # A9 路由翻译覆盖 agent/intent.py 里真实出现过的全部取值
    src = read("agent/intent.py")
    real = set(re.findall(r"_(?:rule_)?done\(\s*[^,]+,\s*[\"']([a-zA-Z_0-9]+)[\"']", src))
    real |= set(re.findall(r"\broute\s*=\s*[\"']([a-zA-Z_0-9]+)[\"']", src))
    missing = sorted(v for v in real if v not in ib.ROUTE_TEXT)
    check("A9 ROUTE_TEXT 覆盖全部真实路由取值", not missing,
          f"缺 {missing}" if missing else f"{len(real)} 个取值全覆盖")
    # 且不能翻出裸英文（翻译后仍等于 key 视为没翻译）
    untranslated = [k for k, v in ib.ROUTE_TEXT.items() if v.strip().lower() == k.lower()]
    check("A9b 没有「译了等于没译」的条目", not untranslated, str(untranslated))

    # A10 未知取值原样透出（不猜、不隐藏）
    check("A10 未知 route 原样返回（可排查）", ib.route_text("some_new_route") == "some_new_route")

    return b


# ────────────────────────── B 组：前端静态契约 ──────────────────────────
def group_b():
    print("\n=== B 组：前端默认态与限高 ===")
    md = read("static/js/mods/05-markdown.js")
    pipe = read("static/js/mods/11-pipeline.js")
    cards = read("static/js/mods/06-cards.js")
    css_chat = read("static/css/chat.css")
    css_app = read("static/css/app.css")

    # B1 代码块默认展开（普通代码走 .code-fold open 分支）
    #   判据：非 mermaid 的返回分支里 code-fold 带 open
    plain = re.search(r"return `<div class=\"code-fold([^\"]*)\">`\s*\n\s*\+ `<div class=\"cf-head\" onclick=\"codeFoldToggle",
                      md)
    check("B1 普通代码块默认展开", bool(plain) and "open" in (plain.group(1) or ""),
          f"class=[{plain.group(1) if plain else 'MATCH-FAIL'}]")

    # B2 可收起：chev 初始必须是展开态字符（▾），且 toggle 会改回 ▸
    check("B2 初始 chevron 为展开态（▾）", '`<span class="chev">▾</span></div>`\n      + `<pre class="md-code">' in md)
    check("B2b codeFoldToggle 会切换 open 并改 chevron",
          "fold.classList.toggle('open')" in md and "open ? '▾' : '▸'" in md)

    # B3 代码块仍限高内滚（展开不等于无限长）
    for f, txt in (("static/css/chat.css", css_chat), ("static/css/app.css", css_app)):
        check(f"B3 {f} 的 .md-code 限高内滚",
              re.search(r"\.code-fold \.md-code\{[^}]*max-height:\d+px[^}]*overflow:auto", txt) is not None)

    # B4 执行过程区整区限高 + 可滚（两个 css 都要，页面同时引了两份）
    for f, txt in (("static/css/chat.css", css_chat), ("static/css/app.css", css_app)):
        m = re.search(r"^\.proc\{([^}]*)\}", txt, re.M)
        body = m.group(1) if m else ""
        check(f"B4 {f} 的 .proc 限高", "max-height:" in body, body[:60])
        check(f"B4b {f} 的 .proc 可滚动查看完整内容", "overflow-y:auto" in body)

    # B4c 历史回看路径（.hist-proc）也必须限高 —— 前端有**两条**执行过程渲染路径：
    #     流式当时用 .proc（procRender），刷新/回看用 .hist-proc（procBlocksHtml）。
    #     只改一条是"治了正在跑的那条，回看旧消息照旧被拉长"（本仓已多次踩到成对修改的坑）。
    for f, txt in (("static/css/chat.css", css_chat), ("static/css/app.css", css_app)):
        m2 = re.search(r"^\.hist-proc\.open \.proc-timeline\{([^}]*)\}", txt, re.M)
        body2 = m2.group(1) if m2 else ""
        check(f"B4c {f} 历史回看 .hist-proc 也限高", "max-height:" in body2, body2[:50].replace("\n", " "))
        check(f"B4d {f} 历史回看可滚动", "overflow-y:auto" in body2)

    # B5 正文不设限（B5-8 纪律：正文必须完整展开）—— 防止把限高误加到 .body
    for f, txt in (("static/css/chat.css", css_chat), ("static/css/app.css", css_app)):
        bad = re.search(r"^\.body\s*\{[^}]*max-height", txt, re.M)
        check(f"B5 {f} 的正文 .body 未被加高度限制", bad is None,
              (bad.group(0)[:50] if bad else ""))

    # B6 意图识别：有 brief 时走「理解：」，无 brief 回退旧 agent 行
    check("B6 前端用 brief.goal 渲染「理解：」", "'理解：' + _b.goal" in pipe)
    check("B6b brief 缺失时回退旧 agent 行（向后兼容）", "→ ${ev.agent||ev.intent}（HIL" in pipe)
    check("B6c followup 轮如实说明（不编造理解）", "本轮不重新分析目标" in pipe)
    check("B6d 澄清条也带「我的理解」", "我的理解：" in pipe)

    # B7 展开体：白名单构造 + 每个动态文本都 esc（detail 是直接 innerHTML 的）
    ib_body = re.search(r"function _intentBriefBody[\s\S]*?\n}", pipe)
    check("B7 _intentBriefBody 存在", ib_body is not None)
    if ib_body:
        seg = ib_body.group(0)
        check("B7b 动态文本逐个 esc", "esc(String(v))" in seg and "esc(k)" in seg)
        check("B7c 给出可操作的下一步（不是死胡同）", "ib-tip" in seg and "不用重开会话" in seg)

    # B8 历史还原路径也带意图理解（防止"刷新后消失"）
    check("B8 历史路径渲染 ex.brief", "ex.brief && (ex.brief.goal || ex.brief.followup)" in cards)

    # B9 思考按轮次分块（不再把全部轮次拼进同一个块）
    check("B9 思考按轮次分块（thinking / thinking_rN）",
          "round > 0 ? ('thinking_r' + round) : 'thinking'" in pipe)
    check("B9b 每块只放自己那轮的文本（detail: esc(_txt)）", "detail: esc(_txt)" in pipe)
    check("B9c 收口时逐个块收口（不只处理 id==='thinking'）",
          "x.type==='thinking' && x.status==='run'" in pipe)

    # B10 前端契约：两种 container 都带滚动跟随
    check("B10 追加环节后外层容器贴底跟随", "b.scrollTop = b.scrollHeight" in pipe)
    return True


# ────────────────────────── C 组：变异自证 ──────────────────────────
def group_c():
    print("\n=== C 组：变异自证（改回旧写法必须变红）===")

    def probe_codefold_default():
        md = read("static/js/mods/05-markdown.js")
        m = re.search(r"return `<div class=\"code-fold([^\"]*)\">`\s*\n\s*\+ `<div class=\"cf-head\" onclick=\"codeFoldToggle\"", md)
        return bool(m) and "open" in (m.group(1) or "")

    mutate_and_check("static/js/mods/05-markdown.js",
                     'return `<div class="code-fold open">`',
                     'return `<div class="code-fold">`',
                     probe_codefold_default, "代码块默认展开")

    def probe_proc_maxh():
        txt = read("static/css/chat.css")
        m = re.search(r"^\.proc\{([^}]*)\}", txt, re.M)
        return bool(m) and "max-height:" in m.group(1)

    mutate_and_check("static/css/chat.css",
                     ".proc{max-height:44vh;overflow-y:auto;overscroll-behavior:contain;padding-right:2px;}",
                     ".proc{padding-right:2px;}",
                     probe_proc_maxh, "执行过程区限高")

    def probe_brief_used():
        return "'理解：' + _b.goal" in read("static/js/mods/11-pipeline.js")

    mutate_and_check("static/js/mods/11-pipeline.js",
                     "'理解：' + _b.goal",
                     "'→ ' + (ev.agent||ev.intent)",
                     probe_brief_used, "意图理解替代 agent 名")

    def probe_route_covered():
        from agent import intent_brief as ib2
        src = read("agent/intent.py")
        real = set(re.findall(r"_(?:rule_)?done\(\s*[^,]+,\s*[\"']([a-zA-Z_0-9]+)[\"']", src))
        return not [v for v in real if v not in ib2.ROUTE_TEXT]

    mutate_and_check("agent/intent_brief.py",
                     '    "llm_first_weak": "模型判断，但把握不大",',
                     "",
                     probe_route_covered, "路由翻译覆盖 llm_first_weak",
                     reload_mod="agent.intent_brief")

    def probe_shell_fallback():
        """壳没被剥离时，goal 会变成澄清壳 —— 断言必须能抓到"""
        from agent import intent_brief as ib3
        shell = "【澄清补充】用户在待澄清状态下直接补充了以下信息（请据此继续，无需再确认）"
        b = ib3.build(shell, intent="design", slots={}, meta={}, label="设计/建模")
        return b["goal"] == "" and b["followup"] is True

    mutate_and_check("agent/intent_brief.py",
                     'if not t or _looks_meta_shell(t):',
                     'if not t:',
                     probe_shell_fallback, "澄清壳剥离（不把壳当理解）",
                     reload_mod="agent.intent_brief")


def main():
    print("=" * 70)
    print("意图理解 + 代码块默认展开 + 执行区限高（含变异自证）")
    print("=" * 70)
    group_a()
    group_b()
    group_c()
    print("\n" + "=" * 70)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAIL:
        print("失败项：" + "；".join(FAILS))
        print("=" * 70)
        return 1
    print("✅ 意图能被读懂（含壳兜底）· 代码块默认展开且可收起 · 执行区限高可滚 · 变异自证成立")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
