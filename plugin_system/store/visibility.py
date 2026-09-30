"""可消费性判定：把「已发布 + 已安装 + 已启用」翻译成运行时可见集合。"""
import json
import sqlite3


# ══════════════════════════════════════════════════════════════════════
# P1-6：可消费性判定 —— 「安装即可消费」（2026-09-16 晚）
#
# 语义：一个能力能否被 AI 运行时消费 = 本体已发布 且 已安装 且 已启用。
#   · 安装来源不分贵贱：系统级(user_id=0，内置能力默认可用) 与 个人级(市场安装/自建)等效
#   · 停用(plugin_installs.enabled=0) 与 全局下架(plugins.status≠published) 都使消费失效
# 这使能力中心的「安装 / 停用」成为运行时的真实开关，而不再只是管理面的标签。
# ══════════════════════════════════════════════════════════════════════

# 历史迁移中表名混用（mcps 与 mcp_servers），反查旧表映射前统一归一化
_LEGACY_TABLE_ALIAS = {"mcps": "mcp_servers"}


def consumable_plugin_ids(conn, user=None, any_user=False) -> set:
    """当前上下文可消费的 plugin_id 集合。

    判定 = 本体已发布(status='published') 且「可用性开关为开」。开关按优先级取：
      ① 安装记录 plugin_installs.enabled —— 公共/内置能力走这条
         （系统级 user_id=0 是内置能力的默认可用标记；个人级是"从市场安装"）
      ② **作者自持**：scope='personal' 且 author_id=本人 —— 自建能力无需"安装"这一步
      ③ 显式停用否决：本人存在 enabled=0 的安装记录 → 一律不可消费
         （这样"停用"对自建能力同样有效，不必依赖 ①）

    ⚠️ 为什么需要 ②：install() 只接受 scope='public'（它表达的是"从市场安装"），
    自建的个人能力永远 install 不成功。若判定只看 installs，自己创建的能力就永远
    不可消费，与"安装即可消费"的语义相悖。

    参数：
      user      有 id → 系统级 ∪ 本人；None → 仅系统级安装记录
      any_user  True → 全局装配模式：任一用户的启用安装，或任一作者的已发布自建能力
                （Agent 意图路由池是全系统共享的，用它）
    """
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    if any_user:
        # 全局装配模式：作者对自己自建能力的显式停用应全局生效
        # （私有能力的唯一控制者就是作者，他关掉就该从全局池消失）
        sql = ("SELECT p.plugin_id, p.author_id, p.author_name, "
               "(SELECT MAX(i.enabled) FROM plugin_installs i WHERE i.plugin_id=p.plugin_id) AS inst_on, "
               "(SELECT COUNT(*) FROM plugin_installs i2 WHERE i2.plugin_id=p.plugin_id "
               "  AND i2.enabled=0 AND p.author_id!=0 AND i2.user_id=p.author_id) AS explicit_off "
               "FROM plugins p WHERE p.status='published'")
        params = ()
    elif uid:
        sql = ("SELECT p.plugin_id, p.author_id, p.author_name, "
               "(SELECT MAX(i.enabled) FROM plugin_installs i "
               "  WHERE i.plugin_id=p.plugin_id AND i.user_id IN (0,?)) AS inst_on, "
               "(SELECT COUNT(*) FROM plugin_installs i2 "
               "  WHERE i2.plugin_id=p.plugin_id AND i2.user_id=? AND i2.enabled=0) AS explicit_off "
               "FROM plugins p WHERE p.status='published'")
        params = (uid, uid)
    else:
        sql = ("SELECT p.plugin_id, p.author_id, p.author_name, "
               "(SELECT MAX(i.enabled) FROM plugin_installs i "
               "  WHERE i.plugin_id=p.plugin_id AND i.user_id=0) AS inst_on, "
               "0 AS explicit_off "
               "FROM plugins p WHERE p.status='published'")
        params = ()
    out = set()
    try:
        for r in conn.execute(sql, params).fetchall():
            pid, author_id, author_name, inst_on, explicit_off = r[0], r[1], r[2], r[3], r[4]
            if explicit_off:
                continue                        # 本人显式停用 → 否决（优先于一切）
            if inst_on == 1:
                out.add(pid)                    # ① 有启用中的安装记录（系统级/个人级）
                continue
            if not author_id:
                # ③ 系统所有（author_id=0）= 平台提供。
                #    2026-09-17 修复：此前这里没有分支，导致「历史迁移的私有能力」
                #    （author_id=0 + scope=personal + 无任何安装记录）被静默排除出运行时 ——
                #    graph_db_query / graph_db_stats / graph_db_nlquery / mbse_pull_ingest
                #    等 9 条 Agent 绑定因此全部失效。
                #
                #    ⚠️ 关键区分（否则会把"停用/下架"一起废掉）：
                #      inst_on is None → **完全没有安装记录** = 归属不明 → fail-open 放行
                #      inst_on == 0    → 有记录但被关掉 = 明确停用 → 尊重，排除
                #
                #    ⚠️ 2026-09-29（数据流转审计 · 断层2）fail-open 收窄：
                #      管理员全局卸载平台种子条目（author_name='平台内置'，删 user_id=0 行）
                #      后，inst_on 变 None —— 若继续 fail-open，卸载的能力会被**放回消费集合**
                #      （卸载假成功，AI 照旧调用）。平台种子的"无安装行"只有一种成因：
                #      被全局卸载了 → 必须排除。历史迁移能力（author_name='历史迁移'等）
                #      本来就没有安装行，维持 fail-open 放行（9 条绑定不误伤）。
                if inst_on is None:
                    if (author_name or "") != "平台内置":
                        out.add(pid)
                continue
            if any_user:
                out.add(pid)                    # 全局装配（无用户上下文）：有作者的自建能力视为可用
                continue
            if uid and int(author_id) == int(uid):
                out.add(pid)                    # ② 自建自持：无需"安装"这一步
    except sqlite3.OperationalError:
        return set()          # 未跑迁移的老库：降级为空集（调用方按"无判定"处理）
    return out


def legacy_mapping(conn) -> dict:
    """全量 legacy 映射 {(表, 旧id): plugin_id}。仅含建立了映射的插件。"""
    out = {}
    try:
        rows = conn.execute(
            "SELECT plugin_id, manifest_json FROM plugins WHERE status!='removed'").fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        try:
            rt = (json.loads(r["manifest_json"] or "{}").get("runtime") or {})
        except Exception:
            continue
        tbl = rt.get("legacy_table") or ""
        tbl = _LEGACY_TABLE_ALIAS.get(tbl, tbl)
        lid = rt.get("legacy_id")
        if tbl and lid:
            out[(tbl, lid)] = r["plugin_id"]
    return out


def consumable_filter(conn, user=None, any_user=False):
    """返回判定闭包 (旧表名, 旧id) -> 是否可被运行时消费。

    规则（关键：不误伤旧体系原生能力）：
      · 该记录**没有**插件映射 → 保留（旧体系原生，不归能力中心管）
      · 有映射且插件可消费   → 保留
      · 有映射但插件不可消费 → 排除（未安装 / 已停用 / 已下架）
    同步桥保证映射两侧状态一致，本判定让「停用」对运行时真正生效。
    """
    mapping = legacy_mapping(conn)
    ok = consumable_plugin_ids(conn, user, any_user=any_user)

    def _keep(table: str, lid) -> bool:
        pid = mapping.get((table, lid))
        if not pid:
            return True
        return pid in ok
    return _keep
