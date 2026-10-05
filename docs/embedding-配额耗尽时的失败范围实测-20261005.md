# 「新文档一律入库失败」到底意味着什么 · 2026-10-05

> 米爸问：是**只是没法向量化**，还是**连上传导入都不行**？
> 这个必须实测回答 —— 答案取决于失败发生在管道的哪一步。
> 结论：**文件和元数据都在，能被管理、能被重试，只是搜不到。**

---

## 一、结论先行（实测，2026-10-05）

**"入库失败" = "内容进了库但没有向量，检索不到"，不是"传不上去"。**

管道有 5 步：`解析 → 分块 → 向量化 → 写库 → 摘要`。
配额耗尽发生在**第 3 步（向量化）**，此时第 1、2 步的产物已经落库。

| 环节 | 是否成功 | 产物 |
|---|---|---|
| ① 上传文件 | ✅ 成功 | 源文件副本落`data/uploads/{id}_{name}` |
| ② 文档登记 | ✅ 成功 | `documents` 行（标题/作者/文件大小/lifecycle=`uploaded`）|
| ③ 元数据 | ✅ 成功 | `doc_metadata` 行 |
| ④ 解析文本 | ✅ 成功 | （内存，已用于分块）|
| ⑤ 分块 | ✅ 成功 | 结构化分块（内存中）|
| ⑥ **向量化** | ❌ **失败** | 配额耗尽 ⇒ 抛 `EmbedQuotaExceeded` |
| ⑦ 写 chunks | ⛔ **不执行** | `document_chunks` **0 行** ⇒ **检索不到** |
| ⑧ 摘要 | ⛔ 不执行 | `summary` 空 |

---

## 二、实测证据

### 同步上传（前端默认路径）

```
POST /api/documents/upload
→ HTTP 400
{
  "id": 836,
  "filename": "quota_probe.md",
  "size": 1225,
  "parse_status": "failed",
  "chunk_count": 0,
  "error": "embedding 供应商额度已耗尽，本次入库**未生成语义向量**（已阻断，
            未写入降级向量）。供应商原话：Free quota exhausted. To continue accessing
            the model on a paid basis, please add funds or disable the "use free tier
            only" mode in the management console.请在供应商控制台充值或关闭
            「仅用免费额度」模式后重试；若要临时离线运行，可将
            embedding.on_quota_exhausted 设为 degrade。"
}
```

⚠️ **注意 `id: 836` 已经返回了** —— 文档 ID、文件大小都在。
HTTP 400 表达的是"处理未完成"，不是"请求被拒"。

### 库内盘点（上传后）

```
parse_status     = failed
chunk_count      = 0
lifecycle_status = uploaded← 生命周期还是"已上传"，没被标记为废弃
document_chunks  = 0 行      ← 没有分块 ⇒ 检索不到
doc_metadata     = 1 行      ← 标题/作者都在
源文件副本       = 1 个      ← 供日后重试
```

### 检索侧确认确实搜不到

```
hybrid_search(con, "需求分解为子系统需求")
→ 返回 5 条，全部来自 doc#812
→ 目标文档 834 在结果里？ False
```

---

## 三、为什么这样设计（对比旧行为）

旧行为（`on_quota_exhausted = degrade`）是**静默降级**：
写入 bigram 词面向量、`parse_status = completed`、界面显示"✅ 完成"。

| | 降级（改之前） | 阻断（现在） |
|---|---|---|
| 库里向量 | bigram 词面向量 | **无** |
| 界面显示 | ✅ 完成（**误导**）| ❌ 失败 + 原因 |
| 检索质量 | **被词面污染且不可区分** | 未污染（可重试）|
| 事后能否分辨 | ❌ 不能 | ✅ 能（`parse_status=failed`）|

实测代价：今天入库的 **810 chunk / 3 篇文档全是 `bigram-tf`**，
和此前6459 条真向量混在同一张表里，**无法区分、无法事后筛出**。
⇒ 阻断的代价（暂时搜不到）远小于降级的代价（永久污染且不可知）。

---

## 四、充值后怎么恢复（**不需要重新上传**）

前提全部满足，实测确认：

1. **源文件副本在**（`data/uploads/{id}_{name}`）⇒ 不用重新上传
2. **`documents` 行在** ⇒ 不会产生重复文档
3. **`doc_metadata` 在** ⇒ 标题/作者/标签不丢

三条恢复路径（任选）：

```bash
# ① 界面：文档列表点「重新解析」（走 retry_doc）
#    后端：POST /api/documents/{id}/retry
#    前端：/api/documents/{id}/retry?mode=async（作业队列版）

# ② 批量：把库里 failed 的文档全部重跑一遍
#    （先充值，然后解除熔断）
```

⚠️ **充值后必须解除熔断**：`Embedder._QUOTA_BLOCK_UNTIL` 是**类级**的，
10 分钟 TTL 内即使供应商已恢复也**不再尝试**。
重启进程，或调 `Embedder.reset_quota_block()`。

```bash
# ③ 已污染的 810 chunk（bigram-tf）换回真向量
.venv/Scripts/python.exe tools/audit_embed_degradation.py          # 先看清单
.venv/Scripts/python.exe tools/audit_embed_degradation.py --reembed # 再重算
```

---

## 五、如果需要"先入库、事后补向量"

把策略临时改成降级即可（**代价是上面那张表的右列**）：

```bash
# core/config.py → embedding.on_quota_exhausted = "degrade"
# 或改配置：core/config.py 的 DEFAULT_CONFIG["embedding"]["on_quota_exhausted"]
```

三档语义：

| 值 | 行为 | 适用 |
|---|---|---|
| **`block`**（默认/现在） | 失败，不写向量 | 生产（不污染）|
| `degrade` | 写 bigram 词面向量 | **离线/演示**，会污染检索 |
| `warn` | 留痕+告警但不阻断 | 观察期 |

**建议保持 `block`**，等充值。若你确实需要"先入库、事后补"，我建议改成
**"入库成功但标记为待向量化、检索时排除未向量化的块"**——
这样既不丢内容、也不污染检索，比 `degrade` 干净。要我做的话说一声。
