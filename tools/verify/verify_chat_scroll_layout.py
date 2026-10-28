"""门禁：聊天滚动错位（`content-visibility` 高度估算）—— 2026-10-28。

## 被验的真实故障

用户报：「AI 输出完成后，页面呈现异常滑动页面时，AI 输出内容区域会错位，
需要刷新页面才会正常」。

## 根因（真实浏览器实测，conv 9629 / 65 条消息 / 7.3 万字 / viewport 1600x900）

`.msg` 上的 `content-visibility:auto; contain-intrinsic-size:auto 132px` 让**屏外消息按
132px 估算高度**（真实均值 ≈640px）：

| 指标 | 有 content-visibility | 禁用后 |
|---|---|---|
| `scrollHeight` 起点 | **4049**（真实 41777，**差 10.3 倍**） | 41777 |
| `scrollHeight` 波动范围 | **2348px**（3534~5882） | **0** |
| 滚动中高度跳变次数 | **7**（最大 1146px） | **0** |
| `scrollTop` 非预期跳变 | **3 次**（最严重：设 2720 → 实际 1574，**−1146px**） | **0** |
| 性能（同步布局 median） | 0ms | 0ms（无差异） |

⇒ 高度跳变时浏览器**滚动锚定**会把视口推走 ⇒ 用户看到「内容错位」。
刷新后视口内消息已实现、高度确定 ⇒ 「正常」。

## 修法

- 默认**移除** `content-visibility`（聊天场景用户本来就从底往上翻历史，
  「高度必须准确」是硬需求；性能由分页渲染 80 条/批 + rAF 分片承担）。
- 仅在**超长会话（>300 条）**由 JS 挂 `.chat-heavy` 恢复兜底，
  估算值 132px → **240px**（132px 远低于真实均值，是错位幅度大的直接原因）。

## 运行

    python tools/verify/verify_chat_scroll_layout.py

退出码 0 = 全绿。
"""

from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PASS = 0
_FAIL = 0
_FAILED = []


