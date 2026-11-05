# -*- coding: utf-8 -*-
"""用户可见产物的**形态契约**（2026-11-04）。

── 为什么单独成模块（而不是只写在门禁里）─────────────────────────────────
本仓已有 20+ 个 Guard，守的全是**数值/流程不变式**（预算、resume、partial、loop、路由…），
**没有一条**守"用户最终看到的东西长什么样"。于是形态类缺陷能一路穿到界面上：

  · 协议 JSON 裸露在正文（真库 conv 990408 / msg 5761，21432 字里 3 段）；
  · 正文围栏 13 个（奇数）⇒ 末段代码块没闭合，中断提示落进代码块；
  · `## 一、结论先行` 出现 2 次（三份报告被拼接）；
  · 173 条视图产物 `nodes=0 / edges=0`（"打开了但是空图"）。

契约写在**产品模块**里、门禁只做调用与断言，理由是：
  ① 运行时也能用（例如产出前自检、HIL 预览前校验）；
  ② 契约与实现同仓同版本演进，不会出现"测试里写一套、产品里另一套"；
  ③ 门禁天然可测 —— 它校验的是纯函数，不依赖真实会话。

**能力边界（重要）**：本模块只校验**结构**（键齐不齐、端点存不存在、有没有泄漏），
**不判断内容对不对**（视图该画 5 个部件还是 3 个、文案是否准确）—— 那是评审的事。
把结构问题与内容问题混在一个门禁里，会导致"因为内容不好而误报结构"。
"""
from __future__ import annotations

import json
import re

#: 产物 kind 合法取值（与 `artifacts.kind` 的实际写入点对齐）
ARTIFACT_KINDS = ("sysml", "code", "document", "report", "matrix", "other")

#: 视图类型（与 `view_generator.VIEW_TYPES` 的键对齐；此处刻意不 import，
#: 避免契约模块反向依赖视图层 —— 视图层新增类型时这里同步加一项即可，门禁会提示）
VIEW_KINDS = ("BDD", "IBD", "REQ", "UC", "ACT", "SEQ", "STM", "PKG", "PAR", "TRACE", "CMP")

#: 一眼就能认出的"内部协议 JSON"标记（出现在**正文/预览**里就是泄漏）
_PROTO_MARK = '{"schema_version"'

#: `undefined`/`NaN` 出现在用户可见文本里 = 前端 JS 序列化残留
_BAD_TOKENS = ("undefined", "NaN", "[object Object]")


def _has_bad_token(s: str) -> bool:
    """用户可见文本里是否残留 JS 序列化垃圾。

    ⚠️ 只看**短文本或整块文本**：长正文里出现英文单词 "undefined" 未必是 bug
    （模型可能在讲 JS）。所以判据收窄为：**整段以它开头/结尾**，或独立成词且长度很短。
    """
    t = str(s or "").strip()
    if not t:
        return False
    for tok in _BAD_TOKENS:
        if t == tok or t.startswith(tok + " ") or t.endswith(" " + tok):
            return True
        if len(t) <= 60 and tok in t:      # 短片段里出现基本可以断定是残留
            return True
    return False


# ── 视图（ViewModel）──────────────────────────────────────────────────────

