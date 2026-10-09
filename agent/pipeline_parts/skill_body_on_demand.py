"""skill_body_on_demand — 技能正文的渐进式披露（真按需加载）。

## 问题（2026-10-08 实测）
`skills.py:173` 装配技能块时写的是 `body[:600]` —— **硬截断 600 字符**。
注释声称"正文按需加载"，但**代码里没有任何正文取回机制** ⇒ 半成品设计。
后果：8 个旧视图 Agent 的 `system_prompt` 合计 **36158 字符**，
搬进 skill 后仍只能注入 4800 字符 ⇒ **丢弃 87%**。

## 本模块做什么
把「长 skill 正文」走**与工具结果 offload 完全同一条通路**：
  · 正文 ≤ cap（默认 600）：全量注入，**与现有行为完全一致**（零回归面）
  · 正文 > cap：头部摘要 + 引用块 + `skill_body_fetch(id=<oid>)` 取回指引
  · 模型按需调用 `skill_body_fetch` 取回全文（上限 20 万字符）

## 为什么不另造轮子
`agent/pipeline_parts/tool_offload.py` 已有：落库表 `tool_result_offloads`、
`fetch_offload` 取回、`build_reference_block` 引用块、`ensure_fetch_tool` 白名单放行、
TTL 清理。**skill 正文复用同一张表与同一个 fetch 工具** ⇒ 不增表、不增协议、
沿用既有清理链路。

## 关键约束
· **阈值以上才 offload**：短 skill 不走 offload（仅去frontmatter 重复）
· **失败不阻断**：落库失败回退为「截断 + 明示」，不让技能注入把主链路带崩
· **幂等**：同一 (skill, content) 不重复落库（同id复用），避免会话内反复注入重复落行
"""
from __future__ import annotations

import hashlib
import re

#: 超过此长度才 offload（与 skills.py:173 原硬截断同值 => 短 skill 行为不变）
DEFAULT_CAP = 600

#: 摘要装配的**总预算**（字符）。超出会被截断，但已按优先级挑过段落。
#:
#: ★ 三轮实测的演进，每轮都被真实产出证伪（2026-10-08）：
#:  ① 600（原始）：切在「本视图必备要素」之前（该标题实测在正文 664~700 处）
#:     => 模型只看到"通用步骤"，缺"这个视图该有哪些要素"。
#:  ② 1100：覆盖了骨架层的必备要素与硬约束，但切掉了迁入规格的语法细则
#:     => `state` 视图两次真实生成都产出**时序图内容**（entry/do/exit 在 1520、
#:     `state def` 禁令在 1943，模型全都没看到）。
#:  ③ 26~60% 自适应：确实覆盖了细则，但**实测反而更差** —— 8 视图里 5 个产出
#:     **完全相同**的内容（md5 一致，全是照抄骨架，视图部分压根没生成）。
#:     根因：迁入的「## 完整视图规范」是长篇散文，按字符截断会把它整段灌进来，
#:     它既冗长又不如「必备要素/硬约束」关键 => 注意力被稀释。
#:
#: => 结论：**窗口大小不是关键，"窗口里放什么"才是关键**。
#:    改为按**段落优先级**挑选后拼接，而非按字符数从头部硬截。
HEAD_CHARS = 2600

#: 段落标题 → 优先级（数字越小越先入选）
#: ⚠️ 迁入规格的小节用的是 **`##` 级**（实测 activity：## 约束 / ## 输出示例 /
#:    ## 职责 / ## 工作流程），**不是 `#` 级** ⇒ 模式必须同时匹配 1~3 个 `#`。
_PRIORITY_SECTIONS = [
    (r"^本视图必备要素", 0),    # 该视图必须有哪些要素
    (r"^本视图硬约束", 1),      # 违反即不合格的纪律
    (r"^约束$", 2),               # 迁入规格里的「明确禁止…」
    (r"^输出示例", 3),           # 迁入规格里的语法样例
    (r"^(职责|输入|输出|工作流程)$", 4),
]
#: 优先级 < 该值的段落视为「执行骨架」，恒定入选
_HEAD_ALWAYS = 4
#: 预算内最多额外纳入的段落数（再多交给 skill_body_fetch 取回）
#: ★ 由 4 提到 8：`_KEEP_IN_EXCLUDED` 会额外产出「约束 / 输出示例」小节，
#:   4 个名额会被这些高价值段占满，真正想补的「职责/输入」反而进不来。
_MAX_EXTRA_SECTIONS = 8

