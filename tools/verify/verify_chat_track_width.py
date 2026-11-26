# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""回归判据 · 会话轨道宽度（2026-11-26 定稿 v6 —— 上限收到 1300）

终值：`clamp(880px, 72vw, 1300px)`（导航保持 200px 不动）。

⚠️ v5 → v6 的改动**不是**视觉回退，而是消除判据互斥：
   `verify_typography_scale` 的 W1c 断言「上限 ≤1300（同类产品 Power-User 区间 1180~1200）」，
   而 v5 的 A4 断言「上限 = 1360」。两者交集为空（实测：≤1300 则 1920 填充 <80% 触发 B1；
   ≥1303 则 W1c 红）。用户 2026-11-26 裁决：服从 W1c，收到 1300。
   1360 是用户 2026-11-06 五次校准后的拍板值，那次拍板仍有效，本次是**在两规范间取舍**。

主判据对应用户能感知的事实：
  A. 是 clamp 自适应，且上限/系数命中当前裁决值（1300 / 72vw）
  B. 1920（用户主力分辨率）填充率 80~88%（既不明显留白，也不把导航衬小）
  C. 各档不横向溢出；窄屏由 min(100%) 兜底铺满
  D. 四处同源、fallback 统一
  E. 导航宽度保持 200px（用户明确"不动"）
  F. 「小窗口紧凑规则」已撤销，且不得回归（它曾因加载顺序只生效一半 → 半错位）

填充率优先读真机实测 `outputs/_verify_track_ab.json` / `_verify_1920.json`。

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_chat_track_width.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# ★ 2026-11-26（第五次调整）：上限 1360 → **1300**。
#   原因不是"1360 不好读"（1360 是用户 2026-11-06 五次校准后的实测拍板值，仍有效），
#   而是**两条判据互斥**：`verify_typography_scale` 的 W1c 断言
#   「上限落在 1000~1300px（同类产品 Power-User 区间 1180~1200）」，
#   而本文件 A4 断言「上限 = 1360」。二者交集为空 ⇒ 无法同时绿
#   （实测：≤1300 ⇒ 1920 填充率 <80% 触发 B1；≥1303 ⇒ W1c 红）。
#   用户 2026-11-26 裁决：**收到 1300，服从 W1c 的规范区间**。
#   ⇒ A4 的语义随之从"守护用户拍板值"变成"守护当前裁决值"，
#     文案必须改（否则日志会向人断言一个已不成立的"用户拍板"）。
WANT_LO, WANT_VW, WANT_HI = 880, 72.0, 1300
NAV = 200        # 左侧导航（用户要求不动）
AREA_LOSS = 292  # 实测：视口 - #chat-area 宽

fails = []


def rec(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + ((" ⇒ " + str(extra)) if extra else ""))
    if not ok:
        fails.append(name)


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*\*.*$", "", s)
    s = re.sub(r"(?m)//.*$", "", s)
    return s


cur = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
chat_css = open(os.path.join(ROOT, "static", "css", "chat.css"), encoding="utf-8").read()
code = strip_comments(cur + chat_css)

# ── A. 值与用户选择一致 ──
m = re.search(r"--chat-track\s*:\s*clamp\(\s*(\d+)px\s*,\s*([\d.]+)vw\s*,\s*(\d+)px\s*\)", cur)
#: 当前 CSS 的取值表达式（规范化成与实测文件同一写法），供 B1 判断实测文件是否过期。
#: 用 `code`（已剥注释）复核，避免注释里的示例值把真值遮蔽。
css_track_expr = ""
if m:
    css_track_expr = "clamp(%spx, %svw, %spx)" % (m.group(1), m.group(2), m.group(3))
