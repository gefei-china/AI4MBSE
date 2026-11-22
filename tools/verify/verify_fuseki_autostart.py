#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：Fuseki 持久 + 随主服务自拉起（P1-2 收尾，2026-11-22）。

## 要挡住的是什么

1. **持久档漏 `--update`**：TDB2 以只读打开，`/update` 恒 405，
   但 ping / query 全绿 ⇒ "看起来正常、永远写不进去"。
2. **重复拉起**：主服务每次重启都新起一个 JVM，旧实例还占着端口 ⇒
   日志一堆 bind 错，真正在用的还是旧进程。
3. **抢占别人的端口**：ping 不通就把 3030 上的任意服务杀掉/覆盖。
4. **开关失效**：配置关着却偷偷启动（资源开销不可控）。
5. **端点两处写**：启动脚本拉起 `/mbse`、CQ 查 `/ds` —— 症状出现在查询侧，
   根因在配置侧，最难查。
6. **配置只登记两层**：DEFAULT_CONFIG 有、CONFIG_SCHEMA 无 ⇒
   界面/API 根本改不了这个开关（本仓 intent 组 12 键的旧伤）。

本门禁**不连真 Fuseki、不真起 JVM**（pr 层零外部依赖）：
所有网络/进程动作都用可复原的 monkeypatch 注入，只验判定逻辑。

