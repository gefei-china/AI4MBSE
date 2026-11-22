# CI-OPTIONAL: [local] 需真实服务/真 LLM/生产真库金标或随包运行时资产，CI 跑不了；源码直连真实服务/真实 key（CI 无服务无 secret） 
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
# 2026-11-22 收敛：端点真值搬到 core.fuseki_runtime，graph_db / CQ / 启动脚本
# 都读它。判据不能再靠「graph_db.py 源码里有没有那个字面量」——那会逼着
# 大家继续在四处抄同一个字符串。改为**运行期取值 + 变异自证**。
import graph_db  # noqa: E402
from core import fuseki_runtime as _fr  # noqa: E402

if os.path.join(_ROOT, "tools", "verify") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "tools", "verify"))
import fuseki_start as _fs  # noqa: E402

# A1 源码取证：不得再出现 `or "…/ds"` 兜底（注释提及不算）
_bad_default = 'or "http://localhost:3030/ds"'
check("A1 代码中不再有 `or \"…/ds\"` 这个兜底默认值（注释提及不算）",
      _bad_default not in SRC,
      "仍存在兜底默认值" if _bad_default in SRC else "")

_w = graph_db.get_writer(True, "fuseki", "", False, "")
check("A2 get_writer 空 endpoint 回落 == fuseki_runtime 单一真源",
      getattr(_w, "endpoint", "") == _fr.DEFAULT_ENDPOINT,
      "实得 %r / 真源 %r" % (getattr(_w, "endpoint", ""), _fr.DEFAULT_ENDPOINT))
check("A2b 单一真源数据集为 mbse（非历史错误值 ds）",
      _fr.DEFAULT_DATASET == "mbse" and _fr.DEFAULT_ENDPOINT.endswith("/mbse"),
      _fr.DEFAULT_ENDPOINT)

# A3/A4 启动脚本与运行时真源交叉核对（不是抄字符串）
_ds = getattr(_fs, "DS", None)
check("A3 fuseki_start.DS 与单一真源一致（%r）" % _ds,
      _ds == _fr.DEFAULT_DATASET, "启动脚本=%r 真源=%r" % (_ds, _fr.DEFAULT_DATASET))
check("A4 默认端点末段 == 注册数据集名",
      _fr.DEFAULT_ENDPOINT.rsplit("/", 1)[-1] == _ds,
      _fr.DEFAULT_ENDPOINT)

# A5 变异自证：把真源改成 /ds，get_writer 兜底必须跟着变 ⇒ 判据有区分力。
#    只改模块属性值（调用时读取），不落盘，finally 复原。
_orig_ep = _fr.DEFAULT_ENDPOINT
try:
    _fr.DEFAULT_ENDPOINT = "http://127.0.0.1:3030/ds"
    _mut_ep = getattr(graph_db.get_writer(True, "fuseki", "", False, ""), "endpoint", "")
finally:
    _fr.DEFAULT_ENDPOINT = _orig_ep
check("A5 变异自证：真源改回 /ds ⇒ A2 判红",
      _mut_ep == "http://127.0.0.1:3030/ds" and _mut_ep != _orig_ep,
      "变异后回落=%r" % _mut_ep)

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
# ⚠️ 2026-11-05：B3~B6b 要测的是「**打错数据集时 Fuseki 回 404**，异常文本能否
#   一眼诊断」。Fuseki 没在跑时拿到的是连接拒绝（10061）⇒ 这四条的**判据面不存在**，
#   必须 SKIP 而非 FAIL。此前它们恒红，把"服务未启"与"错误分类坏了"混同 ——
#   红色一多，真问题就被淹掉（与本轮别的门禁同族：无样本/无环境要 SKIP 且打印原因）。
if ("10061" in _msg) or ("积极拒绝" in _msg) or ("Connection refused" in _msg):
    print("  [SKIP] B3~B6b  端点未监听（%s）⇒ 「端点配置错」的判据面不存在"
          % _msg[:70])
else:
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


if _ds and _port_open("127.0.0.1", 3030):
    # get_writer 签名：endpoint 是第 5 个位置参数（前面是 enabled/backend/store_path/use_memory）
    _good = graph_db.get_writer(True, "fuseki", "", False,
                                "http://127.0.0.1:3030/%s" % _ds)
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