# Docker 部署指南

## 前置要求

- WSL (Ubuntu/Debian) 或 Linux
- Docker + Docker Compose v2
- 项目 `.env` 文件已配置（API key 等）

## 快速启动

```bash
# 进入项目根目录
cd /mnt/d/git/runTime

# 确保必要目录和文件存在
mkdir -p logs cookies
cp .env.example .env  # 首次部署时，复制后编辑填入 API key

# 构建并后台启动
docker compose -f deploy/docker-compose.yml up --build -d
```

## 常用操作

```bash
# 查看容器状态
docker ps
docker compose -f deploy/docker-compose.yml ps

# 查看实时日志
docker logs openclaw-runtime -f

# 重启服务
docker compose -f deploy/docker-compose.yml restart

# 停止服务
docker compose -f deploy/docker-compose.yml down

# 代码修改后重新构建
docker compose -f deploy/docker-compose.yml up --build -d

# 进入容器调试
docker exec -it openclaw-runtime bash
```

## 端口配置

在 `.env` 中设置 `GATEWAY_PORT`：

```bash
# .env
GATEWAY_PORT=8765    # 宿主机监听端口，默认 8765
```

客户端连接：`ws://localhost:${GATEWAY_PORT}`

## 持久化挂载

| 宿主机路径 | 容器路径 | 说明 |
|------------|----------|------|
| `.env` | `/app/.env` | 配置文件（只读） |
| `logs/` | `/app/logs/` | 日志输出（supervisord + runtime + gateway） |
| `cookies/` | `/app/cookies/` | 京东等 cookie 文件（只读） |
| Docker volume `openclaw-memory` | `/app/data/memory/` | memory 本地存储 |

## 进程管理

容器内使用 supervisord 管理两个进程：

- **runtime** — 代理执行服务（端口 8766，容器内部）
- **gateway** — WebSocket 网关（端口 8765，对外暴露）

任一进程崩溃会自动重启（最多 999 次重试）。

查看 supervisord 状态：
```bash
docker exec openclaw-runtime supervisorctl status
```

手动重启单个进程：
```bash
docker exec openclaw-runtime supervisorctl restart gateway
docker exec openclaw-runtime supervisorctl restart runtime
```

## .env 关键配置

容器内需要的环境变量（在 `.env` 中配置）：

```bash
# 必填：LLM 提供商和密钥
LLM_PROVIDER=qwen
QWEN_API_KEY=your_key
QWEN_MODEL=qwen3-coder-plus

# 容器内路径（固定，不需要改）
LOG_FILE=logs/runtime.log
MEMORY_LOCAL_DIR=/app/data/memory
TAOBAO_COOKIE_FILE=/app/cookies/taobao.json

# 容器内强制无头浏览器
BROWSER_HEADLESS=true

# 端口（影响宿主机映射）
GATEWAY_PORT=8765
```

## 测试连接

启动后从 Windows 或 WSL 测试：

```bash
python scripts/cli_agent_client.py ws://localhost:8765
```

## 故障排查

```bash
# 查看构建日志
docker compose -f deploy/docker-compose.yml build --no-cache 2>&1 | tail -50

# 查看容器内文件
docker exec openclaw-runtime ls -la /app/logs/
docker exec openclaw-runtime cat /app/logs/supervisord.log

# 检查进程是否正常
docker exec openclaw-runtime ps aux

# 检查网络
docker exec openclaw-runtime curl -s http://localhost:8766 || echo "Runtime not responding"
```

## 清理

```bash
# 停止并删除容器
docker compose -f deploy/docker-compose.yml down

# 删除构建的镜像
docker compose -f deploy/docker-compose.yml down --rmi all

# 删除持久化数据（谨慎）
docker volume rm openclaw_openclaw-memory
```
