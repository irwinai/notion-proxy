#!/bin/bash
# Notion AI Proxy 一键启动脚本
# 自动启动 Notion（CDP 调试模式）+ 代理服务

set -e

CDP_PORT=${CDP_PORT:-9333}
PROXY_PORT=8100
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  🚀 Notion AI Proxy 一键启动${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# ── 步骤 1: 启动 Notion（CDP 调试模式）──
echo ""
echo -e "${YELLOW}[1/3]${NC} 启动 Notion（CDP 端口: ${CDP_PORT}）..."

# 检查 Notion 是否已经以 CDP 模式运行
if curl -s "http://127.0.0.1:${CDP_PORT}/json" > /dev/null 2>&1; then
    echo -e "${GREEN}  ✅ Notion 已在 CDP 模式运行${NC}"
else
    # 先关闭已有的 Notion 进程（非 CDP 模式的）
    if pgrep -x "Notion" > /dev/null 2>&1; then
        echo -e "${YELLOW}  ⚠️  检测到非 CDP 模式的 Notion，正在关闭...${NC}"
        osascript -e 'quit app "Notion"' 2>/dev/null || true
        sleep 2
    fi

    # 以 CDP 调试模式启动 Notion
    open -a Notion --args --remote-debugging-port=${CDP_PORT}
    echo -e "${YELLOW}  ⏳ 等待 Notion 启动...${NC}"

    # 等待 CDP 端口就绪（最多 30 秒）
    for i in $(seq 1 30); do
        if curl -s "http://127.0.0.1:${CDP_PORT}/json" > /dev/null 2>&1; then
            echo -e "${GREEN}  ✅ Notion 启动成功${NC}"
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo -e "${RED}  ❌ Notion 启动超时，请手动检查${NC}"
            exit 1
        fi
        sleep 1
    done
fi

# ── 步骤 2: 检查依赖 ──
echo ""
echo -e "${YELLOW}[2/3]${NC} 检查 Python 依赖..."

if ! python3 -c "import fastapi, uvicorn, aiohttp, curl_cffi" 2>/dev/null; then
    echo -e "${YELLOW}  ⚠️  缺少依赖，正在安装...${NC}"
    pip3 install -r "${SCRIPT_DIR}/requirements.txt" curl_cffi
else
    echo -e "${GREEN}  ✅ 依赖已就绪${NC}"
fi

# ── 步骤 3: 启动代理服务 ──
echo ""
echo -e "${YELLOW}[3/3]${NC} 启动 Notion AI Proxy..."
echo -e "${CYAN}  📡 代理地址: http://0.0.0.0:${PROXY_PORT}${NC}"
echo -e "${CYAN}  💡 提示: 请确保 Notion 中已打开 AI 聊天页面${NC}"
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

cd "${SCRIPT_DIR}"
exec python3 notion_proxy_http.py
