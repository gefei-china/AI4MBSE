# -*- coding: utf-8 -*-
"""verify_embed_unavail_breaker.py —— 守卫：embedding「服务不可用」短熔断（P1-34）。

背景（2026-10-10 实测事故）：内网 embedding 网关经代理返回 502 / 连接超时，
每批都要白等 30s；而工具 JIT 排序、语义路由在**一次 SSE 请求里**要做多批多次调用
（27 候选 = 3 批 × 30s，还有多组候选集）⇒ 单个请求在 embedding 上爬 5~10 分钟，
用户看到"无限转圈"。py-spy 实证线程卡在 `_receive_response_headers`。

既有 `_QUOTA_BLOCK_*`（P1-10）只认 403/429 **配额**类错误，5xx/连接失败/超时
不属于它 ⇒ 每次都重新打网络 ⇒ 慢是叠加的。本守卫保护的是新增的短熔断：
    `Embedder._UNAVAIL_BLOCK_UNTIL` + `_note_unavail_error()` + `unavail_blocked()`
    + `embed()` / `embed_with_version()` 两个调用点的 `and not self.unavail_blocked()`。

判据（全部行为级，不 grep 源码字符串）：
  [1] 5xx ⇒ 触发短熔断；熔断窗口内 `embed()` **不再打网络**（0 次 `_embed_api`）且返回 bigram。
  [2] 连接失败（ConnectError）⇒ 同样触发短熔断。
  [3] 403/429 ⇒ **不得**误触发短熔断（两类熔断语义必须分开：配额 vs 临时不可用）。
  [4] `embed_with_version()` 同样受保护；窗口过期后自动恢复真向量（不永久降级）。
  [5] 变异自证：exec 孪生体删掉调用点的 `and not self.unavail_blocked()`（磁盘零改动），
      [1] 的判据必须翻红 —— 证明守卫有牙齿。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_embed_unavail_breaker.py
退出码：0 = 全绿；1 = 有失败。
"""
import io
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "knowledge_pipeline", "embedder.py")

_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


def _req():
    import httpx
    return httpx.Request("POST", "http://127.0.0.1:1/v1/embeddings")


def exc_5xx():
    import httpx
    return httpx.HTTPStatusError("502", request=_req(), response=httpx.Response(502, request=_req()))


def exc_connect():
    import httpx
    return httpx.ConnectError("connect failed", request=_req())


def exc_403():
    import httpx
    return httpx.HTTPStatusError("403", request=_req(), response=httpx.Response(403, request=_req()))


def reset(cls):
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    cls._QUOTA_BLOCK_UNTIL = 0.0
    cls.LAST_QUOTA_ERROR = ""


def make(cls, factory):
    """造一个 _api 可用、但 _embed_api 恒定抛指定异常的 Embedder。

    用 `__new__` 绕开 `__init__`（避免连 DB probe llm_providers）；
    `_embed_api` 挂实例方法并计数 —— **调用次数是本守卫的核心观测量**。
    """
    emb = cls.__new__(cls)
    emb._api = ("http://127.0.0.1:1/v1", "k", "m")
    calls = [0]

    def fake(self, texts):
        calls[0] += 1
        raise factory()

    emb._embed_api = types.MethodType(fake, emb)
    return emb, calls


def case_network_after_block(cls, factory):
    """触发一次失败 → 再调一次 embed() → 返回 (是否熔断, 窗口内网络调用次数)。"""
    reset(cls)
    emb, calls = make(cls, factory)
    try:
        emb.embed(["a", "b"])
    except Exception:
        pass
    blocked = cls.unavail_blocked()
    calls_after = calls[0]
    emb.embed(["a", "b"])          # 窗口内第二次：命中则应 0 次新增网络调用
    return blocked, (calls[0] - calls_after)


def main():
    print("=== embedding 服务不可用短熔断守卫（P1-34）===")
    from knowledge_pipeline.embedder import Embedder

    # [1] 5xx
    blocked, extra = case_network_after_block(Embedder, exc_5xx)
    check("[1] 5xx 触发短熔断", blocked)
    check("[1] 熔断窗口内 embed() 不再打网络", extra == 0, "仍调用了 %d 次" % extra)
    reset(Embedder)

    # [2] 连接失败
    blocked2, extra2 = case_network_after_block(Embedder, exc_connect)
    check("[2] 连接失败触发短熔断", blocked2)
    check("[2] 熔断窗口内 embed() 不再打网络", extra2 == 0, "仍调用了 %d 次" % extra2)
    reset(Embedder)

    # [3] 403/429 不得误触发（配额类 ≠ 临时不可用）
    hit403 = Embedder._note_unavail_error(exc_403())
    check("[3] 403 不误触发短熔断（归入配额熔断）", hit403 is False)
    check("[3] 403 后 unavail_blocked() 为 False", Embedder.unavail_blocked() is False)
    reset(Embedder)

    # [4] embed_with_version 同样受保护 + 窗口过期后自动恢复
    emb, calls = make(Embedder, exc_5xx)
    try:
        emb.embed_with_version(["a", "b"])
    except Exception:
        pass
    n1 = calls[0]
    vecs, ver = emb.embed_with_version(["a", "b"])
    check("[4] embed_with_version 熔断窗口内直走 bigram",
          ver == "bigram-tf" and len(vecs) == 2 and calls[0] == n1,
          "ver=%s calls+=%d" % (ver, calls[0] - n1))
    # 窗口过期 ⇒ 必须**重新打网络**（不是永久降级）。
    # ⚠️ 观测量是"网络被重新尝试"，不是"抛错"：短熔断只负责别打网络，
    #   真调用仍失败时按既有语义降级 bigram（不抛 EmbedQuotaExceeded）。
    Embedder._UNAVAIL_BLOCK_UNTIL = time.time() - 1
    expired = Embedder.unavail_blocked() is False      # 过期瞬间必须先解禁
    calls[0] = 0
    emb.embed_with_version(["a", "b"])                 # 服务仍坏 ⇒ 再熔断一次（符合预期）
    check("[4] 窗口过期后自动恢复（重新打网络，非永久降级）",
          expired and calls[0] > 0,
          "expired=%s calls=%d" % (expired, calls[0]))
    reset(Embedder)

    # [5] 变异自证：删掉调用点的 `and not self.unavail_blocked()`（exec 孪生体，不动磁盘）
    src = io.open(SRC, encoding="utf-8").read()
    needle = " and not self.unavail_blocked()"
    n_needle = src.count(needle)
    check("[5] 源码里存在待保护的调用点（否则变异无意义）", n_needle >= 2,
          "只找到 %d 处" % n_needle)
    ns = {}
    exec(compile(src.replace(needle, ""), SRC, "exec"), ns)
    Mut = ns["Embedder"]
    mblocked, mextra = case_network_after_block(Mut, exc_5xx)
    check("[5] 变异自证：去掉调用点判据后窗口内仍打网络 ⇒ 判据必须翻红",
          mblocked is True and mextra > 0,
          "熔断=%s 窗口内调用=%d（守卫空转）" % (mblocked, mextra))

    n_fail = sum(1 for r in _results if not r)
    print("\n%d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
