# -*- coding: utf-8 -*-
"""P1 精确多重性：关系类型 card_src/card_tgt → SHACL minCount/maxCount + 推理基数校验 + 中栏 UI"""
import io, sys

# ── A. ontology_semantics.py：shacl_export 支持基数 ──
p = 'ontology_semantics.py'
src = open(p, encoding='utf-8').read()

old_rel_loop = """            rel_tgts: dict = {}   # 关系名 → 目标类名集合
            for r in rels:
                av = r.get("constraints", {}).get("allowed_values", {})
                srcs = av.get("src") or []
                if isinstance(srcs, str):
                    srcs = [srcs]
                if name not in srcs:
                    continue
                tgts = av.get("tgt") or []
                if isinstance(tgts, str):
                    tgts = [tgts]
                rel_tgts.setdefault(r["name"], set()).update(tgts)
            for rname, tgts in sorted(rel_tgts.items()):
                if not tgts:
                    continue
                if len(tgts) == 1:
                    props.append(f"[ sh:path {_rel(rname)} ; sh:class {_cls(next(iter(tgts)))} ]")
                else:
                    # sh:or 内多个 sh:class → 并集语义（pySHACL/Jena 兼容写法）
                    alt = " ".join(f"[ sh:class {_cls(t)} ]" for t in sorted(tgts))
                    props.append(f"[ sh:path {_rel(rname)} ; sh:or ( {alt} ) ]")"""

new_rel_loop = """            rel_tgts: dict = {}   # 关系名 → 目标类名集合
            # P1 精确多重性（2026-09-10）：关系类型 constraints.card_src/card_tgt = {min,max}
            # → 源端类形状 sh:minCount/sh:maxCount（出边）；目标端类经 sh:inversePath（入边）
            rel_cards: dict = {}
            rel_in_cards: dict = {}
            for r in rels:
                av = r.get("constraints", {}).get("allowed_values", {})
                srcs = av.get("src") or []
                if isinstance(srcs, str):
                    srcs = [srcs]
                tgts = av.get("tgt") or []
                if isinstance(tgts, str):
                    tgts = [tgts]
                _rc = r.get("constraints", {}) or {}
                if name in srcs:
                    rel_tgts.setdefault(r["name"], set()).update(tgts)
                    _cs = _rc.get("card_src") or {}
                    if isinstance(_cs, dict) and (_cs.get("min") is not None or _cs.get("max") is not None):
                        rel_cards[r["name"]] = _cs
                if name in tgts:
                    _ct = _rc.get("card_tgt") or {}
                    if isinstance(_ct, dict) and (_ct.get("min") is not None or _ct.get("max") is not None):
                        rel_in_cards[r["name"]] = _ct

            def _count_part(card: dict) -> str:
                part = ""
                try:
                    _mn, _mx = card.get("min"), card.get("max")
                    if _mn is not None and int(_mn) > 0:
                        part += f" ; sh:minCount {int(_mn)}"
                    if _mx is not None:
                        part += f" ; sh:maxCount {int(_mx)}"
                except Exception:
                    pass
                return part

            for rname, tgts in sorted(rel_tgts.items()):
                _body = f"sh:path {_rel(rname)}"
                if len(tgts) == 1:
                    _body += f" ; sh:class {_cls(next(iter(tgts)))}"
                elif tgts:
                    # sh:or 内多个 sh:class → 并集语义（pySHACL/Jena 兼容写法）
                    alt = " ".join(f"[ sh:class {_cls(t)} ]" for t in sorted(tgts))
                    _body += f" ; sh:or ( {alt} )"
                _body += _count_part(rel_cards.get(rname) or {})
                props.append("[ " + _body + " ]")
            # 入边基数：目标类形状上的 incoming 边计数（SHACL inversePath，pySHACL 支持）
            for rname, _card in sorted(rel_in_cards.items()):
                props.append("[ sh:path [ sh:inversePath " + _rel(rname) + " ]" + _count_part(_card) + " ]")"""

assert old_rel_loop in src, 'A1 anchor missing'
src = src.replace(old_rel_loop, new_rel_loop, 1)
open(p, 'w', encoding='utf-8').write(src)
print('A ontology_semantics.py OK')

# ── B. ingest_gate.py：形状缓存签名含 updated_at（基数改动能失效缓存） ──
p = 'ingest_gate.py'
src = open(p, encoding='utf-8').read()
old_sig = """    n_types = conn.execute("SELECT COUNT(*) FROM ontology_types").fetchone()[0]
    sig = f"{n_types}\""""
new_sig = """    _row = conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),'') FROM ontology_types").fetchone()
    sig = f"{_row[0]}:{_row[1]}"   # 含约束/基数变更时间：PUT 约束后缓存立即失效"""
assert old_sig in src, 'B anchor missing'
src = src.replace(old_sig, new_sig, 1)
open(p, 'w', encoding='utf-8').write(src)
print('B ingest_gate.py OK')
