# ─────────────────────────────────────────────────────────────────────────────
# mbse_system 容器镜像（2026-10-04 新增）
#
# 为什么现在才做：评估报告 §6 P1-5「无 Dockerfile」——此前只能靠
# "本机 python main.py" 起服务，等于没有可交付的部署单元。
#
# ⚠️ 三条硬约束（都来自本工程的真实形态，不是通用模板套话）：
#
# 1) **随包运行时不在 git 里**，因此**不能**在镜像里 COPY：
#      checker.jar      126.79 MB（OMG 官方 SysML v2 校验器）
#      java-runtime/    375 MB（JDK 25）
#      sysml.library/   7.69 MB（SysML v2 标准库）
#    做法：声明为 volume 挂载 + 启动前检查（见 entrypoint）。
#    后果（必须讲清）：**不挂载这三样，SysML v2 校验与视图投影功能不可用**，
#    其余功能正常。这是有意的降级，好过构建出一个 10+ GB 的镜像。
#
# 2) **数据在 SQLite，且是单写者**：因此 volume 挂载单个 db 文件所在目录，
#    绝不用容器内路径；多副本会各自写一份库 —— 见 compose 里明确的单副本约束。
#
# 3) **离线/私有化**：前端库已本地化到 static/vendor/（无 CDN 依赖），
#    故镜像内不需要任何外网访问；LLM/Embedding 的出网由部署方通过环境变量注入。
# ─────────────────────────────────────────────────────────────────────────────

# ── 构建阶段：只留依赖，用完即弃（最终镜像不含 pip/编译工具链）──────────────
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
# 依赖单独一层：改代码时这一层命中缓存，重建只需几秒
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ── 运行阶段 ────────────────────────────────────────────────────────────────
FROM python:3.13-slim

# 时区设为 Asia/Shanghai：否则容器内 CURRENT_TIMESTAMP 与业务口径（UTC）混淆
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    MBSE_DB_PATH=/data/mbse.db \
    MBSE_HOST=0.0.0.0 \
    MBSE_PORT=8012

# curl 用于 HEALTHCHECK；libxml2/libxslt 供文档解析（PDF/Office 链路）依赖
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libxml2 libxslt1.1 tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# 应用代码（.dockerignore 已排除 .venv/tmp/backups/随包运行时/数据）
COPY . /app

# 数据与随包运行时：都走挂载，不进镜像
VOLUME ["/data", "/opt/mbse-runtime"]

# 非 root 运行：容器逃逸时的影响面小一档
# ⚠️ 2026-10-04（CI 首次真跑暴露）：`VOLUME` 只是**声明**挂载点，构建时**不会创建目录**，
#   所以原写法 `chown -R mbse:mbse /app /data` 必然失败：
#     chown: cannot access '/data': No such file or directory
#   ⇒ docker build 直接 exit 1（本机无 docker，只能靠 CI 暴露；这是本仓第一处
#     "静态门禁查不出、只有真构建能抓"的缺陷）。
#   修法：**先 mkdir 再 chown**，且用 `|| true` 兜住"目录已存在"（幂等，重建不炸）。
RUN useradd -m -u 10001 mbse \
 && mkdir -p /data /opt/mbse-runtime \
 && chown -R mbse:mbse /app /data /opt/mbse-runtime
USER mbse

EXPOSE 8012

# 健康检查打的是真实端点；连续失败 3 次才判 unhealthy（启动要建表+预热，别太敏感）
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${MBSE_PORT}/api/monitor/orphan-runs" || exit 1

ENTRYPOINT ["python", "main.py"]
