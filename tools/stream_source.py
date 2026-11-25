# -*- coding: utf-8 -*-
"""stream.py 源码视图 —— 105 个源码断言门禁的**统一读取入口**。

## 为什么需要这一层（2026-11-25，P1 `stream.py` 拆分第一步）

`agent/pipeline_parts/stream.py` 8284 行 / 630 KB，只有 6 个方法，其中
`_stream_orchestrated_flow` 4020 行、`execute_stream` 3488 行把
「SSE 协议 / 业务编排 / 断点续传」交织在同一函数作用域。

要拆它，绕不开一个硬事实：**86 个门禁文件 / 132 处调用直接读取该文件的源码文本做断言**
（`open(...)` / `_read(...)` / `read_src(...)`）。方法一旦搬走，这些断言要么失效、
要么**静默变成假绿** —— 后者比门禁变红更危险。

⇒ 先把读取收口到本模块。门禁不再关心源码物理上在几个文件里：

```python
from tools.stream_source import read_stream_source, module_for

src = read_stream_source()          # 与拆分前的 stream.py **逐字节相同**
path = module_for("execute_stream") # 锚点所在文件（用于变异写回）
```

**执行顺序不可颠倒**（见 `docs/architecture/stream-py拆分规避契约-20261125.md`）：
1. 现在：所有门禁改用本视图，`read_stream_source()` 直接读单一 `stream.py`；
   由 `verify_stream_source_view.py` 做逐字节等价 + 变异自证。
2. 将来：按 mixin 拆成多个文件，只改本模块的 `SOURCE_PARTS` 顺序与
   `ANCHOR_INDEX`，**门禁一行都不用改**。

## 口径校正（重要）

早期估算是「105 个门禁读源码」，那是用「提到 `stream.py` 且文件里任意位置有
`_read(`」筛出来的**假高估**（读别的文件但提到 stream.py 也被算进去）。
按「读取调用的参数确实指向 stream.py」精确统计，实际是 **86 文件 / 132 处**
（2026-11-25 复核口径：含经门禁自定义 `def read(rel)` 辅助函数、`Path(...).read_text()`
 与 `sp = os.path.join(...); open(sp)` 变量间接三种形态 —— 前两版脚本各漏过一种）。
本模块与门禁 `verify_stream_source_view.py` 一律以精确口径为准。
"""
import hashlib
import os
import re
from functools import lru_cache

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_REL = "agent/pipeline_parts/stream.py"

# ── 拆分后的文件顺序（当前尚未拆分：只有一项）────────────────────────────
# 将来按契约拆成 3 个 mixin 时，在这里追加路径即可；顺序必须稳定，
# 否则 `read_stream_source()` 的字节内容会变，门禁的相对位置断言会失效。
SOURCE_PARTS = (DEFAULT_REL,)

# 锚点 → 所在文件。拆分后用于把变异写回正确的文件；当前全部落回单文件。
ANCHOR_INDEX = {}


def _read_file(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read()


def _stamp(parts):
    """分片的「版本戳」= (路径, mtime_ns, size)。

    为什么缓存键必须带文件戳（2026-11-25 实测教训）：
      首版是裸 `lru_cache`，源码被外部改写后视图仍返回旧字节。对**只读门禁**无害，
      但对**变异自证门禁**是致命的 —— 它改文件 → 视图没变 → 变异"看起来没生效" →
      「变异未被抓住」被判成绿的（静默失绿），比门禁变红危险得多。
      带上 mtime/size 后：改文件 ⇒ 戳变 ⇒ 自动重读，**不依赖调用方记得 invalidate**。
      `invalidate_cache()` 保留（用于戳未变但内容被强制刷新的极端场景）。
    """
    out = []
    for rel in parts:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        try:
            st = os.stat(p)
            out.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((rel, 0, -1))
    return tuple(out)


@lru_cache(maxsize=8)
def _parts_bytes(stamp):
    return tuple(_read_file(rel) for rel, _m, _s in stamp)


def read_stream_source_bytes() -> bytes:
    """拼接后的源码字节（视图的唯一真源）。"""
    return b"".join(_parts_bytes(_stamp(SOURCE_PARTS)))


def read_stream_source() -> str:
    """拼接后的源码文本，语义与拆分前的 `open(stream.py, encoding='utf-8').read()` 完全一致。

    ⚠️ 必须做 universal-newline 归一（`\\r\\n` → `\\n`）：Python 的文本模式 open 默认
       会转换换行，而 58 个门禁的断言里写死了 `\\n`（如 `if not dry_run:\\n  ...`）。
       若这里返回带 `\\r\\n` 的原文，门禁会**大面积假红**（实测 31 个失败）。
       字节级等价另由 `read_stream_source_bytes()` 提供，两者口径不可混用。
    """
    return read_stream_source_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def source_sha256() -> str:
    return hashlib.sha256(read_stream_source_bytes()).hexdigest()


def module_for(anchor: str) -> str:
    """给定断言锚点（函数名 / 特征串），返回其所在的**相对路径**。

    拆分前恒为 `DEFAULT_REL`；拆分后由 `ANCHOR_INDEX` 映射。
    找不到锚点时返回默认路径（保持旧行为，不抛异常打断门禁）。
    """
    hit = ANCHOR_INDEX.get(anchor)
    if hit:
        return hit
    for rel in SOURCE_PARTS:
        try:
            if anchor.encode("utf-8") in _read_file(rel):
                return rel
        except OSError:
            continue
    return DEFAULT_REL


def invalidate_cache() -> None:
    """强制丢弃缓存（源码被写回后调用）。

    ⚠️ 正常情况下**不需要调用**：缓存键含 (mtime_ns, size)，改文件会自动失效。
    保留它是为了「戳未变但内容被改写」的极端场景（如 mtime 被显式重置）。
    """
    _parts_bytes.cache_clear()


def main():
    print("stream 源码视图：%d 个分片，%d 字节，sha256=%s"
          % (len(SOURCE_PARTS), len(read_stream_source_bytes()), source_sha256()))
    for rel in SOURCE_PARTS:
        print("  ", rel)


if __name__ == "__main__":
    main()
