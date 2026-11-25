"""HTTP 工具副作用档位判定 —— 唯一真源（零外部依赖的纯函数）。

P1 收敛（2026-11-25）：从 routers/studio_parts/tools.py 下沉。
理由：services/capability_admin.py 与 routers 都需要同一判据；
      放在 routers 会让 services 反向依赖外网层。原路径保留 re-export。
"""


_SE_ORDER = {"read": 0, "write": 1, "destructive": 2}


def _derive_http_side_effect(name: str, method: str, user_pick: str) -> tuple:
    """按 HTTP 方法派生副作用档位，返回 `(最终档位, 判据说明)`。

    ## 为什么必须派生，而不是让用户在表单里选

    界面写 `read` 的 `POST` 工具会**绕过写前确认闸**（写类工具要人点头，读类不要）
    ⇒ 用户以为自己在"只读地接一个查询接口"，实际每次调用都在改对方系统的数据。
    这是本类接入最隐蔽的错误：界面上写着 read，日志里也没有异常。

    判据（可靠性递减，命中即止）：
      ① `DELETE` 方法 ⇒ destructive（HTTP 语义本身最硬）
      ② `POST/PUT/PATCH` ⇒ write
      ③ 名字含破坏性词（delete/remove/drop/purge）⇒ destructive
      ④ 名字含写动词（create/update/import/apply…）⇒ write
      ⑤ `GET/HEAD/OPTIONS` ⇒ read
      ⑥ 其它（含空方法）⇒ write（保守：未知方法不按读放行）

    ## 只收紧、不放宽

    用户手选比派生**更严格**时保留用户选择（例如派生 read、用户选 write ——
    他要求每次调用都人工确认，这是合法的收紧）。
    反过来（派生 write、用户选 read）**一律拒绝放宽**。
    与 `tools.py:1127` 的「agent 级 hil_level 作为下限」同一条原则。
    """
    m = str(method or "").strip().upper()
    n = str(name or "").lower()
    derived, rule = "write", "method_unknown"
    if m == "DELETE":
        derived, rule = "destructive", "method_delete"
    elif m in ("POST", "PUT", "PATCH"):
        derived, rule = "write", "method_write(%s)" % m
    elif m in ("GET", "HEAD", "OPTIONS"):
        derived, rule = "read", "method_read(%s)" % m
    if any(k in n for k in ("delete", "remove", "drop", "purge", "destroy")):
        derived, rule = "destructive", rule + "+name_destructive"
    elif derived == "read" and any(
            k in n for k in ("create", "update", "import", "apply", "set", "move",
                             "write", "save", "upload", "send", "post", "put")):
        derived, rule = "write", rule + "+name_write"

    pick = str(user_pick or "").strip().lower()
    if pick not in _SE_ORDER:
        return derived, rule
    if _SE_ORDER[pick] >= _SE_ORDER[derived]:
        # 用户更严格（或相同）⇒ 采纳用户；把"用户选择"记进判据便于审计
        return pick, rule + "+user_stricter" if _SE_ORDER[pick] > _SE_ORDER[derived] else rule
    return derived, rule + "+user_looser_denied"
