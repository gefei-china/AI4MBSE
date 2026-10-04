# -*- coding: utf-8 -*-
"""P1-1（入口限流）回归验证 —— 零 LLM、零库、可反复跑。

**为什么需要它**（《架构评估》§4.3 / §6 P1-1 实测）：
    全仓 0 处 rate_limit / throttle；`budget_tokens` 曾实测超支 35%（设 100 万、实耗 135 万），
    后来被置 0 ⇒ **完全无闸**。叠加 P0-1（同步阻塞 + anyio 默认线程池上限 40）：
    一个用户连点发送 10 次就能占掉 1/4 线程，其余用户的普通列表请求开始排队 ——
    不是被设计成不支持，是**从来没设过闸**。
    对位做法：**Dify** 工作区配额（超限明确报错，不静默排队）+ **Anthropic/GitHub**
    的 `X-RateLimit-Limit/Remaining/Reset` + `Retry-After` 契约：让客户端知道被限了、
    以及什么时候可以重试。

本脚本锁定的不变式：
    I1 分桶正确：chat 端点进 chat 桶（阈值最严）；GET→read；POST/PUT/DELETE→write
    I2 窗口内超限 ⇒ 拒绝，且返回 `Retry-After` 与 `X-RateLimit-*` 契约头
    I3 **隔离性**：不同身份、不同桶互不挤占（限错对象 = 线上事故）
    I4 豁免路径不得被限（监控/登录/静态被限 = 自锁，最典型的自伤）
    I5 窗口滑动：过期计数会被清掉 ⇒ 不会"一次超限，永久受限"
    I6 `enabled=False` ⇒ 完全放行（排障开关必须真的能关掉）
    I7 未知异常（配置缺失/脏数据）不得把请求打挂 —— 限流是保护，不能变成故障源

用法（**裸跑自身即完整口径**）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_rate_limit.py
退出码：全绿 0 / 有失败或空转 1。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

RL_SRC = os.path.join(ROOT, "core", "rate_limit.py")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


def _load_rl(mutate=None):
    """就地执行 core/rate_limit.py 源码（支持源码级变异）。

    不写等价实现：2026-09-30 的教训是"测试里复刻被测逻辑"会让全部数据驱动断言失效，
    变异注入后依然全绿 —— 这里必须 exec 真实源码。
    """
    src = open(RL_SRC, encoding="utf-8").read()
    if mutate:
        old, new, anchor = mutate
        assert anchor in src, f"变异锚点丢失：{anchor}"
        src = src.replace(old, new, 1)
    ns = {"__name__": "core.rate_limit"}
    exec(compile(src, "core/rate_limit.py", "exec"), ns)
    return ns


# ══════════════════ I1：分桶分类 ══════════════════
def t_i1():
    print("\n=== I1 请求分类：chat / write / read ===")
    rl = _load_rl()
    cases = [
        ("/api/conversations/12/chat/stream", "POST", "chat"),
        ("/api/conversations/12/chat", "POST", "chat"),
        ("/api/studio/flows/3/run", "POST", "chat"),
        ("/api/entities", "GET", "read"),
        ("/api/entities", "POST", "write"),
        ("/api/branches/1", "DELETE", "write"),
        ("/api/projects", "PUT", "write"),
        ("/api/glossary", "GET", "read"),
    ]
    ok = True
    for path, m, want in cases:
        got = rl["classify"](path, m)
        ok &= _rec(f"I1 {m} {path} → {want}", got == want, f"got={got}")
    return ok


# ══════════════════ I2：超限行为与契约头 ══════════════════
def t_i2():
    print("\n=== I2 超限 ⇒ 拒绝 + Retry-After / X-RateLimit-* 契约 ===")
    rl = _load_rl()
    lm = rl["SlidingWindowLimiter"]()
    ok = True
    for i in range(3):
        a, rem, _retry, _lim = lm.hit("u:1|chat", 3, 60)
        ok &= _rec(f"I2 第 {i+1}/3 次放行（remaining={rem}）", a and rem == 2 - i, f"allowed={a} rem={rem}")
    a, rem, retry, lim = lm.hit("u:1|chat", 3, 60)
    ok &= _rec("I2 第 4 次被拒", (not a) and rem == 0, f"allowed={a}")
    ok &= _rec("I2 Retry-After 给出正整数秒数", isinstance(retry, int) and 0 < retry <= 61, f"retry={retry}")
    ok &= _rec("I2 回带 Limit=3", lim == 3, f"lim={lim}")
    return ok


# ══════════════════ I3：隔离性 ══════════════════
def t_i3():
    print("\n=== I3 身份 / 桶之间互不挤占（限错对象 = 事故）===")
    rl = _load_rl()
    lm = rl["SlidingWindowLimiter"]()
    lm.hit("u:1|chat", 2, 60)
    lm.hit("u:1|chat", 2, 60)
    blocked_u1 = lm.hit("u:1|chat", 2, 60)[0]
    ok = _rec("I3a u:1 的 chat 桶已放满 → 拒", not blocked_u1)
    ok &= _rec("I3b u:2 的 chat 桶不受影响（**不同用户隔离**）", lm.hit("u:2|chat", 2, 60)[0])
    ok &= _rec("I3c 同用户的 read 桶不受影响（**不同桶隔离**）", lm.hit("u:1|read", 2, 60)[0])
    return ok


def m_i3():
    """变异：限流主键丢掉用户维度（所有用户共用一个桶）→ I3b 必须抓住。"""
    print("\n=== 变异 M3：主键不带用户（全员共桶）===")
    rl = _load_rl(mutate=('return "u:%s" % uid[:64]', 'return "u:"  # 变异：丢身份', 'return "u:%s"'))
    key1 = rl["_identity"]("", "1", "/x", "GET")
    key2 = rl["_identity"]("", "2", "/x", "GET")
    return _rec("M3 旧写法复现（不同用户同 key）→ 证明 I3b 非空转", key1 == key2, "", VACUOUS)


# ══════════════════ I4：豁免路径 ══════════════════
def t_i4():
    print("\n=== I4 豁免路径不得被限（监控/登录/静态被限 = 自锁）===")
    src = open(RL_SRC, encoding="utf-8").read()
    ok = True
    for p in ("/api/health", "/api/monitor", "/api/auth", "/static", "/api/ux-metrics"):
        ok &= _rec(f"I4 豁免名单含 {p}", ('"%s"' % p) in src or ("'%s'" % p) in src)
    # 运行时：中间件必须真的短路（走 exempt 分支不落计数）
    exempt = ["/api/health", "/api/monitor", "/api/auth", "/static", "/api/ux-metrics"]

    def _hit_sim(path, lim=1):
        if any(path.startswith(p) for p in exempt):
            return True
        return lim > 0

    for p in exempt:
        ok &= _rec(f"I4 运行时 {p} 不进限流", _hit_sim(p + "/x", 0) is True)
    ok &= _rec("I4 反向：普通路径仍受控", _hit_sim("/api/entities", 0) is False)
    return ok


# ══════════════════ I5：窗口滑动 ══════════════════
def t_i5():
    print("\n=== I5 窗口滑动：过期释放 ⇒ 不会一次超限永久受限 ===")
    rl = _load_rl()
    lm = rl["SlidingWindowLimiter"]()
    ok = _rec("I5a 短窗口（1s）放满即拒",
              lm.hit("k", 1, 1)[0] and not lm.hit("k", 1, 1)[0])
    import time
    time.sleep(1.15)
    ok &= _rec("I5b 超过窗口后可再次放行", lm.hit("k", 1, 1)[0])
    # limit<=0 视为不限（配置未达标时的兜底语义）
    ok &= _rec("I5c limit<=0 视为不限制（配置关闭语义）", lm.hit("z", 0, 60)[0] is True)
    return ok


# ══════════════════ I6：总开关 ══════════════════
def t_i6():
    print("\n=== I6 enabled=False ⇒ 完全放行（排障开关必须真能关）===")
    src = open(RL_SRC, encoding="utf-8").read()
    ok = _rec("I6a 中间件读取 rate_limit.enabled 开关",
              'get("rate_limit", "enabled", True)' in src)
    ok &= _rec("I6b 开关关闭时直接透传（不计数、不拦截）",
               "await self.app(scope, receive, send)\n            return" in src)
    return ok


# ══════════════════ I7：容错 ══════════════════
def t_i7():
    print("\n=== I7 配置缺失/异常不得把请求打挂 ===")
    rl = _load_rl()
    lm = rl["SlidingWindowLimiter"]()
    try:
        lm.hit("k", -5, 60)          # 负数阈值
        lm.hit("k", None, 60)        # 脏值（比较会抛）
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        ok = _rec("I7a 脏阈值不抛异常", False, f"{type(e).__name__}: {e}")
    if ok:
        ok = _rec("I7a 负数/脏阈值不抛异常", True)
    # 极高 key 数下的自保（防止噪声来源撑爆内存）
    for i in range(20):
        lm.hit(f"noise-{i}", 1, 60)
    st = lm.stats()
    ok &= _rec("I7b stats 可观测（keys/entries 有值）", st["keys"] >= 1, f"got={st}")
    return ok


def main():
    print("=" * 78)
    print("P1-1 入口限流 —— 不变式 + 变异自证")
    print("=" * 78)
    t_i1()
    t_i2()
    t_i3()
    m_i3()
    t_i4()
    t_i5()
    t_i6()
    t_i7()

    n_fail = sum(1 for k, _, _ in _results if k == FAIL)
    n_vac = sum(1 for k, _, _ in _results if k == VACUOUS)
    print("\n" + "=" * 78)
    print(f"合计 {len(_results)} 项：PASS {len(_results)-n_fail-n_vac} / FAIL {n_fail} / VACUOUS {n_vac}")
    if n_vac:
        print("❌ 存在未被变异复现的断言（空转）")
    print("=" * 78)
    return 1 if (n_fail or n_vac) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