#: ★ 显式**降权**的长篇段落（2026-10-08 实测踩坑）：
#: 迁入的旧 Agent 正文里，「## 完整视图规范」与「## 视图规范参考」是
#: **大段散文 + Markdown 表格**（实测 state 的这两段合计 4444 字符，占正文 83%）。
#: 首版把它们当"骨架"全量塞进摘要 ⇒ 摘要 = 全文 100%，
#: 实测 8 个视图里 5 个产出**完全相同**的照抄内容。
#:
#: ⚠️ 但**不能整段排除**——它们内部含`#约束`（"明确禁止state def / part def…"）
#: 与 `#输出示例` 这两段**最关键的语法纪律**（实测 state 的语法细则就在其中）。
#: ⇒ 做法：排除段只**提取其中的 `#约束` / `#输出示例` 小节**，
#:    其余散文与表格（职责/输入/工作流程/规范参考表）一律不进摘要。
_EXCLUDE_SECTIONS = [
    r"^完整视图规范",
    r"^视图规范参考",
]
#: 被排除段落内部**仍然要捞出来**的小节（真正的语法纪律所在）
_KEEP_IN_EXCLUDED = [
    r"^约束$",
    r"^输出示例",
]


def _split_sections(text: str) -> list:
    """按 Markdown 标题把正文切成 [(标题, 正文块)]。无标题时整篇当一段。

    ⚠️ `#` 后**允许无空格**（实测迁入规格用的是 `#职责` / `## 约束` 这类写法，
    若要求 `#{1,3}\\s` 会一个都切不出来 ⇒ "排除段里捞子节"永远捞不到）。
    """
    parts = re.split(r"(?m)^(?=#{1,4}[ \t]*\S)", text)
    return [(p.split("\n", 1)[0].strip().lstrip("#").strip(), p) for p in parts if p.strip()]


def build_head(text: str, budget: int = HEAD_CHARS) -> str:
    """按段落优先级装配摘要正文。

    ① 先放「执行骨架」（目标 / 通用步骤 / 判据 / 失败处理）—— 恒定入选；
    ② 再按 `_PRIORITY_SECTIONS` 依次补入**必备要素 / 硬约束 / 迁入规格的约束与示例**，
       预算不足即停；
    ③ 未入选的段落**不静默丢弃**：引用块里给出了 `skill_body_fetch` 取回指引。
    """
    if len(text) <= budget:
        return text
    out, used, extra = [], 0, []
    for title, body in _split_sections(text):
        if any(re.match(pat, title) for pat in _EXCLUDE_SECTIONS):
            # 排除段：只捞出其中的「约束 / 输出示例」小节（关键语法纪律），
            # 其余散文与表格丢弃（实测全量注入会让模型照抄骨架、视图压根不生成）。
            # ⚠️ 拼装时**剔除父标题行**（"## 完整视图规范…"）——
            #   它的内容已被拆散，只留标题会让模型误以为下面全是"规范散文"。
            picked = [(sub_t, sub_b) for sub_t, sub_b in _split_sections(body)[1:]
                      if any(re.match(pat, sub_t) for pat in _KEEP_IN_EXCLUDED)]
            if not picked:
                # 该段**没有可切分的子节**（实测 state：整段就是散文，
                # 「明确禁止…」以行内列表形式出现）⇒ 退而抽取「明确禁止」所在段落
                m = re.search(r"(?ms)^.*明确禁止.*?(?:\n\n|\Z)", body)
                if m:
                    picked = [("（行内约束）", m.group(0))]
            for sub_t, sub_b in picked:
                # 长样例 / 长表格同样截头（纪律条目已在上面完整保留）
                if len(sub_b) > 1200:
                    sub_b = sub_b[:1200] + "\n…（已截断，完整内容可用 skill_body_fetch 取回）"
                extra.append((2, sub_t, sub_b))
            continue
        prio = None
        for pat, p in _PRIORITY_SECTIONS:
            if re.match(pat, title):
                prio = p
                break
        # ★「## 输出示例」是**长样例**（实测 activity 3597 字符，占正文 39%），
        #   它是"参考"而非"纪律"。整段塞进来会让摘要膨胀到 56%，
        #   实测这正是模型照抄样例、不生成视图的诱因之一。
        #   ⇒ 只取其**头部**（语法形态最集中的开头 ~1200 字符），够看写法即可。
        if prio == 3 and len(body) > 1200:
            body = body[:1200] + "\n…（示例已截断，完整样例可用 skill_body_fetch 取回）"
        #★ 只让**白名单里的执行骨架**恒定入选；未匹配任何规则的段落（如
        #   「## 完整视图规范」这类迁入的长篇散文）**不得**默认入选——
        #   否则预算形同虚设、摘要退化成全文（实测 100%），模型注意力被稀释。
        if prio is not None and prio < _HEAD_ALWAYS:
            out.append(body)
            used += len(body)
        else:
            extra.append((99 if prio is None else prio, title, body))
    extra.sort(key=lambda x: x[0])
    added = 0
    for _prio, _title, body in extra:
        if added >= _MAX_EXTRA_SECTIONS or used + len(body) > budget:
            break
        out.append(body)
        used += len(body)
        added += 1
    return "\n".join(out)


