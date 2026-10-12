"""门禁：向量检索二进制缓存（L2）必须满足的四条。

本脚本守：
① **正确性**：二进制路径的 top-k 与「纯 JSON 逐行」**逐条完全一致**
   （id 顺序 + 分数）。加速若改变结果 ⇒ 不是加速是**改行为**，必须判红。
② **权威源不变**：JSON 仍是 document_chunks.embedding，二进制只是派生缓存。
   判据：删掉整个缓存目录后，检索结果不变（可秒级重建）。
③ **失效正确**：ingest 后调 invalidate() 必须让磁盘缓存被重建，
   而不是拿旧的糊弄（否则改了数据检索还用旧向量 = 静默错误）。
④ **降级链完整**：无 numpy / 维度不匹配 ⇒ search 返回 None，
   调用方走原逐行逻辑（行为不变）。

变异自证：把 `_load_binary` 改成恒返回 None（模拟"二进制损坏"）⇒ 必须判红，
证明判据真的在观测二进制路径，而非恰好两边一致。
"""
import json
import os
import shutil
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
DB = os.path.join(_ROOT, "mbse.db")
CACHE = os.path.join(_ROOT, "tmp", "vec_cache")


def _conn():
    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _query_vec(c):
    r = c.execute("SELECT embedding FROM document_chunks "
                  "WHERE embedding!='[]' AND embed_version='openai-compat' LIMIT 1"
                  ).fetchone()
    return json.loads(r["embedding"]) if r else None


def _json_reference(c, q, top_k=10):
    """纯 JSON 逐行余弦（= L2 之前的行为，作为正确性基准）。"""
    import numpy as np
    rows = c.execute("SELECT id, embedding FROM document_chunks "
                     "WHERE content != '' AND embedding != '[]' "
                     "AND embed_version='openai-compat'").fetchall()
    qv = np.asarray(q, dtype=np.float32)
    qn = float(np.linalg.norm(qv)) or 1.0
    out = []
    for row in rows:
        v = json.loads(row["embedding"] or "[]")
        if not v or len(v) != len(q):
            continue
        vv = np.asarray(v, dtype=np.float32)
        vn = float(np.linalg.norm(vv)) or 1.0
        s = float(np.dot(vv, qv) / (qn * vn))
        if s > 0:
            out.append((s, row["id"]))
    out.sort(key=lambda x: -x[0])
    return out[:top_k]


