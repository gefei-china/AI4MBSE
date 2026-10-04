# -*- coding: utf-8 -*-
"""P1-5 容器化：静态门禁（**不需要 Docker**）—— 2026-10-04。

━━━ 为什么是静态门禁而不是真构建 ━━━
本机 `docker` 命令不存在（实测 `docker: command not found`），无法 `docker build` 验证。
于是交付拆成两半，并在文件里写死这条边界：
  ① 本脚本能验的：构建上下文体量、关键指令、非 root、健康检查、密钥不入镜像、
     compose 与 Dockerfile 的口径一致、随包运行时的挂载声明；
  ② 本脚本**验不了**的（必须由 CI 或有 Docker 的机器补）：镜像能否真正构建、
     容器内服务能否起来、挂载后 SysML 校验是否可用。
把"验过的"和"没验过的"分开写，比给一个跑不起来的 build 脚本更有价值。

本脚本锁定的不变式：
    C1  Dockerfile 存在且为多阶段（builder + 运行阶段，工具链不进最终镜像）
    C2  以**非 root** 运行（容器逃逸时影响面小一档）
    C3  有 HEALTHCHECK，且打的是**真实端点**（不是 / 也不是不存在的路径）
    C4  **随包运行时不被 COPY 进镜像**（checker.jar 126MB / java-runtime 375MB /
        sysml.library 7.7MB 都不在 git 里，COPY 必然构建失败）
    C5  .dockerignore 排除了本机最大的几块（tmp 7.7G / backups 1.8G / .venv 398M /
        java-runtime 375M / *.db）—— 漏一条就是构建上下文爆炸
    C6  数据库走 volume 挂载，且 compose 里**没有** replicas>1（SQLite 单写者）
    C7  密钥不入镜像（.dockerignore 排 .env / *.key / secrets/）
    C8  compose 里的 DB 路径与 Dockerfile 的 ENV 一致（两处口径分叉会让人白跑）
    C9  compose 引用的 Dockerfile 真实存在（防改名后 compose 指向空气）

**变异自证**：把排除项去掉 / 改成 root 运行 / 去掉 HEALTHCHECK，脚本必须判 FAIL。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_containerization.py
退出码：全绿 0 / 有失败 1。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


def _read(name):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return ""
    return open(p, encoding="utf-8").read()


def c1_c4_dockerfile():
    print("\n=== C1~C4 Dockerfile ===")
    src = _read("Dockerfile")
    if not _rec("C1a Dockerfile 存在", bool(src)):
        return False
    ok = _rec("C1b 多阶段构建（builder + 运行阶段）",
              "FROM python" in src and src.count("FROM ") >= 2, f"FROM x{src.count('FROM ')}")
    ok &= _rec("C1c 依赖装在 builder 的 venv 里（最终镜像不含编译工具链）",
               "/opt/venv" in src and "pip install -r requirements.txt" in src)
    ok &= _rec("C2  以非 root 运行（USER 非 root）", _is_nonroot(src),
               "未找到非 root USER 指令")
    # C2b（2026-10-04，由 CI 首次真实 docker build 暴露）：
    #   原写法 `RUN useradd ... && chown -R mbse:mbse /app /data` 在**真实构建时必然失败**：
    #   `VOLUME ["/data", ...]` 只是**声明**挂载点，构建阶段不会创建该目录
    #   ⇒ `chown: cannot access '/data': No such file or directory` → build exit 1。
    #   而本静态门禁当时是全绿的 —— **这类缺陷只有真构建能抓**。
    #   现在把"chown 的目标目录必须先被创建"锁成断言。
    _chown_targets = []
    for _m in re.finditer(r"chown\s+-R\s+\S+\s+((?:/\S+\s*)+)", src):
        _chown_targets += _m.group(1).split()
    _vol = re.search(r"VOLUME\s+\[([^\]]*)\]", src)
    _vol_paths = re.findall(r'"([^"]+)"', _vol.group(1)) if _vol else []
    # mkdir 可以一次声明多个：`mkdir -p /data /opt/mbse-runtime`
    # ⇒ 必须解析**参数集合**，不能对每个目标做子串匹配（第一版就是这么错的：
    #    `/opt/mbse-runtime` 明明在同一行，却报"缺 mkdir"）。
    _mkdir_paths = set()
    for _m in re.finditer(r"mkdir\s+(?:-\w+\s+)*((?:/\S+\s*)+)", src):
        _mkdir_paths |= set(_m.group(1).split())
    _missing = [t for t in _chown_targets
                if t in _vol_paths and t not in _mkdir_paths]
    ok &= _rec("C2b chown 的挂载点目标已先 mkdir（VOLUME 不会在构建期建目录）",
               not _missing,
               "chown 目标=%s；已 mkdir=%s；缺=%s"
               % (sorted(set(_chown_targets)), sorted(_mkdir_paths), _missing))
    ok &= _rec("C3a 有 HEALTHCHECK（行首有效指令，注释不算）", _has_healthcheck(src))
    ok &= _rec("C3b 健康检查打的是真实端点 /api/monitor/orphan-runs",
               "/api/monitor/orphan-runs" in src,
               "必须是已存在的端点，否则 unhealthy 恒为真")
    # C4：随包运行时绝不能出现在 COPY 里
    ok &= _rec("C4a checker.jar 未被 COPY 进镜像",
               not any("COPY" in l and "checker.jar" in l for l in src.splitlines()))
    ok &= _rec("C4b java-runtime 未被 COPY 进镜像",
               not any("COPY" in l and "java-runtime" in l for l in src.splitlines()))
    ok &= _rec("C4c sysml.library 未被 COPY 进镜像",
               not any("COPY" in l and "sysml.library" in l for l in src.splitlines()))
    ok &= _rec("C4d 声明了随包运行时挂载点 /opt/mbse-runtime",
               "/opt/mbse-runtime" in src, "不声明则挂载点无契约")
    return ok


def c5_c7_dockerignore():
    print("\n=== C5/C7 .dockerignore ===")
    src = _read(".dockerignore")
    if not _rec("C5a .dockerignore 存在", bool(src)):
        return False
    ok = True
    # 每一项都附实测体积依据（2026-10-04 本机 du -sh）
    for pat, why in [("tmp", "本机 7.7 GB"),
                     ("backups", "本机 1.8 GB"),
                     (".venv", "本机 398 MB"),
                     ("java-runtime", "本机 375 MB"),
                     ("sysml.library", "本机 7.7 MB"),
                     ("*.jar", "checker.jar 126.79 MB"),
                     ("*.db", "库文件走 volume 挂载")]:
        ok &= _rec(f"C5 排除 {pat:<16}（{why}）", _ignores(src, pat))
    for pat in (".env", "*.key", "secrets"):
        ok &= _rec(f"C7 排除 {pat:<16}（密钥不入镜像）", _ignores(src, pat))
    ok &= _rec("C5b 排除了 __pycache__", "__pycache__" in src)
    return ok


def c6_c9_compose():
    print("\n=== C6/C8/C9 docker-compose.yml ===")
    src = _read("docker-compose.yml")
    if not _rec("C9a docker-compose.yml 存在", bool(src)):
        return False
    ok = _rec("C9b compose 指向的 Dockerfile 真实存在", os.path.exists(os.path.join(ROOT, "Dockerfile")))
    ok &= _rec("C6a 数据走 volume 挂载", "./docker-data:/data" in src or "/data" in src)
    ok &= _rec("C6b 随包运行时以只读挂载（ro）", ":ro" in src, "可写挂载会让容器改坏本机运行时")
    # 单副本：compose 里出现未被注释的 replicas>1 即失败
    bad_replicas = [l for l in src.splitlines()
                    if "replicas" in l and not l.strip().startswith("#")]
    ok &= _rec("C6c 未配置 replicas>1（SQLite 单写者，多副本会分叉）",
               not bad_replicas, f"发现 {bad_replicas}")
    ok &= _rec("C6d 显式写明单副本约束（注释里）", "单副本" in src)
    df = _read("Dockerfile")
    env_df = _env_vars(df).get("MBSE_DB_PATH", "")
    env_cp = ""
    for l in src.splitlines():
        if "MBSE_DB_PATH=" in l:
            env_cp = l.split("MBSE_DB_PATH=")[-1].strip()
    ok &= _rec("C8  DB 路径两处一致（Dockerfile ENV vs compose environment）",
               bool(env_df) and bool(env_cp) and env_df == env_cp,
               f"dockerfile={env_df!r} compose={env_cp!r}")
    ok &= _rec("C6e env_file 用 required:false（无 .env 也能起）",
               "required: false" in src)
    ok &= _rec("C6f 声明了挂载 /data 的 volume", 'VOLUME ["/data"' in df or 'VOLUME ["/data"' in df.replace(" ", ""))
    return ok


def _env_vars(dockerfile_src):
    """解析 Dockerfile 的 ENV（含 `\\` 续行）。

    ⚠️ 第一版按"行首是 ENV"去找 `MBSE_DB_PATH`，结果漏掉了 —— 因为本文件把多个变量写在
    一条多行 ENV 里（`ENV A=1 \\` 换行 `B=2`）。**这是我自己写的解析器的 bug，不是被测代码的**。
    """
    out = {}
    # 先把续行拼成一条，再解析 key=value
    joined, buf = [], ""
    for line in dockerfile_src.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
        else:
            joined.append(buf + line)
            buf = ""
    if buf:
        joined.append(buf)
    for stmt in joined:
        s = stmt.strip()
        if not s.startswith("ENV "):
            continue
        for kv in s[4:].split():
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _has_healthcheck(src):
    """真实 Dockerfile 语义：HEALTHCHECK 出现在有效指令行（行首），注释行不算。"""
    for l in src.splitlines():
        s = l.strip()
        if s.startswith("HEALTHCHECK"):
            return True
    return False


def _is_nonroot(src):
    for l in src.splitlines():
        s = l.strip()
        if s.startswith("USER "):
            return "root" not in s.split()[1]
    return False


def _ignores(di_src, pat):
    """某个排除模式是否在 .dockerignore 里生效（行首匹配，兼容 `!` 反选）。"""
    for l in di_src.splitlines():
        s = l.strip()
        if not s or s.startswith("#"):
            continue
        if s == pat or s.endswith("/" + pat) or s == pat.rstrip("/") + "/":
            return True
        if s == pat.rstrip("/"):
            return True
    return False


def m_mutations():
    """变异自证：把检查函数**套用到变异文本上**，要求"原文通过、变异后判红"。

    ⚠️ 第一版写成"字符串里去掉某项 ⇒ 断言该判红"，那种写法自证的是**我自己的算术**，
    不是被测的判据（例如把 `HEALTHCHECK` 替换成 `# HEALTHCHECK` 后，字符串依然存在，
    断言反而自己判红）。**变异检测必须真跑一遍判据函数**。
    """
    print("\n=== 变异自证（对判据函数本身跑变异文本）===")
    di = _read(".dockerignore")
    df = _read("Dockerfile")
    ok = True

    # M1 漏排 tmp/（本机 7.7 GB）
    m1 = di.replace("tmp/", "")
    ok &= _rec("M1 漏排 tmp/ ⇒ 判据判红（原文真通过）",
               _ignores(di, "tmp/") and not _ignores(m1, "tmp/"))
    # M2 漏排 .env（密钥）
    m2 = di.replace(".env", "")
    ok &= _rec("M2 漏排 .env ⇒ 判据判红", _ignores(di, ".env") and not _ignores(m2, ".env"))
    # M3 改成 root 运行
    m3 = df.replace("USER mbse", "USER root")
    ok &= _rec("M3 改成 root 运行 ⇒ 判据判红", _is_nonroot(df) and not _is_nonroot(m3))
    # M4 注释掉 HEALTHCHECK
    m4 = df.replace("HEALTHCHECK --interval", "# HEALTHCHECK --interval")
    ok &= _rec("M4 注释掉 HEALTHCHECK ⇒ 判据判红",
               _has_healthcheck(df) and not _has_healthcheck(m4))
    # M5（2026-10-04 改判）：删掉随包运行时的挂载点声明。
    #   ⚠️ 第一版判据是「自己比自己」（`("/opt/mbse-runtime" in df) and (… not in m5)`）——
    #   那只证明"字符串被替换掉了"，**没证明门禁能抓错** ⇒ 典型的变异自证空转。
    #   改为：把判据函数抽出来，拿变异文本**真跑一遍**。
    def _check_mounts(s: str) -> tuple:
        """判据：VOLUME 必须同时声明 /data 与 /opt/mbse-runtime。"""
        _v = re.search(r"VOLUME\s+\[([^\]]*)\]", s)
        _paths = re.findall(r'"([^"]+)"', _v.group(1)) if _v else []
        missing = [p for p in ("/data", "/opt/mbse-runtime") if p not in _paths]
        return (not missing), ("VOLUME=%s 缺=%s" % (_paths, missing))

    m5 = df.replace('VOLUME ["/data", "/opt/mbse-runtime"]', 'VOLUME ["/data"]')
    _ok_now, _why = _check_mounts(df)
    _ok_mut, _why_mut = _check_mounts(m5)
    ok &= _rec("M5 删掉 /opt/mbse-runtime 挂载点 ⇒ 判据判红（真跑判据函数，非自比）",
               _ok_now and not _ok_mut,
               "现状=%s(%s) / 变异=%s(%s)" % (_ok_now, _why, _ok_mut, _why_mut))

    # M6（2026-04 新增）：去掉 mkdir 还原成原写法 ⇒ C2b 必须判红。
    #   这条对应的是 CI 首次真实 docker build 暴露的真缺陷
    #   （`VOLUME` 不会在构建期建目录 ⇒ `chown /data` 必然失败 ⇒ build exit 1）。
    def _check_mkdir(s: str) -> tuple:
        # 路径 token 只取"以 / 开头、且不含引号/反引号/逗号"的那种，
        # 否则跨行续行（`\`）与反引号会被吃进路径，产生 `/data\` 这种脏值。
        _tok = r"/[A-Za-z0-9_.\-/]+"
        _chown = []
        for _m in re.finditer(r"chown\s+-R\s+\S+\s+((?:%s\s*)+)" % _tok, s):
            _chown += _m.group(1).split()
        _v = re.search(r"VOLUME\s+\[([^\]]*)\]", s)
        _vols = re.findall(r'"([^"]+)"', _v.group(1)) if _v else []
        _mk = set()
        for _m in re.finditer(r"mkdir\s+(?:-\w+\s+)*((?:%s\s*)+)" % _tok, s):
            _mk |= set(_m.group(1).split())
        miss = [t for t in _chown if t in _vols and t not in _mk]
        return (not miss), ("chown=%s mkdir=%s 缺=%s" % (sorted(set(_chown)), sorted(_mk), miss))

    _m6 = df.replace(" \\\n && mkdir -p /data /opt/mbse-runtime \\\n && chown", " \\\n && chown")
    if _m6 == df:      # Dockerfile 换过写法 ⇒ 退回宽松匹配
        _m6 = df.replace("mkdir -p /data /opt/mbse-runtime ", "")
    _ok6, _w6 = _check_mkdir(df)
    _ok6m, _w6m = _check_mkdir(_m6)
    ok &= _rec("M6 去掉 mkdir 还原成原写法 ⇒ C2b 判据判红（真跑判据函数）",
               _ok6 and _m6 != df and not _ok6m,
               "变异是否生效=%s；现状=%s(%s) / 变异=%s(%s)"
               % (_m6 != df, _ok6, _w6, _ok6m, _w6m))
    return ok


def main():
    print("=" * 72)
    print("P1-5 容器化静态门禁（不含 docker build —— 本机无 Docker）")
    print("=" * 72)
    ok = c1_c4_dockerfile()
    ok &= c5_c7_dockerignore()
    ok &= c6_c9_compose()
    ok &= m_mutations()
    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 72)
    print(f"断言总数 {len(_results)}  PASS {len(_results) - len(bad)}  FAIL {len(bad)}")
    print("\n⚠️ 本门禁的边界（务必写进交付说明，别让人误以为镜像已验证）：")
    print("   验过：构建上下文体量 / 关键指令 / 非 root / 健康检查端点 / 密钥排除 / 端口与路径一致")
    print("   没验：docker build 能否成功、容器内服务能否起来、挂载后 SysML 校验是否可用")
    print("        （本机无 docker 命令；需在 CI 或有 Docker 的机器上补 `docker build` + 冒烟）")
    if bad:
        for k, n, d in bad:
            print(f"  {k}  {n}  {d}")
        return 1
    print("结论：静态门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
