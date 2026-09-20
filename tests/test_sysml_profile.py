"""P1：sysml_profile 核心函数测试（parse_profile_v2 / to_profile_v2 / 往返）。

覆盖（docs/sysml-profile-本体转换详细设计.md §7.1）：
1. parse_profile_v2：名称/父类/属性/required/枚举/对象引用判定
2. to_profile_v2：层级/属性/必填/枚举注释/关系 feature
3. 往返 round-trip：to → parse → 关键集合一致
4. 边界：空库提示 / 名称清洗 / 未知类型兜底
"""
import json
import sqlite3

from sysml_profile import parse_profile_v2, to_profile_v2, parse_profile_1x, to_profile_1x

KERML = """package XingWangProfile {
  metadata def 部件 {
    attribute 名称 : String;  // required: 名称
    attribute 频段 : String;  // allowed 频段: V, Ka
  }
  metadata def 载荷 : 部件 {
    attribute 吞吐量 : ScalarValue;
  }
  metadata def 天线 : 部件 { }
  metadata def 包含 {
    feature 包含 : 部件;
  }
}
"""


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE ontology_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        type_kind TEXT NOT NULL, parent_id INTEGER,
        properties TEXT DEFAULT '{}', constraints TEXT DEFAULT '{}',
        description TEXT DEFAULT '', icon TEXT DEFAULT '', color TEXT DEFAULT '#185FA5')""")
    return c


def test_parse_v2():
    pm = parse_profile_v2(KERML)
    assert pm["name"] == "XingWangProfile"
    assert pm["format"] == "v2"
    names = [s["name"] for s in pm["stereotypes"]]
    assert names == ["部件", "载荷", "天线", "包含"]
    bujian = pm["stereotypes"][0]
    assert bujian["parent"] is None
    attrs = {a["name"]: a for a in bujian["attributes"]}
    assert attrs["名称"]["dataType"] == "string"
    assert attrs["频段"]["type"] == "String"
    assert "名称" in bujian["required"]
    assert pm["enumerations"].get("频段") == ["V", "Ka"]
    assert pm["stereotypes"][1]["parent"] == "部件"   # specialization
    assert pm["stereotypes"][3]["relation_ends"] == [{"src": "包含", "tgt": "部件", "name": "包含"}]
    assert pm["stereotypes"][2]["attributes"] == [] and pm["stereotypes"][2]["relation_ends"] == []


def test_to_v2():
    conn = _conn()
    def add(name, kind, parent=None, props=None, cons=None):
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, parent_id, properties, constraints) VALUES (?,?,?,?,?)",
            (name, kind, parent, json.dumps(props or {}, ensure_ascii=False),
             json.dumps(cons or {}, ensure_ascii=False)))
    add("部件", "entity", None, {"名称": {"type": "string"}, "频段": {"type": "enum"}},
        {"required": ["名称"], "allowed_values": {"频段": ["V", "Ka"]}})
    add("需求", "entity", None, {"正文": {"type": "text"}}, {})
    add("载荷", "entity", 1, {"吞吐量": {"type": "decimal"}}, {})
    add("天线", "entity", 1, {}, {})
    add("包含", "relation", None, {}, {"allowed_values": {"src": ["部件"], "tgt": ["部件"]}})
    add("满足", "relation", None, {}, {"allowed_values": {"src": ["需求"], "tgt": ["部件"]}})
    conn.commit()
    ker = to_profile_v2(conn)
    assert "package XingWangProfile {" in ker
    assert "metadata def 载荷 : 部件 {" in ker
    assert "attribute 名称 : String;  // required: 名称" in ker
    assert "attribute 频段 : String;  // allowed 频段: V, Ka" in ker
    assert "feature 包含 : 部件;  // 关系" in ker
    assert "feature 满足 : 部件;  // 关系" in ker
    return conn, ker


def test_round_trip():
    _, ker = test_to_v2()
    pm2 = parse_profile_v2(ker)

    def key_set(pm):
        out = set()
        for s in pm["stereotypes"]:
            out.add((s["name"], s["parent"] or ""))
            for a in s["attributes"]:
                out.add((s["name"], "attr", a["name"], a["dataType"]))
            for r in s["relation_ends"]:
                out.add((s["name"], "rel", r["name"], r["tgt"]))
        return out

    expected = {
        ("部件", ""), ("载荷", "部件"), ("天线", "部件"), ("需求", ""),
        ("部件", "attr", "名称", "string"), ("部件", "attr", "频段", "string"),
        ("载荷", "attr", "吞吐量", "decimal"), ("需求", "attr", "正文", "string"),
        ("部件", "rel", "包含", "部件"), ("需求", "rel", "满足", "部件"),
    }
    assert expected <= key_set(pm2)


def test_edges():
    c = _conn()
    assert "本体为空" in to_profile_v2(c)
    c.execute("INSERT INTO ontology_types (name, type_kind, properties) VALUES (?,?,?)",
              ("部件", "entity", json.dumps({"自定义字段": {"type": "custom"}}, ensure_ascii=False)))
    ker = to_profile_v2(c)
    assert "attribute 自定义字段 : String;" in ker  # 未知类型兜底 string → String


# ═══════════ SysML 1.x（XMI）═══════════

XMI = """<?xml version="1.0" encoding="UTF-8"?>
<uml:Profile xmi:version="2.1"
             xmlns:xmi="http://www.omg.org/XMI"
             xmlns:uml="http://www.eclipse.org/uml2/5.0.0/UML"
             name="XingWangProfile">
  <packagedElement xmi:type="uml:PrimitiveType" xmi:id="_T_string" name="String"/>
  <packagedElement xmi:type="uml:PrimitiveType" xmi:id="_T_real" name="Real"/>
  <packagedElement xmi:type="uml:Enumeration" xmi:id="_E_band" name="频段">
    <ownedLiteral xmi:id="_E_band_v" name="V"/>
    <ownedLiteral xmi:id="_E_band_ka" name="Ka"/>
  </packagedElement>
  <packagedElement xmi:type="uml:Stereotype" xmi:id="_S_part" name="部件">
    <ownedAttribute xmi:id="_A_part_name" name="名称" type="_T_string" lower="1"/>
    <ownedAttribute xmi:id="_A_part_band" name="频段" type="_E_band"/>
  </packagedElement>
  <packagedElement xmi:type="uml:Stereotype" xmi:id="_S_zai" name="载荷">
    <generalization xmi:id="_G_zai" general="_S_part"/>
    <ownedAttribute xmi:id="_A_zai_thru" name="吞吐量" type="_T_real"/>
    <ownedAttribute xmi:id="_A_zai_contains" name="包含" type="_S_part"/>
  </packagedElement>
  <packagedElement xmi:type="uml:Extension" xmi:id="_EXT_part" name="Ext_部件">
    <ownedEnd xmi:id="_EO_part" name="base_Block" type="_S_part" aggregation="none"/>
    <memberEnd xmi:idref="_EO_part"/>
  </packagedElement>
