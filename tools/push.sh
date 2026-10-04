#!/usr/bin/env bash
# ============================================================================
# push 配方（2026-10-04 实测得出）
# ============================================================================
# 【本机环境的两个特殊性 —— 这两条是当初 push 失败两小时的根因】
#
# 1. **代理只放行普通 GET，不放行 CONNECT 隧道**
#    · 走代理：  curl 普通请求 → 200 / 401（1.2s，正常）
#               git push/ls-remote → `CONNECT tunnel failed, response 502`
#    · 绕代理：  curl → 000 超时；git → `Recv failure: Connection was reset`
#    ⇒ **git 必须绕代理，curl 必须走代理**。两者恰好相反，凭直觉判断必错。
#    ⇒ 解法：`env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy`
#             并加 `http.version=HTTP/1.1`（schannel 在 h2 上会 renegotiate 后被 reset）。
#
# 2. **凭据必须显式注入，不能依赖交互**
#    · Windows 凭据管理器助手 `git-credential-wincred.exe` 里**确实有**有效 token
#      （gh 的 keyring token 失效，但这个是好的 —— 两者是不同存储）。
#    · 但走代理时 helper 与 CONNECT 隧道叠加 ⇒ 拿不到凭据。
#    ⇒ 解法：`http.extraheader="Authorization: Basic <base64(x-access-token:TOKEN)>"`
#      一次性注入，**不落盘、不改 git config**。
#
# 【用法】
#   bash tools/push.sh                     # 推送当前分支
#   bash tools/push.sh --dry-run           # 只查连通性与差异，不推
#
# 【为什么写成脚本】这套配方是"试出来的"，不是"看文档知道的"：
#   直觉（先试绕过代理 / 先试 credential helper）都会失败，且每次失败要花 10+ 分钟。
#   固化成脚本后，换台机器/隔天再来也是一条命令。
# ============================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
GIT_BIN_DIR="/c/Users/gefei/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin"
HELPER="C:/Users/gefei/.workbuddy/binaries/PortableGit/versions/1.2.0/mingw64/bin/git-credential-wincred.exe"
export PATH="$GIT_BIN_DIR:$PATH"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

BRANCH=$(git branch --show-current)

# ── 取凭据（优先 Windows 凭据管理器，其次 gh keyring）────────────────────────
TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | "$HELPER" get 2>/dev/null \
        | sed -n 's/^password=//p')
if [ -z "${TOKEN:-}" ]; then
  TOKEN=$(gh auth token 2>/dev/null || true)
fi
if [ -z "${TOKEN:-}" ]; then
  echo "✗ 拿不到 GitHub 凭据。"
  echo "  可选做法：① 确认 Windows 凭据管理器里有 github.com 的条目"
  echo "            ② gh auth login -h github.com"
  exit 2
fi
AUTH=$(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)

# ── git 的网络参数：绕代理 + HTTP/1.1 + 显式凭据 ────────────────────────────
git_net() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
      GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never \
      git -c credential.helper= \
          -c http.version=HTTP/1.1 \
          -c "http.extraheader=Authorization: Basic $AUTH" "$@"
}

# 带重试的网络调用。
#【为什么必须重试】实测同一条命令在几分钟内出现「成功 → 超时 → 又成功」的波动
#   （直连 github.com:443 有时 21s 连不上，有时 1s 就通）⇒ 单次失败不代表配置错。
#   第一版脚本没有重试，结果 push 其实成功了、后续 fetch 超时，整个脚本报失败，
#   **差点让我误判成"没推上去"**。
git_net_retry() {
  local tries="${RETRY:-4}" i out
  for i in $(seq 1 "$tries"); do
    out=$(git_net "$@" 2>&1)
    if [ $? -eq 0 ]; then
      printf '%s' "$out"
      return 0
    fi
    [ "$i" -lt "$tries" ] && sleep $((i * 3))
  done
  printf '%s' "$out"
  return 1
}

echo "== 分支 $BRANCH  HEAD=$(git rev-parse --short HEAD)"
echo "== 连通性检查（绕代理 + HTTP/1.1 + 显式凭据）"
if ! git_net_retry ls-remote origin "refs/heads/$BRANCH" >/tmp/_pr.txt 2>&1; then
  echo "✗ 仍无法访问远程。原始错误："; head -3 /tmp/_pr.txt
  echo ""
  echo "排查顺序（别凭直觉试）："
  echo "  1) 先用 curl 走代理测普通 GET： curl -s -o /dev/null -w '%{http_code}' https://github.com"
  echo "     —— 有码（200/401）说明代理通，但**CONNECT 隧道被拒**⇒ git 必须绕代理（本脚本已做）"
  echo "  2) 绕代理后 git 报 reset ⇒ 加上 http.version=HTTP/1.1（本脚本已做）"
  echo "  3) 报认证失败 ⇒ 凭据无效，重跑 gh auth login"
  exit 3
fi
REMOTE_SHA=$(awk '{print $1}' /tmp/_pr.txt | head -1)
echo "   远程 $BRANCH = ${REMOTE_SHA:0:12}"

LOCAL_SHA=$(git rev-parse HEAD)
# AHEAD = 本地有、远程没有（待推送）；BEHIND = 远程有、本地没有（会丢东西）
AHEAD=$(git rev-list --count "$REMOTE_SHA..HEAD" 2>/dev/null || echo 0)
BEHIND=$(git rev-list --count "HEAD..$REMOTE_SHA" 2>/dev/null || echo 0)
echo "   待推送 $AHEAD 笔提交 / 落后（远程独有）$BEHIND 笔"

if [ "$BEHIND" != "0" ]; then
  echo "   ⚠️ 远程有本地没有的提交 ⇒ 不能直接推（会丢东西）。先 fetch 并处理："
  git fetch origin 2>&1 | tail -2
  echo "   处理完再重跑本脚本。"
  exit 4
fi

if [ "$DRY" = "1" ]; then
  echo "== dry-run：可安全快进推送 $AHEAD 笔提交。去掉 --dry-run 即执行。"
  exit 0
fi

echo "== 推送（fast-forward，非强推）"
git_net_retry push origin "$BRANCH" 2>&1 | tail -4

# ── 完整性对拍（不信"push 返回 0"就是推全了）──────────────────────────────
echo "== 验证：本地 vs 远程逐个 ref 对拍"
#⚠️ fetch 必须走同一套网络参数：裸 `git fetch` 用默认配置（走代理 ⇒ CONNECT 502），
#   会静默卡到超时。第一版就踩了这个坑 —— dry-run 明明成功了，真跑却挂在这里。
git_net_retry fetch origin 2>/dev/null
L=$(git rev-parse HEAD)
R=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$REMOTE_SHA")
echo "   local  = $L"
echo "   remote = $R"
if [ "$L" = "$R" ]; then
  echo "   ✅ 完全一致（推全了）"
else
  echo "   ❌ 不一致 —— 不要当作成功。差异："
  git log --oneline "$L..$R" 2>/dev/null | head -5
  exit 5
fi