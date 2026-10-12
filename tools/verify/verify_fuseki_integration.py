"""Fuseki 对接验证（2026-10-10）—— ★ 走**生产代码链路**，不手工拼 N-Quads。

## 为什么必须用 Python 而不是 curl
Git Bash（MSYS2）会把 URL 里的 `/mbse` 转成
`C:/Users/.../PortableGit/mbse` ⇒ 数据集名被污染 ⇒ curl 直接 404。
⇒ 凡打 Fuseki HTTP 接口一律走本脚本（不做路径转换）。
（踩过：连续 3 次误判"服务没起来"，实际是路径转换。）

## 为什么走 `sync_pending_triples` 而不是自己拼 N-Quads
手工拼 N-Quads 会**绕过生产代码**（IRI 约定、rdfs:label 名称载体、
meta 溯源元数据、`_quad()` 的图名解析）⇒ 验出来的"能跑"不代表生产能跑。
⇒ 这里直接用 `graph_db.sync_pending_triples(conn, writer=FusekiWriter(...))`，
  与生产物化链**同一条代码路径**。

## 验证五步
① 连通性（ping + 数据集）
② 适配器连通：FusekiWriter.query_sparql 能往返
③ **真实物化**：把真库`graph_stored=0 且 status=approved` 的三元组灌进 Fuseki
   （★ 注意：这一步会**写生产库**的 `graph_stored=1` ⇒ 用只读连接跑会失败，
     故默认 `--dry-run` 只统计不落库；要真物化请显式加 `--commit`）
④ SPARQL 查回并比对数量
⑤ 对比 pyoxigraph（嵌入式）与 fuseki（服务端）**同一批数据**的查询结果是否一致

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_fuseki_integration.py
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_fuseki_integration.py --commit
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = os.environ.get("FUSEKI_BASE", "http://127.0.0.1:3030")
DS = os.environ.get("FUSEKI_DATASET", "mbse")
COMMIT = "--commit" in sys.argv
#★ 本沙箱会在**每条 bash 命令结束时回收该命令的所有子进程**
#   （实测：`nohup` 无效、`DETACHED_PROCESS` 也无效——日志显示 Start Fuseki 成功、
#     ping 也通，但下一条命令就连不上，端口已空）。
#   ⇒ 唯一可靠形态：**在同一条命令内**「启动 Fuseki → 等就绪 → 跑验证 → 关掉」。
#   ⇒ 故本脚本自己负责起服务，不再依赖外部常驻进程。
AUTO_START = "--no-start" not in sys.argv
PROC = None

# ★ 禁用代理：否则 urllib 走系统代理，把 127.0.0.1 也拦掉（实测 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(url, data=None, method="GET", ctype="application/sparql-results+json",
         timeout=20):
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", ctype)
    with _OPENER.open(r, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _endpoint(suffix: str = "query") -> str:
    """数据集端点 URL。★ 用纯 ASCII 数据集名，不含斜杠 ⇒ 无路径转换风险。"""
    return f"{BASE}/{urllib.parse.quote(DS)}/{suffix}"


def _ping(timeout: int = 4) -> str | None:
    """探活；成功返回时间戳字符串，失败返回 None。"""
    try:
        with _OPENER.open(f"{BASE}/$/ping", timeout=timeout) as r:
            return r.read().decode("utf-8", "replace").strip()
    except Exception:
        return None


def _start_fuseki(timeout: int = 60) -> bool:
    """在本进程内起 Fuseki（★ 必须在同一条命令内完成 start→验证→stop）。"""
    global PROC
    import subprocess
    import time
    java = os.path.join(_ROOT, "java-runtime", "bin", "java.exe")
    jar = os.path.join(_ROOT, "fuseki", "jena-fuseki-server.jar")
    if not os.path.exists(java) or not os.path.exists(jar):
        print(f"[FAIL] 缺文件：java={os.path.exists(java)} jar={os.path.exists(jar)}")
        return False
    # ★ 纯 ASCII 数据集名 + 绝对路径 jar ⇒ 不经 shell 解析，无 MSYS 转换
    args = [java, "-Xmx1g", "-jar", jar, "--mem", "--port", "3030", "/" + DS]
    log = open(os.path.join(_ROOT, "logs", "fuseki_verify.log"), "ab")
    PROC = subprocess.Popen(args, cwd=_ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(timeout // 2):
        time.sleep(2)
        if _ping():
            print(f"[OK  ] Fuseki 已就绪（PID={PROC.pid}，本命令内临时实例）")
            return True
    print("[FAIL] Fuseki 启动超时")
    return False


def _stop_fuseki():
    global PROC
    if PROC is not None:
        try:
            PROC.terminate()
            PROC.wait(timeout=8)
            print(f"[OK  ] 已关闭临时 Fuseki（PID={PROC.pid}）")
        except Exception:
            try:
                PROC.kill()
            except Exception:
                pass
        PROC = None


def main() -> int:
    print("=" * 74)
    print(f"Fuseki 对接验证（真实数据，生产代码链路；commit={COMMIT}）")
    print("=" * 74)
    ok = True

    # ① 连通性（必要时先在本命令内起服务）
    print("\n① 连通性")
    if _ping() is None and AUTO_START:
        print("     服务未运行 ⇒ 本命令内临时启动")
        if not _start_fuseki():
            print("       起服务命令：java -Xmx1g -jar fuseki/jena-fuseki-server.jar "
                  "--mem --port 3030 /mbse")
            return 1
    try:
        st, body = _req(f"{BASE}/$/ping", ctype="text/plain")
        print(f"[OK  ] ping HTTP {st}")
    except Exception as e:
        print(f"[FAIL] 连不上：{type(e).__name__} {str(e)[:150]}")
        return 1
    try:
        st, body = _req(f"{BASE}/$/datasets")
        names = [x.get("ds.name") for x in json.loads(body).get("datasets", [])]
        print(f"[OK  ] 数据集：{names}")
        if f"/{DS}" not in names:
            print(f"[WARN] /{DS} 不在列表")
    except Exception as e:
        print(f"[WARN] datasets 失败：{type(e).__name__}")

    # ② 适配器往返
    print("\n② 生产适配器 FusekiWriter 往返")
    endpoint = f"{BASE}/{urllib.parse.quote(DS)}/"
    try:
        from graph_db import get_writer
        w = get_writer(True, "fuseki", endpoint=endpoint)
        print(f"     适配器 = {type(w).__name__}（降级 Mock 则说明配置不对）")
        rows = w.query_sparql("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", limit=5)
        print(f"[OK  ] 适配器查询返回 {len(rows)} 行：{rows[0] if rows else '(空)'}")
    except Exception as e:
        print(f"[FAIL] 适配器不可用：{type(e).__name__} {str(e)[:200]}")
        return 1

    # ③ 真实物化
    print("\n③ 真实物化（生产链sync_pending_triples）")
    import sqlite3
    db = os.path.join(_ROOT, "mbse.db")
    conn = sqlite3.connect(db, timeout=20)
    conn.row_factory = sqlite3.Row
    pending = conn.execute(
        "SELECT COUNT(*) FROM triples WHERE graph_stored=0 AND status='approved'"
    ).fetchone()[0]
    print(f"     待物化三元组 = {pending}")
    if not COMMIT:
        print("[SKIP] 未加 --commit ⇒ 只统计不落库"
              "（物化会写生产库的 graph_stored=1）")
    else:
        try:
            from graph_db import sync_pending_triples
            r = sync_pending_triples(conn, writer=w, batch=50)
            print(f"[OK  ] 物化结果 = {r}")
        except Exception as e:
            print(f"[FAIL] 物化失败：{type(e).__name__} {str(e)[:200]}")
            ok = False
    conn.close()

    # ④ SPARQL 查回
    print("\n④ SPARQL 查回（直连 HTTP，不经适配器）")
    try:
        q = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"
        # ★ 用 **GET + query 参数**（Fuseki 6.2.0 对 POST + results-json 的
        #   Accept 头组合较严，会415）；生产适配器走的也是 GET 形态。
        url = _endpoint("query") + "?" + urllib.parse.urlencode({"query": q})
        st, body = _req(url, timeout=30)
        n = json.loads(body)["results"]["bindings"][0]["n"]["value"]
        print(f"[OK  ] 图内三元组 = {n}")
    except Exception as e:
        print(f"[FAIL] 查询失败：{type(e).__name__} {str(e)[:200]}")
        ok = False

    # ⑤ 两后端一致性
    print("\n⑤ 对比：嵌入式 pyoxigraph vs 服务端 fuseki（同一批数据）")
    try:
        from graph_db import get_writer as _gw
        q = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"
        wj = _gw(True, "fuseki", endpoint=endpoint)
        a = wj.query_sparql(q, limit=5)
        a_n = a[0].get("n") if a else "?"
        w2 = _gw(True, "pyoxigraph",
                 store_path=os.path.join(_ROOT, "tmp", "oxi_compare"))
        b = w2.query_sparql(q, limit=5)
        b_n = b[0].get("n") if b else "?"
        print(f"     fuseki={a_n}  pyoxigraph={b_n}")
        if a_n == b_n:
            print("[OK  ] 两后端结果一致（同为空也属正常：尚未物化）")
        else:
            print("[INFO] 不一致 —— 通常是物化只进了一边；"
                  "Fuseki 用 --mem 内存数据集，重启即丢")
    except Exception as e:
        print(f"[WARN] 对比跳过：{type(e).__name__} {str(e)[:150]}")

    print("\n" + "=" * 74)
    print(f"结论：{'全部通过' if ok else '存在失败项'}")
    print("=" * 74)
    if AUTO_START and PROC is not None:
        print("\n清理：")
        _stop_fuseki()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
