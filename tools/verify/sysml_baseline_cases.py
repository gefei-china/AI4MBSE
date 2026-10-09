"""SysML v2 基准集 —— 门禁回归的「已知通过 / 已知失败」最小集。

为什么需要这个
------------
v3.0 走查发现：`sysml_models/ev_thermal_mgmt/*.sysml`6 个样例里**5 个过不了官方校验器**
（01/02/03/04/05 全block，12~44 errors），根因是样例里写了
`satisfaction satisfy ReqX by Y;` —— `satisfaction` 是中间表示节点名，被误写进 .sysml 文本。

**不能拿脏样例当回归基准**：否则 N4 门禁上线后会全判红，而红的原因是基准本身脏，
不是被测代码退化。本文件提供一套**最小、干净、已实测**的基准。

两类用例，缺一不可
------------------
· PASS_CASE —— 已知合法（verdict=pass）。作用：证明校验器与门禁链路**能放行**。
  若这类红了 ⇒ 校验器/门禁坏了（假阴性）。
· FAIL_CASE —— 已知非法（verdict=block）。作用：证明门禁**能拦住**。
  若这类绿了 ⇒ 门禁形同虚设，比红更危险（**这是「变异自证」思想的应用**）。

每条用例都带 `doc_ref` 指向知识文档的实测编号，语法依据可追溯。
禁止「凭感觉写用例」—— 每条都要真跑过 checker.jar 并记录 verdict。
"""

# ── 已知合法（verdict=pass，0 词法/语法/语义）──────────────────────
PASS_CASES = {
    "S01_min_pkg_part": dict(
        desc="最小包 + part def + 可见性前缀 import",
        code="""package P {
  private import ScalarValues::*;
  part def Vehicle;
}""",
        doc_ref="L0 卡 ■包与导入",
    ),
    "S02_port_def": dict(
        desc="part def 内嵌 port def（端口定义不用 ':'）",
        code="""package P {
  part def Vehicle;
  part def Battery {
    port def p;
  }
}""",
        doc_ref="实测：port def p : X 会报 no viable alternative at input ':'",
    ),
    "S03_satisfy_inline": dict(
        desc="内联 satisfy 声明（satisfy 后必须跟 usage，不能引用 def）",
        code="""package P {
  part def Vehicle;
  part vehicle1;
  requirement def ReqRange { doc /* 需求 */ }
  satisfy requirement r1 : ReqRange by vehicle1;
}""",
        doc_ref="知识文档 [W02]；反例 [U02]/[T22]",
    ),
    "S04_cn_identifier": dict(
        desc="中文标识符必须单引号包裹",
        code="""package P {
  part def '电池包';
}""",
        doc_ref="L0 卡 ■编码/标识符",
    ),
    "S05_subject_only": dict(
        desc="subject 用 usage，且不与 satisfy 并存（二选一）",
        code="""package P {
  part def Vehicle;
  part vehicle1;
  requirement def ReqRange {
    subject = vehicle1;
    doc /* 需求 */
  }
}""",
        doc_ref="知识文档 §4.11 / [S01] Cannot override a binding feature value",
    ),
    "S06_connect_dot": dict(
        desc="connect 用点号访问嵌套特征",
        code="""package P {
  part def Vehicle;
  part vehicle1 {
    part engine;
  }
  connect vehicle1.engine to vehicle1;
}""",
        doc_ref="实测：'::' 双冒号报 Must be an accessible feature",
    ),
    "S07_attribute_def": dict(
        desc="attribute 必须由 attribute def 定型",
        code="""package P {
  private import ScalarValues::*;
  attribute def mass;
  part def Vehicle {
    attribute massValue : mass;
  }
}""",
        doc_ref="实测：直接attribute x : ScalarValues::Real 报 An attribute must be typed by attribute definitions",
    ),
    "S08_requirement_refine": dict(
        desc="需求细化用 :>（specializes），非 refines",
        code="""package P {
  part def Vehicle;
  requirement def ReqA { doc /* 顶层 */ }
  requirement def ReqB :> ReqA { doc /* 细化 */ }
}""",
        doc_ref="L0 卡 ■关键字与符号",
    ),
    "S09_action": dict(
        desc="part usage 内嵌 action",
        code="""package P {
  part def Vehicle;
  part vehicle1 {
    action start;
    action stop;
  }
}""",
        doc_ref="N2/N3 骨架与视图基础",
    ),
}

