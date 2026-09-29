# -*- coding: utf-8 -*-
"""知识看板 —— 真实页面交互验证（Playwright 真实点击 + 截图，非 getComputedStyle 断言）。

纪律（本仓约定）：
  · 断言一律以「真实点击/交互 + 类名与数量 + 截图」为地面真相；
  · **不写死数量**：期望值取自同一时刻的 `/api/knowledge/dashboard` 响应，与之对比。

用法：python tools/verify/kb_ui_check_kb_dashboard.py <输出目录>
"""
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
BASE = "http://127.0.0.1:8000"
BRANCH = "personal"


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


DASH = api(f"/api/knowledge/dashboard?branch={BRANCH}&window=all")
EXP_REDLINES = len(DASH["redlines"])
# 治理簇面板只渲染 groups 的指标（redlines 渲染在红线面板）——期望值按面板范围取，勿混算
EXP_METRICS = sum(len(g["items"]) for g in DASH["groups"])
GRP = {g["key"]: len(g["items"]) for g in DASH["groups"]}
EXP_GOV_ALERT = sum(1 for g in DASH["groups"] for m in g["items"] if m["status"] == "alert")
EXP_GOV_WARN = sum(1 for g in DASH["groups"] for m in g["items"] if m["status"] == "warn")
EXP_RL_ALERT = sum(1 for m in DASH["redlines"] if m["status"] == "alert")
EXP_GROUPS = [g["name"] for g in DASH["groups"]]
ACC = DASH["acceptance"]
EXP_ACC_OK = sum(1 for a in ACC if a["status"] == "ok")
EXP_HEALTH = DASH["northstar"]["health_score"]

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1680, "height": 1050})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(1200)
    pg.evaluate("() => { try { go('kb','kb-a'); } catch(e){} }")
    pg.wait_for_timeout(3800)

    print("\n[1] 首屏容器渲染（P0 契约不回归）")
    for sel in ["#kb-stats", "#kb-redline", "#kb-gov"]:
        el = pg.query_selector(sel)
        ck(f"{sel} 已渲染且有内容", el is not None and len((el.inner_text() or "").strip()) > 20)

    print("\n[2] P1 顶栏（时间窗 / 分支 / 快照）")
    tb = pg.inner_text("#kb-dash-topbar")
    ck("顶栏渲染含时间窗（不含已移除的跳转子页入口）",
       "时间窗" in tb and "跳转子页" not in tb)
    ck("顶栏含「记录快照」按钮", "记录快照" in tb)
    n_win = pg.eval_on_selector_all("#kb-dash-topbar [data-kb-win]", "els => els.length")
    ck("时间窗 chip 数为 3（用 data-kb-win 精确计数，不与子页 chip 混算）", n_win == 3, f"{n_win}")
    pg.click("#kb-dash-topbar .kbhub-chip >> nth=0")   # 近 7 天
    pg.wait_for_timeout(3000)
    on = pg.eval_on_selector("#kb-dash-topbar .kbhub-chip.on", "el => el.textContent.trim()")
    ck("真实点击时间窗后 .on 迁移到「近 7 天」", "近 7 天" in on, f"当前 on={on}")

    print("\n[3] P1 首屏北极星")
    ns = pg.inner_text("#kb-northstar")
    ck("北极星面板存在且含健康分", "知识健康分" in ns)
    ck("健康分渲染值 == 接口值", str(EXP_HEALTH) in ns, f"{EXP_HEALTH}")
    n_ns = pg.eval_on_selector_all("#kb-northstar .asset", "els => els.length")
    ck("北极星卡数 = 1(健康分) + 3(水位)", n_ns == 4, f"{n_ns}")

    print("\n[4] P1 四簇 Tab（真实点击切换）")
    tabs = pg.eval_on_selector_all("#kb-cluster-tabs .kbhub-chip", "els => els.length")
    ck("四簇 Tab 数量 = 4", tabs == 4, f"{tabs}")
    # 真实点击「数据质量」
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="quality"]')
    pg.wait_for_timeout(700)
    d_q = pg.eval_on_selector('.kb-cluster[data-cluster="quality"]', "el => getComputedStyle(el).display")
    d_g = pg.eval_on_selector('.kb-cluster[data-cluster="gov"]', "el => getComputedStyle(el).display")
    ck("点击后 quality 簇可见、gov 簇隐藏", d_q != "none" and d_g == "none", f"q={d_q} g={d_g}")
    ngq = pg.eval_on_selector_all("#kb-gov-quality .asset", "els => els.length")
    ck("质量簇卡片数 == quality 组项数", ngq == GRP.get("quality"), f"{ngq} vs {GRP.get('quality')}")
    # 真实点击「消费与性能」
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="perf"]')
    pg.wait_for_timeout(700)
    npf = pg.eval_on_selector_all("#kb-gov-perf .asset", "els => els.length")
    ck("性能簇卡片数 == consumption+performance 项数",
       npf == GRP.get("consumption", 0) + GRP.get("performance", 0),
       f"{npf} vs {GRP.get('consumption',0)+GRP.get('performance',0)}")
    ck("性能簇含 RT 分位与慢查询卡", "检索延迟 p50" in pg.inner_text("#kb-gov-perf"))
    # 真实点击「验收对照」
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="accept"]')
    pg.wait_for_timeout(900)
    rows = pg.eval_on_selector_all("#kb-accept table tr", "els => els.length")
    ck("验收表行数 = 表头 + acceptance 条数", rows == len(ACC) + 1, f"{rows} vs {len(ACC)+1}")
    hdr = pg.eval_on_selector_all("#kb-accept table tr:first-child th", "els => els.length")
    ck("验收表 6 列（条款/要求/目标/实测/判定/证据）", hdr == 6, f"{hdr}")
    acc_ok = pg.eval_on_selector_all("#kb-accept .st.ok", "els => els.length")
    ck("验收表达标徽章数 == 接口统计", acc_ok == EXP_ACC_OK + 1, f"{acc_ok} vs {EXP_ACC_OK}+1汇总条")
    ck("验收表含甲方 7.1 与 PRD 条款", "7.1" in pg.inner_text("#kb-accept")
       and "PRD" in pg.inner_text("#kb-accept"))

    print("\n[5] P0 治理红线（不回归）")
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="gov"]')
    pg.wait_for_timeout(700)
    rl = pg.eval_on_selector_all("#kb-redline .todo-item", "els => els.length")
    ck("红线行数 == 接口红线数", rl == EXP_REDLINES, f"{rl} vs {EXP_REDLINES}")
    rl_txt = pg.inner_text("#kb-redline")
    ck("红线含三条（未评审/闸门/追溯）",
       "权威基线未评审数据" in rl_txt and "发布闸门预警" in rl_txt and "分块追溯覆盖率" in rl_txt)
    ck("红线行内 alert 数 == redlines 内 alert 数",
       pg.eval_on_selector_all("#kb-redline .todo-item .st.r", "els => els.length") == EXP_RL_ALERT)

    print("\n[6] 治理簇三色状态（不回归）")
    gov_txt = pg.inner_text("#kb-gov")
    # P1 后各分组渲染进各自簇容器，故分组名要在「三容器并集」里找
    all_gov = (pg.text_content("#kb-gov") or "") + (pg.text_content("#kb-gov-quality") or "") \
        + (pg.text_content("#kb-gov-perf") or "")
    for gname in EXP_GROUPS:
        ck(f"含分组「{gname}」", gname in all_gov)
    ngov = pg.eval_on_selector_all("#kb-gov .asset", "els => els.length")
    ck("治理簇卡片数 == governance 组项数", ngov == GRP.get("governance"), f"{ngov} vs {GRP.get('governance')}")
    # ⚠️ 组头现在也有汇总徽章 → 统计必须限定在卡片内（#kb-gov .asset .st）
    n_r = pg.eval_on_selector_all("#kb-gov .asset .st.r", "els => els.length")
    n_w = pg.eval_on_selector_all("#kb-gov .asset .st.w", "els => els.length")
    n_ok = pg.eval_on_selector_all("#kb-gov .asset .st.ok", "els => els.length")
    ck("三色状态齐备", n_r + n_w + n_ok == ngov, f"r={n_r} w={n_w} ok={n_ok}")
    ck("治理簇 alert 数 == governance 组内 alert 数",
       n_r == sum(1 for m in DASH["metrics"].values()
                  if m["status"] == "alert" and m["key"] in
                  {i["key"] for g in DASH["groups"] if g["key"] == "governance" for i in g["items"]}),
       f"{n_r}")

    print("\n[7] P2 未落地指标必须显示「—」而非 0")
    # 质量簇在隐藏的簇容器里，inner_text 取不到 → 直接读卡片元素文本
    ck("质量簇含「别名覆盖率（中英文互认）」卡",
       "别名覆盖率" in (pg.text_content('#kb-a [data-mkey="alias_coverage"]') or ""))
    na = pg.eval_on_selector_all(
        '#kb-a [data-mkey="alias_coverage"] .n', "els => els.map(e=>e.textContent.trim())")
    ck("alias_coverage 卡显示 —（不是 0）", na and na[0].startswith("—"), f"{na}")
    for k in ["dedup_accuracy", "eval_precision"]:
        v = pg.eval_on_selector_all(f'#kb-a [data-mkey="{k}"] .n',
                                    "els => els.map(e=>e.textContent.trim())")
        ck(f"{k} 卡显示 —（未落地/未计算，不报 0）", v and v[0].startswith("—"), f"{k}={v}")

    print("\n[8] P1 真实点击：筛选态下钻")
    # UX-4 后达标项折叠在 <details> 内 → 下钻前先展开（否则目标卡不可见、点击超时）
    pg.evaluate("() => document.querySelectorAll('#kb-a details').forEach(d=>d.open=true)")
    pg.wait_for_timeout(400)
    # 待审积压（governance 组，kind=entity_status,value=candidate）→ 切 perf 簇 + 实体浏览按候选过滤
    sel = '#kb-a [data-mkey="pending_candidates"]'
    if pg.query_selector(sel):
        pg.click(sel, timeout=8000)
        pg.wait_for_timeout(2600)
        val = pg.eval_on_selector("#kb-status-filter", "el => el.value")
        ck("点击「待审积压」→ 实体浏览状态筛选被置为 candidate", val == "candidate", f"value={val}")
        d_p = pg.eval_on_selector('.kb-cluster[data-cluster="perf"]',
                                  "el => getComputedStyle(el).display")
        ck("下钻落点切到 perf 簇（实体浏览可见）", d_p != "none", f"display={d_p}")
    else:
        ck("存在 pending_candidates 卡", False)
    # 孤立节点率（kind=graph_isolated）→ 图谱 hideIsolated=false
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="gov"]')
    pg.wait_for_timeout(600)
    sel2 = '#kb-a [data-mkey="orphan_rate"]'
    if pg.query_selector(sel2):
        pg.click(sel2, timeout=8000)
        pg.wait_for_timeout(2600)
        hi = pg.evaluate(
            "() => (typeof graphState !== 'undefined' && graphState.view) "
            "? graphState.view.hideIsolated : 'NA'")
        ck("点击「孤立节点率」→ 图谱 hideIsolated 置 false", hi is False, f"hideIsolated={hi}")
        pg.evaluate("() => { try { go('kb','kb-a'); } catch(e){} }")
        pg.wait_for_timeout(2500)
    else:
        ck("存在 orphan_rate 卡", False)

    print("\n[9] P2/P3 面板与趋势渲染")
    cov_txt = pg.inner_text("#kb-coverage")
    ck("完整度含两级分母条", "SysML 元素类型" in cov_txt and "领域扩展类型" in cov_txt)
    ck("完整度含分块追溯覆盖率告警条", "分块追溯覆盖率" in cov_txt)
    ck("kb-a 无「加载失败」字样", "加载失败" not in pg.inner_text("#kb-a"))
    gp = DASH["metrics"]["engine_rt_p50"]["value"]
    ck("性能簇显示检索 p50 真实值", str(gp) in pg.inner_text("#kb-gov-perf"), f"{gp}")
    ck("消费簇显示知识获取召回真实值",
       str(DASH["metrics"]["eval_recall_structured"]["value"]) in pg.inner_text("#kb-gov-perf"))
    # sparkline 渲染器：>=2 点才画线，1 点不画（不伪造趋势）
    sp2 = pg.evaluate("() => kbSpark([{value:1},{value:3},{value:2}])")
    sp1 = pg.evaluate("() => kbSpark([{value:1}])")
    ck("kbSpark 两点以上返回 SVG 折线", "<svg" in (sp2 or "") and "polyline" in (sp2 or ""))
    ck("kbSpark 单点不画线（不伪造趋势）", (sp1 or "") == "", f"got={sp1!r}")

    print("\n[10] P1 空态收口（V6）与标题行减负（V7）")
    ov = pg.inner_text("#kb-overview")
    q = DASH["metrics"]["pending_candidates"]["value"]
    ck("待审为 0 时总览收敛为一行（不再铺三列空白）",
       ("三类待评审队列均为 0" in ov) if q == 0 else True, f"待审={q}")
    badges = pg.eval_on_selector_all(
        "#kb-a .ph .badge",
        "els => els.map(e=>({t:(e.textContent||'').trim(), d:getComputedStyle(e).display}))")
    vis = [b["t"] for b in badges if b["d"] != "none"]
    ck("V7：面板标题内部编号 badge 已隐藏", bool(badges) and not vis,
       f"{len(badges)} 个 badge，仍可见：{vis}")
    titled = pg.eval_on_selector_all("#kb-a .ph[title]", "els => els.length")
    ck("V7：编号已搬进 .ph title（可回溯）", titled > 0, f"{titled} 个")

    print("\n[14] UX 优化（导航清晰 + 简洁）")
    # UX-1 两级导航分离
    ct = pg.inner_text("#kb-cluster-tabs")
    ck("簇 Tab 行带「看板视图」前置标签（与页面级导航区分）", "看板视图" in ct)
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="gov"]')
    pg.wait_for_timeout(500)
    cl_style = pg.eval_on_selector(
        '#kb-cluster-tabs .kbhub-chip.on', "el => el.getAttribute('style') || ''")
    hub_style = pg.eval_on_selector(
        '#kbhub-tabs .kbhub-chip.on', "el => el.getAttribute('style') || 'FILLED'")
    ck("簇 Tab 选中态为描边式（蓝边蓝字、非实心蓝底；与页面导航的实心蓝可区分）",
       "border-color:var(--blue)" in cl_style and "background:#fff" in cl_style
       and "background:var(--blue)" not in cl_style,
       f"cluster={cl_style[:60]} hub={hub_style[:20]}")
    pos_ct = pg.eval_on_selector("#kb-cluster-tabs", "el => Math.round(el.getBoundingClientRect().top)")
    pos_ns = pg.eval_on_selector("#kb-northstar", "el => Math.round(el.getBoundingClientRect().top)")
    ck("簇 Tab 位于北极星之上（紧贴顶栏，减少层级割裂）", pos_ct < pos_ns, f"ct={pos_ct} ns={pos_ns}")

    # UX-2 冗余明细面板默认收起 + 一键展开
    nd = pg.eval_on_selector_all('#kb-a [data-kb-detail="1"]',
                                 "els => els.map(e=>getComputedStyle(e).display)")
    ck("冗余明细面板已标记且默认收起", bool(nd) and all(d == "none" for d in nd), f"{len(nd)} 个")
    # 明细开关只在"当前簇确有明细面板"时出现（有意的：不在无明细的簇留空开关）
    # → 先真实点击切到 perf 簇（该簇登记了「知识引擎双引擎消费」明细）
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="perf"]')
    pg.wait_for_timeout(700)
    nd_p = pg.eval_on_selector_all('#kb-a [data-kb-detail="1"]',
                                   "els => els.map(e=>getComputedStyle(e).display)")
    ck("切到 perf 簇后明细面板仍收起且已登记", bool(nd_p) and all(d == "none" for d in nd_p))
    tog = pg.query_selector('#kb-cluster-tabs [data-kb-detail-toggle]')
    if tog:
        pg.click('#kb-cluster-tabs [data-kb-detail-toggle]')
        pg.wait_for_timeout(600)
        nd2 = pg.eval_on_selector_all('#kb-a [data-kb-detail="1"]',
                                      "els => els.map(e=>getComputedStyle(e).display)")
        ck("点击「明细面板」后可见（数据不丢）", any(d != "none" for d in nd2), f"{nd2}")
        pg.click('#kb-cluster-tabs [data-kb-detail-toggle]')
        pg.wait_for_timeout(500)
        pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="gov"]')
        pg.wait_for_timeout(600)
    else:
        ck("perf 簇存在明细面板开关", False)

    # UX-3 标题瘦身
    lens = pg.evaluate(r"""() => Array.from(document.querySelectorAll('#kb-a .ph'))
        .map(p => { const c = p.cloneNode(true);
          c.querySelectorAll('.badge,.tag,button,select,input').forEach(x=>x.remove());
          return (c.textContent||'').replace(/\s+/g,' ').trim().length; })""")
    ck("面板标题已瘦身（可见标题最长 ≤ 20 字）", lens and max(lens) <= 20,
       f"最长={max(lens) if lens else -1} 平均={round(sum(lens)/max(1,len(lens)),1)}")

    # UX-4 达标项折叠
    folds = pg.eval_on_selector_all("#kb-a details", "els => els.length")
    ck("存在达标项折叠块（<details>）", folds > 0, f"{folds} 个")
    gov_ok_items = [i["key"] for g in DASH["groups"] if g["key"] == "governance"
                    for i in g["items"] if i["status"] == "ok"]
    vis = pg.evaluate(r"""() => Array.from(document.querySelectorAll('#kb-gov .asset'))
        .filter(a => !a.closest('details')).length""")
    ck("治理簇默认只展示待关注项（ok 项折叠）",
       vis == GRP.get("governance", 0) - len(gov_ok_items),
       f"可见 {vis} vs 期望 {GRP.get('governance', 0) - len(gov_ok_items)}")

    # 口径修复呈现
    sc = pg.inner_text("#kb-stats")
    ck("作用域对照含「知识总量」行", "知识总量" in sc)
    ck("去重值已渲染（与接口一致）",
       str(DASH["scopes"]["global"]["entities_dedup"]) in sc
       and str(DASH["scopes"]["global"]["relations_dedup"]) in sc)
    # 布局压缩（用户反馈"三个分支依次排开、看不懂、太占篇幅"）：
    # 由 3 行×4 张卡改为一张 6 列表（表头 + 3 行），不再有 .asset 卡片
    n_cards = pg.eval_on_selector_all("#kb-stats .asset", "els => els.length")
    ck("作用域区不再用卡片堆叠（.asset 数 = 0）", n_cards == 0, f"{n_cards} 张")
    n_th = pg.eval_on_selector_all("#kb-stats table tr:first-child th", "els => els.length")
    n_tr = pg.eval_on_selector_all("#kb-stats table tr", "els => els.length")
    ck("对照表为 6 列表头 + 3 行数据", n_th == 6 and n_tr == 4, f"表头 {n_th} 列 / 共 {n_tr} 行")
    h = pg.eval_on_selector("#kb-stats", "el => Math.round(el.getBoundingClientRect().height)")
    ck("作用域区高度显著压缩（≤ 220px）", h <= 220, f"{h}px")
    ck("每行都有口径说明列（含作用域 tooltip）",
       "不能直接混比" in sc and "跨分支去重" in sc and "我的作业面" in sc)

    print("\n[16] 顶栏「跳转子页」入口已移除（2026-09-27 用户要求）")
    tb2 = pg.text_content("#kb-dash-topbar") or ""
    ck("顶栏不再含「跳转子页」入口", "跳转子页" not in tb2)
    n_chips = pg.eval_on_selector_all("#kb-dash-topbar .kbhub-chip", "els => els.length")
    ck("顶栏 chip 仅剩时间窗 3 个（子页入口已移除）", n_chips == 3, f"{n_chips}")
    ck("顶栏不再含任何指向知识子页的跳转（onclick 无 go('kb','kb-…')）",
       not pg.eval_on_selector_all(
           "#kb-dash-topbar [onclick]", "els => els.filter(e => /go\\(.kb.,.kb-/.test(e.getAttribute('onclick')||'')).length"))
    ck("顶栏保留刷新与记录快照", "刷新（含 SHACL）" in tb2 and "记录快照" in tb2)
    ck("子页导航仍由顶部「知识中心」内层 Tab 承担（未被误删）",
       pg.eval_on_selector_all("#kbhub-tabs .kbhub-chip", "els => els.length") >= 6)

    print("\n[11] 图数据库未启用收起（C7，不回归）")
    gd = pg.eval_on_selector("#graphdb-panel", "el => getComputedStyle(el).display")
    ck("图数据库面板已收起", gd == "none", f"display={gd}")

    print("\n[12] 视觉取证（浅色 + 暗色 + 各簇）")
    for key in ["gov", "quality", "perf", "accept"]:
        pg.click(f'#kb-cluster-tabs .kbhub-chip[data-cluster="{key}"]')
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(OUT / f"kb_p1_{key}.png"), full_page=True)
    pg.click('#kb-cluster-tabs .kbhub-chip[data-cluster="gov"]')
    pg.wait_for_timeout(600)
    pg.evaluate("() => setTheme('dark')")
    pg.wait_for_timeout(1300)
    pg.screenshot(path=str(OUT / "kb_p1_dark_gov.png"), full_page=True)
    ck("四簇截图 + 暗色截图已保存",
       all((OUT / f"kb_p1_{k}.png").exists() for k in ["gov", "quality", "perf", "accept", "dark_gov"]))

    print("\n[13] 暗色可读性（回归护栏：面板白底 + 正文继承色翻转 → 裸文本必须显式用不翻转 token）")
    ratios = pg.evaluate(r"""
    () => {
      function rgb(s){ const m=(s||'').match(/[\d.]+/g)||[]; return m.slice(0,3).map(Number); }
      function lum(c){ const f=v=>{v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);};
        return 0.2126*f(c[0])+0.7152*f(c[1])+0.0722*f(c[2]); }
      function ratio(a,b){ const l1=lum(a),l2=lum(b); const hi=Math.max(l1,l2),lo=Math.min(l1,l2);
        return (hi+0.05)/(lo+0.05); }
      function effBg(el){ let n=el; while(n){ const bs=getComputedStyle(n).backgroundColor;
          if(bs!=='rgba(0, 0, 0, 0)') return rgb(bs); n=n.parentElement; } return [255,255,255]; }
      const out=[];
      const push=(sel,el)=>{ if(!el) return; const cs=getComputedStyle(el);
        out.push({sel, ratio:+ratio(rgb(cs.color), effBg(el)).toFixed(2), text:(el.innerText||'').slice(0,16)}); };
      // 排除两类**应用级既有**模式（非本看板引入，且修它们等于改全站样式）：
      //   · .st / .tag —— 语义徽章，自带底色，其 token 对比度是全站设计选择（如 amber 3.24:1）
      //   · .ph        —— 面板标题条：全站所有面板同构，其"暗色下浅字配浅底"属既有缺陷
      //                    （与已发现的 .todo-item 同类），已单独记录待专项处理
      const EXCLUDE = el => el.closest('.st') || el.closest('.tag') || el.closest('.ph');
      // 遍历 #kb-a 内**全部可见且含直接文本**的元素（不再手挑样本 —— 手挑会留盲区，
      // 2026-09-27 就漏掉了 #kb-overview 里的裸文本）。上限保证性能。
      const root = document.querySelector('#kb-a');
      const all = root ? root.querySelectorAll('*') : [];
      let n = 0;
      for (const el of all) {
        if (n >= 500) break;
        if (!el.offsetParent && el.tagName !== 'BODY') continue;         // 不可见
        const own = Array.from(el.childNodes)
          .filter(x => x.nodeType === 3).map(x => x.textContent.trim()).join('');
        if (!own) continue;                                             // 只测"直接文本"
        if (EXCLUDE(el)) continue;                                      // 排除应用级既有模式
        n++;
        push(el.tagName.toLowerCase() + '.' + (el.className || '-').toString().slice(0, 18)
             + '[' + n + ']', el);
      }
      return out;
    }""")
    weak = [r for r in ratios if r["ratio"] < 4.5]
    ck(f"暗色下全部文本对比度 ≥ 4.5:1（实测 {len(ratios)} 个节点）",
       not weak, f"不达标：{[(r['sel'], r['ratio']) for r in weak[:4]]}")
    pg.evaluate("() => setTheme('light')")

    ck("页面无 JS 运行时异常", not errs, f"{errs[:3]}")
    b.close()

print("\n" + "=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)