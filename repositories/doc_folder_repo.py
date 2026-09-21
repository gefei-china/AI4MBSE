"""文档目录树数据访问（2026-09-21，「基于文件的管理」P0-b）。

设计依据：`docs/文档库基于文件的管理-评估与优化方案-20260921.md` §4.1/§4.2（唯一实现依据）。
- 目录树是**逻辑视图**，不映射物理文件系统（`data/uploads/` 维持扁平 `{doc_id}_{safe}`）；
  移动/改名是纯 DB 事务，"文件感"由 UX 层提供。
- 文档是**全局资产**：本域任何表/查询都**不带 `branch`**（该维度已随 §1.1 作废）。
- **`0` 是"未归类"哨兵**，不是 NULL —— 与 `doc_folders.parent_id` 同款。
  理由见 `database/migrations/documents.py::_migrate_doc_folders`：
  SQLite 的 UNIQUE/等值判断在 NULL 上会静默漏判，哨兵值让 `folder_id=0` 的查询是可靠的。

公开方法：
  list_tree()                    目录树（含直接/含子目录两级计数）+ 未归类计数
  create(name, parent_id, ...)   新建（同名/父不存在 → 结构化错误）
  rename(folder_id, new_name)    重命名（级联刷新自身与全部后代的 path）
  delete(folder_id)              **删除整棵子树，其中的文档回到"未归类"**（绝不级联删文档）
  move_document(doc_id, folder_id) 单文档移动
  move_documents(ids, folder_id)   批量移动（逐条校验，任一失败不影响其他，与 batch_transition 同风格）
  assign_path / breadcrumb       供上传落点与面包屑
"""
from repositories.base import BaseRepo

# 未归类哨兵（与 documents.folder_id 的 DDL 默认值一致）
ROOT_FOLDER_ID = 0


def normalize_parent(parent_id) -> int:
    """父目录归一：None/''/'/' → 0（根）。前端传什么形状都收敛到哨兵值。"""
    if parent_id in (None, "", "/", "0", 0):
        return ROOT_FOLDER_ID
    try:
        v = int(parent_id)
    except (TypeError, ValueError):
        return ROOT_FOLDER_ID
    return v if v > 0 else ROOT_FOLDER_ID


def normalize_folder(folder_id) -> int:
    """文档归属目录归一：None/''/'/' → 0（未归类）。"""
    return normalize_parent(folder_id)


