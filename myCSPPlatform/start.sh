#!/bin/bash
# CSP Platform 管理腳本

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

COMPOSE_FILE="docker/docker-compose.yml"
ENV_FILE=".env"

check_env() {
    if [ ! -f "$ENV_FILE" ]; then
        echo -e "${YELLOW}找不到 .env 檔案，從範本建立...${NC}"
        cp .env.example "$ENV_FILE"
        echo -e "${GREEN}已建立 .env，請先編輯配置再啟動：${NC}"
        echo "  vim .env"
        exit 1
    fi
}

case "${1:-help}" in
    up|start)
        check_env
        echo -e "${GREEN}啟動 CSP Platform...${NC}"
        docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build
        echo ""
        echo -e "${YELLOW}等待服務就緒...${NC}"
        until curl -sfk https://localhost/health > /dev/null 2>&1; do
            printf '.'
            sleep 2
        done
        echo ""
        echo -e "${GREEN}服務已就緒${NC}"
        echo ""
        echo "存取位址："
        echo "  管理平台: https://localhost (透過 Nginx)"
        echo "  健康檢查: https://localhost/health"
        echo "  API 文件: https://localhost/docs"
        echo ""
        echo "管理指令："
        echo "  ./start.sh logs    - 查看日誌"
        echo "  ./start.sh status  - 查看狀態"
        echo "  ./start.sh down    - 停止服務"
        ;;

    down|stop)
        echo -e "${YELLOW}停止 CSP Platform...${NC}"
        docker compose -f "$COMPOSE_FILE" down
        echo -e "${GREEN}服務已停止${NC}"
        ;;

    restart)
        echo -e "${YELLOW}重啟 CSP Platform...${NC}"
        docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down
        docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build
        echo -e "${GREEN}服務已重啟${NC}"
        ;;

    logs)
        echo -e "${BLUE}查看日誌 (Ctrl+C 退出)...${NC}"
        docker compose -f "$COMPOSE_FILE" logs -f ${2:+--tail=100 "$2"}
        ;;

    status)
        echo -e "${BLUE}=== CSP Platform 容器狀態 ===${NC}"
        echo ""
        docker compose -f "$COMPOSE_FILE" ps
        echo ""
        echo -e "${BLUE}=== Nginx 連線狀態 ===${NC}"
        docker compose -f "$COMPOSE_FILE" exec nginx nginx -t 2>&1 || true
        ;;

    build)
        check_env
        echo -e "${GREEN}建構 CSP Platform 映像...${NC}"
        docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --no-cache
        echo -e "${GREEN}建構完成${NC}"
        ;;

    shell)
        echo -e "${BLUE}進入 CSP 後端容器...${NC}"
        docker compose -f "$COMPOSE_FILE" exec csp /bin/bash
        ;;

    *)
        echo -e "${BLUE}CSP Platform 管理腳本${NC}"
        echo ""
        echo "用法: $0 <指令>"
        echo ""
        echo "指令："
        echo "  up, start    啟動所有服務"
        echo "  down, stop   停止所有服務"
        echo "  restart      重啟所有服務"
        echo "  logs [服務]  查看日誌（可指定服務名稱）"
        echo "  status       查看容器狀態"
        echo "  build        重新建構映像"
        echo "  shell        進入後端容器 shell"
        echo ""
        echo "範例："
        echo "  $0 up              # 啟動平台"
        echo "  $0 logs csp        # 只看 CSP 後端日誌"
        echo "  $0 logs nginx      # 只看 Nginx 日誌"
        ;;
esac