#: 取回上限（与 tool_result_fetch 的 20000 对齐，留余量）
FETCH_MAX = 20000

# skill 正文 fetch 的工具名（与工具结果 fetch 分开，避免 id 空间混淆）
FETCH_TOOL = "skill_body_fetch"


def _cap() -> int:
    """阈值走配置，便于按场景调（不硬编码）。"""
    try:
        from core import config
        v = config.get("skills", "body_offload_cap", None)
        return int(v) if v is not None else DEFAULT_CAP
    except Exception:
        return DEFAULT_CAP


def _content_key(skill_name: str, content: str) -> str:
    return hashlib.sha1(f"{skill_name}\x00{content}".encode("utf-8")).hexdigest()[:16]


#: skill 正文的 YAML frontmatter 头（`---\n...\n---\n`）
_FM_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.S)


def strip_frontmatter(body: str) -> str:
    """剥掉正文开头的 YAML frontmatter 块。

    ★ 为什么要剥（2026-10-08 实测）：
    `skills.py` 装配技能块时是 `元数据：frontmatter[:400]` + `正文摘要：<body[:N]>`，
    而这批 skill 的 `content` **本身就以同一段 frontmatter 开头**
    （实测 activity：fm=207 字符，`frontmatter == content` 的前缀完全一致）。
    ⇒ 不剥的话，同一段 frontmatter 在模型眼前**连续出现两遍**，
    白占掉 ~200 字符预算，恰好把「本视图必备要素」挤出摘要窗口。

    只在**生成摘要 head 时**剥离；落库与取回的仍是完整原文
    （frontmatter 里的 name/triggers 等对模型取回后仍有用）。
    """
    return _FM_RE.sub("", body or "", count=1)


def build_skill_reference(oid: int, skill_name: str, full_len: int, head: str) -> str:
    """技能正文引用块（与工具结果引用块同构，便于模型统一理解）。"""
    return (
        f"[技能正文已 offload #{oid}（{skill_name}，原 {full_len} 字符）]\n"
        f"头部摘要：\n{head}\n…\n"
        f"（如需完整正文，请调用工具 {FETCH_TOOL}，参数 {{\"id\": {oid}}}。"
        f"该技能正文含此视图的硬约束与判据，**建议取回后再生成代码**。"
    )