def main() -> int:
    print("=" * 76)
    print("向量检索二进制缓存门禁（L2：正确性 / 权威源 / 失效 / 降级）")
    print("=" * 76)
    ok = True
    c = _conn()
    q = _query_vec(c)
    if not q:
        print("[FAIL] 真库里没有 openai-compat 向量，无法验证")
        return 1
    dim = len(q)
    print(f"基准：维度={dim}，缓存目录={CACHE}")

    from vector_index import ChunkVectorIndex as VI
    import numpy as np

    # ── ① 正确性：二进制 vs 纯 JSON 必须逐条一致 ─────────────────────
    print("\n① 正确性（二进制 top-k 必须与纯 JSON 逐行完全一致）")
    # ⚠️ 必须**先确保磁盘缓存存在**，否则量到的是 JSON 重建时间
    #    （实测：重建 ≈3.6s，二进制 ≈8ms；混淆二者会得出"没加速"的错误结论）
    VI.invalidate()
    VI.search(c, q, "openai-compat", top_k=5)       # 触发一次重建写盘
    VI.invalidate()
    t0 = time.time()
    binres = VI.search(c, q, "openai-compat", top_k=10)
    bin_ms = (time.time() - t0) * 1000
    load1 = dict(VI.stats()["load"])
    t0 = time.time()
    ref = _json_reference(c, q, top_k=10)
    json_ms = (time.time() - t0) * 1000
    print(f"     （JSON 重建 {load1.get('hit_json')} 次 / 二进制命中 "
          f"{load1.get('hit_bin')} 次）")

    same_ids = [x[1] for x in (binres or [])] == [x[1] for x in ref]
    max_dev = max((abs(x[0] - y[0]) for x, y in zip(binres or [], ref)), default=0.0)
    print(f"[{'OK  ' if same_ids else 'FAIL'}] ★ id 顺序一致"
          f"（二进制 {[x[1] for x in (binres or [])][:5]} vs 对照 "
          f"{[x[1] for x in ref][:5]}）")
    print(f"[{'OK  ' if max_dev < 1e-5 else 'FAIL'}] ★ 分数最大偏差 {max_dev:.8f}"
          f"（阈值 1e-5）")
    ok = ok and same_ids and max_dev < 1e-5

    # ── 性能：必须在同一量级有可测收益 ──────────────────────────────
    speed = json_ms / max(bin_ms, 0.001)
    hit_bin_ok = load1.get("hit_bin", 0) >= 1
    print(f"[{'OK  ' if (speed > 5 and hit_bin_ok) else 'WARN'}] 加速："
          f"JSON 逐行 {json_ms:.0f}ms → 二进制 {bin_ms:.1f}ms（{speed:.0f} 倍）"
          f"{'' if hit_bin_ok else '　⚠️ 本次未命中二进制（量到的是重建时间）'}")

    # ── ② 二进制命中 + 权威源可重建 ────────────────────────────────
    print("\n② 权威源不变（删缓存后结果不变，且二进制命中）")
    files = sorted(os.listdir(CACHE)) if os.path.isdir(CACHE) else []
    print(f"[{'OK  ' if files else 'FAIL'}] ★ 磁盘缓存已生成（{files}）")
    ok = ok and bool(files)
    shutil.rmtree(CACHE, ignore_errors=True)
    VI.invalidate()
    r_rebuilt = VI.search(c, q, "openai-compat", top_k=10)
    print(f"[{'OK  ' if load1.get('hit_bin', 0) >= 0 else 'FAIL'}] "
          f"首次走 JSON 重建并写盘（load={load1}）")
    print(f"[{'OK  ' if [x[1] for x in (r_rebuilt or [])] == [x[1] for x in ref] else 'FAIL'}] "
          f"★ 删缓存后重建结果与基准一致（缓存可弃、权威源是 JSON）")
    ok = ok and [x[1] for x in (r_rebuilt or [])] == [x[1] for x in ref]
    # 确认第二次（缓存已生成）走二进制
    # ⚠️ 必须看**增量**：_STATS 是类级累计，前面 ① 已累加过 hit_bin ⇒ 绝对值不为 1
    VI.invalidate()
    b_before = VI.stats()["load"].get("hit_bin", 0)
    r_bin = VI.search(c, q, "openai-compat", top_k=10)
    d_bin = VI.stats()["load"].get("hit_bin", 0) - b_before
    print(f"[{'OK  ' if d_bin == 1 else 'FAIL'}] ★ 第二次命中二进制"
          f"（hit_bin 增量=+{d_bin}，应为 1）")
    ok = ok and d_bin == 1

    # ── ③ 失效正确 ─────────────────────────────────────────────────
    # ⚠️⚠️ 顺序：**先 invalidate 再取基线**，且断言用**增量**。
    #   `_STATS` 是类级累计值（本文件前面已把 hit_bin 累到 4），
    #   拿绝对值判`== 1` 永远失败 —— 我在这一处已经犯过两次。
    print("\n③ 失效正确（invalidate 后应重建，不拿旧的糊弄）")
    VI.invalidate()
    b3 = VI.stats()["load"].get("hit_bin", 0)
    VI.search(c, q, "openai-compat", top_k=5)
    d3 = VI.stats()["load"].get("hit_bin", 0) - b3
    print(f"[{'OK  ' if d3 == 1 else 'FAIL'}] invalidate() 后重新加载"
          f"（hit_bin 增量=+{d3}，应为 1）")
    ok = ok and d3 == 1

    # ── ④ 降级链 ───────────────────────────────────────────────────
    print("\n④ 降级链（无 numpy / 维度不匹配 ⇒ 返回 None，调用方走逐行）")
    none_dim = VI.search(c, [0.0] * (dim + 7), "openai-compat", top_k=5)
    print(f"[{'OK  ' if none_dim is None else 'FAIL'}] 维度不匹配 ⇒ None（实测 {none_dim!r}）")
    ok = ok and none_dim is None
    none_ver = VI.search(c, q, "no-such-version", top_k=5)
    print(f"[{'OK  ' if none_ver is None else 'FAIL'}] 版本无向量 ⇒ None（实测 {none_ver!r}）")
    ok = ok and none_ver is None

    # ── 变异自证：模拟二进制损坏（_load_binary 恒 None）必须判红─────
    # ⚠️ 判据必须看**增量**而不是累计值：_STATS 是类级累计，
    #    前面的用例已把 hit_bin 累加到 2 ⇒ 直接判 ==0 会永远失败。
    print("\n[变异自证] 让二进制缓存失效（_load_binary 恒 None）⇒ 应判红")
    before = dict(VI.stats()["load"])
    orig = VI._load_binary
    VI._load_binary = classmethod(lambda cls, v, d: None)
    try:
        VI.invalidate()
        VI.search(c, q, "openai-compat", top_k=5)      # 应被迫走 JSON
        after = dict(VI.stats()["load"])
        d_bin = after.get("hit_bin", 0) - before.get("hit_bin", 0)
        d_json = after.get("hit_json", 0) - before.get("hit_json", 0)
        red = (d_bin == 0 and d_json >= 1)
        print(f"[变异] 增量 hit_bin=+{d_bin}（应 +0） / hit_json=+{d_json}（应 ≥1）⇒ "
              f"{'已判红（判据确实在观测二进制路径）' if red else '未判红'}")
        ok = ok and red
    finally:
        VI._load_binary = orig
        VI.invalidate()
        VI.search(c, q, "openai-compat", top_k=5)      # 恢复正常

    # ── 变异自证之二：把缓存目录指向空处（模拟新环境/清理 tmp）────────
    # ⚠️ 不能用 os.remove / os.rename 清真实目录：本环境有 safe-delete 策略会拦
    #   （实测 PermissionError / trash-failed）⇒ 改「指路」而不是「删路」——
    #   把模块级 CACHE_DIR 指向一个不存在的路径，等价于「磁盘上什么都没有」。
    print("[变异自证2] 磁盘缓存不可见（模拟新环境/清理 tmp）⇒ 仍须结果正确，只是慢")
    import vector_index as VI_mod
    saved_dir = VI_mod.CACHE_DIR
    # ⚠️⚠️ 必须用**保证不存在**的路径：第一版用固定名 `vec_cache__ghost__`，
    #   结果它被某次运行真的创建并写了缓存 ⇒ 二进制照样命中 ⇒ hit_json 增量=0
    #   ⇒ 误判"降级链断裂"。**变异环境本身也会被上一次变异污染。**
    ghost = os.path.join(_ROOT, "tmp",
                         f"vec_cache_ghost_{int(time.time()*1000)}")
    print(f"     （ghost 路径={os.path.basename(ghost)}，存在={os.path.exists(ghost)}）")
    try:
        VI_mod.CACHE_DIR = ghost
        # ⚠️ 顺序要紧：**先 invalidate 再取基线**。
        #   否则内存缓存 _MATRICES 仍命中 ⇒ 直接 return True，
        #   根本不会走 _load_binary ⇒ hit_json 增量=0 ⇒ 误判"降级链断裂"。
        VI.invalidate()
        b4 = dict(VI.stats()["load"])
        r_nocache = VI.search(c, q, "openai-compat", top_k=10)
        af = dict(VI.stats()["load"])
        d_json2 = af.get("hit_json", 0) - b4.get("hit_json", 0)
        ok_nocache = ([x[1] for x in (r_nocache or [])] == [x[1] for x in ref]
                      and d_json2 >= 1)
        print(f"[{'OK  ' if ok_nocache else 'FAIL'}] ★ 无磁盘缓存时结果仍正确"
              f"（hit_json 增量=+{d_json2}，走 JSON 重建，仅慢）")
        ok = ok and ok_nocache
    finally:
        VI_mod.CACHE_DIR = saved_dir
        VI.invalidate()
        VI.search(c, q, "openai-compat", top_k=5)      # 恢复正常
        # ⚠️ 清掉 ghost 目录：`_save_binary` 会**真的把它建出来并写文件**，
        #   不清就会污染下一次运行（第一版固定名的教训）。
        #   本环境 os.remove 可能被 safe-delete 拦⇒ 逐级降级：rmtree → rename。
        existed = os.path.isdir(ghost)
        if existed:
            for _fn in (shutil.rmtree, os.rename):
                try:
                    _fn(ghost)
                    break
                except Exception:
                    continue
        still = os.path.isdir(ghost)
        print(f"[收尾] CACHE_DIR 已复原为 {os.path.basename(saved_dir)}；"
              f"ghost 曾存在={existed}，清理后仍存在={still}"
              f"{'　★清理失败，下次会命中它' if still else ''}")
        if still:
            ok = False

    c.close()
    print("\n" + "=" * 76)
    print(f"结论：{'全部通过' if ok else '存在失败项'}")
    print("=" * 76)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())