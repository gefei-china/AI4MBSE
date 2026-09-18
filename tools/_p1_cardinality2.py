# -*- coding: utf-8 -*-
"""P1 精确多重性 part2：推理基数校验 + 中栏 UI"""

# ── C. ontology_reasoning.py：cardinality_check + run_reasoning 接线 ──
p = 'ontology_reasoning.py'
src = open(p, encoding='utf-8').read()

anchor = """    results.append({
        "name": "关系类型校验",
        "pass": not bad_edges,
        "detail": "关系均符合本体 domain/range" if not bad_edges else f"{len(bad_edges)} 条非法: {'、'.join(bad_edges[:6])}"})
    return results"""

new_block = anchor + """


def cardinality_check(conn) -> list:
    \"\"\"P1 精确多重性校验（2026-09-10）：关系类型 min/max 基数 vs 图库实际出边/入边计数。

    数据源：权威表 entities/relations（与 SHACL 数据图同源）。
    card_src：源端类的每个实例发出的该关系数量；card_tgt：目标端类每个实例接收的数量。
    返回 [{name, pass, detail}]，与 consistency_check 同形。
    \"\"\"
    import json as _json
    from collections import Counter
    results = []
    rtypes = {}
    for r in conn.execute("SELECT name, constraints FROM ontology_types "
                          "WHERE type_kind='relation'").fetchall():
        try:
            cons = _json.loads(r["constraints"] or "{}")
        except Exception:
            continue
        av = cons.get("allowed_values") or {}
        srcs = av.get("src") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        tgts = av.get("tgt") or []
        if isinstance(tgts, str):
            tgts = [tgts]
        cs = cons.get("card_src") or {}
        ct = cons.get("card_tgt") or {}
        _cs_ok = isinstance(cs, dict) and (cs.get("min") is not None or cs.get("max") is not None)
        _ct_ok = isinstance(ct, dict) and (ct.get("min") is not None or ct.get("max") is not None)
        if _cs_ok or _ct_ok:
            rtypes[r["name"]] = (set(srcs), set(tgts),
                                 cs if _cs_ok else {}, ct if _ct_ok else {})
    if not rtypes:
        results.append({"name": "基数校验", "pass": True,
                        "detail": "无基数约束（在关系类型中栏「基数」设置 min/max 后即启用）"})
        return results
    etype = {e["name"]: (e["entity_type"] or "") for e in conn.execute(
        "SELECT name, entity_type FROM entities WHERE status!='deprecated'").fetchall()}
    eid2name = {e["id"]: e["name"] for e in conn.execute("SELECT id, name FROM entities").fetchall()}
    out_cnt, in_cnt = Counter(), Counter()
    for rel in conn.execute("SELECT source_id, target_id, relation_type FROM relations "
                            "WHERE status!='deprecated'").fetchall():
        rt = str(rel["relation_type"] or "")
        if rt in rtypes:
            out_cnt[(rt, rel["source_id"])] += 1
            in_cnt[(rt, rel["target_id"])] += 1
    viol, checked = [], 0

    def _check(pool, cnt, rt, card, tag):
        nonlocal checked
        mn, mx = card.get("min"), card.get("max")
        for nm in pool:
            checked += 1
            c = cnt.get(nm, 0)
            if (mn is not None and c < int(mn)) or (mx is not None and c > int(mx)):
                viol.append(f"{nm} {tag} {rt}×{c}"
                            f"（要求 {mn if mn is not None else 0}..{mx if mx is not None else '∞'}）")

    for rt, (srcs, tgts, cs, ct) in rtypes.items():
        if cs:
            cnt_by_name = Counter()
            for (r2, sid), n in out_cnt.items():
                if r2 == rt:
                    cnt_by_name[eid2name.get(sid, sid)] += n
            _check([n for n, t in etype.items() if t in srcs] if srcs else list(cnt_by_name.keys()),
                   cnt_by_name, rt, cs, "出边")
        if ct:
            cnt_by_name = Counter()
            for (r2, tid), n in in_cnt.items():
                if r2 == rt:
                    cnt_by_name[eid2name.get(tid, tid)] += n
            _check([n for n, t in etype.items() if t in tgts] if tgts else list(cnt_by_name.keys()),
                   cnt_by_name, rt, ct, "入边")
    results.append({"name": "基数校验", "pass": not viol,
                    "detail": (f"已核对 {checked} 个实例的基数，全部满足" if checked else "无可核对实例")
                              if not viol else f"{len(viol)} 处违反: {'、'.join(viol[:6])}"})
    return results"""

assert anchor in src, 'C1 anchor missing'
src = src.replace(anchor, new_block, 1)

# run_reasoning 接线
wire = """    checks += consistency_check(conn, triples)"""
assert wire in src, 'C2 anchor missing'
src = src.replace(wire, wire + "\n    checks += cardinality_check(conn)", 1)
open(p, 'w', encoding='utf-8').write(src)
print('C ontology_reasoning.py OK')

# ── D. 中栏 UI：关系类型「基数」区块 + ontSaveCard ──
p = 'static/js/mods/21-ontology.js'
src = open(p, encoding='utf-8').read()