# ── 已知非法（verdict=block，n_hard > 0）──────────────────────────
# 这些是 L0 卡里点名的语法陷阱；门禁必须能拦住它们
FAIL_CASES = {
    "F01_import_no_visibility": dict(
        desc="import 缺可见性前缀（实样例就是这个错）",
        code="""package P {
  import ScalarValues::*;
  part def Vehicle;
}""",
        expect_source="syntax",
        doc_ref="L0 卡 ■高频错误对照：import X::*; → private import X::*;",
    ),
    "F02_cn_identifier_bare": dict(
        desc="中文标识符未加单引号（词法错，会级联成语法错）",
        code="""package P {
  part def 电池包;
}""",
        expect_source="lexical",
        doc_ref="L0 卡 ■编码/标识符（英文标识符 + 中文 doc 注释）",
    ),
    "F03_refines_wrong_keyword": dict(
        desc="用 refines 而非 :>（v2 无 refines 关键字）",
        code="""package P {
  part def Vehicle;
  requirement def ReqA { doc /* a */ }
  requirement def ReqB refines ReqA { doc /* b */ }
}""",
        expect_source="syntax",
        doc_ref="L0 卡：requirement R2 refines R1 → requirement r2 :> r1;",
    ),
    "F04_subject_and_satisfy": dict(
        desc="subject 与 satisfy 同时写（语义错，实时能过校验器才暴露）",
        code="""package P {
  part def Vehicle;
  part vehicle1;
  requirement def ReqRange {
    subject = vehicle1;
    doc /* 需求 */
  }
  satisfy requirement r1 : ReqRange by vehicle1;
}""",
        # ⚠️ 实测 verdict=report 而非 block —— 语义错 n_hard=0，按门禁判据**不阻断**。
        # 这正是判据的设计意图：只有词法/语法错才阻断（`verdict()` 文档有述）。
        # 本用例的用途是固化「该错会被报出来」这一事实，供 N4 决策是否收紧口径。
        expect_verdict="report",
        expect_source="semantic",
        doc_ref="知识文档 [S01]；L0 卡★需求与满足",
    ),
    "F05_satisfy_def_not_usage": dict(
        desc="satisfy 直接引用 requirement def（必须引用 usage）",
        code="""package P {
  part def Vehicle;
  part vehicle1;
  requirement def ReqRange { doc /* 需求 */ }
  satisfy ReqRange by vehicle1;
}""",
        expect_verdict="report",
        expect_source="semantic",
        doc_ref="知识文档 [U02] Must reference a requirement",
    ),
    "F06_logical_and_and_or": dict(
        desc="if 条件用 && 而非 &",
        code="""package P {
  part def Vehicle;
  part vehicle1 {
    action a1;
    if x > 0 && y > 0 {
      action go;
    }
  }
}""",
        expect_verdict="block",
        expect_source="syntax",
        doc_ref="L0 卡：a && b / a || b → a & b / a or b",
    ),
    "F07_extend_keyword": dict(
        desc="用 extend（v2 无此关键字）",
        code="""package P {
  part def Vehicle;
  part vehicle1 {
    action doIt;
  }
  extend Vehicle;
}""",
        expect_verdict="block",
        expect_source="syntax",
        doc_ref="L0 卡：extend SubUC → include use case SubUC",
    ),
    "F08_doc_double_quote": dict(
        desc="doc 用双引号字符串（v2 要用 /* */ 注释）",
        code="""package P {
  part def Vehicle { doc "文本" }
}""",
        expect_verdict="block",
        expect_source="syntax",
        doc_ref="L0 卡：doc \"文本\" → doc /* 文本 */",
    ),
}

# 门禁阈值：PASS 集合必须全pass（判据见 verify_sysml_baseline.py）
EXPECT_PASS = "pass"
EXPECT_FAIL = "block"