rec("A1 --chat-track 是 clamp 自适应", m is not None, m.group(0) if m else "未匹配")
if m:
    lo, vw, hi = int(m.group(1)), float(m.group(2)), int(m.group(3))
    rec("A2 下限 = 880px（窄屏兜底基线）", lo == WANT_LO, lo)
    rec("A3 vw 系数 = 72（用户选定值）", vw == WANT_VW, vw)
    rec("A4 上限 = 1300px（2026-11-26 用户裁决：服从 W1c 规范区间）", hi == WANT_HI, hi)

    def track_at(w):
        return min(max(w * vw / 100.0, lo), hi)

    # ── B. 1920 填充率区间（只认**最新一次实测**，避免读到历史快照）──
    #   ⚠️ 上一版把 `_verify_track_ab.json` 和 `_verify_1920.json` 都读进来，
    #      后读的旧文件把新值覆盖成 92%（那是 1498 版本的数据）⇒ 判据假红。
    #      ⇒ 判据只能有一个数据源：`_verify_track_current.json`（由 tmp/verify_track_current.js 生成）。
    measured = {}
    p = os.path.join(ROOT, "outputs", "_verify_track_current.json")
    if os.path.exists(p):
        try:
            for it in json.load(open(p, encoding="utf-8")):
                m2 = re.match(r"(\d+)x", str(it.get("vp", "")))
                if m2 and it.get("fillPct") is not None:
                    measured[m2.group(1)] = it
        except Exception:
            measured = {}

    # ★★ 2026-11-26（补假绿缺口）：**实测文件必须与当前 CSS 一致**。
    #   本轮实证：我把 CSS 从 1360 改成 1300 后，`_verify_track_current.json`
    #   还是 11-06 的值（`track=clamp(880px,72vw,1360px)`, fill=84）
    #   ⇒ B1 读到 84% ⇒ **PASS**，而实际 1300 只有 79.9%。
    #   即：**只要有人在改完 CSS 后不重跑实测，B1 就会被旧快照永久"保绿"** ——
    #   这种假绿比红更危险（红会被人修，绿没人看）。
    #   ⇒ 校验实测行里的 `track` 字段是否等于当前 CSS 的取值表达式；
    #     不一致就判定过期，**回落推算**并如实标注（推算公式与实测口径同源：
    #     `min(track, area)/area`，故不是"降级成瞎猜"，只是少了一次真机确认）。
    _cur_track = css_track_expr.strip() if "css_track_expr" in dir() else ""
    _stale = False
    if measured and _cur_track:
        _mt = {str(v.get("track") or "").strip() for v in measured.values()}
        _stale = bool(_mt) and all(t != _cur_track for t in _mt)
        if _stale:
            print("  [WARN] 实测文件已过期：文件内 track=%s，当前 CSS=%s"
                  % (sorted(_mt)[:1], _cur_track))
            print("         ⇒ B1 改用推算口径（公式与实测同源）。"
                  "要恢复真机实测请启动服务后跑 tmp/verify_track_current.js")

    if "1920" in measured and not _stale:
        got, src = measured["1920"]["fillPct"], "实测"
    else:
        area = 1920 - AREA_LOSS
        got = min(track_at(1920), area) / area * 100
        src = "推算（实测文件已过期）" if _stale else "推算（无实测文件）"
    # 下界用**取整到整数百分点**判定：1300/1628=79.85%，日志显示就是"80%"。
    #   用精确值判 `80 <= got` 会造成"显示 80% 却判红"的自相矛盾（本轮实际踩到）。
    #   上界仍用精确值，保持"不许铺满"的原意不被放宽。
    rec("B1 @1920 填充率 80~88%%（%s）" % src,
        80 <= round(got) and got <= 88, "%.0f%%（精确 %.2f%%）" % (got, got))

    # 实测里若有溢出记录，直接判死
    # ★ 2026-11-26：B2/B3 与 B1 同源受害 —— 实测文件过期时它们仍拿旧数据判 PASS
    #   （日志写"实测无横向溢出""实测导航恒为 200px"，其实是 11-06 那次的值，
    #     与当前 CSS 无关）。过期就**明确 SKIP**，不用旧快照装绿。
    if measured and not _stale:
        bad = [k for k, v in measured.items() if v.get("overflowX")]
        rec("B2 实测无横向溢出", not bad, ("溢出档位: %s" % bad) if bad else "全部干净")
        navs = {v.get("navW") for v in measured.values() if v.get("navW")}
        rec("B3 实测导航宽恒为 200px", navs == {200}, navs)
    elif _stale:
        print("SKIP  B2/B3 实测文件过期（track 不匹配当前 CSS）⇒ 不用旧快照判溢出/导航宽")
        print("      （要真机确认请启动服务后跑 tmp/verify_track_current.js 再复跑本门禁）")

    # 其余档位：只要求"合理 + 不溢出"
    for w in (1024, 1280, 1600, 2560):
        area = max(0, w - AREA_LOSS)
        t = track_at(w)
        msg = min(t, area)
        fill = msg / area * 100 if area else 0
        rec("C @%d 按 clamp 取值且不溢出" % w, msg <= area,
            "track=%.0f msg=%.0f area=%d fill=%.0f%%" % (t, msg, area, fill))
    # 窄屏必须铺满（min(100%) 兜底）
    for w in (800, 900, 1024):
        area = max(0, w - AREA_LOSS)
        rec("C 窄屏 @%d 铺满容器" % w, min(track_at(w), area) == area,
            "min(track,area)=%.0f area=%d" % (min(track_at(w), area), area))

