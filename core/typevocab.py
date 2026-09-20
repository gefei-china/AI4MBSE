# -*- coding: utf-8 -*-
"""core/typevocab.py —— 属性数据类型词表单一事实来源（P0-1，2026-09-07）。

背景：评估（docs/ontology-schema-gap-assessment.md §0.1）发现数据类型四套写法并存：
  1) UI 属性行:  string / date / enum / decimal / text（后经 UI 扩展补 int / boolean / dateTime）
  2) SHACL 导出: 自带 XSD 映射（bool 而非 boolean，无 int）
  3) OWL 导出:   constraints.xsd_type（xsd:* 前缀）
  4) 实例编辑器 / xsd 校验器: 只认 xsd:* 前缀
本模块提供「规范名 ↔ 别名 ↔ xsd:IRI ↔ 正则 pattern」的唯一映射，所有消费端统一调用。

规范名（canonical）：string / text / int / decimal / boolean / date / dateTime / enum
- enum 不参与 xsd 校验（枚举值由 allowed_values 的 sh:in 承载）
- text 与 string 同义（text=多行长文本），均映射 xsd:string
"""
import re

# 规范名 → xsd IRI 后缀
CANONICAL_TO_XSD = {
    "string": "xsd:string",
    "text": "xsd:string",
    "int": "xsd:int",
    "decimal": "xsd:decimal",
    "boolean": "xsd:boolean",
    "date": "xsd:date",
    "dateTime": "xsd:dateTime",
    "enum": "",          # 枚举由 allowed_values 承载，无 xsd 类型
}

# 全部可接受的别名 → 规范名（含 xsd:* 前缀形式与历史写法）
_ALIASES = {
    "string": "string", "text": "text", "str": "string",
    "int": "int", "integer": "int", "long": "int", "xsd:int": "int",
    "xsd:integer": "int", "xsd:long": "int",
    "decimal": "decimal", "float": "decimal", "double": "decimal", "number": "decimal",
    "xsd:decimal": "decimal", "xsd:float": "decimal", "xsd:double": "decimal",
    "boolean": "boolean", "bool": "boolean",
    "xsd:boolean": "boolean",
    "date": "date", "xsd:date": "date",
    "datetime": "dateTime", "dateTime": "dateTime", "xsd:dateTime": "dateTime",
    "enum": "enum", "enumeration": "enum",
}

# 规范名 → 值格式正则（非 string/text/enum 参与 xsd 格式校验）
PATTERNS = {
    "int": r"^[+-]?\d+$",
    "decimal": r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$",
    "boolean": r"^(true|false|0|1)$",
    "date": r"^\d{4}-\d{2}-\d{2}$",
    "dateTime": r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$",
}


def normalize(declared) -> str:
    """任意历史写法 → 规范名；未识别返回 'string'（宽松兜底，保持向后兼容）。"""
    if not declared or not isinstance(declared, str):
        return "string"
    return _ALIASES.get(declared.strip(), _ALIASES.get(declared.strip().lower(), "string"))


def xsd_of(declared) -> str:
    """任意历史写法 → xsd IRI（如 'xsd:int'）；enum/未识别返回 ''。"""
    c = normalize(declared)
    return CANONICAL_TO_XSD.get(c, "")


def is_typed(declared) -> bool:
    """是否有 xsd 类型约束（enum 与未识别不算）。"""
    return bool(xsd_of(declared))


def check_value(declared, value) -> str:
    """校验单值是否符合声明类型的格式。返回错误信息（''=通过）。
    None/'' 视为未填，不校验（必填性由 required 承载）。"""
    if value is None or value == "":
        return ""
    c = normalize(declared)
    pat = PATTERNS.get(c)
    if not pat:
        return ""
    if not re.match(pat, str(value)):
        return f"值 {value} 不符合类型 {declared}（{c}）"
    return ""


def validate_node_props(prop_type_getter, props: dict) -> list:
    """批量校验：prop_type_getter(key) 返回该属性键声明的类型（或 None）。
    只校验有类型声明且值非空的键。返回错误列表。"""
    errs = []
    for k, v in (props or {}).items():
        decl = prop_type_getter(k)
        if decl is None:
            continue
        msg = check_value(decl, v)
        if msg:
            errs.append(f"属性 {k}: {msg}")
    return errs
