# Mirofish Relay

Mirofish Relay 是一个面向本地或自托管环境的多账号中转服务，提供 Anthropic 与 OpenAI
兼容接口、账号管理、会话亲和路由、Mihomo 代理池，以及内置的中文 WebUI。

> 本项目不是 Mirofish 官方项目。请遵守上游服务条款，仅管理你有权使用的账号与代理，
> 不要公开分享账号凭证、代理订阅、主密钥或本地代理密钥。

## 主要功能

- 多账号邮箱验证码登录、状态查看与本地凭证管理。
- Anthropic-compatible `/v1/messages`，支持真实 SSE 流式传输。
- OpenAI-compatible `/v1/chat/completions`，支持流式响应、工具调用与图片消息。
- `/v1/messages/count_tokens` token 计数接口。
- 会话亲和与配额感知路由，让同一对话持续使用同一账号。
- Mihomo 代理池：账号固定节点、失败自动轮换、多槽位并发出口。
- 加密凭证存储：容器内使用 scrypt + AES-256-GCM。
- 中文 WebUI：账号、额度、用量、代理池与模型调用测试。
- 单容器 Docker 部署，Relay 与 Mihomo 一起运行。

## 工作方式

```text
Anthropic / OpenAI 客户端
            │
            ▼
    FastAPI Relay + WebUI
       │       │
       │       └── 加密凭证与 SQLite 元数据
       │
       ├── 账号选择与会话亲和
       └── 固定代理节点与失败轮换
                    │
                    ▼
               Mihomo 代理池
                    │
                    ▼
               Mirofish 上游
```

## Docker 快速开始

### 1. 准备配置

```bash
git clone https://github.com/jin-wind/mirofish.git
cd mirofish/deploy/mirofish-relay
cp .env.example .env
```

编辑 `.env`，至少设置一个长度不少于 16 字符的主密钥：

```dotenv
MIROFISH_MASTER_KEY=请替换为随机且足够长的主密钥
```

可以使用以下命令生成随机值：

```bash
openssl rand -base64 32
```

如需使用代理池，再填写 Mihomo 订阅地址：

```dotenv
MIROFISH_PROXY_SUBSCRIPTION_URL=https://example.com/sub?token=...
```

不配置订阅时，服务会以直连模式运行。

### 2. 启动服务

```bash
docker compose up -d --build
docker compose logs -f mirofish
```

管理页面：

```text
http://127.0.0.1:8787/
```

### 3. 获取本地代理密钥

为避免 bearer 密钥进入持久化日志，服务启动时不会打印完整值。请从仅容器用户可读的
数据卷文件获取：

```bash
docker compose exec mirofish cat /data/proxy.key
```

在 WebUI 输入密钥后，即可添加账号、查看额度、管理代理池并测试模型调用。

更完整的 Docker、静态订阅、升级和故障排查说明见
[部署文档](deploy/mirofish-relay/README.md)。

## API 调用

所有接口都需要本地代理密钥，以下三种鉴权方式均可：

- `X-Mirofish-Proxy-Key: <proxy-key>`
- `X-Api-Key: <proxy-key>`
- `Authorization: Bearer <proxy-key>`

### Anthropic Messages

```bash
curl -N http://127.0.0.1:8787/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'X-Mirofish-Proxy-Key: <proxy-key>' \
  -d '{
    "model": "gpt-5.6-luna",
    "max_tokens": 128,
    "stream": true,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### OpenAI Chat Completions

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <proxy-key>' \
  -d '{
    "model": "gpt-5.6-luna",
    "stream": true,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

如需指定账号，可增加请求头：

```text
X-Mirofish-Account: main
```

未指定账号时，服务会按照默认账号、会话亲和和配额感知轮询规则自动选择账号。
OpenAI 兼容请求省略 `model` 时使用 `MIROFISH_DEFAULT_MODEL`。旧客户端发送
`claude-haiku-4-5-20251001` 时会规范化为 `claude-haiku-4-5`；`/v1/models` 中出现某个模型
不代表上游此刻一定有后端容量，遇到 `no upstream available for model` 时请切换模型。

## 常用接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 服务与账号概况 |
| `GET` | `/accounts` | 账号列表 |
| `GET` | `/accounts/<alias>/status` | 账号状态 |
| `GET` | `/accounts/<alias>/limits` | 单账号额度窗口 |
| `GET` | `/api/limits` | 批量查询账号额度 |
| `GET` | `/proxies` | 代理池状态 |
| `POST` | `/api/proxies/refresh` | 刷新代理池 |
| `GET` | `/v1/models` | 模型列表 |
| `POST` | `/v1/messages` | Anthropic Messages |
| `POST` | `/v1/messages/count_tokens` | Token 计数 |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions |
| `GET` | `/api/usage?hours=24` | 用量统计 |

## 关键配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MIROFISH_MASTER_KEY` | 无 | 加密凭证的主密钥，至少 16 字符 |
| `MIROFISH_DEFAULT_ACCOUNT` | 空 | 强制使用的默认账号别名 |
| `MIROFISH_DEFAULT_MODEL` | `gpt-5.6-luna` | OpenAI 兼容请求未提供模型时使用的上游模型 ID |
| `MIROFISH_RELAY_BASE` | `https://relay.mirasim.ai` | 官方客户端当前使用的模型 relay 地址 |
| `MIROFISH_PROXY_SUBSCRIPTION_URL` | 空 | Mihomo 代理订阅地址 |
| `MIROFISH_PROXY_REFRESH_SECONDS` | `600` | 代理池刷新间隔 |
| `MIROFISH_PROXY_FAILURE_THRESHOLD` | `2` | 节点停用前的连续失败次数 |
| `MIROFISH_MIHOMO_SLOTS` | `8` | 独立代理出口槽位数量 |
| `MIROFISH_SESSION_TTL` | `1800` | 会话亲和有效期，单位为秒 |

完整配置项及示例见
[`deploy/mirofish-relay/.env.example`](deploy/mirofish-relay/.env.example)。

## 本地开发

要求：Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 20+。

```bash
uv sync

cd webui
npm ci
npm run build
cd ..

uv run mirofish serve --host 127.0.0.1 --port 8787
```

常用命令：

```bash
uv run mirofish add main --email you@example.com
uv run mirofish list
uv run mirofish status main
uv run mirofish models main
uv run mirofish remove main
```

运行测试：

```bash
uv run pytest -q
cd webui && npm run build
```

## 数据与安全

Docker 数据默认保存在 `mirofish-data` 数据卷：

- `/data/secrets.enc`：加密后的账号 token 与设备私钥。
- `/data/accounts.sqlite3`：账号元数据、代理绑定和用量日志。
- `/data/proxy.key`：调用本地 API 所需的代理密钥。
- `/data/mihomo/`：Mihomo 配置与 provider 缓存。

请注意：

- 丢失 `MIROFISH_MASTER_KEY` 后，已有加密凭证无法恢复。
- 不要将 `.env`、订阅链接、验证码、token 或代理密钥提交到 Git。
- 默认 Docker 配置监听 `0.0.0.0:8787`。公网部署必须增加 TLS 与额外鉴权，
  或将端口改为只监听 `127.0.0.1`。
- 账号“资料+额度”使用 `/v1/limits`，不产生模型调用；显式模型扫描仍会发送最小工作请求，
  可能消耗少量额度。
- 删除账号只会清除本地数据，不会注销远端账号

## 项目结构

```text
mirofish/                 Python 服务与内置静态 WebUI
webui/                    Vue 3 + TypeScript 前端源码
tests/                    pytest 测试
deploy/mirofish-relay/    Docker Compose 部署文件与详细文档
```
