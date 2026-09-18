# -*- coding: utf-8 -*-
"""知识库全链路验证脚本（KB Flow Verify）——演示 / 验收 / 回归复用。

覆盖数据完整流程（米爸要求"一步步验证数据完整流程"）：
  ① 上传文档 → ② 解析完成（pipeline_detail 四阶段 done） → ③ 分块入库（chunks+section+bm25_text）
  → ④ 元数据（标题自动提取） → ⑤ 实体/关系抽取（v2g 候选落库） → ⑥ 批量确认入库（过本体校验）
  → ⑦ chunk↔实体溯源链接 → ⑧ 图谱可见（力导向数据源）

用法：
  python tools/verify_kb_flow.py                 # 默认验证（含批量文档）
  python tools/verify_kb_flow.py --only-batch    # 只跑批量上传+批量确认
  python tools/verify_kb_flow.py --base URL       # 自定义服务地址

退出码：0=全绿，1=有失败项
"""
import argparse
import json
import os
import sys
import time

try:
    import httpx
except ImportError:
    print("[ERR] 需要 httpx: pip install httpx")
    sys.exit(2)

BASE = "http://127.0.0.1:8000"
PASS, FAIL = 0, 0
REPORT = []


def check(name, ok, detail=""):
    global PASS, FAIL
    tag = "✅" if ok else "❌"
    PASS += 1 if ok else 0
    FAIL += 1 if not ok else 0
    REPORT.append((tag, name, detail))
    print(f"  {tag} {name}  {detail}")


def api(method, path, **kw):
    r = httpx.request(method, BASE + path, timeout=30, **kw)
    return r