</uml:Profile>
"""


def test_parse_1x():
    pm = parse_profile_1x(XMI)
    assert "error" not in pm, pm
    assert pm["name"] == "XingWangProfile"
    assert pm["format"] == "1x"
    sts = {s["name"]: s for s in pm["stereotypes"]}
    bujian = sts["部件"]
    attrs = {a["name"]: a for a in bujian["attributes"]}
    assert attrs["名称"]["dataType"] == "string" and "名称" in bujian["required"]
    assert attrs["频段"]["dataType"] == "enum" and attrs["频段"]["enum"] == ["V", "Ka"]
    zai = sts["载荷"]
    assert zai["parent"] == "部件"
    # 吞吐量 → Real → decimal
    thru = {a["name"]: a for a in zai["attributes"]}["吞吐量"]
    assert thru["dataType"] == "decimal"
    # 对象引用（type=_S_part）→ relation_ends
    assert {"src": "载荷", "tgt": "部件", "name": "包含"} in zai["relation_ends"]
    # Extension base_Block
    assert pm["base_metaclass_map"].get("部件") == "Block"


def test_to_1x():
    c = _conn()
    def add(name, kind, parent=None, props=None, cons=None):
        c.execute(
            "INSERT INTO ontology_types (name, type_kind, parent_id, properties, constraints) VALUES (?,?,?,?,?)",
            (name, kind, parent, json.dumps(props or {}, ensure_ascii=False),
             json.dumps(cons or {}, ensure_ascii=False)))
    add("部件", "entity", None, {"名称": {"type": "string"}, "频段": {"type": "enum"}},
        {"required": ["名称"], "allowed_values": {"频段": ["V", "Ka"]}})
    add("载荷", "entity", 1, {"吞吐量": {"type": "decimal"}}, {})
    add("包含", "relation", None, {}, {"allowed_values": {"src": ["部件"], "tgt": ["部件"]}})
    c.commit()
    xml = to_profile_1x(c)
    assert 'name="XingWangProfile"' in xml
    assert 'xmi:type="uml:Stereotype"' in xml and 'name="载荷"' in xml
    assert 'name="名称"' in xml and 'lower="1"' in xml
    assert 'name="频段"' in xml and 'xmi:type="uml:Enumeration"' in xml
    assert 'name="包含"' in xml and 'type="_S_部件"' in xml  # 关系引用属性
    assert 'general="_S_部件"' in xml                         # 层级
    assert 'name="base_Block"' in xml                         # Extension
    return c, xml


def test_round_trip_1x():
    _, xml = test_to_1x()
    pm = parse_profile_1x(xml)
    assert "error" not in pm
    sts = {s["name"]: s for s in pm["stereotypes"]}
    assert set(sts) == {"部件", "载荷"}
    assert sts["载荷"]["parent"] == "部件"
    attrs = {a["name"]: a for a in sts["部件"]["attributes"]}
    assert attrs["名称"]["dataType"] == "string" and "名称" in sts["部件"]["required"]
    assert attrs["频段"]["dataType"] == "enum" and attrs["频段"]["enum"] == ["V", "Ka"]
    assert {"src": "部件", "tgt": "部件", "name": "包含"} in sts["部件"]["relation_ends"]
    assert pm["base_metaclass_map"].get("部件") == "Block"


def test_parse_1x_bad_xml():
    pm = parse_profile_1x("<uml:Profile>")  # 未闭合
    assert "error" in pm
    pm2 = parse_profile_1x("not xml at all")
    assert "error" in pm2
