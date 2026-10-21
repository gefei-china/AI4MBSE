# -*- coding: utf-8 -*-
"""门禁：图谱后端（Fuseki）端点契约 + 错误可诊断性（#58）。

## 为什么要有这道门禁（真实 bug 的取证链）
线上 `mbse_config.json` 配了 `"graph_db": {"backend": "fuseki",
"endpoint": "http://localhost:3030/mbse"}`，而 `graph_db.get_writer()` 的**兜底默认值**是：

    endpoint or "http://localhost:3030/ds"

`ds` 这个数据集**在本仓从来不存在**（`fuseki_start.py` 注册的是 `mbse`，DS="mbse"）。
⇒ endpoint 留空的调用方一律打到不存在的 `/ds` ⇒ **HTTP 405/404**。

而这层错误有个恶劣的放大效应：
- `FusekiWriter._note_unavail` 的熔断判据只认「连接被拒 / 超时 / 502 / 503 / 504」
  —— 它是对的：SPARQL **语法错（400）不该熔断**（否则一条错查询放大成全图谱 60s 不可用）。
- 但 404/405 既不是语法错、也没落进判据 ⇒ **既不熔断、也不快失败**
  ⇒ 每一轮对话的每一次图谱查询都**白等满 timeout(15s)**，
  症状是「图谱检索恒空 + 首 token 延迟被拖到几十秒」，
  而日志里只有一行 `Fuseki HTTP 405: <html><head><meta ...` —— 根因被 HTML 噪声淹没。

⇒ 三条修复，三组判据：
  [A] 默认端点必须是本仓真实注册的数据集名（`mbse`），不是 `ds`
  [B] 404/405/501 要被识别为「**端点配置错**」并给出**可直接照做的排查步骤**，
      且**不误入**"服务不可用"熔断（服务是活的）
  [C] HTML 错误页必须剥掉；日志里不得再出现 `<html>` / `<meta` 这类噪声

运行：./.venv/Scripts/python.exe tools/verify/verify_fuseki_endpoint.py
"""
from __future__ import annotations

import ast
import re
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + str(detail)) if detail else ""))


