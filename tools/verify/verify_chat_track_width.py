# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""回归判据 · 会话轨道宽度（2026-11-06 定稿 v5 —— 用户拍板 1360）

终值：`clamp(880px, 72vw, 1360px)`（用户在 4 个候选里选定，导航保持 200px 不动）。

主判据对应用户能感知的事实：
  A. 是 clamp 自适应，且上限/系数命中用户选择（1360 / 72vw）
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

WANT_LO, WANT_VW, WANT_HI = 880, 72.0, 1360
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
rec("A1 --chat-track 是 clamp 自适应", m is not None, m.group(0) if m else "未匹配")
if m:
    lo, vw, hi = int(m.group(1)), float(m.group(2)), int(m.group(3))
    rec("A2 下限 = 880px（窄屏兜底基线）", lo == WANT_LO, lo)
    rec("A3 vw 系数 = 72（用户选定值）", vw == WANT_VW, vw)
    rec("A4 上限 = 1360px（用户拍板）", hi == WANT_HI, hi)

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

    if "1920" in measured:
        got, src = measured["1920"]["fillPct"], "实测"
    else:
        area = 1920 - AREA_LOSS
        got = min(track_at(1920), area) / area * 100
        src = "推算（无实测文件）"
    rec("B1 @1920 填充率 80~88%%（%s）" % src, 80 <= got <= 88, "%.0f%%" % got)

    # 实测里若有溢出记录，直接判死
    if measured:
        bad = [k for k, v in measured.items() if v.get("overflowX")]
        rec("B2 实测无横向溢出", not bad, ("溢出档位: %s" % bad) if bad else "全部干净")
        navs = {v.get("navW") for v in measured.values() if v.get("navW")}
        rec("B3 实测导航宽恒为 200px", navs == {200}, navs)

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
    rec("G1 服务端发放 1360 版本",
        bool(got) and "1360px" in got.group(0), got.group(0) if got else "未找到")

print("\nALL_OK=%s" % (not fails))
if fails:
    print("FAILED:", fails)
sys.exit(0 if not fails else 1)
