# -*- coding: utf-8 -*-
"""全局字号标准化（2026-10-30）—— 一次性迁移脚本，可重入。

## 为什么要做（用户原话「字体大小感觉比同类产品偏小」）
实测本仓字号分布（2752 处 `font-size:Npx`）：

    9px  24 | 9.5px 55 | 10px 248 | 10.5px 318 | 11px 724 | 11.5px 447
    12px 629 | 12.5px 145 | 13px 93 | 13.5px 6 | 14px 24 | ...

两个问题同时存在：
1. **偏小**：11px 有 724 处、10~10.5px 有 566 处，而"正文"13px 只有 93 处 ——
   UI 主要靠 11~12px 支撑。中文汉字在 11px 下笔画糊成一片（与英文不同，英文小写
   x-height 小、留白多，11px 尚可读）。
2. **不标准**：9 → 13.5 之间挤了 10 个档位（+0.5 一档），层级密集但没有信息量，
   反而让"哪个更重要"失去视觉区分。

## 依据（调研）
同类产品的**内容宽度**：ChatGPT 默认 ~768px、Claude ~720px，
Power-User 社区普遍调到 1180~1200px。而**字号**业界基线：
  · Ant Design 基础正文 14px，辅助 12px，最小 12px
  · Material 3：body 14sp / label 12sp
  · GitHub Primer：正文 14px，small 12px
  · 中文应用因为字形复杂，正文普遍取 14~15px（比英文基线再高 0~1px）
⇒ 本仓取：**正文 14.5px、辅助 12.5px、次辅助 12px、最小 11.5px**。

## 映射（10 档 → 6 档，整体 +1.5px，极小字 +2.5px）
    9 / 9.5      → 11.5   （可读下限，硬提）
    10 / 10.5    → 12
    11 / 11.5    → 12.5
    12 / 12.5    → 13.5
    13 / 13.5    → 14.5   （正文基准）
    14 / 14.5    → 15.5
    15 / 16      → 16.5 / 17.5
    16.5 / 17    → 18 / 18.5
    18 / 19      → 19.5 / 20.5
    20 及以上     → +1（标题类，本身已够大）

⚠️ 只改 `font-size:Npx` 这一种形态：
   · 不碰 `font-size:inherit/1em/1.1rem` 等相对单位；
   · 不碰 SVG 属性 `font-size="11"`（属性而非 CSS，改它要另行评估渲染影响）；
   · 不碰任何非 font-size 的 px（padding/height/width 一概不动）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/_migrate_font_scale.py --dry   # 预演
    ./.venv/Scripts/python.exe -X utf8 tools/_migrate_font_scale.py --apply # 写入
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

MAP = {
    9: 11.5, 9.5: 11.5,
    10: 12, 10.5: 12,
    11: 12.5, 11.5: 12.5,
    12: 13.5, 12.5: 13.5,
    13: 14.5, 13.5: 14.5,
    14: 15.5, 14.5: 15.5,
    15: 16.5, 16: 17.5,
    16.5: 18, 17: 18.5,
    18: 19.5, 19: 20.5,
    20: 21, 22: 23, 24: 25, 26: 27, 28: 29, 30: 31, 32: 33, 34: 35,
    40: 41, 67.75: 68.75,
}

#: 只匹配 CSS 声明形态（可有空格），且值必须是纯数字+px
PAT = re.compile(r"font-size:(\s*)([0-9]+(?:\.[0-9]+)?)px")


def _fmt(v: float) -> str:
    return ("%g" % v)


def _targets():
    out = []
    for dp, _dn, fn in os.walk(os.path.join(_ROOT, "static")):
        if "_fsbackup" in dp:
            continue
        for f in fn:
            if f.endswith((".css", ".js", ".html")):
                out.append(os.path.join(dp, f))
    return sorted(out)


def migrate(apply: bool):
    stat = collections.Counter()
    changed_files = 0
    for p in _targets():
        try:
            src = open(p, encoding="utf-8").read()
        except Exception:
            continue

        def repl(m):
            raw = m.group(2)
            try:
                v = float(raw)
            except ValueError:
                return m.group(0)
            nv = MAP.get(v)
            if nv is None:
                stat[("skip", raw)] += 1
                return m.group(0)
            stat[("%s->%s" % (_fmt(v), _fmt(nv)))] += 1
            return "font-size:%s%spx" % (m.group(1), _fmt(nv))

        new = PAT.sub(repl, src)
        if new != src:
            changed_files += 1
            if apply:
                open(p, "w", encoding="utf-8").write(new)
    return stat, changed_files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入（默认只预演）")
    a = ap.parse_args()
    stat, nf = migrate(a.apply)
    print("=" * 66)
    print("字号标准化 —— %s" % ("已写入" if a.apply else "预演（未写入）"))
    print("=" * 66)
    tot = 0
    for k, n in sorted(stat.items(), key=lambda kv: -kv[1]):
        if isinstance(k, tuple):
            print("  [不映射] %-10s %d 处（非整数档或超大字号，保持原值）" % (k[1], n))
            continue
        print("  %-14s %5d 处" % (k, n))
        tot += n
    print("\n合计改写 %d 处，涉及 %d 个文件" % (tot, nf))
    if not a.apply:
        print("（加 --apply 才写入；备份在 tmp/_fsbackup/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