def make_docs():
    """批量测试文档（多类型，供批量上传/批量确认验证）。"""
    return [
        ("batch_载荷需求.md", "md", "宽带通信载荷采用V波段，支持高速率用户链路，天线采用相控阵体制。转发器实现信号转发功能。".encode()),
        ("batch_热控.md", "md", "热管理系统用于卫星温度调节，采用相变材料，负责整星热控。".encode()),
        ("batch_数据表.csv", "csv", "指标,数值,单位\nEIRP,62,dBW\n带宽,2,GHz\n".encode()),
    ]


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--only-batch", action="store_true")
    args = ap.parse_args()
    BASE = args.base

    print("=" * 64)
    print("知识库全链路验证（KB Flow Verify v1.0）")
    print(f"目标服务: {BASE}   时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)

    # ── 0. 服务健康 ──
    try:
        r = api("GET", "/api/documents")
        check("服务在线 /api/documents", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        check("服务在线", False, str(e)[:100])
        print(f"\n总计: PASS={PASS} FAIL={FAIL}")
        sys.exit(1)

    # ── ① 上传（首个文档，走完整链路）──
    print("\n[步骤1] 上传文档 → 解析管道")
    docs = make_docs()
    fn, ft, content = docs[0]
    files = {"file": (fn, content, "text/markdown")}
    try:
        r = api("POST", "/api/documents/upload", files=files,
                data={"title": "", "author": "验证脚本", "version": "v1.0", "tags": "链路验证"})
        up = r.json()
        check("上传响应 200", r.status_code == 200, f"HTTP {r.status_code} {str(up.get('error',''))[:80]}")
        doc_id = up.get("id")
        check("上传返回 doc_id", bool(doc_id), f"id={doc_id}")
        check("解析完成 chunk_count>0", up.get("parse_status") == "completed" and up.get("chunk_count", 0) > 0,
              f"status={up.get('parse_status')} chunks={up.get('chunk_count')} embed={up.get('embed_version')}")
    except Exception as e:
        check("上传文档", False, str(e)[:120])
        doc_id = None

    # ── ② 解析管道阶段明细 ──
    print("\n[步骤2] 解析管道阶段明细（parse/chunk/embed/insert）")
    if doc_id:
        r = api("GET", "/api/documents")
        d = next((x for x in r.json() if x["id"] == doc_id), None)
        if d:
            pd = json.loads(d.get("pipeline_detail") or "{}")
            ok = all(pd.get(k) == "done" for k in ("parse", "chunk", "embed", "insert"))
            check("四阶段全 done", ok, str(pd))
            check("状态=completed", d.get("parse_status") == "completed", d.get("parse_status"))
        else:
            check("阶段明细", False, "文档未出现在列表")
        # ── ③ 分块与溯源字段 ──
        r = api("GET", f"/api/documents/{doc_id}")
        dd = r.json()
        chunks = dd.get("chunks") or []
        check("分块入库 >0", len(chunks) > 0, f"{len(chunks)} 块")
        check("分块含 section/bm25 字段", all(c.get("section") is not None and c.get("bm25_text") for c in chunks[:2]),
              f"section={chunks[0].get('section','')[:12] if chunks else '-'}")
        check("元数据标题自动提取", bool(dd.get("title")), f"title={dd.get('title')}")

    # ── ④ 抽取（v2g）──
    print("\n[步骤3] 实体/关系抽取（v2g 候选，限定本文档内抽取）")
    try:
        r = api("POST", "/api/knowledge/v2g/extract",
                json={"query": "宽带通信载荷 天线 转发器 热控", "top_k": 5, "doc_id": doc_id})
        vr = r.json()
        batch = vr.get("batch_id")
        n = (vr.get("node_count") or 0) + (vr.get("edge_count") or 0)
        check("抽取返回候选", n > 0, f"batch={batch} nodes={vr.get('node_count')} edges={vr.get('edge_count')} rejected={len(vr.get('rejected') or [])}")
        check("候选落库", bool(batch), batch)
        # ── ⑤ 批量确认入库（勾选所有 pending）──
        r = api("GET", f"/api/knowledge/v2g/candidates?limit=100")
        cands = [c for c in r.json() if c.get("status") == "pending"]
        ids = [c["id"] for c in cands[:10]]
        check("待审候选可列出", len(ids) > 0, f"{len(ids)} 条")
        if ids:
            rr = api("POST", "/api/knowledge/v2g/confirm", json={"batch_id": batch, "candidate_ids": ids})
            cf = rr.json()
            check("批量确认入库", cf.get("confirmed", 0) > 0,
                  f"confirmed={cf.get('confirmed')} linked_chunks={len(cf.get('linked_chunks') or [])}")
    except Exception as e:
        check("抽取/确认", False, str(e)[:120])

    # ── ⑥ chunk↔实体溯源 ──
    print("\n[步骤4] chunk↔实体溯源链接")
    try:
        if doc_id:
            r = api("GET", f"/api/documents/{doc_id}")
            dd = r.json()
            ents = dd.get("linked_entities") or []
            check("文档关联实体可追溯", len(ents) > 0, f"{len(ents)} 个实体")
    except Exception as e:
        check("溯源", False, str(e)[:120])

    # ── ⑦ 图谱可见 ──
    print("\n[步骤5] 图谱可见性（力导向数据源）")
    try:
        r = api("GET", "/api/knowledge/graph")
        g = r.json()
        check("图谱返回实体/关系", isinstance(g.get("entities"), list) and isinstance(g.get("relations"), list),
              f"entities={len(g.get('entities') or [])} relations={len(g.get('relations') or [])}")
        # 抽查刚确认入库的实体（匹配 载荷/天线/转发器/热控）
        names = [e.get("name", "") for e in (g.get("entities") or [])]
        hit = any(k in ("".join(names)) for k in ("载荷", "天线", "转发器", "热控"))
        check("新实体已在图谱中", hit, f"抽查含 载荷/天线/转发器/热控")
    except Exception as e:
        check("图谱", False, str(e)[:120])

    # ── ⑧ 批量能力验证（多文档批量上传）──
    if not args.only_batch:
        print("\n[步骤6] 批量上传（3 文档队列）")
        batch_ok = 0
        try:
            for fn, ft, content in docs[1:]:
                files = {"file": (fn, content, "text/markdown" if ft == "md" else "text/csv")}
                r = api("POST", "/api/documents/upload", files=files,
                        data={"title": "", "author": "验证脚本", "version": "v1.0", "tags": "批量"})
                if r.status_code == 200 and r.json().get("parse_status") == "completed":
                    batch_ok += 1
            check("批量上传 2/2 成功", batch_ok == len(docs) - 1, f"{batch_ok}/{len(docs)-1}")
        except Exception as e:
            check("批量上传", False, str(e)[:120])
        # 批量确认（新抽取批次）
        try:
            r = api("POST", "/api/knowledge/v2g/extract", json={"query": "热控 温度调节 相变材料", "top_k": 5})
            b2 = r.json().get("batch_id")
            r = api("GET", "/api/knowledge/v2g/candidates?limit=100")
            ids = [c["id"] for c in r.json() if c.get("status") == "pending"][:10]
            if ids:
                rr = api("POST", "/api/knowledge/v2g/confirm", json={"batch_id": b2, "candidate_ids": ids})
                check("批量确认（第2批）", rr.json().get("confirmed", 0) > 0,
                      f"confirmed={rr.json().get('confirmed')}")
        except Exception as e:
            check("批量确认2", False, str(e)[:120])

    print("\n" + "=" * 64)
    print(f"验证结束: PASS={PASS}  FAIL={FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