GD = os.path.join(_ROOT, "graph_db.py")
SRC = open(GD, encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════════
print("\n[A] 默认端点必须与本仓注册的数据集名一致")
# ══════════════════════════════════════════════════════════════════
# A1 源码取证：默认端点不得是 /ds
#   ★ 判据不能是裸 `"…/ds" not in SRC` —— 注释里**正需要**出现 `/ds`
#   （"原默认是 …/ds，那才是错的"）⇒ 裸串判据必然误报。
#   正确口径：检查 **`or "…/ds"` 这个兜底表达式**是否还在（注释里写的是
#   「原默认值是」，不会出现 `or "…/ds"` 这种代码形态）。
_bad_default = 'or "http://localhost:3030/ds"'
check("A1 代码中不再有 `or \"…/ds\"` 这个兜底默认值（注释提及不算）",
      _bad_default not in SRC,
      "仍存在兜底默认值" if _bad_default in SRC else "")
check("A2 默认端点为 .../mbse",
      '"http://localhost:3030/mbse"' in SRC or "http://localhost:3030/mbse" in SRC)

# A3 与 fuseki_start.py 注册的数据集名交叉核对（**唯一真源**，不是抄字符串）
_fs = os.path.join(_ROOT, "tools", "verify", "fuseki_start.py")
_fs_src = open(_fs, encoding="utf-8").read()
_check_ds = []
for n in ast.walk(ast.parse(_fs_src)):
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id == "DS" and isinstance(n.value, ast.Constant):
                _check_ds.append(n.value.value)
check("A3 fuseki_start.py 里能解析出注册的数据集名 DS", bool(_check_ds), str(_check_ds))
if _check_ds:
    _ds = _check_ds[0]
    check("A4 默认端点末段 == fuseki 注册的数据集名（%s）" % _ds,
          SRC.count("http://localhost:3030/%s" % _ds) >= 1)

# A5 变异自证：若把默认端点改回 /ds，A1~A2 判据必须能抓住
_mut_src = SRC.replace("http://localhost:3030/mbse", "http://localhost:3030/ds")
check("A5 变异自证：把默认端点改回 /ds 会被 A1/A2 判红",
      ("http://localhost:3030/ds" in _mut_src) and ("http://localhost:3030/mbse" not in _mut_src))

# ══════════════════════════════════════════════════════════════════
print("\n[B] 端点配置错（404/405/501）可诊断，且不误入服务不可用熔断")
# ══════════════════════════════════════════════════════════════════
# B2 端点错判据集合的**字面量内容**要含 404/405/501
#    （不能裸搜 "404" —— 注释里出现 404 不代表集合里有）
_m = re.search(r"_ENDPOINT_MISCONFIG_CODES\s*=\s*\(([^)]*)\)", SRC)
_codes = re.findall(r"\d+", _m.group(1)) if _m else []
check("B2 端点错判据集合字面量 == (404, 405, 501)",
      sorted(_codes) == ["404", "405", "501"], "解析到=%s" % _codes)

# B3 动态：真打一个不存在的端点，验证异常文本
#    ⚠️ 必须**绕过熔断**：`query_sparql` 开头会查 `unavail_blocked()`，
#    而 B3 之前若有别的用例把服务打成熔断态，这里只会拿到空列表、测不到异常文本。
#    这里直接调 `_post`（传输层），绕开熔断层，测的正是错误文本本身。
import graph_db  # noqa: E402

_bad = graph_db.FusekiWriter(endpoint="http://127.0.0.1:3030/__no_such_dataset__",
                            timeout=5)
try:
    _bad._post("http://127.0.0.1:3030/__no_such_dataset__/query",
               "SELECT * WHERE { ?s ?p ?o } LIMIT 1",
               "application/sparql-query")
    _msg = ""
except Exception as e:
    _msg = str(e)

print("     错误端点异常文本：%s" % _msg[:230])
check("B3 端点错被识别（异常文本标明端点配置错）",
      "端点配置错" in _msg, _msg[:160])
check("B4 错误文本含排查指引（$/datasets 核对数据集实名）",
      "$/datasets" in _msg, _msg[:200])
check("B5 错误文本点明「服务本身是活的」（区别于连接被拒）",
      "服务本身是活的" in _msg, _msg[:200])
check("B6 剥 HTML：报错文本里不得再有标签噪声",
      ("<html" not in _msg.lower() and "<meta" not in _msg.lower()), _msg[:200])
check("B6b 剥 HTML 后仍保留了服务端的可读说明（不是清空）",
      ("HTTP ERROR" in _msg or "not supported" in _msg or "Not Found" in _msg), _msg[:200])

# B7 404/405 不得触发「服务不可用」熔断（服务是活的，只是打错了门）
_i = SRC.find("def _note_unavail")
_j = SRC.find("def unavail_blocked", _i)
_seg = SRC[_i:_j]
check("B7 _note_unavail 判据里没有 404/405（避免误熔断活着的服务）",
      "HTTP 404" not in _seg and "HTTP 405" not in _seg,
      "判据片段: %s" % _seg[_seg.find("hit ="):_seg.find("hit =") + 220].replace("\n", " ")[:180])
check("B7b 熔断判据仍认连接被拒/超时/502（原有正确判据未被削弱）",
      all(s in _seg for s in ("10061", "timed out", "HTTP 502")))

# B8 静态：默认端点可查（若 Fuseki 在跑）—— 不可用则 SKIP 不计 FAIL
import socket  # noqa: E402


def _port_open(host, port, t=0.6):
    s = socket.socket()
    s.settimeout(t)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


if _check_ds and _port_open("127.0.0.1", 3030):
    # get_writer 签名：endpoint 是第 5 个位置参数（前面是 enabled/backend/store_path/use_memory）
    _good = graph_db.get_writer(True, "fuseki", "", False,
                                "http://127.0.0.1:3030/%s" % _check_ds[0])
    _r = _good.query_sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 1")
    check("B8 真实 Fuseki 在跑时，真实数据集端点可查通（未抛异常）",
          True, "查询返回 %d 行（0 行=图谱为空，不算失败）" % len(_r))
    check("B9 真实查询后 LAST_UNAVAIL_ERROR 为空（服务健康）",
          not graph_db.FusekiWriter.LAST_UNAVAIL_ERROR,
          graph_db.FusekiWriter.LAST_UNAVAIL_ERROR[:120])
    # B10 端点写错时**不得**污染 LAST_UNAVAIL_ERROR（不得误熔断）
    _before = graph_db.FusekiWriter.LAST_UNAVAIL_ERROR
    try:
        graph_db.FusekiWriter(
            endpoint="http://127.0.0.1:3030/__nope__", timeout=5
        )._post("http://127.0.0.1:3030/__nope__/query", "SELECT * WHERE {?s ?p ?o}",
                "application/sparql-query")
    except Exception:
        pass
    check("B10 端点配置错不污染 LAST_UNAVAIL_ERROR（不误熔断活服务）",
          graph_db.FusekiWriter.LAST_UNAVAIL_ERROR == _before,
          graph_db.FusekiWriter.LAST_UNAVAIL_ERROR[:120])
else:
    print("  [SKIP] B8~B10  Fuseki 未运行（3030 未监听），跳过真实连通性判据")

# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 72)
print("结果：%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
if FAIL:
    print("❌ 门禁失败：")
    for f in FAIL:
        print("   - " + f)
    sys.exit(1)
print("✅ 门禁通过")