def check(group: str, name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {group} {name}")
    else:
        _FAIL += 1
        _FAILED.append(f"{group} {name} :: {detail}")
        print(f"  [FAIL] {group} {name} :: {detail}")


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """剥掉 CSS 注释再匹配。

    ⚠️ 必须剥：本次修复的注释里**引用了** `content-visibility:auto` 这个词，
       直接 grep 会命中注释 ⇒ 假阳性（本仓已多次踩过"判据没剥注释"）。
    """
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


def _msg_rule_body(css: str) -> str:
    """取出裸 `.msg{...}` 规则体（不是 `#chat-area.chat-heavy .msg`）。"""
    clean = _strip_comments(css)
    m = re.search(r"(?m)^\.msg\{([^}]*)\}", clean)
    return m.group(1) if m else ""


# ══════════════════════════════════════════════════════════════════════
# A. CSS：`.msg` 不得再带 content-visibility（两个文件都要）
# ══════════════════════════════════════════════════════════════════════
def group_a():
    for rel in ("static/css/app.css", "static/css/chat.css"):
        css = _read(rel)
        body = _msg_rule_body(css)
        check("A", f"{rel} 存在 .msg 规则", bool(body), "未找到 `.msg{`")
        check("A", f"★{rel} 的 .msg 不含 content-visibility",
              "content-visibility" not in body, body[:120])
        check("A", f"★{rel} 的 .msg 不含 contain-intrinsic-size",
              "contain-intrinsic-size" not in body, body[:120])
        # 兜底规则必须存在且只在 .chat-heavy 下
        clean = _strip_comments(css)
        check("A", f"★{rel} 有 .chat-heavy 兜底规则",
              "#chat-area.chat-heavy .msg" in clean, "未找到兜底规则")

    # A-extra：两个文件的 .msg 规则体必须一致（避免只改一处，调整加载顺序就复发）
    a = _msg_rule_body(_read("static/css/app.css"))
    c = _msg_rule_body(_read("static/css/chat.css"))
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    check("A", "★★app.css 与 chat.css 的 .msg 规则体一致（改一处必复发）",
          norm(a).replace(" ", "") == norm(c).replace(" ", ""),
          f"app={norm(a)[:60]!r} chat={norm(c)[:60]!r}")

    # A-extra2：估算值必须是 240px（132px 是错位幅度大的直接原因）
    for rel in ("static/css/app.css", "static/css/chat.css"):
        clean = _strip_comments(_read(rel))
        m = re.search(r"#chat-area\.chat-heavy \.msg\{[^}]*contain-intrinsic-size:\s*auto\s*(\d+)px",
                      clean)
        check("A", f"{rel} 兜底估算值 = 240px（不是 132px）",
              bool(m) and m.group(1) == "240", m.group(0)[:80] if m else "未匹配")


# ══════════════════════════════════════════════════════════════════════
# B. JS：动态切换 .chat-heavy
# ══════════════════════════════════════════════════════════════════════
def group_b():
    js = _read("static/js/mods/03-chat.js")

    check("B", "存在 syncChatHeavy 函数", "function syncChatHeavy" in js)
    check("B", "★阈值常量 CHAT_HEAVY_THRESHOLD = 300",
          bool(re.search(r"CHAT_HEAVY_THRESHOLD\s*=\s*300", js)),
          str(re.findall(r"CHAT_HEAVY_THRESHOLD\s*=\s*\d+", js)))

    # 必须在渲染完成时调（renderMessagesChunked 的 else 分支）
    m = re.search(r"function renderMessagesChunked[\s\S]*?\n\}", js)
    body = m.group(0) if m else ""
    check("B", "★首屏渲染完成后调用 syncChatHeavy",
          "syncChatHeavy(" in body, body[-160:] if body else "未找到 renderMessagesChunked")

    # 必须在 loadEarlierMessages 里调，且**在视口锚定之后**
    m2 = re.search(r"async function loadEarlierMessages[\s\S]*?\n\}", js)
    body2 = m2.group(0) if m2 else ""
    i_anchor = body2.find("area.scrollTop += area.scrollHeight - prevHeight")
    i_sync = body2.find("syncChatHeavy(")
    check("B", "★加载更早后也调用 syncChatHeavy", i_sync >= 0, body2[-200:] if body2 else "未找到")
    check("B", "★★syncChatHeavy 在视口锚定**之后**（否则锚定用错高度 ⇒ 又错位）",
          i_anchor >= 0 and i_sync > i_anchor, f"anchor@{i_anchor} sync@{i_sync}")

    # 逻辑正确性：只在超过阈值时加类（不能恒加，否则等于没修）
    m3 = re.search(r"function syncChatHeavy\(area\)\{[\s\S]*?\n\}", js)
    b3 = m3.group(0) if m3 else ""
    check("B", "★syncChatHeavy 按阈值条件切换（非恒加/恒摘）",
          "CHAT_HEAVY_THRESHOLD" in b3 and "toggle" in b3, b3[:160])


# ══════════════════════════════════════════════════════════════════════
# C. ★★ 变异自证：还原旧写法 ⇒ 判据必须翻红
# ══════════════════════════════════════════════════════════════════════
def group_c():
    def judge(msg_body: str) -> bool:
        """判据：.msg 规则体不含 content-visibility。"""
        return "content-visibility" not in msg_body

    real = _msg_rule_body(_read("static/css/app.css"))
    old = "display:flex;gap:10px;max-width:94%;min-width:0;content-visibility:auto;contain-intrinsic-size:auto 132px;"

    check("C", "★★变异自证：把 content-visibility 加回 .msg ⇒ A 组判据翻红",
          judge(real) and not judge(old),
          f"真实现={judge(real)} 变异={judge(old)}")

    # C2 反向：剥注释这件事本身要有鉴别力
    #   构造"只有注释里出现 content-visibility"的 CSS ⇒ 剥注释后应判通过
    only_comment = ".msg{display:flex;}\n/* 曾经有 content-visibility:auto */"
    check("C", "★反向自证：注释里提到不算违规（剥注释生效）",
          judge(_msg_rule_body(only_comment)),
          _msg_rule_body(only_comment))

    # C3 ★ 若不剥注释，判据会把注释当违规 ⇒ 证明"必须剥"是有依据的
    def judge_no_strip(css: str) -> bool:
        m = re.search(r"(?m)^\.msg\{([^}]*)\}", css)
        # 不剥注释 + 全文搜（模拟错误的判据写法）
        return "content-visibility" not in css

    check("C", "★★判据若全文搜不剥注释会误报（本次踩过的假阳性）",
          judge_no_strip(only_comment) is False, "应当为 False 才能证明剥注释必要")


# ══════════════════════════════════════════════════════════════════════
# D. 证据留档：浏览器实测结论必须可追溯
# ══════════════════════════════════════════════════════════════════════
def group_d():
    ev = os.path.join(_ROOT, "outputs", "_verify_cv_fixed.json")
    check("D", "★修复后浏览器实测证据已留档", os.path.exists(ev), ev)
    if os.path.exists(ev):
        import json
        try:
            data = json.loads(open(ev, encoding="utf-8").read())
            res = (data[0] or {}).get("result") or {}
            check("D", "★★实测 scrollHeight 跳变次数 = 0",
                  res.get("shJumpCount") == 0, str(res.get("shJumps"))[:120])
            check("D", "★★实测 scrollTop 非预期跳变 = 0",
                  res.get("stJumpCount") == 0, str(res.get("stJumps"))[:120])
            check("D", "★实测 contentVisibility = visible",
                  res.get("contentVisibility") == "visible", str(res.get("contentVisibility")))
            check("D", "★实测真实高度（>40000px，非 4049 估算值）",
                  int(res.get("shStart") or 0) > 40000, str(res.get("shStart")))
            check("D", "最后一条消息在视口内", res.get("lastInViewport") is True)
        except Exception as exc:
            check("D", "证据文件可解析", False, repr(exc))


def main() -> int:
    print("=" * 74)
    print("门禁：聊天滚动错位（content-visibility 高度估算）2026-10-28")
    print("=" * 74)
    for name, fn in (("A. CSS 规则", group_a),
                     ("B. JS 动态切换", group_b),
                     ("C. 变异自证", group_c),
                     ("D. 浏览器实测证据", group_d)):
        print(f"\n── {name} ──")
        try:
            fn()
        except Exception as exc:
            check(name[0], f"{name} 未预期异常", False, repr(exc))

    total = _PASS + _FAIL
    print("\n" + "=" * 74)
    if _FAIL:
        print(f"结果：{_PASS}/{total} 通过，{_FAIL} 项失败")
        for f in _FAILED:
            print(f"  ✗ {f}")
        print("=" * 74)
        return 1
    print(f"结果：{_PASS}/{total} 全部通过")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