def offload_body(skill_name: str, content: str, conversation_id: int = 0,
                 cap: int | None = None) -> tuple:
    """返回 (注入文本, offloaded_bool, offload_id)。

    · 正文 ≤ cap → (content, False, 0)      ← **不offload**
    · 正文 > cap → (引用块, True, oid)
    · offload 失败 → (截断文本 + 明示, False, 0)，不阻断主链路

    ★ 两条路径都会剥离开头的 YAML frontmatter（2026-10-08 实测修正）：
      `skills.py` 已单独注入「元数据：frontmatter[:400]」，而这批 skill 的 `content`
      本身以同一段 frontmatter 开头 ⇒ 不剥则同一段内容在模型眼前**连出两遍**。
      ⚠️ 剥离后为空（正文只有 frontmatter）时**回退原文**，绝不返回空块。
      非 frontmatter 开头的正文（占多数）**逐字不变** ⇒ 零回归面。
    """
    body = content or ""
    cap = cap if cap is not None else _cap()
    disp = strip_frontmatter(body) or body     # 注入用（去重后）；剥空则回退原文
    if len(body) <= cap:
        return disp, False, 0

    # 摘要 head 同样跳过 frontmatter（已单独注入为「元数据」，重复即浪费预算）
    # ★ 按**段落优先级**装配（`build_head`），不是从头部按字符硬截
    head = build_head(disp)
    key = _content_key(skill_name, body)
    try:
        from agent.pipeline_parts import tool_offload as _to
        # 幂等：同会话内同 skill 同正文不重复落库（多轮对话每轮都装配技能块）
        oid = _to.save_offload_by_key(FETCH_TOOL, body, conversation_id, key)
        if not oid:
            raise RuntimeError("offload 落库返回 0")
        return build_skill_reference(oid, skill_name, len(body), head), True, int(oid)
    except Exception as exc:                # noqa: BLE001
        # 失败回退：截断 + 明示（不谎称已 offload），且**把原因带出来**便于排查
        _h = build_head(disp)
        return (f"{_h}\n…\n"
                f"[⚠️ 技能正文过长（{len(body)} 字符）且 offload 失败（{type(exc).__name__}），"
                f"以上为摘要（{len(_h)} 字符）。"
                f"如正文未覆盖你需要的约束，请如实说明『技能正文不完整』，不要臆造约束。]"), False, 0


def fetch_body(offload_id: int) -> str:
    """按 id 取回技能正文全文（上限 FETCH_MAX）。"""
    try:
        from agent.pipeline_parts import tool_offload as _to
        txt = _to.fetch_offload(offload_id)
        if txt:
            mark_fetched(offload_id)
        return txt[:FETCH_MAX] if txt else ""
    except Exception:
        return ""


#: 本进程内「已被模型取回」的 offload id。
#: ★ 用途（2026-10-09 实测）：收尾轮需要知道**哪些正文模型始终没取回**，
#:   好把它们直接附进收尾请求（那时不能再调工具）。
#:   实测 N3 视图展开 6/8 产出雷同的根因正是：
#:   模型把工具预算花在查标准库上→ 收尾时手里只有「未找到包 X」这种失败信息，
#:   而视图规范正文还在 offload 里没被取回 ⇒ 只能凭记忆写 ⇒ 交出上一轮残留。
#: ⚠️ **进程内**即可（不落库）：只在**同一轮对话**内判定"没取回"，
#:   跨轮次早该忘了——那不是本机制要解决的问题。
_FETCHED: set = set()


def mark_fetched(offload_id: int) -> None:
    _FETCHED.add(int(offload_id))


def unfetched_bodies(offload_ids, limit: int = 2) -> str:
    """取出「已 offload 但模型始终没取回」���正文，拼成收尾补充材料。

    · 只取**前 `limit` 份**——收尾请求也要控长，全塞进去会挤掉真正的产出；
    · 返回空串时 `finalize_messages` 行为与改动前逐字相同；
    · 取不到就跳过（宁缺勿错，不让补正文把主链路带崩）。
    """
    out, used = [], 0
    for oid in list(offload_ids or []):
        try:
            oid = int(oid)
        except (TypeError, ValueError):
            continue
        if oid in _FETCHED:
            continue
        body = fetch_body(oid)
        if not body:
            continue
        out.append(body[:3000])
        used += 1
        if used >= limit:
            break
    return "\n\n---\n\n".join(out)


def ensure_fetch_tool(tools_def, whitelist=None) -> None:
    """把 skill_body_fetch 注入工具定义并放行白名单（幂等）。"""
    if whitelist and FETCH_TOOL not in whitelist:
        whitelist.append(FETCH_TOOL)
    if not tools_def:
        return
    if any((t.get("function") or {}).get("name") == FETCH_TOOL for t in tools_def):
        return
    tools_def.append({
        "type": "function",
        "function": {
            "name": FETCH_TOOL,
            "description": ("读取此前被 offload 的完整技能正文。"
                            "当技能结果提示『已 offload』且需要该技能的完整硬约束与判据时使用。"),
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "offload 编号（见技能提示中的 #编号）"}},
                "required": ["id"],
            },
        },
    })


