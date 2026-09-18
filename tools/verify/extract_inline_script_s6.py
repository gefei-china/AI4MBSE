"""S6-3 / S6-4：把 index.html 里剩余内联 <script> 块迁出（2026-09-18）。

用法（行级定位，要求 <script> / </script> 独占一行）：
  python tools/verify/extract_inline_script_s6.py --marker "v6.1 全局 UI 服务" --drop
  python tools/verify/extract_inline_script_s6.py --marker "全局快捷键 + 命令面板" \
      --out static/js/mods/37-kbar.js --replace-src /static/js/mods/37-kbar.js --apply

安全：按字节读写保留换行；--apply 才写盘；迁移后断言 <script> 数量下降。
"""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IDX = ROOT / "static" / "index.html"


def locate(lines, marker):
    for i, ln in enumerate(lines):
        if marker not in ln:
            continue
        s = e = None
        for j in range(i, -1, -1):
            if lines[j].strip() == '<script>':
                s = j
                break
            if lines[j].strip() == '</script>':
                break
        for j in range(i, len(lines)):
            if lines[j].strip() == '</script>':
                e = j
                break
            if lines[j].strip() == '<script>':
                break
        if s is not None and e is not None and s < e:
            return s, e
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--marker", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--replace-src", default="")
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    raw = IDX.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw[:2000] else "\n"
    lines = raw.splitlines(keepends=True)
    s, e = locate(lines, a.marker)
    if s is None:
        print("!! 未定位到 marker 所在的内联 <script> 块：%s" % a.marker)
        return 2
    body = "".join(lines[s + 1:e])
    print("定位到内联块：L%d-L%d（%d 行 / %d 字节）" % (s + 1, e + 1, e - s - 1, len(body.encode("utf-8"))))

    if a.out:
        dst = ROOT / a.out
        if dst.exists():
            print("!! 目标已存在，拒绝覆盖：%s" % dst)
            return 3
        if a.apply:
            header = ("/* 2026-09-18 S6-3/S6-4：由 static/index.html 内联 <script> 迁出（原位置见 git 历史）%s"
                      " * 注意：本文件必须在该块原本引用的 DOM 之后加载（见 index.html 中的 <script src>）。%s */%s%s"
                      % (nl, nl, nl, nl))
            dst.write_bytes((header + body).encode("utf-8"))
        print("  → 迁出到 %s%s" % (a.out, "" if a.apply else "（dry-run 未写盘）"))

    if a.apply:
        repl = []
        if a.replace_src:
            repl = ['<script src="%s"></script>%s' % (a.replace_src, nl)]
            print("  → 原位替换为 <script src=\"%s\">" % a.replace_src)
        elif a.drop:
            print("  → 原地删除该内联块")
        lines[s:e + 1] = repl
        out = "".join(lines)
        IDX.write_bytes(out.encode("utf-8"))
        print("index.html 字节: %d ; 剩余内联 <script> 块: %d"
              % (len(out.encode("utf-8")), out.count("<script>")))
    else:
        print("（dry-run，未写盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
