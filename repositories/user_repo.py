"""用户与角色域 Repository：roles / users 表。

对应 routers/users.py 的全部数据访问。
"""
from repositories.base import BaseRepo


class UserRepo(BaseRepo):
    """用户与角色数据访问。"""

    # ── roles ──
    def list_roles(self) -> list:
        """全部角色（含 user_count）。"""
        return self.rows("""
            SELECT r.*, COUNT(u.id) as user_count FROM roles r
            LEFT JOIN users u ON u.role_id=r.id GROUP BY r.id""")

    def get_role(self, role_id: int) -> dict | None:
        return self.one("SELECT * FROM roles WHERE id=?", (role_id,))

    def get_role_by_name(self, name: str) -> dict | None:
        return self.one("SELECT * FROM roles WHERE name=?", (name,))

    def create_role(self, name: str, description: str, permissions: str) -> None:
        self.execute(
            "INSERT INTO roles (name, description, permissions, type) VALUES (?,?,?,?)",
            (name, description, permissions, "custom"),
        )

    def update_role(self, role_id: int, name: str, description: str, permissions: str) -> None:
        self.execute(
            "UPDATE roles SET name=?, description=?, permissions=? WHERE id=?",
            (name, description, permissions, role_id),
        )

    def delete_role(self, role_id: int) -> None:
        self.execute("DELETE FROM roles WHERE id=?", (role_id,))

    def count_users_by_role(self, role_id: int) -> int:
        return self.count("users", "role_id=?", (role_id,))

    # ── users ──
    def list_users(self) -> list:
        """全部用户（含角色名/类型）。"""
        return self.rows("""
            SELECT u.*, r.name as role_name, r.type as role_type FROM users u
            LEFT JOIN roles r ON u.role_id=r.id""")

    def get_user(self, user_id: int) -> dict | None:
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_user_by_username(self, username: str) -> dict | None:
        return self.one("SELECT * FROM users WHERE username=?", (username,))

    def create_user(self, username: str, display_name: str, department: str,
                    role_id: int | None, workspace: str) -> None:
        self.execute(
            "INSERT INTO users (username, display_name, department, role_id, workspace) VALUES (?,?,?,?,?)",
            (username, display_name, department, role_id, workspace),
        )

    def update_user(self, user_id: int, display_name: str, department: str,
                    role_id: int | None, status: str | None = None,
                    workspace: str | None = None) -> None:
        """更新用户；status/workspace 传 None 表示不修改。"""
        sets, params = ["display_name=?", "department=?", "role_id=?"], [display_name, department, role_id]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if workspace is not None:
            sets.append("workspace=?")
            params.append(workspace)
        params.append(user_id)
        self.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", tuple(params))

    def update_user_status(self, user_id: int, status: str) -> None:
        self.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))

    def delete_user(self, user_id: int) -> None:
        self.execute("DELETE FROM users WHERE id=?", (user_id,))

    def count_conversations_by_user(self, user_id: int) -> int:
        return self.count("conversations", "user_id=?", (user_id,))

    # ── departments ──
    def list_departments(self) -> list:
        """全部部门（含使用人数），按 sort_order 排序。"""
        return self.rows("""
            SELECT d.*, (SELECT COUNT(*) FROM users u WHERE u.department=d.name) as user_count
            FROM departments d ORDER BY d.sort_order, d.id""")

    def get_department(self, dep_id: int) -> dict | None:
        return self.one("SELECT * FROM departments WHERE id=?", (dep_id,))

    def get_department_by_name(self, name: str) -> dict | None:
        return self.one("SELECT * FROM departments WHERE name=?", (name,))

    def create_department(self, name: str, description: str, sort_order: int) -> None:
        self.execute(
            "INSERT INTO departments (name, description, sort_order) VALUES (?,?,?)",
            (name, description, sort_order),
        )

    def update_department(self, dep_id: int, name: str, description: str,
                          sort_order: int, status: str) -> None:
        """更新部门；改名时同步 users.department 历史归属（保持一致性）。"""
        self.execute(
            "UPDATE departments SET name=?, description=?, sort_order=?, status=? WHERE id=?",
            (name, description, sort_order, status, dep_id),
        )

    def delete_department(self, dep_id: int) -> None:
        self.execute("DELETE FROM departments WHERE id=?", (dep_id,))

    def rename_users_department(self, old_name: str, new_name: str) -> None:
        """部门改名时，同步 users.department 历史归属。"""
        self.execute("UPDATE users SET department=? WHERE department=?", (new_name, old_name))

    def count_users_by_department_name(self, name: str) -> int:
        return self.count("users", "department=?", (name,))