if __name__ == "__main__":
    # ⚠️ 直接运行时 sys.path[0] = 本文件所在目录（agent/pipeline_parts），
    #   `from agent.pipeline_parts import tool_offload` 会 ImportError ⇒ 静默走 except 回退。
    #   （2026-10-08 实测踩过：自测一直报 offload 失败，根因就是这里少上溯一层。）
    import os as _os
    import sys as _sys
    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)

    short = "短正文" * 40# 120字符（< cap）
    long_ = "活动图必须包含异常分支。" * 200                   # 2400 字符（> cap）
    print("=" * 68)
    print("skill_body_on_demand 自测")
    print("=" * 68)
    print(f"\n① 短正文（{len(short)} 字符，<cap）：应原样返回")
    t, off, oid = offload_body("demo_short", short, conversation_id=1)
    print(f"   offloaded={off} oid={oid} 与原文一致={t == short}")
    assert not off and t == short, "非 frontmatter 开头的短正文必须逐字不变（零回归）"

    print(f"\n② 长正文（{len(long_)} 字符，>cap）：应 offload + 给引用块")
    t2, off2, oid2 = offload_body("demo_long", long_, conversation_id=1)
    print(f"   offloaded={off2} oid={oid2}")
    print(f"   引用块:\n   " + t2.replace("\n", "\n   ")[:420])
    assert off2 and oid2 > 0 and FETCH_TOOL in t2

    print(f"\n③ 幂等：同 skill 同正文再跑，oid 应相同（不重复落库）")
    t3, off3, oid3 = offload_body("demo_long", long_, conversation_id=1)
    print(f"   第一次 oid={oid2}｜第二次 oid={oid3}｜相同={oid2 == oid3}")
    assert oid2 == oid3

    print(f"\n④ 取回全文：")
    back = fetch_body(oid2)
    print(f"   取回 {len(back)} 字符｜与原文一致={back == long_}")
    assert back == long_

    print(f"\n⑤ 工具注入：")
    # ⚠️ 必须给**非空** tools_def：空列表在既有语义里代表「本轮无工具」
    #    （tool_offload.ensure_fetch_tool 同款：if not tools_def: return），
    #    用空列表测会把「幂等注入」误判为「不注入」。
    td = [{"type": "function", "function": {"name": "sysml_v2_validate"}}]
    wl = ["sysml_v2_validate"]
    ensure_fetch_tool(td, wl)
    ensure_fetch_tool(td, wl)          # 幂等
    names = [(t.get('function') or {}).get('name') for t in td]
    print(f"   tools_def 工具名={names}｜出现次数={names.count(FETCH_TOOL)}")
    print(f"   白名单={wl}")
    assert names.count(FETCH_TOOL) == 1 and wl.count(FETCH_TOOL) == 1

    print(f"\n⑥ 无效 id 的降级：")
    print(f"   fetch_body(0)={fetch_body(0)!r}（应为空串）")

    # ── ⑦ frontmatter 去重：摘要里不该再出现 frontmatter（已单独注入为「元数据」）──
    # ⚠️ 必须**同时测短正文与长正文两条路径**：≤cap 走「原样返回」，
    #    >cap 走「摘要 head」，两条路径都得剥——第一版只改了长正文路径，
    #    被这条断言当场抓住（短样本因未 offload 而仍带 frontmatter）。
    print(f"\n⑦ frontmatter 去重（与 skills.py 的「元数据：」不重复）：")
    fm = "---\nname: demo_fm\ndescription: 演示技能\ntriggers: [活动图]\n---\n"
    tail = "# 目标\n生成活动视图。\n\n## 本视图必备要素\n- action 动作\n- if 判定分支\n"
    for label, filler in (("短正文(≤cap)", ""),
                          ("长正文(>cap)", "补充说明。" * 400)):
        body_fm = fm + tail + filler
        _t, _o, _i = offload_body("demo_fm", body_fm, conversation_id=1)
        head_txt = _t.split("头部摘要：\n", 1)[-1]
        dup = "name: demo_fm" in head_txt
        has_need = "必备要素" in head_txt
        print(f"   {label:14} 原长{len(body_fm):5} | 摘要含 frontmatter={dup}（期望 False）"
              f"｜含必备要素={has_need}（期望 True）")
        assert not dup, f"{label}：frontmatter 在摘要里重复出现"
        assert has_need, f"{label}：摘要窗口没覆盖到「本视图必备要素」"

    # ── ⑧ 剥空回退：正文只有 frontmatter 时不能返回空块 ──
    print(f"\n⑧ 剥空回退（正文仅含 frontmatter）：")
    only_fm = fm
    _tf, _of, _idf = offload_body("demo_onlyfm", only_fm, conversation_id=1)
    print(f"   返回长度={len(_tf)} offloaded={_of} 非空={bool(_tf.strip())}")
    assert _tf.strip(), "正文只有 frontmatter 时返回了空块"
    print(f"\n⑧ 真实视图正文头部窗口覆盖检查（模拟 skills.py 的元数据+摘要装配）：")
    import sqlite3 as _sq
    import os as _os2
    _db = _os2.path.join(_os2.path.dirname(_os2.path.dirname(_os2.path.dirname(
        _os2.path.abspath(__file__)))), "mbse.db")
    if not _os2.path.exists(_db):
        print("   [SKIP] 未找到 mbse.db")
    else:
        _c = _sq.connect(_db)
        # 每个视图必须能"看见自己的语法纪律"，而不只是必备要素标题：
        #   ① 「本视图必备要素」标题在窗口内（第一版缺陷）
        #   ② 迁移进来的详细约束段落（#约束 / 明确禁止 / #输出示例）在窗口内
        #      —— 第二版缺陷：state 视图因看不到 1520~1943 处的语法细则，
        #         两次真实生成都产出了时序图内容
        _probes = {"activity": "明确禁止", "state": "#约束", "sequence": "#约束",
                   "parameter": "#约束", "requirement": "明确禁止",
                   "structure": "明确禁止", "usecase": "明确禁止",
                   "ibd": "明确禁止"}
        _n_ok = _n_all = 0
        _bad = []
        _filler = "## 完整视图规范"
        for (_nm, _body) in _c.execute(
                "SELECT name, content FROM skills WHERE name LIKE 'sysml_view_generation_%'"
                " AND name <> 'sysml_view_generation_guide' ORDER BY name").fetchall():
            _n_all += 1
            _vt = _nm.replace("sysml_view_generation_", "")
            _body_s = strip_frontmatter(_body)
            _h = build_head(_body_s)
            _need = "必备要素" in _h
            _det = _probes.get(_vt, "")
            _det_ok = (_det in _h) if _det else True
            _hit = _need and _det_ok
            _n_ok += _hit
            print(f"   [{'OK  ' if _hit else 'MISS'}] {_nm:38} "
                  f"必备要素={_need} 详细约束[{_det}]={_det_ok}")

            # 反退化检查（2026-10-08 实测踩坑）：
            #   上一版把窗口放大到 26~60% 后，**8 个视图里 5 个产出完全相同的内容**
            #   （全是照抄骨架）—— 根因是「## 完整视图规范」这类长篇散文被整段
            #   灌进摘要，稀释了「必备要素 / 硬约束」的注意力。
            #   => 摘要必须**剔除**该散文段落，且总量收敛。
            _has_filler = _filler in _h
            _ratio = len(_h) / max(1, len(_body_s))
            _okk = (not _has_filler) and _ratio <= 0.75
            if not _okk:
                _bad.append((_nm, _has_filler, round(_ratio, 2)))
            print(f"   [{'OK  ' if _okk else 'MISS'}] {_nm:38} "
                  f"摘要{len(_h):5}/{len(_body_s):5} ({_ratio:.0%}) "
                  f"含长篇规范={_has_filler}")
        _c.close()
        assert _n_ok == _n_all, (
            f"{_n_all - _n_ok} 个视图的摘要没覆盖必备要素或详细约束段落")
        assert not _bad, f"摘要被长篇规范散文挤占：{_bad}"
        print(f"   → {_n_ok}/{_n_all} 覆盖（必备要素 + 详细约束段）")

    print("\n" + "=" * 68)
    print("  自测通过：短正文零回归 / 长正文 offload / 幂等 / 可取回 / 工具注入幂等")
    print("            / frontmatter 去重 / 8 视图摘要窗口覆盖必备要素与详细约束")
    print("=" * 68)