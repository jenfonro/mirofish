# Mirofish Relay

Mirofish Relay 是一个面向本地或自托管环境的多账号中转服务，提供 Anthropic 与 OpenAI
兼容接口、账号管理、会话亲和路由、手动管理的固定代理，以及内置的中文 WebUI。

> 本项目不是 Mirofish 官方项目。请遵守上游服务条款，仅管理你有权使用的账号与代理，
> 不要公开分享账号凭证、代理凭证、主密钥或本地代理密钥。
> 协议兼容不等于与官方客户端完全等价，也不保证账号不会被限制或封禁。

## 主要功能

- 多账号邮箱验证码登录、状态查看与本地凭证管理。
- Anthropic-compatible `/v1/messages`，支持真实 SSE 流式传输。
- OpenAI-compatible `/v1/chat/completions`，支持流式响应、工具调用与图片消息。
- Codex Responses 透明代理：`/v1/responses` 与 `/backend-api/codex/responses`。
- `/v1/messages/count_tokens` token 计数接口。
- 会话亲和与调用前额度预检；默认 90% 硬阈值，显式指定账号也不能绕过。
- 固定 HTTP / HTTPS / SOCKS5 代理：手动添加、编辑、绑定与测试，不自动选择或回退。
- 加密凭证存储：容器内使用 scrypt + AES-256-GCM。
- 中文 WebUI：账号、额度缓存、用量、固定代理与手动模型调用。
- 单容器 Docker 部署，仅运行 Relay；日志使用宿主机 journald。

## 工作方式

```text
Anthropic / OpenAI / Codex 客户端
            │
            ▼
    FastAPI Relay + WebUI
       │       │
       │       └── 加密凭证与 SQLite 元数据
       │
       ├── 账号选择、会话亲和与额度硬门禁
       └── 每账号固定出口
                    │
                    ▼
       HTTP / HTTPS / SOCKS5 或明确 direct
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

代理在 WebUI 中手动管理，登录时选择固定节点或明确选择 `direct`（直连）。
不再支持 Mihomo、代理订阅或自动轮换。已有账号绑定缺失、无效或指向已删除节点时会
fail-closed，不会自动选节点或退回直连。

### 2. 启动服务

```bash
docker compose up -d --build
docker compose logs -f mirofish
```

Compose 使用 `journald` 日志驱动；需要宿主机支持 systemd-journald，并配置持久化，
否则容器重建后可查询日志不代表宿主机重启后仍会保留。配置方法见[部署文档](deploy/mirofish-relay/README.md#日志持久化)。

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

在 WebUI 输入密钥后，即可添加账号、查看额度缓存、手动刷新额度、管理固定代理并手动调用模型。

更完整的 Docker、固定代理、升级和日志说明见
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

### Codex Responses（透明转发）

```bash
curl -N 'http://127.0.0.1:8787/v1/responses?beta=true' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <proxy-key>' \
  -d '{
    "model": "gpt-5.6-codex",
    "stream": true,
    "input": "你好"
  }'