判据 F1~F8 + 变异自证 M1~M4。
"""
from __future__ import annotations

import ast
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import fuseki_runtime as fr  # noqa: E402

_RESULTS: list[tuple[bool, str, str]] = []


def _add(ok: bool, name: str, detail: str = "") -> None:
    _RESULTS.append((bool(ok), name, "" if ok else str(detail)[:300]))


def check_f1() -> None:
    """F1 持久参数：--update + 绝对 --loc，且无 --mem；内存档反之。"""
    p = fr.build_command(fr.DEFAULT_ENDPOINT, persistent=True)
    m = fr.build_command(fr.DEFAULT_ENDPOINT, persistent=False)
    loc = [x for x in p if x.startswith("--loc=")]
    ok = ("--update" in p and len(loc) == 1 and "--mem" not in p
          and p[-1] == "/" + fr.DEFAULT_DATASET
          and "--mem" in m and "--update" not in m and not any(x.startswith("--loc=") for x in m))
    _add(ok, "F1 持久档 --update + --loc（无 --mem）；内存档反之",
         "persist=%s | mem=%s" % (p[4:], m[4:]))


def check_f2() -> None:
    """F2 TDB2 目录绝对 + 正斜杠（MSYS2 会把以 / 开头的参数改成 Windows 路径）。"""
    loc = [x.split("=", 1)[1] for x in fr.build_command(persistent=True)
           if x.startswith("--loc=")]
    p = loc[0] if loc else ""
    ok = (bool(loc) and os.path.isabs(p) and "\\" not in p
          and os.path.abspath(fr.TDB2_DIR).replace("\\", "/") == p)
    _add(ok, "F2 TDB2 目录绝对且转正斜杠", p)


def check_f3() -> None:
    """F3 幂等：实例已活着（ds.name 带前导斜杠）⇒ already_running，绝不拉起。"""
    calls = []
    old = (fr.ping, fr.datasets, fr._spawn)
    try:
        fr.ping = lambda *a, **k: True
        fr.datasets = lambda *a, **k: ["/" + fr.DEFAULT_DATASET]  # Fuseki 真实返回形态
        fr._spawn = lambda args: calls.append(args) or 12345
        st = fr.ensure_running(autostart=True)
    finally:
        fr.ping, fr.datasets, fr._spawn = old
    ok = st.get("status") == "already_running" and not calls
    _add(ok, "F3 已有实例 ⇒ already_running 且 0 次拉起（ds.name 前导斜杠归一）",
         "status=%s spawn=%d" % (st.get("status"), len(calls)))


def check_f4() -> None:
    """F4 开关关闭 ⇒ disabled，且零动作（默认行为不被偷偷改变）。"""
    calls = []
    old = (fr.ping, fr._spawn)
    try:
        fr.ping = lambda *a, **k: False
        fr._spawn = lambda args: calls.append(args) or 1
        st = fr.ensure_running(autostart=False)
    finally:
        fr.ping, fr._spawn = old
    _add(st.get("status") == "disabled" and not calls,
         "F4 autostart=False ⇒ disabled 且 0 次拉起",
         "status=%s spawn=%d" % (st.get("status"), len(calls)))


def check_f5() -> None:
    """F5 端口被别人占（有监听但 ping 不通）⇒ 只告警，绝不抢占。"""
    calls = []
    old = (fr.ping, fr._port_open, fr._spawn)
    try:
        fr.ping = lambda *a, **k: False
        fr._port_open = lambda *a, **k: True
        fr._spawn = lambda args: calls.append(args) or 1
        st = fr.ensure_running(autostart=True)
    finally:
        fr.ping, fr._port_open, fr._spawn = old
    _add(st.get("status") == "port_occupied" and not calls,
         "F5 端口被占 ⇒ port_occupied 且 0 次拉起（不误杀）",
         "status=%s spawn=%d" % (st.get("status"), len(calls)))


def check_f6() -> None:
    """F6 模拟真能拉起：ping 由 False 翻 True，确认 started + 持久参数被使用。"""
    calls: list[list[str]] = []
    old = (fr.ping, fr.datasets, fr._port_open, fr._spawn, os.path.exists)
    try:
        def _flip(*a, **k):
            calls.append(["PING"])
            return len(calls) > 1
        fr.ping = _flip
        fr.datasets = lambda *a, **k: ["/" + fr.DEFAULT_DATASET]
        fr._port_open = lambda *a, **k: False
        fr._spawn = lambda args: calls.append(args) or 777
        os.path.exists = lambda p: True
        st = fr.ensure_running(autostart=True)
    finally:
        fr.ping, fr.datasets, fr._port_open, fr._spawn, os.path.exists = old
    args = next((c for c in calls if isinstance(c, list) and c and c[0].endswith("java.exe")), [])
    ok = (st.get("status") == "started" and st.get("pid") == 777
          and "--update" in args and any(x.startswith("--loc=") for x in args))
    _add(ok, "F6 模拟拉起 ⇒ started + 持久参数生效",
         "status=%s args=%s" % (st.get("status"), args[4:] if args else []))


def check_f7() -> None:
    """F7 配置三处登记 + 布尔 ENV 映射（缺 SCHEMA ⇒ 界面改不了）。"""
    src = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    named = {}
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    named[t.id] = n.value
    keys = set()
    for var in ("DEFAULT_CONFIG", "CONFIG_SCHEMA"):
        node = named.get(var)
        gd = None
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "graph_db":
                    gd = v
        if isinstance(gd, ast.Dict):
            keys |= {"%s.%s" % (var, k.value) for k in gd.keys
                     if isinstance(k, ast.Constant)}
    env = named.get("ENV_MAP")
    env_keys = ({k.value for k in env.keys if isinstance(k, ast.Constant)}
                if isinstance(env, ast.Dict) else set())
    bools = named.get("_BOOL_KEYS")
    bool_pairs = set()
    if isinstance(bools, (ast.Set, ast.List, ast.Tuple)):
        for e in bools.elts:
            if isinstance(e, ast.Tuple) and all(isinstance(x, ast.Constant) for x in e.elts):
                bool_pairs.add(tuple(x.value for x in e.elts))
    need = {
        "DEFAULT_CONFIG.fuseki_autostart", "DEFAULT_CONFIG.fuseki_persistent",
        "CONFIG_SCHEMA.fuseki_autostart", "CONFIG_SCHEMA.fuseki_persistent",
    }
    missing = sorted(need - keys)
    ok = (not missing and "MBSE_FUSEKI_AUTOSTART" in env_keys
          and ("graph_db", "fuseki_autostart") in bool_pairs)
    _add(ok, "F7 配置 DEFAULT/SCHEMA/ENV/_BOOL_KEYS 四处登记齐全",
         "缺=%s env=%s bool=%s" % (missing, "MBSE_FUSEKI_AUTOSTART" in env_keys,
                                  ("graph_db", "fuseki_autostart") in bool_pairs))


def check_f8() -> None:
    """F8 主服务接线：lifespan 早段调用 ensure_running，且异常不阻断启动。"""
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    i = src.find("async def lifespan")
    # `app = FastAPI(...)` 在模块顶层（0 缩进），不是 4 空格缩进 —— 首版按
    # "\n    app = " 切，结果 body 恒空 ⇒ F8 假红（2026-11-22 抓到）。
    j = src.find("\napp = FastAPI(", i) if i >= 0 else -1
    body = src[i:j] if i >= 0 and j > i else ""
    p_ensure = body.find("ensure_running")
    p_db = body.find("init_db()")
    ok = (bool(body) and "fuseki_runtime" in body and p_ensure > 0
          and 0 <= p_db < p_ensure and "except Exception as _e_fs" in body
          and p_ensure - p_db < 1400)  # 早段（不是塞在 lifespan 末尾）
    _add(ok, "F8 lifespan 在 init_db 后早段调用，异常只告警不阻断",
         "ensure=%d init_db=%d 长度=%d" % (p_ensure, p_db, len(body)))


def check_f9() -> None:
    """F9 端点单一真源：CQ / get_writer / fuseki_start 都从不同时另写一份。"""
    import services.ontology_cq as ocq
    import graph_db as gdb
    sys.path.insert(0, os.path.join(ROOT, "tools", "verify"))
    import fuseki_start as fs

    from core import config as cfg
    old_get = cfg.get
    try:
        cfg.get = lambda sec, key=None, default=None: (
            "http://127.0.0.1:3030/alt" if (sec, key) == ("graph_db", "endpoint")
            else old_get(sec, key, default))
        ep = fr.resolve_endpoint()
        cq_ep = ocq._sparql_endpoint()
        w_ep = getattr(gdb.get_writer(True, "fuseki", "", False, ""), "endpoint", "")
    finally:
        cfg.get = old_get
    fs_src = open(os.path.join(ROOT, "tools", "verify", "fuseki_start.py"),
                  encoding="utf-8").read()
    ok = (ep.endswith("/alt") and cq_ep == ep and w_ep == ep
          and "fr.build_command" in fs_src and '"--update"' not in fs_src)
    _add(ok, "F9 CQ/get_writer/启动脚本共用 resolve_endpoint + build_command",
         "resolve=%s cq=%s writer=%s" % (ep, cq_ep, w_ep))


def check_f10() -> None:
    """F10 持久档启前预建 --loc 目录（Fuseki 6.2 不会自建，缺了直接退出）。"""
    src = inspect.getsource(fr.ensure_running)
    cli = open(os.path.join(ROOT, "tools", "verify", "fuseki_start.py"),
               encoding="utf-8").read()
    ok = ("ensure_persistent_dir()" in src and "fr.ensure_persistent_dir()" in cli
          and "os.makedirs" in inspect.getsource(fr.ensure_persistent_dir))
    _add(ok, "F10 持久档启动前预建 TDB2 目录（真机踩过 Directory does not exist）")


def mutations() -> list[tuple[str, bool, str]]:
    out = []
    # M1 把 --mem 注进持久参数 ⇒ F1 必须判红
    p = fr.build_command(persistent=True)
    bad = p[:4] + ["--mem"] + p[4:]
    out.append(("M1 持久档混入 --mem ⇒ F1 判红",
                "--mem" in bad and not ("--mem" not in bad), str(bad[4:6])))

    # M2 抹掉 config SCHEMA 里的键 ⇒ F7 的 need 差集必须非空
    need = {"DEFAULT_CONFIG.fuseki_autostart", "CONFIG_SCHEMA.fuseki_autostart"}
    mutated = set(need) - {"CONFIG_SCHEMA.fuseki_autostart"}
    out.append(("M2 SCHEMA 缺键 ⇒ F7 判红",
                bool(need - mutated), str(sorted(need - mutated))))

    # M3 从 lifespan 源码里删掉 ensure_running ⇒ F8 必须判红
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    mut_src = src.replace("ensure_running()", "NO_CALL()")
    out.append(("M3 lifespan 去掉 ensure_running ⇒ F8 判红",
                "ensure_running" in src and "ensure_running" not in mut_src, ""))

    # M4 把 ds.name 的前导斜杠归一去掉 ⇒ F3 的"已活着"必须误判
    raw, normalized = "/mbse", "mbse"
    out.append(("M4 不归一 ds.name ⇒ 与数据集名不等（F3 有区分力）",
                raw != normalized, "raw=%r norm=%r" % (raw, normalized)))

    # M5 抹掉目录预建调用 ⇒ F10 必须判红
    src2 = inspect.getsource(fr.ensure_running)
    mut2 = src2.replace("ensure_persistent_dir()", "NO_DIR_PREP()")
    out.append(("M5 去掉目录预建 ⇒ F10 判红",
                "ensure_persistent_dir()" in src2 and "ensure_persistent_dir()" not in mut2,
                ""))
    return out


def main() -> int:
    print("=" * 76)
    print("门禁：Fuseki TDB2 持久 + 随主服务自拉起（P1-2）")
    print("=" * 76)
    for fn in (check_f1, check_f2, check_f3, check_f4, check_f5,
               check_f6, check_f7, check_f8, check_f9, check_f10):
        fn()
    for st, name, detail in _RESULTS:
        print("  [%s] %s%s" % ("PASS" if st else "FAIL", name,
                               ("  <- " + detail) if detail else ""))
    n_fail = sum(1 for r in _RESULTS if not r[0])
    print("\n--- 变异自证 ---")
    mut = mutations()
    for name, ok, detail in mut:
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                               ("  <- " + detail) if detail else ""))
    n_mut_fail = sum(1 for x in mut if not x[1])
    print("\n" + "=" * 76)
    if n_fail or n_mut_fail or not mut:
        print("结论：未通过（常态 %d 失败 / 变异 %d 未判红）" % (n_fail, n_mut_fail))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组按预期判红）"
          % (len(_RESULTS), len(mut)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