def validate_viewmodel(vm, expect_kind: str | None = None) -> list:
    """校验单个 ViewModel（`{view, nodes, edges, warnings}`）。返回**问题清单**（空=通过）。

    判据（每条都来自实测缺陷，不是凭空设计）：
      · 四键齐 —— 缺 `warnings` 会让前端读 `undefined` 崩渲染；
      · `view.type` 必须存在，且与所在键名一致 —— 否则"点的是追溯视图，渲染成需求图"；
      · **悬空边**：`edges[].source/target` 必须指向 `nodes` 里存在的 id
        —— 这是最隐蔽的一类：图能画出来，但有一条线连到空气；
      · `nodes[]` 必须各有 `id` 与 `name`（前端按 id 建索引、按 name 显示）；
      · 不得出现 `undefined`/`NaN`。
    """
    probs = []
    if not isinstance(vm, dict):
        return ["ViewModel 不是 dict：%r" % type(vm).__name__]
    for k in ("view", "nodes", "edges", "warnings"):
        if k not in vm:
            probs.append("缺键 %s（前端读 undefined）" % k)
    if probs:
        return probs
    v = vm.get("view")
    if not isinstance(v, dict):
        probs.append("view 不是 dict")
    else:
        t = v.get("type")
        if not t:
            probs.append("view.type 为空（前端无法选渲染器）")
        elif expect_kind and str(t) != str(expect_kind):
            probs.append("view.type=%s 与键名 %s 不一致" % (t, expect_kind))
        elif str(t) not in VIEW_KINDS:
            probs.append("view.type=%s 不在已知视图类型内" % t)
        if _has_bad_token(v.get("name")):
            probs.append("view.name 含 undefined/NaN")

    nodes = vm.get("nodes")
    if not isinstance(nodes, list):
        probs.append("nodes 不是 list")
        nodes = []
    ids = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            probs.append("nodes[%d] 不是 dict" % i)
            continue
        nid = n.get("id")
        if nid in (None, ""):
            probs.append("nodes[%d] 缺 id" % i)
        else:
            ids.add(str(nid))
        if not str(n.get("name") or "").strip():
            probs.append("nodes[%d] 缺 name" % i)
        if _has_bad_token(n.get("name")):
            probs.append("nodes[%d].name 含 undefined/NaN" % i)

    edges = vm.get("edges")
    if not isinstance(edges, list):
        probs.append("edges 不是 list")
        edges = []
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            probs.append("edges[%d] 不是 dict" % i)
            continue
        for end in ("source", "target"):
            val = e.get(end)
            if val in (None, ""):
                probs.append("edges[%d] 缺 %s" % (i, end))
            elif str(val) not in ids:
                probs.append("edges[%d].%s=%r 指向不存在的节点（悬空边）" % (i, end, val))
    return probs


def validate_sysml_views(meta) -> list:
    """校验 `artifacts.meta.sysml_views` 整块（可能含多个视图）。返回问题清单。"""
    probs = []
    if not isinstance(meta, dict):
        return ["meta 不是 dict"]
    sv = meta.get("sysml_views")
    if sv is None:
        return []                      # 非视图类产物：不做视图校验
    if not isinstance(sv, dict):
        return ["sysml_views 不是 dict"]
    views = sv.get("views")
    if views is None:
        return ["sysml_views 缺 views"]
    if not isinstance(views, dict):
        return ["sysml_views.views 不是 dict"]
    if not views:
        return ["sysml_views.views 为空（产物存在但没有任何视图）"]
    for kind, item in views.items():
        if not isinstance(item, dict):
            probs.append("views[%s] 不是 dict" % kind)
            continue
        # ⚠️ 实测结构：`views.{TYPE}` **本身就是一个 ViewModel** ——
        #   `{view, nodes, edges, warnings}` 四键**平铺**在这一层，
        #   而 `view` 只是其中的"视图元信息"子对象（type/name/v1/v2/kind/kb/desc）。
        #   本模块第一版误写成 `validate_viewmodel(item.get("view"))`，
        #   于是拿元信息子对象当 ViewModel 校验 ⇒ 恒报"缺 nodes/edges"（自测 B1g 抓到）。
        for p in validate_viewmodel(item, expect_kind=kind):
            probs.append("views[%s]: %s" % (kind, p))
    return probs


# ── 卡片（card_data）─────────────────────────────────────────────────────

#: 卡片顶层必填键（缺了前端渲染不出意图/来源，且"来源可追溯"是产品承诺）
CARD_REQUIRED = ("intent", "agent")

#: 子任务卡片必填键
SUBTASK_REQUIRED = ("key", "title", "status")


def _walk_proto_leak(obj, path="") -> list:
    """找"协议 JSON 出现在不该出现的字段"的泄漏。

    实测口径（重要，避免误报）：真库 card_data 里 `schema_version` **绝大多数是合法的** ——
    它出现在 `/plan[i]/summary/` 与 `/exec/subtasks[i]/summary/` 下，
    那是**子任务结构化摘要的专用字段**（`summary_json` 的落点）。
    ⇒ 只有出现在**这两个路径之外**才算泄漏。
    """
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = "%s/%s" % (path, k)
            if k == "schema_version" and path and not path.endswith("/summary"):
                bad.append("协议键泄漏在 %s" % child)
            bad.extend(_walk_proto_leak(v, child))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):        # 只看前 50 项，避免超大卡片拖慢
            bad.extend(_walk_proto_leak(v, "%s[%d]" % (path, i)))
    return bad