```

两个 Responses 路径映射到上游 `/v1/responses`，`/v1/responses/compact` 与
`/backend-api/codex/responses/compact` 映射到上游 `/v1/responses/compact`，`/v1/alpha/search` 与
`/backend-api/codex/alpha/search` 映射到上游 `/v1/alpha/search`；签名用的 pathname 是各自的
上游路径，不会被另一条路径顶替。查询串会保留在 URL 中，但签名只使用 pathname。
gzip / deflate / br / zstd 请求体会先按大小上限解压；无需改写时保留原始 JSON 字节，
需要规范化模型或隔离会话时重新序列化，再以最终正文重算 `Content-Length`、SHA-256 与
Ed25519 签名。调用方的鉴权头和 `x-mirasim-*` 不能覆盖代理生成的字段；
`X-Mirofish-Account` 等 `x-mirofish-*` 是 relay 本地头，不转发上游。
会话/身份协议头与正文中的对应元数据按所选账号确定性改写，其余 Codex 协议头按 blocklist
方式保留顺序。请求兼容所参考的客户端协议：`user-agent` 统一改写为
`MIROFISH_CODEX_USER_AGENT`（默认取自参考抓包：
`mirasim/0.153.4 (Mac OS 26.6.2; x86_64) Apple_Terminal/470.2 (mirasim; 0.1.0)`），`originator`
固定为 `mirasim`，独立 Codex CLI 才会带的 `openai-beta` 与 `accept-encoding` 不转发；调用方的
cookie 一律丢弃，relay 按「账号 × 出口」维护自己的 cookie 罐，把上游（Cloudflare）下发给 relay
域名的 `__cflb` / `_cfuvid` / `__cf_bm` 等按 RFC 6265 规则在 `authorization` 之前回传，作用域
不匹配的 cookie（例如 `Domain=chatgpt.com`）不会回传；Claude 路径与官方 Node 客户端一样不带 cookie。
请求体只带 `prompt`（引用已存储的 prompt）而不带 `model` 也是合法的，本地不会拦下来：
由上游决定，拒绝原样回传。`model` 字段本身仍会校验格式。

Codex 流是原始字节透传，同时会旁路读取 `response.completed` / `response.incomplete` /
`response.failed` 事件里的 `usage`，把 token 数写入用量日志，转发出去的字节不受影响。
上游返回 4xx/5xx 时错误原样回传，但不会给出口节点记成一次成功。

如需指定账号，可增加请求头：

```text
X-Mirofish-Account: main
```

该头仅用于 relay 本地选择账号。未指定时，服务在符合额度与健康门禁的账号中保留会话亲和，
为新会话优先考虑配置的默认账号，再按固定调度规则选择；默认账号不是强制绕过门禁的通道。
OpenAI 兼容请求省略 `model` 时使用 `MIROFISH_DEFAULT_MODEL`。旧客户端发送
`claude-haiku-4-5-20251001` 时会规范化为 `claude-haiku-4-5`。当前上游会把缺少 Claude Agent
SDK system 标记的第三方极简请求误报为 `no upstream available for model`；relay 会仅对缺少该
标记的 `claude-*` 请求补一个独立的兼容块，原有 system 内容会完整保留，官方客户端请求和其他
模型的请求体不会被这项兼容逻辑改写。`/v1/models` 中出现某个模型仍不代表上游此刻一定有后端容量。

`claude-*` 请求还会自动补上官方客户端抓包中的 3 个 prompt cache 断点：Agent SDK 标记块、
最后一个 system 块，以及最后一个 user 轮次的末尾内容块（`tools` 不打断点，与官方一致）。
调用方自己带了任何 `cache_control` 时整体不改动。同时，非 `claude-cli/...` 调用方的请求头会
被补全为完整的官方客户端指纹（`User-Agent`、`x-stainless-*`、`x-app`、`accept-encoding`），
避免出现半真半假、反而更容易识别的指纹；只有 `anthropic-version` 和 `anthropic-beta` 这两个
会改变请求语义的选项保留调用方的值。上游会话标识同时用于 `x-mirasim-session` 和
`x-claude-code-session-id`，格式为裸 v4 UUID，对“同一账号 × 同一对话”保持确定性。
真实 CLI 提供的会话 UUID、Codex session/thread 头及正文会话元数据也会按账号改写；
切换账号后不能继续向上游发送同一会话 ID。消息内容、tool ID 和 `previous_response_id` 不属于会话改写范围。

请求头的顺序与大小写也按抓包写到线上：官方客户端把 `Host`、`Connection` 放在最后，relay 替换了
h11 默认的 Host 前置写法（`mirofish/wire.py`），loopback 测试逐字节核对。签名的控制面 GET
（`/v1/model-roster`）使用小写 `authorization`，只有 `/v1/limits` 额度读取保留大写 `Authorization`。
协议回归测试参考 `tests/fixtures/request_profiles/*_official.json`，这些文件由抓包经
`tools/request_profile.py` 脱敏生成。没有桌面端 behavior 回放、`/events` 遥测、
更新检查、后台 limits/profile/roster 探测或模型扫描。

0.0.303 客户端的设备身份按**每账号**隔离：每个 alias 有自己的 Ed25519 密钥（首次使用时惰性
创建并持久化到 vault）；同一账号重新登录或更新凭据不会擅自轮换设备私钥。机器层面的字段
（arch / os、stainless 版本、locale）仍是全安装共用，看起来始终是同一台机器；分散的只有
`x-mirasim-device`。非 CLI 调用方补全为
同一套抓包确认的 Claude CLI 指纹；真实 `claude-cli/...` 调用方的 arch / os 等字段保持原值。
`x-mirasim-device` 是该账号密钥公钥派生出的 22 字符设备 ID：签名请求与（仅旧版客户端标识
才允许的）降级到账号 token 的请求发出同一个值，只有时间戳、nonce 与签名三个字段是签名路径独有
的。若降级路径改用形状不同的标识（例如 36 字符 UUID），单看这一个字段就能区分两条路径。签名为
`mrs-sig-v2`：签名内容是 method、pathname、时间戳、nonce、设备 ID、客户端版本，以及 bearer
凭证、relay 元数据和请求体三者的 SHA-256 摘要，凭证本身不会进入被签名的记录。0.0.303 的模型
请求拿不到 device ticket 时直接返回 503（`device_session_required`），不会用账号 token 顶替。

0.0.303 还会把模型请求的 relay 元数据封装为 `x-mirasim-enc`：除明文保留的
`x-mirasim-client` 外，session / agent / device / account / locale / call 以及签名字段都不会
以明文头发送。封装格式是临时 X25519 公钥、12 字节 nonce 与 ChaCha20-Poly1305 密文，HKDF-SHA256
使用 `mrs-seal-v1` 作为 info，AAD 绑定 HTTP method 与上游 pathname。relay 使用内置公钥，若上游
轮换可通过 `MIROFISH_MIRASIM_SEAL_PUBLIC_KEY`（或官方兼容的 `MIRASIM_SEAL_PUBKEY`）覆盖；默认
开启，无法封装时请求会 fail-closed，而不会退回明文元数据。旧中转可临时设置
`MIROFISH_MIRASIM_SEAL_METADATA=0`。

默认的协议兼容处理发生在请求头与请求体层面。TLS 模仿默认关闭：ClientHello 由 OpenSSL 生成，官方客户端
是 Electron 的 BoringSSL，cipher 列表、扩展顺序与 GREASE 都属于 TLS 库而非本项目，要对齐 JA3
只能替换 TLS 栈。`upstream.tls_context()` 说明了 Python 能调的两个旋钮，以及为什么 ALPN 扩展
被刻意保留（官方同样带这个扩展，去掉反而更显眼）；`tests/test_tls_profile.py` 在本机回环上抓
ClientHello，依赖升级导致的指纹变化会直接测试失败，而不是悄悄改变。

### TLS 选项与兼容性边界

`MIROFISH_TLS_IMPERSONATE` 默认 `off`。如需可选的 Chromium/BoringSSL 风格 ClientHello，
在本地安装 optional extra 并显式开启：

```bash
uv sync --extra tls-impersonate
# 环境变量（Docker 可写入 .env）
MIROFISH_TLS_IMPERSONATE=chrome136
```

开启后使用 `curl_cffi` 提供的 TLS profile，ALPN 仍为 `http/1.1`，账号继续使用自己明确绑定的
固定代理或 `direct`，不会新增出口或回退路径。Docker 镜像已包含该 extra，但仍默认关闭。
这些兼容选项与回归测试不构成“完全等价官方客户端”或“不封禁”的保证。

## 额度、调度与账号健康

- **按需额度读取**：业务调用前预检 `/v1/limits`，每账号默认缓存 600 秒，成功和失败均占用
  缓存期，并以 singleflight 合并并发；手动刷新或业务遭遇 429 时可额外 `force` 刷新，
  并发强制刷新也会合并。没有后台定时扫描；额度读取失败或相关窗口缺失不是放行依据。
- **硬阈值**：默认 90%，最大 100%。自动分配、默认账号、会话亲和、显式指定账号全部受限，
  相关窗口达到阈值即不能发送业务请求；所有候选账号额度耗尽时本地返回 429，不兜底穿透。

  | 请求模型 | 必须检查的窗口 |
  | --- | --- |
  | Fable（`claude-fable-*`） | `5h`、`7d`、`7d_claude`、`7d_fable` |
  | 其它 Claude | `5h`、`7d`、`7d_claude` |
  | 其它模型 | `5h`、`7d` |

- **分窗口冷却**：额度 429 按 `credit_exhausted_*` 的窗口及实际重置时间处理，不按小时探测恢复。
  Fable 窗口耗尽不连坐其它模型，Claude 窗口耗尽不连坐非 Claude 模型；共同窗口仍约束所有模型。
  普通短暂 429 冷却 60 秒。自动请求可换符合条件的账号，显式请求不换账号，任何错误都不轮换代理。
- **固定调度**：新分配先按 48 小时内的 `7d` 重置时间排序（1 小时分档），同档优先
  `7d_fable` 已用比例更高者，再优先最久未分配者。活跃会话数只用于展示，不作为权重；
  没有三种模式切换。`POST /api/schedule` 只修改硬阈值，不触发额度扫描。
- **资料与登录**：资料刷新只访问 `/auth/me`、`/auth/referral`，不访问 `/me/tenant`，
  不顺带读取额度。登录先保存凭据，资料读取成功后再读额度；额度失败仍保留凭据、已读取套餐、
  `disabled` 状态和代理绑定。
- **健康状态**：已记录的 401 必须重新登录；永久 403 标记为 `suspended`，不自动重试；
  临时 403 使用上游真实恢复 deadline。真正的 `overloaded_error` 503 等一天后只由业务流量
  重试，也可手动执行真实模型调用验证恢复；普通 edge 503 不算额度耗尽或账号级过载。
  资料、limits、model roster、`count_tokens` 成功都不能清除模型健康异常。

`GET /accounts` 和 `GET /api/limits` 只读本地缓存；`POST /api/limits/refresh` 才手动批量刷新
（跳过 disabled/suspended），`GET /accounts/<alias>/limits` 是手动单账号刷新。

### 模型目录与本地短路

明确请求 `GET /v1/models` 时按所选账号读取 signed `/v1/model-roster`，缓存 600 秒，
返回标准 OpenAI `{"object":"list","data":[{"id":"…","object":"model",…}]}` 对象列表。
Claude 条目只有该账号 roster 的 `contextWindow >= 1_000_000` 才提供 `[1m]` 变体；
不做模型 probe，不使用内置列表虚构权限。目录和额度都不证明模型此刻能成功调用。

默认开启的 `max_tokens<=1` 短路在 Messages/OpenAI Chat 路径返回本地合成响应，输入 token
仅作本地估算：不选账号、不读额度/目录、不申请 ticket、不调用上游 `count_tokens` 或模型，
不记录用量也不恢复健康。响应带 `X-Mirofish-Probe: short-circuit` 和
`X-Mirofish-Synthetic: true`，不能用于证明账号或模型可用。

## 常用接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 服务与账号概况 |
| `GET` | `/accounts` | 本地账号列表与额度缓存，不访问上游 |
| `GET` | `/accounts/<alias>/status` | 手动刷新资料，不读取额度 |
| `GET` | `/accounts/<alias>/limits` | 手动刷新单账号额度 |
| `GET` | `/api/limits` | 只读批量额度缓存 |
| `POST` | `/api/limits/refresh` | 手动批刷新额度，跳过 disabled/suspended |
| `GET` | `/proxies` | 固定代理配置与本地状态 |
| `POST` | `/api/proxies`、`/api/proxies/import` | 手动添加节点、导入代理 URI |
| `PATCH` / `DELETE` | `/api/proxies/<id>` | 手动编辑 / 删除节点 |
| `POST` | `/api/proxies/<id>/test` | 手动测试代理网络，不测试模型 |
| `GET` | `/v1/models` | 当前账号的模型目录，不探测模型 |
| `POST` | `/v1/messages` | Anthropic Messages |
| `POST` | `/v1/messages/count_tokens` | Token 计数 |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions |
| `POST` | `/v1/responses` | Codex Responses 原始流式转发 |
| `POST` | `/backend-api/codex/responses` | Codex 原生兼容路径，映射到 `/v1/responses` |
| `POST` | `/v1/responses/compact` | Codex 上下文压缩转发 |
| `POST` | `/backend-api/codex/responses/compact` | Codex 原生兼容路径，映射到 `/v1/responses/compact` |
| `POST` | `/v1/alpha/search` | Codex 检索透传 |
| `POST` | `/backend-api/codex/alpha/search` | Codex 原生兼容路径，映射到 `/v1/alpha/search` |
| `POST` | `/api/accounts/<alias>/enabled` | 面板开关：启用 / 停用账号 |
| `DELETE` | `/api/accounts/<alias>` | 删除账号 |
| `PATCH` | `/api/accounts/<alias>` | 编辑显示名、明确设置 `proxy_id` 或 `direct` |
| `GET` / `POST` | `/api/schedule` | 查看固定策略 / 设置额度硬阈值 |
| `GET` | `/api/usage?hours=24` | 用量统计 |

## 关键配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MIROFISH_MASTER_KEY` | 无 | 加密凭证的主密钥，至少 16 字符 |
| `MIROFISH_DEFAULT_ACCOUNT` | 空 | 新会话优先考虑的账号，不能绕过额度与健康门禁 |
| `MIROFISH_DEFAULT_MODEL` | `gpt-5.6-luna` | OpenAI 兼容请求未提供模型时使用的上游模型 ID |
| `MIROFISH_RELAY_BASE` | `https://relay.mirasim.ai` | 官方客户端当前使用的模型 relay 地址 |
| `MIROFISH_CLAUDE_CLI_USER_AGENT` | `claude-cli/2.1.261 (external, mirasim)` | 为非 CLI 调用方补全的官方客户端 User-Agent |
| `MIROFISH_CODEX_USER_AGENT` | `mirasim/0.153.4 (Mac OS 26.6.2; x86_64) Apple_Terminal/470.2 (mirasim; 0.1.0)` | Codex 路径统一改写成的官方内置 Codex User-Agent |
| `MIROFISH_MIRASIM_CLIENT_VERSION` | `0.0.303` | relay 客户端版本标识 |
| `MIROFISH_MIRASIM_SEAL_PUBLIC_KEY` | 内置 32 字节公钥 | `x-mirasim-enc` 的 X25519 接收公钥 |
| `MIROFISH_MIRASIM_SEAL_METADATA` | `1` | 是否封装模型请求的 relay 元数据 |
| `MIROFISH_QUOTA_CEILING` | `0.90` | 额度硬阈值，范围 `0.10`–`1.0`；面板保存值优先 |
| `MIROFISH_LIMITS_TTL` | `600` | 每账号额度成功/失败缓存秒数，无后台刷新 |
| `MIROFISH_SESSION_TTL` | `1800` | 会话亲和有效期，单位为秒 |
| `MIROFISH_KEEPALIVE_EXPIRY` | `75` | 上游 HTTP/1.1 空闲连接保留秒数 |
| `MIROFISH_MAX_CONNECTIONS` | `100` | 上游连接池总连接上限 |
| `MIROFISH_MAX_KEEPALIVE_CONNECTIONS` | `20` | 上游空闲连接上限 |
| `MIROFISH_STREAM_READ_TIMEOUT` | `600` | 上游流式响应读取超时，单位为秒 |
| `MIROFISH_ONE_TOKEN_SHORT_CIRCUIT` | `1` | `max_tokens<=1` 零上游本地短路，不证明可用性 |
| `MIROFISH_MAX_BODY_BYTES` | `8388608` | 压缩体与解压后请求体的最大字节数 |
| `MIROFISH_TLS_IMPERSONATE` | `off` | 可选 `chrome136` TLS profile；本地需 `tls-impersonate` extra |

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

`status` 只刷新资料；兼容保留的 `--probe` 不再触发额度读取。`models` 只读取模型目录，
不发测试模型请求，`--scan` 已删除。

运行测试：

```bash
uv run pytest -q
cd webui && npm run build
```

在已获授权的协议兼容性排查中，可用脱敏工具从本地 mitmproxy flow 生成请求画像：

```bash
mitmdump -q -nr /path/to/flows -s tools/request_profile.py
python tools/request_profile.py validate tests/fixtures/request_profiles/messages_beta_official.json
```

工具只处理 `mirasim.ai` 域名，输出固定端点、头部顺序、动态字段类型和 JSON 结构；凭证、
会话值、未知路径/查询/头名、正文内容均不会写入画像。原始 flow 不应放入仓库或构建上下文。

## 数据与安全

Docker 数据默认保存在 `mirofish-data` 数据卷：

- `/data/secrets.enc`：加密后的账号 token、每账号 Ed25519 私钥与代理凭证。
  设备标识由各账号私钥派生，不额外落盘。
- `/data/accounts.sqlite3`：账号元数据、代理绑定和用量日志。
- `/data/proxy.key`：调用本地 API 所需的代理密钥。

上述数据路径保持不变；journald 日志在宿主机，不保存在该数据卷内。

请注意：

- 丢失 `MIROFISH_MASTER_KEY` 后，已有加密凭证无法恢复。
- 不要将 `.env`、代理凭证、验证码、token 或代理密钥提交到 Git。
- 默认 Compose 仅发布到 `127.0.0.1:8787`；容器内服务监听 `0.0.0.0`。
  公网部署必须增加 TLS 与额外鉴权。
- 手动资料/额度/目录读取不执行模型生成，但会访问上游；手动真实模型调用可能消耗额度。
- 删除账号只会清除本地数据，不会注销远端账号

## 项目结构

```text
mirofish/                 Python 服务与内置静态 WebUI
webui/                    Vue 3 + TypeScript 前端源码
tests/                    pytest 测试
deploy/mirofish-relay/    Docker Compose 部署文件与详细文档
```
