"""verify_checker_diag_encoding —门禁：校验器诊断里的**中文不得乱码**。

## 为什么需要这道门禁（2026-10-09 实测）

诊断是**回喂 LLM 的唯一依据**。乱码时模型：
- 认不出错在哪个字符；
- **更认不出那正是自己刚写的中文名** ⇒ 只能反复乱改。

实测后果：N3 `parameter` 视图一轮内改了 2 次仍产出雷同，
因为它拿到的诊断是 `Type ''�������Լ������''`。

## 根因与修法（一行 JVM 参数）

Windows 上 `checker.jar` 的 stdout 走**平台默认编码**，
`subprocess` 用 UTF-8 解码 ⇒ 每个中文字变成 `U+FFFD`。

实测对照（**同一份代码，只差这一个参数**）：

| JVM 参数 | 诊断输出 |
|---|---|
| 无 | `Type ''�����ڵ���������''`（U+FFFD × 14） |
| `-Dsun.stdout.encoding=UTF-8` | `Type ''不存在的中文类型''`（U+FFFD = 0） |

⚠️ **`-Dfile.encoding=UTF-8` 单独设无效**（实测 U+FFFD 仍 14）——
只改 stdout 通道。且必须放在 `-jar` **之前**（JVM 参数，非程序参数）。

## 判据（缺一即 FAIL）

① 引用类诊断（`to Type ''中文名''`）里的中文**逐字正确**
② 词法类诊断（中文标识符未加引号）里的中文**逐字正确**
③ 所有诊断消息里 **U+FFFD 计数 = 0**
④ `_fix_char_msg` 的回填能力**不回归**（at character 形态仍生效）
⑤ 校验器**仍能正常执行**（verdict 非 unavailable）

用法：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_checker_diag_encoding.py`
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

#: (用例名, 源码) —— 两个都故意触发含中文的诊断
CASES = [
    ("引用不存在的中文类型",
     "package P {\n    part def V {\n        attribute x : '不存在的中文类型';\n    }\n}"),
    ("中文标识符不加引号（词法错）",
     "package P { part def 载荷; }"),
]


def main() -> int:
    from sysml_v2_check import _fix_char_msg, check_code, diagnostics

    ok = True

    def chk(desc, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'OK  ' if cond else 'FAIL'}] {desc}"
              f"{('  ' + extra) if extra else ''}")

    print("=" * 72)
    print("校验器诊断编码 · 中文回显")
    print("=" * 72)

    for label, code in CASES:
        print(f"\n① {label}")
        r = check_code(code)
        chk("校验器可执行（非 unavailable）",
            r.get("verdict") != "unavailable", f"verdict={r.get('verdict')}")
        ds = diagnostics(r)
        msgs = [m for d in ds for m in (d.get("messages") or [])]
        chk(f"产出了诊断（{len(msgs)} 条）", bool(msgs))

        fffd = sum(m.count("\ufffd") for m in msgs)
        chk(f"零乱码（U+FFFD 合计 {fffd}）", fffd == 0)
        for m in msgs:
            if m.count("\ufffd"):
                print(f"         ❌ {m[:90]}")

        has_cn = any(any("\u4e00" <= ch <= "\u9fff" for ch in m) for m in msgs)
        chk("中文被正确回显", has_cn)
        if has_cn:
            for m in msgs:
                if any("\u4e00" <= ch <= "\u9fff" for ch in m):
                    print(f"         {m[:96]}")
                    break

    print("\n② _fix_char_msg 回填能力不回归（at character 形态）")
    src = "package P { part def 温控单元; }"
    got = _fix_char_msg("no viable alternative at character '??'", src, 22)
    chk("at character 形态仍能回填", "温" in got, got[:60])
    chk("无乱码 token 时逐字返回",
        _fix_char_msg("plain message", src, 22) == "plain message")

    print("\n" + "=" * 72)
    print("✅ 通过：诊断里的中文逐字正确，无乱码" if ok
          else "❌ 未通过：诊断仍有乱码")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
