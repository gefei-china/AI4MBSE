"""S6-2：把 index.html 里剩余 3 段内联 <style> 迁到对应 css 文件（2026-09-18）。

映射（按内容特征识别，不依赖行号）：
  .tool-elapsed / .errface / .art-card  → static/css/chat.css   （消息流：工具耗时 / 错误面 / 产物卡）
  .ont-tnd / .ont-sec / .ont-chip ...    → static/css/kb.css     （本体工作台树与编辑区）
  .am-item:hover                         → static/css/app.css    （附件菜单）

安全：按**字节**读写，保留原换行风格；迁移后断言 index.html 中不再有 <style>。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IDX = ROOT / "static" / "index.html"
MAP = [
    (".tool-elapsed", ROOT / "static" / "css" / "chat.css", "chat.css"),
    (".ont-tnd", ROOT / "static" / "css" / "kb.css", "kb.css"),
    (".am-item:hover", ROOT / "static" / "css" / "app.css", "app.css"),
]


def main(apply=False):
    raw = IDX.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw[:2000] else "\n"
    lines = raw.splitlines(keepends=True)

    def find_block(marker):
        """行级定位：先找到含 marker 的行，再向上找最近的 '<style>'、向下找最近的 '</style>'。
        2026-09-18 修正：原用正则匹配 <style>...</style>，会把内联 JS 里字符串中的 <style>
        也当作块，导致删出"未闭合 <style>"（浏览器会把后续内容全当 CSS 吞掉）。
        行级定位要求开/闭标签**独占一行**，因此不会误伤字符串中的标签。
        """
        for i, ln in enumerate(lines):
            if marker not in ln:
                continue
            s = e = None
            for j in range(i, -1, -1):
                t = lines[j].strip()
                if t == '<style>':
                    s = j
                    break
                if t == '</style>':
                    break
            for j in range(i, len(lines)):
                t = lines[j].strip()
                if t == '</style>':
                    e = j
                    break
                if t == '<style>':
                    break
            if s is not None and e is not None and s < e:
                return s, e
        return None, None

    # 按行号倒序处理，避免删除后索引漂移
    targets = []
    for marker, dst, label in MAP:
        s, e = find_block(marker)
        if s is None:
            print("!! 未定位到 %s 所在的内联块" % marker)
            continue
        targets.append((s, e, marker, dst, label))
    targets.sort(key=lambda x: -x[0])

    for s, e, marker, dst, label in targets:
        body = "".join(lines[s + 1:e])
        rules = nl.join(ln.strip() for ln in body.strip().splitlines() if ln.strip())
        header = "%s%s/* ── 2026-09-18 S6-2：由 index.html 内联 <style> 迁入（原位置见 git 历史） ── */%s%s" % (nl, nl, nl, nl)
        dst_raw = dst.read_bytes().decode("utf-8")
        if marker in dst_raw:
            print("  = %s 已含 %s —— 视为已迁移，仅删除内联块（L%d-L%d）" % (label, marker, s + 1, e + 1))
        else:
            if apply:
                dst.write_bytes((dst_raw.rstrip() + nl + header + rules + nl).encode("utf-8"))
            print("  → 迁入 %-10s %d 行%s（L%d-L%d）" % (label, len(body.splitlines()), "" if apply else "（dry-run 未写盘）", s + 1, e + 1))
        if apply:
            end = e + 1
            if end < len(lines) and lines[end].strip() == "":   # 连带吃掉紧随的空行
                end += 1
            del lines[s:end]

    if apply:
        out = "".join(lines)
        IDX.write_bytes(out.encode("utf-8"))
        print("index.html 剩余 <style>: %d / </style>: %d（均应为 0）"
              % (out.count("<style>"), out.count("</style>")))
        print("index.html 字节: %d" % len(out.encode("utf-8")))
    else:
        print("（dry-run，未写盘；加 --apply 生效）")
    print("处理段数: %d" % len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
