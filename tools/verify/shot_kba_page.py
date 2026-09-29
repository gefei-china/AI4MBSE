# -*- coding: utf-8 -*-
"""抓取知识看板（kb-a）当前渲染实况，作为知识看板优化方案的现状证据。"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
URL = "http://127.0.0.1:8000/"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1680, "height": 1050})
    pg.goto(URL, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(1500)
    # 进入知识中心 → 数据看板
    pg.evaluate("() => { try { go('kb','kb-a'); } catch(e){} }")
    pg.wait_for_timeout(3500)
    # 逐面板取证
    panels = ["kb-stats", "kb-overview", "kb-lifecycle", "kb-coverage",
              "engine-stats", "graphdb-stats"]
    for pid in panels:
        el = pg.query_selector("#" + pid)
        if not el:
            print(f"[MISS] #{pid} 不存在")
            continue
        txt = (el.inner_text() or "").replace("\n", " | ")
        box = el.bounding_box()
        print(f"[{pid}] h={round(box['height']) if box else -1} :: {txt[:400]}")
    pg.screenshot(path=str(OUT / "kb_a_full.png"), full_page=True)
    print("saved:", OUT / "kb_a_full.png")
    b.close()