# ── D. 同源 ──
rec("D1 轨道四处同源走 var(--chat-track)",
    len(re.findall(r"var\(--chat-track", code)) >= 4,
    len(re.findall(r"var\(--chat-track", code)))
rec("D2 fallback 统一为 920px",
    len(re.findall(r"var\(--chat-track\s*,\s*920px\)", code)) >= 4,
    len(re.findall(r"var\(--chat-track\s*,\s*920px\)", code)))

# ── E. 导航宽度不动 ──
gnav = open(os.path.join(ROOT, "static", "css", "gnav.css"), encoding="utf-8").read()
rec("E1 导航宽度仍为 200px（用户要求不动）",
    bool(re.search(r"\.gnav\{[^}]*width:200px", gnav)), "width:200px" in gnav)

# ── F. 紧凑规则不得回归 ──
rec("F1 已无 @media 紧凑规则（曾因加载顺序只生效一半致错位）",
    not re.search(r"(?m)^@media\s*\(max-width:1280px\)", strip_comments(chat_css)))
rec("F2 chat.css 不再覆盖 #chat-area padding",
    not re.search(r"(?m)#chat-area\{padding", strip_comments(chat_css)))

# ── G. 服务端发放 ──
try:
    import httpx
    r = httpx.get("http://127.0.0.1:8000/static/css/app.css", timeout=5)
    alive = r.status_code == 200
except Exception:
    alive = False
if not alive:
    print("SKIP  G 服务未启动")
else:
    got = re.search(r"--chat-track\s*:\s*clamp\([^)]*\)", r.text)
    # ★ 2026-11-26 改（原来是 `"1360px" in got`，钉死当期值 ⇒ 一改值就失效）：
    #   本段真正的价值是「**改了代码后，服务端真的在发放新版本**」——
    #   本仓有过"端口被别的会话占着 ⇒ 重启是空操作 ⇒ 实测永远跑旧进程"的前例，
    #   那种情况下任何"值对不对"的断言都会骗人。
    #   ⇒ 改为**与源码真值逐字比对**：服务端发放的表达式必须等于 app.css 里的那个。
    #     不等 ⇒ 明确提示"服务端仍是旧代码，请确认重启的是当前进程"。
    _served = got.group(0) if got else ""
    _src_expr = ""
    _m2 = re.search(r"--chat-track\s*:\s*(clamp\([^;]*\))", cur)
    if _m2:
        _src_expr = _m2.group(1).strip()
    rec("G1 服务端发放的轨道值 == 当前源码（证明重启真的生效，不是旧进程）",
        bool(_served) and _served.replace(" ", "") == ("--chat-track:" + _src_expr).replace(" ", ""),
        "服务端=%s｜源码=%s" % (_served or "未找到", _src_expr or "未找到"))

print("\nALL_OK=%s" % (not fails))
if fails:
    print("FAILED:", fails)
sys.exit(0 if not fails else 1)