def validate_card_data(card_data) -> list:
    """校验卡片（`messages.card_data`）。入参可为 JSON 字符串或 dict。返回问题清单。"""
    probs = []
    if card_data in (None, "", "{}"):
        return []                              # 空卡片合法（纯文本消息）
    obj = card_data
    if isinstance(card_data, str):
        try:
            obj = json.loads(card_data)
        except Exception as e:
            return ["card_data 不是合法 JSON：%s" % str(e)[:60]]
    if not isinstance(obj, dict):
        return ["card_data 不是 dict"]

    for k in CARD_REQUIRED:
        if not str(obj.get(k) or "").strip():
            probs.append("缺必填键 %s（前端无法显示意图/来源）" % k)

    # 子任务列表（两条来源：plan 与 exec.subtasks）
    for holder, where in ((obj.get("plan") or [], "plan"),
                          (((obj.get("exec") or {}).get("subtasks")) or [], "exec.subtasks")):
        if not isinstance(holder, list):
            continue
        for i, it in enumerate(holder[:50]):
            if not isinstance(it, dict):
                probs.append("%s[%d] 不是 dict" % (where, i))
                continue
            for k in SUBTASK_REQUIRED:
                if k not in it:
                    probs.append("%s[%d] 缺 %s" % (where, i, k))

    probs.extend(_walk_proto_leak(obj))
    if _has_bad_token(obj.get("agent")):
        probs.append("agent 字段含 undefined/NaN")
    return probs


# ── 产物（artifacts）──────────────────────────────────────────────────────

def validate_artifact(art) -> list:
    """校验产物行（dict，含 kind/title/file_path/preview_content/meta）。返回问题清单。

    判据：
      · kind 必须在枚举内（未知 kind 前端没有对应渲染器 ⇒ 点开空白）；
      · title 非空（产物卡片要显示名字）；
      · **可打开性**：三类之一必须存在 ——
          ① `file_path`（落盘文件）② `preview_content`（内联预览）
          ③ `meta.sysml_views`（视图产物，内容在 meta 里，**本来就没有文件**）
        ⚠️ 实测：真库 173 条 `kind='sysml'` 全部"无 file_path 也无 preview_content"，
          这是**正常**的（视图由 ViewModel 渲染）⇒ 若一刀切要求 file_path，会 173 条全红。
      · `preview_content` 不得以协议 JSON 开头（那就是把内部信封当正文给用户看）。
    """
    probs = []
    if not isinstance(art, dict):
        return ["产物不是 dict"]
    kind = str(art.get("kind") or "").strip()
    if not kind:
        probs.append("kind 为空")
    elif kind not in ARTIFACT_KINDS:
        probs.append("kind=%r 不在枚举内（前端无渲染器）" % kind)
    if not str(art.get("title") or "").strip():
        probs.append("title 为空")

    fp = str(art.get("file_path") or "").strip()
    pc = str(art.get("preview_content") or "").strip()
    meta = art.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    has_views = isinstance(meta, dict) and bool((meta.get("sysml_views") or {}).get("views"))
    if not fp and not pc and not has_views:
        probs.append("不可打开：file_path / preview_content / meta.sysml_views 三者皆空")

    if pc.startswith(_PROTO_MARK) or pc.startswith("```json\n" + _PROTO_MARK):
        probs.append("preview_content 以协议 JSON 开头（内部信封被当正文展示）")
    if _has_bad_token(pc) and len(pc) <= 60:
        probs.append("preview_content 含 undefined/NaN")
    return probs


def summarize(problems: list, label: str = "") -> str:
    """把问题清单压成一行（给日志/门禁用，避免刷屏）。"""
    if not problems:
        return ""
    head = "；".join(problems[:5])
    more = ("…共 %d 项" % len(problems)) if len(problems) > 5 else ""
    return ("%s: " % label if label else "") + head + more