anchor2 = """    h+=`<div class="ont-sec">值域 <span style="letter-spacing:0;">Range</span></div><div>${rngArr.map(x=>ontChipHtml(':'+x,'',`ontDelDomRange('tgt','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAdd('rng',this)" title="添加值域">＋</span></div>`;"""
add = anchor2 + """
    // P1 精确多重性（2026-09-10）：min/max → SHACL minCount/maxCount + 推理「基数校验」
    const _csrc=(cons.card_src||{}), _ctgt=(cons.card_tgt||{});
    const _cInp='border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;width:100%;box-sizing:border-box;';
    h+=`<div class="ont-sec">基数 <span style="letter-spacing:0;">Cardinality · 精确多重性</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="源端=该关系的每个源实例发出的边数；目标端=每个目标实例接收的边数（SHACL inversePath）。min/max 留空=不限。保存后自动生成 sh:minCount/sh:maxCount 形状，推理面板「基数校验」即时消费">ⓘ</span></div>
      <div style="display:grid;grid-template-columns:auto 1fr 1fr;gap:4px 8px;align-items:center;font-size:11.5px;max-width:340px;">
        <span style="color:var(--mut);">源端（发出）</span>
        <input id="ont-cs-min" type="number" min="0" step="1" value="${_csrc.min ?? ''}" placeholder="min（空=0）" style="${_cInp}">
        <input id="ont-cs-max" type="number" min="0" step="1" value="${_csrc.max ?? ''}" placeholder="max（空=∞）" style="${_cInp}">
        <span style="color:var(--mut);">目标端（接收）</span>
        <input id="ont-ct-min" type="number" min="0" step="1" value="${_ctgt.min ?? ''}" placeholder="min（空=0）" style="${_cInp}">
        <input id="ont-ct-max" type="number" min="0" step="1" value="${_ctgt.max ?? ''}" placeholder="max（空=∞）" style="${_cInp}">
      </div>
      <div style="margin-top:4px;"><span class="ont-add" onclick="ontSaveCard()" title="写入 constraints.card_src/card_tgt，SHACL 形状与推理基数校验即时消费">💾 保存基数</span></div>`;"""
assert anchor2 in src, 'D1 anchor missing'
src = src.replace(anchor2, add, 1)

# ontSaveCard（挂在 ontDelDomRange 附近）
anchor3 = "async function ontPutType(t, constraints, parentId, extra){"
assert anchor3 in src, 'D2 anchor missing'
src = src.replace(anchor3, """// P1 精确多重性：保存关系类型基数（constraints.card_src/card_tgt）
async function ontSaveCard(){
  const t=ontCurType(); if(!t || t.type_kind!=='relation') return;
  const g=id=>{ const v=(document.getElementById(id)||{}).value; return (v===''||v===null||v===undefined)?null:parseInt(v,10); };
  const cs={min:g('ont-cs-min'), max:g('ont-cs-max')};
  const ct={min:g('ont-ct-min'), max:g('ont-ct-max')};
  if(cs.min!==null && cs.max!==null && cs.min>cs.max){ toast('源端 min 不能大于 max'); return; }
  if(ct.min!==null && ct.max!==null && ct.min>ct.max){ toast('目标端 min 不能大于 max'); return; }
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  if(cs.min===null && cs.max===null) delete cons.card_src; else cons.card_src=cs;
  if(ct.min===null && ct.max===null) delete cons.card_tgt; else cons.card_tgt=ct;
  const ok=await ontPutType(t, cons);
  if(ok) toast('✅ 基数已保存——SHACL minCount/maxCount 形状与「基数校验」即时生效');
}
""" + anchor3, 1)

# 图谱侧信息行（1068/1137 附近）展示结构化基数
old107 = "${cons.cardinality?'（'+esc(cons.cardinality)+'）':''}"
new107 = "${(cons.card_src||cons.card_tgt)?'（基数: 源 '+(cons.card_src?(cons.card_src.min??0)+'..'+(cons.card_src.max??'∞'):'—')+' · 目标 '+(cons.card_tgt?(cons.card_tgt.min??0)+'..'+(cons.card_tgt.max??'∞'):'—')+'）':(cons.cardinality?'（'+esc(cons.cardinality)+'）':'')}"
assert old107 in src, 'D3 anchor missing'
src = src.replace(old107, new107, 1)
old1137 = "+row('基数',esc(cons.cardinality||'—'))"
new1137 = "+row('基数', (cons.card_src||cons.card_tgt) ? ('源 '+(cons.card_src?(cons.card_src.min??0)+'..'+(cons.card_src.max??'∞'):'—')+' / 目标 '+(cons.card_tgt?(cons.card_tgt.min??0)+'..'+(cons.card_tgt.max??'∞'):'—')) : esc(cons.cardinality||'—'))"
assert old1137 in src, 'D4 anchor missing'
src = src.replace(old1137, new1137, 1)
open(p, 'w', encoding='utf-8').write(src)
print('D 21-ontology.js OK')