class DocFolderRepo(BaseRepo):
    """目录树 CRUD + 文档挂目录。"""

    # ── 查询 ──
    def get(self, folder_id: int) -> dict | None:
        return self.one("SELECT * FROM doc_folders WHERE id=?", (folder_id,))

    def list_all(self) -> list:
        """全部目录（扁平，按 parent+sort+name 稳定序；前端组树）。"""
        return self.rows(
            "SELECT * FROM doc_folders ORDER BY parent_id ASC, sort ASC, name ASC")

    def _direct_counts(self) -> dict:
        """每个目录的**直接**文档数（folder_id 精确匹配）。"""
        return {r["folder_id"]: r["n"] for r in self.rows(
            "SELECT folder_id, COUNT(*) AS n FROM documents GROUP BY folder_id")}

    def uncategorized_count(self) -> int:
        """未归类文档数（folder_id=0）。"""
        return self.scalar(
            "SELECT COUNT(*) FROM documents WHERE folder_id=?", (ROOT_FOLDER_ID,)) or 0

    def list_tree(self) -> dict:
        """目录树 + 计数。

        `doc_count`     = 该目录**直接**挂载的文档数；
        `doc_count_all` = 含全部子目录的累计（"含子目录"视图/搜索要用它）。
        两者都给：只给直接数会让父目录显示 0（用户以为文件丢了），
        只给累计数会让用户点了父目录却看到子目录的文件（"移动错了？"的困惑）。
        """
        folders = self.list_all()
        direct = self._direct_counts()
        by_parent = {}
        for f in folders:
            by_parent.setdefault(f["parent_id"], []).append(f)
        # 自底向上累计（按父链深度倒序处理，避免递归深度问题）
        acc = {}

        def rollup(fid: int) -> int:
            if fid in acc:
                return acc[fid]
            acc[fid] = 0  # 占位防环（数据被手工写坏时不死循环）
            total = direct.get(fid, 0)
            for ch in by_parent.get(fid, []):
                total += rollup(ch["id"])
            acc[fid] = total
            return total

        nodes = []
        for f in folders:
            total = rollup(f["id"])
            nodes.append({
                "id": f["id"], "name": f["name"], "parent_id": f["parent_id"],
                "path": f["path"], "sort": f["sort"], "domain": f["domain"] or "",
                "created_by": f["created_by"] or "", "created_at": f["created_at"],
                "doc_count": direct.get(f["id"], 0),
                "doc_count_all": total,
            })
        return {
            "folders": nodes,
            "uncategorized": self.uncategorized_count(),
            "total_docs": self.scalar("SELECT COUNT(*) FROM documents") or 0,
        }

    def path_of(self, folder_id: int) -> str:
        """物化路径（'/' = 根；'/规范/热管理/'）。"""
        if not folder_id:
            return "/"
        row = self.one("SELECT path FROM doc_folders WHERE id=?", (folder_id,))
        return (row or {}).get("path") or "/"

    def breadcrumb(self, folder_id: int) -> list:
        """面包屑：[{id,name},…] 从根到本目录（含本目录）。供中栏标题栏。"""
        chain, guard = [], 0
        cur = folder_id
        while cur and guard < 64:  # guard：数据损坏成环时不死循环
            row = self.one("SELECT id, name, parent_id FROM doc_folders WHERE id=?", (cur,))
            if not row:
                break
            chain.append({"id": row["id"], "name": row["name"]})
            cur = row["parent_id"]
            guard += 1
        return list(reversed(chain))

    def _descendant_ids(self, folder_id: int) -> list:
        """本目录 + 全部后代 id（物化路径前缀法，一次查询，不递归 SQL）。"""
        p = self.path_of(folder_id)
        if p == "/":
            return []
        rows = self.rows("SELECT id, path FROM doc_folders WHERE path LIKE ?", (p + "%",))
        return [r["id"] for r in rows]

    # ── 写 ──
    def create(self, name: str, parent_id=ROOT_FOLDER_ID, domain: str = "",
               created_by: str = "") -> dict:
        """新建目录。返回 {ok, id, path} 或 {ok:False, error}。

        同名/父不存在都以**结构化错误**返回，不抛异常 —— 调用方（上传预建目录、
        拖拽落点、批量导入）需要把原因回给用户，而不是 500。
        """
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "目录名不能为空"}
        if len(name) > 80:
            return {"ok": False, "error": "目录名过长（≤80 字符）"}
        if "/" in name or "\\" in name:
            # 物化路径用 '/' 分隔，名字里带分隔符会让前缀查询与面包屑失去意义
            return {"ok": False, "error": "目录名不能包含 '/' 或 '\\'"}
        pid = normalize_parent(parent_id)
        if pid != ROOT_FOLDER_ID:
            parent = self.get(pid)
            if not parent:
                return {"ok": False, "error": f"父目录不存在（id={pid}）"}
            base = parent["path"]
        else:
            base = "/"
        try:
            new_id = self.execute(
                "INSERT INTO doc_folders (name, parent_id, path, sort, domain, created_by) "
                "VALUES (?,?,?,?,?,?)",
                (name, pid, (base if base.endswith("/") else base + "/") + name + "/",
                 0, domain or "", created_by or ""))
        except Exception as e:
            # UNIQUE(parent_id, name) 或 ux_doc_folders_name_parent（表达式索引）拦截
            if "UNIQUE" in str(e).upper() or "unique" in str(e):
                return {"ok": False, "error": f"同级下已存在同名目录「{name}」"}
            return {"ok": False, "error": f"新建失败：{e}"}
        row = self.get(new_id)
        return {"ok": True, "id": new_id, "path": (row or {}).get("path", "")}

    def rename(self, folder_id: int, new_name: str) -> dict:
        """重命名目录，并**级联刷新自身与全部后代的 `path`**。

        `path` 是物化路径 —— 只改 name 不刷 path 是最容易漏的一步：
        改了之后「含子目录」前缀查询与面包屑会指向已经不存在的老路径，
        表现为"子目录的文件在父目录视图里查不到"，且**不报任何错**。
        """
        new_name = (new_name or "").strip()
        if not new_name:
            return {"ok": False, "error": "目录名不能为空"}
        if "/" in new_name or "\\" in new_name:
            return {"ok": False, "error": "目录名不能包含 '/' 或 '\\'"}
        cur = self.get(folder_id)
        if not cur:
            return {"ok": False, "error": f"目录不存在（id={folder_id}）"}
        old_path = cur["path"]
        parent_path = self.path_of(cur["parent_id"])
        new_path = (parent_path if parent_path.endswith("/") else parent_path + "/") + new_name + "/"
        if new_path == old_path and cur["name"] == new_name:
            return {"ok": True, "id": folder_id, "path": new_path, "unchanged": True}
        try:
            self.execute("UPDATE doc_folders SET name=? WHERE id=?", (new_name, folder_id))
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                return {"ok": False, "error": f"同级下已存在同名目录「{new_name}」"}
            return {"ok": False, "error": f"重命名失败：{e}"}
        # 级联刷 path：自身 + 后代（用旧前缀重写为新前缀）
        refreshed = 0
        for r in self.rows("SELECT id, path FROM doc_folders WHERE path LIKE ?", (old_path + "%",)):
            self.execute("UPDATE doc_folders SET path=? WHERE id=?",
                         (new_path + r["path"][len(old_path):], r["id"]))
            refreshed += 1
        return {"ok": True, "id": folder_id, "path": new_path, "refreshed": refreshed}

    def delete(self, folder_id: int) -> dict:
        """删除目录**及其整棵子树**；其中的文档**回到「未归类」**。

        ⚠️ **绝不级联删文档**（方案 §4.2 的硬约束）。目录是组织维度，
        删一个"文件夹"顺带毁掉文档是不可接受的数据丢失 —— 与操作系统里
        "删文件夹"的直觉一致（内容物被移出而非销毁）。
        返回被移出的文档数，供前端确认文案使用真实数字。
        """
        cur = self.get(folder_id)
        if not cur:
            return {"ok": False, "error": f"目录不存在（id={folder_id}）"}
        ids = self._descendant_ids(folder_id)
        if not ids:
            ids = [folder_id]
        ph = ",".join("?" * len(ids))
        moved = self.scalar(
            f"SELECT COUNT(*) FROM documents WHERE folder_id IN ({ph})", list(ids)) or 0
        self.execute(
            f"UPDATE documents SET folder_id={ROOT_FOLDER_ID} WHERE folder_id IN ({ph})", list(ids))
        self.execute(f"DELETE FROM doc_folders WHERE id IN ({ph})", list(ids))
        return {"ok": True, "deleted": cur["name"], "deleted_folders": len(ids),
                "moved_to_uncategorized": moved}

    def move_document(self, doc_id: int, folder_id=ROOT_FOLDER_ID) -> dict:
        """把单个文档移动到目标目录（0 = 未归类）。不触碰 chunks/向量。"""
        fid = normalize_folder(folder_id)
        if fid != ROOT_FOLDER_ID and not self.get(fid):
            return {"ok": False, "error": f"目标目录不存在（id={fid}）"}
        if not self.one("SELECT 1 FROM documents WHERE id=?", (doc_id,)):
            return {"ok": False, "error": f"文档不存在（id={doc_id}）"}
        self.execute("UPDATE documents SET folder_id=? WHERE id=?", (fid, doc_id))
        return {"ok": True, "doc_id": doc_id, "folder_id": fid}

    def move_documents(self, doc_ids: list, folder_id=ROOT_FOLDER_ID) -> dict:
        """批量移动（逐条校验，任一失败不影响其他；与 `batch_transition` 同风格）。"""
        fid = normalize_folder(folder_id)
        if fid != ROOT_FOLDER_ID and not self.get(fid):
            return {"ok": 0, "failed": [{"doc_id": None, "reason": f"目标目录不存在（id={fid}）"}]}
        results = {"ok": 0, "failed": []}
        for did in doc_ids:
            r = self.move_document(did, fid)
            if r.get("ok"):
                results["ok"] += 1
            else:
                results["failed"].append({"doc_id": did, "reason": r.get("error", "")})
        return results

    def folder_options(self) -> list:
        """扁平选项列表（含路径标签），供上传落点/移动目标的下拉与树渲染。"""
        return self.rows(
            "SELECT id, name, parent_id, path FROM doc_folders "
            "ORDER BY parent_id ASC, sort ASC, name ASC")
