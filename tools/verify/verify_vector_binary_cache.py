# -*- coding: utf-8 -*-
"""门禁：向量检索二进制缓存（L2）必须满足的六条。

本脚本守什么
────────────────────────────────────────────────────────────────
① **正确性**：二进制加载路径的 top-k 与「纯 JSON 逐行」**逐条完全一致**（id + 分数）。
   加速若改变结果 ⇒ 不是加速是**改行为**，必须判红。
② **写盘可生效**：无缓存时必须能从 JSON 重建**并成功写盘**，下一次检索**真的命中二进制**。
   判据是 `hit_json +1` → `hit_bin +1` 的**增量序列**，外加磁盘文件存在。
   ⚠️ 写盘失败必须被看见：`save_fail` 增量必须为 0（2026-11-26 起实现会留痕）。
③ **陈旧必须被发现**：磁盘缓存行数 ≠ 库行数时，**不能**"能加载就用"，必须判陈旧并回退 JSON。
   这是 2026-11-02 真实事故（缓存 6457 / 库 7179 ⇒ 722 条块余弦恒 0）的常驻判据。
④ **权威源不变**：二进制只是派生缓存 —— 缓存目录完全不存在时结果仍正确，只是慢。
⑤ **可替换性（mmap 回归守卫）**：缓存被本进程加载后，仍必须能被原子替换。
   2026-11-26 实测：`np.load(mmap_mode="r")` 会让别的进程 `os.replace` 报
   `PermissionError [WinError 5/32]` ⇒ `_save_binary` 静默失败 ⇒ 陈旧缓存永远修不掉
   ⇒ 本门禁时红时绿。**这条专门防有人把 mmap 改回去。**
⑥ **降级链完整**：维度不匹配 / 版本无向量 ⇒ `search` 返回 None，
   调用方走原逐行逻辑（行为不变）。

为什么判据全部在**独立临时缓存目录**里跑（2026-11-26 改）
────────────────────────────────────────────────────────────────
原版在 `tmp/vec_cache` 上跑，且靠 `shutil.rmtree(CACHE)` 制造"无缓存"。本机实测两条都不成立：
  · `shutil.rmtree` 被 safe-delete 策略拦（`trash-failed`），`ignore_errors=True` ⇒ **静默没删**，
    "删缓存后重建"那条根本没被验证过；
  · 运行中的服务会持有该目录里 `.npy` 的 mmap ⇒ 写盘必然失败，
    红绿取决于"服务在不在跑 / 缓存新不新"，**这不是确定性门禁**。
⇒ 现在每次运行在 `tmp/vec_cache_probe_<ts>/` 里自建环境，真库只读参与，跑完清理。

变异自证（不是"看起来能红"，是与正常组逐条比对）
────────────────────────────────────────────────────────────────
M1 `_load_binary` 恒 None（模拟二进制不可用）⇒ 必须变成 `hit_json` 递增、`hit_bin` 恒 0；
M2 写盘路径不可写（父级是普通文件）⇒ 必须 `save_fail ≥ 1` 且 `hit_bin` 恒 0。
两条都与"正常组"共用**同一个探针函数**，避免判据与变异各写一遍而漂移。
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
DB = (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db"))
VER = "openai-compat"
REAL_CACHE = os.path.join(_ROOT, "tmp", "vec_cache")


def _conn():
    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _query_vec(c):
    r = c.execute("SELECT embedding FROM document_chunks "
                  "WHERE embedding!='[]' AND embed_version=? LIMIT 1", (VER,)).fetchone()
    return json.loads(r["embedding"]) if r else None


def _json_reference(c, q, top_k=10):
    """纯 JSON 逐行余弦（= L2 之前的行为，作为正确性基准）。"""
    import numpy as np
    rows = c.execute("SELECT id, embedding FROM document_chunks "
                     "WHERE content != '' AND embedding != '[]' AND embed_version=?",
                     (VER,)).fetchall()
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


def _ids(res):
    return [x[1] for x in (res or [])]


def _probe(VI, c, q, cache_dir, taint=""):
    """在指定缓存目录里跑「重建 → 写盘 → 再加载」一轮，返回可断言的观测值。

    taint=""     正常
    taint="load" `_load_binary` 恒 None（模拟二进制不可用）
    taint="save" 调用方保证 cache_dir 不可建（父级是普通文件）⇒ 写盘必失败
    """
    CVI = VI.ChunkVectorIndex
    saved_dir = VI.CACHE_DIR
    orig_load = CVI.__dict__["_load_binary"]
    try:
        VI.CACHE_DIR = cache_dir
        if taint == "load":
            CVI._load_binary = classmethod(lambda cls, v, d: None)
        CVI.invalidate()
        st0 = dict(CVI.stats()["load"])
        r1 = CVI.search(c, q, VER, top_k=10)          # 无缓存 ⇒ JSON 重建（+ 试写盘）
        mp, ip = VI._cache_paths(VER, len(q))
        disk = (os.path.exists(mp), os.path.exists(ip))
        d_json1 = _delta(VI, st0, "hit_json")

        st1 = dict(CVI.stats()["load"])
        CVI.invalidate()
        r2 = CVI.search(c, q, VER, top_k=10)          # 应命中磁盘二进制
        return {"r1": r1, "r2": r2, "disk": disk,
                "d_json1": d_json1,
                "d_json2": _delta(VI, st1, "hit_json"),
                "d_bin2": _delta(VI, st1, "hit_bin"),
                # 写盘失败可能发生在任一轮（第二轮重建也会再试一次写盘）
                "d_savefail": _delta(VI, st0, "save_fail"),
                "mp": mp, "ip": ip}
    finally:
        CVI._load_binary = orig_load
        VI.CACHE_DIR = saved_dir
        CVI.invalidate()


def _delta(VI, before, key):
    return VI.ChunkVectorIndex.stats()["load"].get(key, 0) - before.get(key, 0)


def main() -> int:
    print("=" * 76)
    print("向量检索二进制缓存门禁（L2：正确性 / 写盘 / 陈旧 / 权威源 / 可替换 / 降级）")
    print("=" * 76)
    ok = True
    c = _conn()
    q = _query_vec(c)
    if not q:
        # 空库/无向量环境（CI 的 nightly-guards 就是这种）⇒ 无样本可验，显式 SKIP。
        # ⚠️ 只有"整表为空"才 SKIP；表里有块却一条 openai-compat 向量都没有
        #    = 数据被清空/写坏，那必须判红，不能拿 SKIP 糊过去。
        try:
            total = c.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        except Exception:
            total = 0
        if total == 0:
            print("[SKIP] document_chunks 为空（空库/干净库）⇒ 无向量可验，放行")
            c.close()
            return 0
        print("[FAIL] document_chunks 有 %d 行，但没有任何 %s 向量 —— 数据异常，不是 SKIP"
              % (total, VER))
        return 1
    dim = len(q)
    from adapters import vector_index as VI
    import numpy as np
    CVI = VI.ChunkVectorIndex

    stamp = int(time.time() * 1000)
    probe_dir = os.path.join(_ROOT, "tmp", "vec_cache_probe_%d" % stamp)
    m1_dir = os.path.join(_ROOT, "tmp", "vec_cache_m1_%d" % stamp)
    ghost = os.path.join(_ROOT, "tmp", "vec_cache_ghost_%d" % stamp)
    blocked_parent = os.path.join(_ROOT, "tmp", "vec_cache_blocked_%d" % stamp)
    print("基准：维度=%d，库=%s" % (dim, os.path.basename(DB)))
    print("临时缓存目录=%s（跑完清理）" % os.path.basename(probe_dir))

    ref = _json_reference(c, q, top_k=10)
    print("JSON 逐行基准：%d 条（首个 id=%s）" % (len(ref), _ids(ref)[:1]))

    # ── ①② 正确性 + 写盘可生效（正常组） ─────────────────────────────
    print("\n①② 正确性 + 写盘可生效（无缓存 → JSON 重建 → 写盘 → 再命中二进制）")
    t0 = time.time()
    obs = _probe(VI, c, q, probe_dir)
    ms = (time.time() - t0) * 1000
    same = _ids(obs["r2"]) == _ids(ref)
    max_dev = max((abs(a[0] - b[0]) for a, b in zip(obs["r2"] or [], ref)), default=0.0)
    print("[%s] ★ 二进制加载的 id 顺序与纯 JSON 一致（%s vs %s）"
          % ("OK  " if same else "FAIL", _ids(obs["r2"])[:5], _ids(ref)[:5]))
    print("[%s] ★ 分数最大偏差 %.8f（阈值 1e-5）"
          % ("OK  " if max_dev < 1e-5 else "FAIL", max_dev))
    ok = ok and same and max_dev < 1e-5
    print("[%s] 首次检索走 JSON 重建（hit_json 增量=+%d，应为 1）"
          % ("OK  " if obs["d_json1"] == 1 else "FAIL", obs["d_json1"]))
    ok = ok and obs["d_json1"] == 1
    print("[%s] ★ 磁盘缓存已生成（npy=%s / ids=%s）"
          % ("OK  " if all(obs["disk"]) else "FAIL", obs["disk"][0], obs["disk"][1]))
    ok = ok and all(obs["disk"])
    print("[%s] ★ 第二次命中二进制（hit_bin 增量=+%d，应为 1；hit_json 增量=+%d，应为 0）"
          % ("OK  " if (obs["d_bin2"] == 1 and obs["d_json2"] == 0) else "FAIL",
             obs["d_bin2"], obs["d_json2"]))
    ok = ok and obs["d_bin2"] == 1 and obs["d_json2"] == 0
    print("[%s] 写盘失败留痕为 0（save_fail 增量=+%d）"
          % ("OK  " if obs["d_savefail"] == 0 else "FAIL", obs["d_savefail"]))
    ok = ok and obs["d_savefail"] == 0
    print("     （一轮共 %.1fs，含一次 JSON 全量重建）" % (ms / 1000.0))

    # ── ③ 陈旧必须被发现（改「行数比库少 1 但自身自洽」） ──────────────
    print("\n③ 陈旧检测（缓存行数 ≠ 库行数 ⇒ 必须判陈旧并回退 JSON，不许「能加载就用」）")
    want = CVI._db_group_count(c, VER)
    VI.CACHE_DIR = probe_dir
    try:
        m = np.load(obs["mp"])
        ids_arr = np.load(obs["ip"])
        # 两个文件同时截短：保持 shape 自洽 ⇒ `_load_binary` 会**成功**，
        # 只能靠 `_ensure_group` 的行数对账发现陈旧 —— 正是 2026-11-02 事故的形态。
        np.save(obs["mp"], m[:-1])
        np.save(obs["ip"], ids_arr[:-1])
        st = dict(CVI.stats()["load"])
        CVI.invalidate()
        r_stale = CVI.search(c, q, VER, top_k=10)
        d_json = _delta(VI, st, "hit_json")
        d_stale = _delta(VI, st, "stale_bin")
        d_bin = _delta(VI, st, "hit_bin")
        detail = CVI.stats()["load"].get("stale_detail") or {}
        print("     库行数=%s / 缓存行数=%d ⇒ 期望判陈旧" % (want, len(ids_arr) - 1))
        print("[%s] ★ 陈旧被识别并回退 JSON 重建（stale_bin +%d / hit_json +%d / hit_bin +%d）"
              % ("OK  " if (d_stale == 1 and d_json == 1 and d_bin == 0) else "FAIL",
                 d_stale, d_json, d_bin))
        ok = ok and d_stale == 1 and d_json == 1 and d_bin == 0
        print("[%s] 陈旧明细可读（cached=%s / db=%s / diff=%s）"
              % ("OK  " if detail.get("cached") is not None else "FAIL",
                 detail.get("cached"), detail.get("db"), detail.get("diff")))
        ok = ok and detail.get("cached") is not None
        print("[%s] 重建结果仍与基准一致（陈旧缓存没有污染结果）"
              % ("OK  " if _ids(r_stale) == _ids(ref) else "FAIL"))
        ok = ok and _ids(r_stale) == _ids(ref)
    finally:
        VI.CACHE_DIR = REAL_CACHE
        CVI.invalidate()

    # ── ④ 权威源不变：缓存目录完全不存在时结果仍正确 ─────────────────
    print("\n④ 权威源不变（缓存目录不存在 ⇒ 结果仍正确，只是慢）")
    VI.CACHE_DIR = ghost
    CVI.invalidate()
    st = dict(CVI.stats()["load"])
    r_ghost = CVI.search(c, q, VER, top_k=10)
    d_json = _delta(VI, st, "hit_json")
    print("[%s] ★ 无磁盘缓存时结果正确且走 JSON 重建（hit_json 增量=+%d）"
          % ("OK  " if (_ids(r_ghost) == _ids(ref) and d_json == 1) else "FAIL", d_json))
    ok = ok and _ids(r_ghost) == _ids(ref) and d_json == 1

    # ── ⑤ 可替换性（mmap 回归守卫） ─────────────────────────────────
    print("\n⑤ 可替换性（缓存被加载后仍必须能被原子替换 —— 防 mmap 回归）")
    VI.CACHE_DIR = probe_dir
    CVI.invalidate()
    CVI.search(c, q, VER, top_k=5)          # 先把缓存修成与库一致（③ 可能让它陈旧）
    st = dict(CVI.stats()["load"])
    CVI.invalidate()
    CVI.search(c, q, VER, top_k=5)          # 这一次**必须**真的从磁盘命中二进制
    d_bin = _delta(VI, st, "hit_bin")
    # ⚠️ 前置断言不能省：若这次没命中二进制（走了重建），本进程根本没持有映射，
    #    下面的 replace 必然成功 ⇒ ⑤ 变成**空转断言**（实测踩过：变异改回 mmap 也照样绿）。
    print("[%s] 前置：本次确实从磁盘加载（hit_bin 增量=+%d，应为 1）"
          % ("OK  " if d_bin == 1 else "FAIL", d_bin))
    ok = ok and d_bin == 1
    mp, _ip = VI._cache_paths(VER, dim)
    dummy = mp + ".probe_dummy.npy"
    np.save(dummy, np.zeros((1, dim), dtype=np.float32))
    try:
        os.replace(dummy, mp)
        repl = "OK"
    except Exception as e:
        repl = "%s: %s" % (type(e).__name__, e)
    print("[%s] ★ 加载后仍可原子替换（%s）"
          % ("OK  " if repl == "OK" else "FAIL", repl))
    ok = ok and repl == "OK"
    if repl != "OK":
        print("     ⇒ 若这里失败，多半是有人把 _load_binary 改回了 mmap_mode（见模块 docstring）")

    # ── ⑥ 降级链 ───────────────────────────────────────────────────
    print("\n⑥ 降级链（维度不匹配 / 版本无向量 ⇒ None，调用方走逐行）")
    none_dim = CVI.search(c, [0.0] * (dim + 7), VER, top_k=5)
    print("[%s] 维度不匹配 ⇒ None（实测 %r）" % ("OK  " if none_dim is None else "FAIL", none_dim))
    ok = ok and none_dim is None
    none_ver = CVI.search(c, q, "no-such-version", top_k=5)
    print("[%s] 版本无向量 ⇒ None（实测 %r）" % ("OK  " if none_ver is None else "FAIL", none_ver))
    ok = ok and none_ver is None

    # ── 变异自证：与正常组共用同一个探针 ────────────────────────────
    print("\n[变异自证 M1] _load_binary 恒 None（二进制不可用）⇒ ② 的判据必须翻")
    obs_m1 = _probe(VI, c, q, m1_dir, taint="load")
    m1_red = (obs_m1["d_bin2"] == 0 and obs_m1["d_json2"] == 1)
    print("[变异M1] hit_bin 增量=+%d（正常应 +1，这里应 +0） / hit_json 增量=+%d（应 ≥1）⇒ %s"
          % (obs_m1["d_bin2"], obs_m1["d_json2"],
             "已判红（判据确实在观测二进制路径）" if m1_red else "未判红"))
    ok = ok and m1_red
    print("     （对照：正常组 hit_bin 增量=+%d）" % obs["d_bin2"])

    print("\n[变异自证 M2] 缓存目录不可建（父级是普通文件）⇒ save_fail 必须被看见")
    open(blocked_parent, "w").close()          # 父级 = 普通文件
    obs_m2 = _probe(VI, c, q, os.path.join(blocked_parent, "cache"), taint="save")
    m2_red = (obs_m2["d_savefail"] >= 1 and obs_m2["d_bin2"] == 0)
    print("[变异M2] save_fail 增量=+%d（应 ≥1） / hit_bin 增量=+%d（应 +0）⇒ %s"
          % (obs_m2["d_savefail"], obs_m2["d_bin2"],
             "已判红（写盘失败不再静默）" if m2_red else "未判红"))
    ok = ok and m2_red

    # ── 收尾：复原 + 清理（清理失败要报，但不判红） ───────────────────
    VI.CACHE_DIR = REAL_CACHE
    CVI.invalidate()
    leftovers = []
    for d in (probe_dir, ghost, m1_dir, blocked_parent):
        if not os.path.exists(d):
            continue
        if os.path.isfile(d):
            try:
                os.remove(d)
            except Exception:
                pass
        else:
            shutil.rmtree(d, ignore_errors=True)
        if os.path.exists(d):
            leftovers.append(os.path.basename(d))
    print("\n[收尾] CACHE_DIR 已复原为 tmp/vec_cache；残留临时目录 %d 个%s"
          % (len(leftovers), ("：%s" % leftovers) if leftovers else ""))
    if leftovers:
        print("     ⚠️ 删不掉只影响磁盘占用（可人工清理 tmp/vec_cache_*），不判红")

    c.close()
    print("\n" + "=" * 76)
    print("结论：%s" % ("全部通过" if ok else "存在失败项"))
    print("=" * 76)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
