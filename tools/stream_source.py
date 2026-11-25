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


# ══════════════════════════════════════════════════════════════════════════
# 写侧：变异自证门禁的写回出口（2026-11-25，拆分第二步的硬前置）
#
# 债是什么：7 个门禁做「变异 → 整文件改写 → 回滚」，写的是
# `open(fp,"wb").write(拼接后的全文)`。一旦拆分，这句会把**全文**写进某一个
# 分片 ⇒ 直接写坏源码（而且是变异门禁在写，坏了很难归因）。
#
# 收口方式不是让 7 个门禁各写一套定位逻辑，而是**把写回也交给视图**：
# `write_stream_source(data)` 自己算出「变化落在哪一片」，只写那一片。
#  · 未拆分时：唯一分片 = 整文件 ⇒ 与旧的整文件写**行为完全等价**（可现场自证）；
#  · 拆分后：自动只改受影响的片；变化跨片 / 定位不到 ⇒ **抛错**（不静默写错）。
# 这样拆分时这 7 个门禁一行都不用改，且它们的「逐字节还原」断言仍然成立。
# ══════════════════════════════════════════════════════════════════════════


class StreamSourceError(RuntimeError):
    """写回无法安全定位到唯一分片时抛出（宁可红，不可写坏源码）。"""


def _detect_eol(raw: bytes) -> bytes:
    """检测原文件换行风格。含 CRLF ⇒ 写回也用 CRLF（否则改了行尾，破坏契约）。"""
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _encode_like(data, eol: bytes) -> bytes:
    """把 str（LF 归一语义）编码成「按原文件行尾风格」的字节；bytes 原样。"""
    if isinstance(data, bytes):
        return data
    t = str(data).replace("\r\n", "\n").replace("\r", "\n")
    return t.replace("\n", eol.decode("ascii")).encode("utf-8")


def _locate_changed_part(old_parts, new_bytes):
    """返回 (分片下标, 该片新内容)；无变化返回 (None, None)。

    算法：先求「与旧全文相同的前缀长 i」「相同后缀长 j」，变化区间 = 旧[i, n-j)。
    该区间必须**完整落在某一个分片**内 —— 跨片意味着这次改动本身就跨越了
    拆分边界（例如删掉了两个 mixin 之间的连接行），视图无法安全归位 ⇒ 抛错。

    ⚠️ 纯函数、不碰磁盘：这样「拆分后的行为」现在就能被门禁直接验证，
    而不必等真的拆了才发现算法错了。
    """
    old = b"".join(old_parts)
    if old == new_bytes:
        return None, None
    n_old, n_new = len(old), len(new_bytes)
    lo = min(n_old, n_new)
    i = 0
    while i < lo and old[i] == new_bytes[i]:
        i += 1
    j = 0
    while j < lo - i and old[n_old - 1 - j] == new_bytes[n_new - 1 - j]:
        j += 1
    old_end = n_old - j
    # 变化区间 [i, old_end) 到分片边界的归属
    starts, pos = [], 0
    for p in old_parts:
        starts.append(pos)
        pos += len(p)
    idx = None
    for k in range(len(old_parts)):
        s = starts[k]
        e = s + len(old_parts[k])
        if s <= i and old_end <= e:
            idx = k
            break
    if idx is None:
        raise StreamSourceError(
            "变更区间 [%d,%d) 跨越多于一个分片（分片边界 %s）⇒ 拒绝写回。"
            "请改成分片内的小改动，或显式给出 anchor 并先拆分该改动。"
            % (i, old_end, [s + len(p) for s, p in zip(starts, old_parts)]))
    s = starts[idx]
    new_part = old_parts[idx][:i - s] + new_bytes[i:n_new - j] + old_parts[idx][old_end - s:]
    return idx, new_part


def _module_for_strict(anchor: str) -> str:
    """写回专用的严格版定位：锚点找不到就**抛错**。

    为什么不能复用 `module_for()`：那个函数为「只读断言」设计，找不到锚点时刻意
    宽容（返回默认路径，避免打断门禁）。但写回场景里"锚点找不到"意味着
    **无法确认改动该归到哪一片** —— 宽容地写默认片就是在赌，赌错等于写坏源码。
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
    raise StreamSourceError(
        "anchor=%r 不在任何分片中 ⇒ 无法确认改动归属，拒绝写回" % (anchor,))


def write_stream_source(data, anchor=None) -> str:
    """把 data 写回 stream 源码，返回被写入的分片相对路径。

    参数：
      data   —— `read_stream_source()` 口径的文本（LF），或 `read_stream_source_bytes()`
                口径的原始字节。**行尾按原文件风格自动还原**（CRLF 文件不会被写成 LF）。
      anchor —— 可选。给出时要求变化区间落在 `module_for(anchor)` 所在分片内，
                否则抛错（防止"锚点说在这片、实际改的是那片"）。

    自证：写完立刻读回，`str` 口径必须与 data 逐字符相同、`bytes` 口径必须逐字节相同；
    不一致直接抛错（不把"看起来写了"当"写了"）。
    """
    parts = [_read_file(rel) for rel in SOURCE_PARTS]
    eol = _detect_eol(parts[0]) if parts else b"\n"
    new_bytes = _encode_like(data, eol)
    idx, new_part = _locate_changed_part(parts, new_bytes)
    if idx is None:
        invalidate_cache()
        return SOURCE_PARTS[0] if SOURCE_PARTS else DEFAULT_REL
    rel = SOURCE_PARTS[idx]
    if anchor:
        want = _module_for_strict(anchor)
        if want != rel:
            raise StreamSourceError(
                "anchor=%r 属于 %s，但变化落在 %s ⇒ 拒绝写回（锚点与改动不一致）"
                % (anchor, want, rel))
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "wb") as f:
        f.write(new_part)
    invalidate_cache()
    # 读回自证
    got = b"".join(_read_file(r) for r in SOURCE_PARTS)
    if got != new_bytes:
        expect = data if isinstance(data, bytes) else new_bytes
        if got != expect:
            raise StreamSourceError(
                "写回自证失败：读回 %d 字节 ≠ 期望 %d 字节（分片 %s）"
                % (len(got), len(new_bytes), rel))
    return rel


def write_stream_source_verified(data, anchor=None) -> str:
    """同 `write_stream_source`，额外做「可解码 + 语法可解析」检查。

    语法检查只对**未拆分**的形态有意义（单片=完整模块）；拆分后单片不是完整模块，
    此时跳过（否则会误报）。门禁改变异时用它更安全。
    """
    rel = write_stream_source(data, anchor=anchor)
    txt = read_stream_source()
    try:
        compile(txt, rel, "exec") if len(SOURCE_PARTS) == 1 else compile("pass", rel, "exec")
    except SyntaxError as exc:
        raise StreamSourceError("写回后源码语法不可解析：%s" % exc)
    return rel



def main():
    print("stream 源码视图：%d 个分片，%d 字节，sha256=%s"
          % (len(SOURCE_PARTS), len(read_stream_source_bytes()), source_sha256()))
    for rel in SOURCE_PARTS:
        print("  ", rel)


if __name__ == "__main__":
    main()
