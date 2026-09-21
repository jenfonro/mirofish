# Mirofish Relay — Docker + WebUI

容器化运行 `mirofish/` Python 包：多账号管理、邮箱验证码登录、凭证加密持久化、
按账号固定 HTTP / HTTPS / SOCKS5 代理、Anthropic-compatible `/v1/messages` 真流式中转、
OpenAI-compatible `/v1/chat/completions` 翻译（含 tool calls / 图片 / 流式）、Codex Responses
透明代理，以及内置 Vue 管理 WebUI。

**单容器只运行 relay**，入口脚本直接启动 Python 服务。不再包含 Mihomo、订阅下载、
节点轮换、behavior 回放、后台资料/额度/roster 探测或模型扫描。
协议兼容不代表与官方客户端完全等价，也不能保证账号不被限制或封禁。

## 快速开始

    cd deploy/mirofish-relay
    cp .env.example .env
    # 编辑 .env，设置 MIROFISH_MASTER_KEY（至少 16 字符，可用 openssl rand -base64 32 生成）
    docker compose up -d --build

打开管理页面：

    http://127.0.0.1:8787/

为避免 bearer 密钥进入 `docker logs` 或外部日志平台，容器不会打印完整代理密钥。
密钥保存在权限为 `0600` 的数据卷文件 `/data/proxy.key`（compose 服务名为 `mirofish`）：

    docker compose exec mirofish cat /data/proxy.key

在 WebUI 中输入密钥后即可：手动管理固定代理、添加账号（选择代理或明确 `direct` →
发送邮箱验证码 → 输入验证码 → 完成登录）、
查看每个账号绑定的节点、套餐资料（套餐层级徽章、到期日与剩余天数、持有人姓名；悬停徽章可见
用户 ID、上游已返回的租户字段、邀请升级进度与各窗口预算，资料只来自 `/auth/me` 与
`/auth/referral`，不请求 `/me/tenant`，不后台刷新）、配额利用率、**用量额度卡片**（展示已缓存的
`/v1/limits` 窗口，包括 5 小时 / 7 天 / 7 天 Claude / 7 天 Fable，以及上游提供时的 30 天窗口：
已用百分比、匀速线平均参照、超前 / 落后、剩余额度、
重置倒计时；不消耗额度。其中 **7 天 Fable 窗口**由上游把所有 fable 模型合并计量、只给一个
总数，卡片会在该窗口下额外列出 `fable-5` 与 `fable-5-1` 各自的请求数与 token 消耗——这份
拆分来自本地用量日志，且只统计当前窗口区间内的记录，因此**跟着窗口重置一起归零**，不需要
清理数据；鼠标悬停可看输入 / 输出 / 缓存读写明细）、近 24 小时用量图表、流式测试调用模型、
删除本地账号。WebUI 支持浅色 / 深色主题，
并内置可开关的 Miku 二次元皮肤（默认开启，顶栏可切回标准皮肤）：皮肤本体是纯配色，
把透明 PNG 角色图放进 `webui/public/miku/`（文件名与尺寸见该目录 README）并重新构建前端后，
顶栏头像、登录页立绘与右下角看板娘才会显示；缺图时自动隐藏，不影响功能。

### 日志持久化

Compose 使用 Docker `journald` 驱动，tag 为 `mirofish-relay`。日志由宿主机管理，
不在 `mirofish-data` 数据卷内；宿主机必须支持 systemd-journald。
仅配置 Docker 日志驱动并不能保证日志跨宿主机重启保留，需要在**宿主机**启用持久化，例如：

```bash
sudo mkdir -p /etc/systemd/journald.conf.d /var/log/journal
printf '[Journal]\nStorage=persistent\n' | sudo tee /etc/systemd/journald.conf.d/mirofish.conf >/dev/null
sudo systemctl restart systemd-journald
sudo journalctl --flush
sudo journalctl CONTAINER_TAG=mirofish-relay -f
```

也可用 `docker compose logs -f mirofish` 查看当前服务日志。保留期和磁盘上限由宿主机
journald 策略控制；重启后的历史可用 `journalctl --list-boots` 核查。日志中不要写入凭据。

## 凭证存储

容器内没有 macOS Keychain，因此使用加密文件后端：

- token、每账号 relay 设备私钥及代理凭证保存在 `/data/secrets.enc`，使用 `MIROFISH_MASTER_KEY` 经 scrypt 派生密钥、
  AES-256-GCM 加密（v2 格式）；旧版单文件 relay 写入的 v1 格式（PBKDF2 + HMAC）
  首次读取时自动迁移为 v2，主密钥不变；
- SQLite `/data/accounts.sqlite3` 只保存元数据（邮箱、plan、上游已返回的租户字段、固定代理绑定、
  额度缓存、调度硬阈值、用量日志），本地代理密钥仍在 `/data/proxy.key`；
- 丢失主密钥将无法解密已有账号凭证，需要重新登录。

模型 relay 还要求设备签名：每个账号各自持久化一把 Ed25519 密钥，首次使用时创建；
同一账号重新登录或更新凭据不自动轮换私钥。按「账号 × 出口」申请约 15 分钟的
device ticket，再为每个请求生成 `mrs-sig-v2` 签名（覆盖 method、pathname、时间戳、nonce、
设备 ID、客户端版本，以及凭证、relay 元数据与请求体的摘要）。机器层面的字段（arch/os、
stainless 版本、locale）仍是全安装共用，看起来始终是同一台机器；分散的只有设备 ID。
私钥与账号 token 一样只进入加密凭证存储，不写入 SQLite 或日志。当前默认客户端标识为
按 0.0.303 客户端静态分析确认的 `0.0.303`。ticket 会提前 120 秒
刷新；申请端点返回 404/501 时缓存 15 分钟“不支持签名”，其他失败按 1–30 秒退避。0.0.272+ 的
模型请求没有 ticket 时直接返回 503（`device_session_required`，处于退避期时附带 `retry_after` 秒数），不会改用
账号 token——那会把中转故障变成对用户自有账号的意外扣费；只有把
`MIROFISH_MIRASIM_CLIENT_VERSION` 固定在 0.0.272 以下时才保留旧的账号 token 降级（不发送伪签名）。
ticket 恢复后自动继续。

当前 0.0.303 的模型请求还会把 relay 自己的 `x-mirasim-*` 元数据（明文保留的
`x-mirasim-client` 除外）封装到 `x-mirasim-enc`。封装使用临时 X25519 + HKDF-SHA256
(`mrs-seal-v1`) + ChaCha20-Poly1305，并把上游 pathname 与 HTTP method 放进 AAD；因此
session、账号、设备和签名字段不会在网络上明文出现。默认公钥内置于 relay，也可用
`MIROFISH_MIRASIM_SEAL_PUBLIC_KEY` 或 `MIRASIM_SEAL_PUBKEY` 覆盖。封装失败会停止该请求，
不会降级为明文；与旧的自托管端点联调时可显式设置 `MIROFISH_MIRASIM_SEAL_METADATA=0`。

Anthropic 请求会保留 `?beta=true` 与白名单内的 Claude SDK 特征头；Codex 的 `/v1/responses`
和 `/backend-api/codex/responses` 都映射到上游 `/v1/responses`；
`/v1/responses/compact` 与 `/backend-api/codex/responses/compact` 映射到上游
`/v1/responses/compact`，`/v1/alpha/search` 与
`/backend-api/codex/alpha/search` 映射到上游 `/v1/alpha/search`；各组路径各自用自己的上游
pathname 签名，保留查询串但签名只包含 pathname。
压缩的 Codex 请求体先有界解压，必要时规范化模型并按账号改写会话元数据，
再以最终精确字节计算长度、哈希和签名；无需改写时保留原始 JSON 字节。调用方的
`Authorization` / `X-Api-Key` 绝不会转发给上游；`X-Mirofish-Account` 等
`x-mirofish-*` 仅供 relay 本地使用。Codex 请求参考客户端协议：
`user-agent` 改写为 `MIROFISH_CODEX_USER_AGENT`（默认为抓包值），`originator` 固定为
`mirasim`，不转发 `openai-beta` 与 `accept-encoding`，调用方 cookie 丢弃，改为回传 relay 自己
按「账号 × 出口」收到的 Cloudflare cookie。所有请求头的顺序与大小写按抓包写到线上（`Host`、
`Connection` 在最后；签名的 `/v1/model-roster` 用小写 `authorization`）。模型流量默认发往
官方客户端当前使用的 `https://relay.mirasim.ai`；旧的 `mirasim-relay.mirofish.ai` 分发可能仍返回
模型目录；当前观察到它可能对同一 Claude 请求返回 `no upstream available for model`。

## 固定代理

只支持手动维护的 HTTP / HTTPS / SOCKS5 端点，可在 WebUI 添加、编辑、导入 URI、
绑定账号或手动测试。导入与查看列表不探测网络；只有明确点击测试才访问网络，
且代理连通成功不代表账号或模型可用。

每个账号持久绑定一个 `proxy_id`，或明确绑定字符串 `direct`。空绑定、无效配置、
已删除节点和旧 Mihomo 节点都不能视为直连：请求会 fail-closed，等待管理员修复。
代理网络失败只记录诊断状态，不自动换出口、不选择其它节点、不回退直连；
额度或区域 429 也不触发代理轮换。后续业务请求仍使用原绑定。
节点编辑保留 ID 和账号绑定；被账号引用的节点必须先手动改绑才能删除。

相关接口：

    GET    /proxies                         # 只读本地节点配置与诊断状态
    POST   /api/proxies                     # {"scheme":"socks5","host":"proxy.example","port":1080}
    POST   /api/proxies/import              # {"text":"socks5://proxy.example:1080\nhttp://proxy.example:8080"}
    PATCH  /api/proxies/<id>                # 手动编辑端点
    DELETE /api/proxies/<id>                # 已绑定节点返回 409，须先改绑
    POST   /api/proxies/<id>/test           # 手动连通性测试，不扫描模型
    PATCH  /api/accounts/<alias>            # {"proxy_id":"<id>"} 或明确 {"proxy_id":"direct"}

订阅刷新接口和订阅/Mihomo 环境变量已经移除。代理凭证属于敏感数据，不要放入源码或日志。

## API

所有接口需要鉴权，三种写法等价：`X-Mirofish-Proxy-Key: <key>`、`X-Api-Key: <key>`、
`Authorization: Bearer <key>`。

    GET    /health
    GET    /accounts                  # 只读本地资料、健康状态与额度缓存
    GET    /accounts/<alias>/status   # 仅 /auth/me + /auth/referral；旧 probe=1 也不读额度
    GET    /accounts/<alias>/limits    # 手动单账号 force 刷新上游 /v1/limits
                                       # 7d_fable 窗口附带 models[]：各 fable 模型本窗口用量
    GET    /api/limits                 # 只读所有账号的额度缓存
    POST   /api/limits/refresh         # 手动批量 force 刷新，跳过 disabled/suspended
    GET    /proxies
    GET    /v1/models                # 明确请求才按需读 signed /v1/model-roster；600 秒缓存
    POST   /v1/messages              # Anthropic Messages；"stream":true 为真 SSE 透传
                                     # max_tokens<=1 由 relay 本地作答，不转发（见下）
    POST   /v1/messages/count_tokens # 额度门禁后转发；不支持/网络故障可本地估算，不能自愈健康
    POST   /v1/chat/completions      # OpenAI 兼容；支持 tools/图片/流式
                                     # 注意：上游以 thinking 模式服务，仅接受 temperature=1
                                     # 且不接受 top_p；其他采样参数会被自动丢弃而非转发
    POST   /v1/responses             # Codex Responses 原始字节/状态/响应头透传
    POST   /backend-api/codex/responses # Codex 原生路径，映射到 /v1/responses
    POST   /v1/responses/compact     # Codex 上下文压缩
    POST   /backend-api/codex/responses/compact # 映射到 /v1/responses/compact
    POST   /v1/alpha/search          # Codex 检索透传
    POST   /backend-api/codex/alpha/search # Codex 原生路径，映射到 /v1/alpha/search
    POST   /api/login/start          # {"alias","email","proxy_id"} 发送验证码；明确选节点或 direct
    POST   /api/login/finish         # {"alias","code"} 完成登录
    POST   /api/accounts/<alias>/enabled # {"enabled":true|false} 面板启用/停用开关
    DELETE /api/accounts/<alias>     # 删除本地账号及凭证
    GET    /api/usage?hours=24       # 用量统计（按小时 × 账号聚合）
    GET    /api/schedule             # 固定策略 reset_first_fable、硬阈值与 limits_ttl
    POST   /api/schedule             # {"max_utilization":0.90}，允许 0.10–1.0，不触发上游刷新

验证码一旦验证成功，access/refresh token 会先写入加密存储，再通过 `/auth/me` 与
`/auth/referral` 读取资料，资料成功后才读取额度。后续资料或额度失败不会要求重复提交已消费的
验证码；响应会带 `profile_error` 或 `limits_error`，资料尚未获得时保留 `profile_pending`。
额度失败仍保留凭据、已获取的套餐、`disabled` 状态和代理绑定。资料刷新与额度刷新是独立操作。

### 额度预检与固定调度

业务调用前按账号预检额度：成功和失败都默认缓存 **600 秒**（`MIROFISH_LIMITS_TTL`），
singleflight 合并同账号并发读取。手动刷新及业务 429 可额外 `force` 刷新，并发强制刷新也合并。
页面 GET 只展示缓存，不因打开页面或保存设置而扫描上游；无缓存、读取失败或缺少相关窗口时
不能据此推断“还有额度”并放行。

硬阈值由 `MIROFISH_QUOTA_CEILING` 提供默认值 **90%**，可在面板保存，最大 **100%**。
任一相关窗口达到阈值就停止分配，而非降低优先级：

| 模型 | 检查窗口 |
| --- | --- |
| Fable（`claude-fable-*`） | `5h`、`7d`、`7d_claude`、`7d_fable` |
| 其它 Claude | `5h`、`7d`、`7d_claude` |
| 其它模型 | `5h`、`7d` |

自动分配、默认账号、会话亲和、`X-Mirofish-Account` 显式指定都不能穿透门禁。
所有候选账号在相关窗口用尽时，本地返回 429，没有“全部用尽继续兜底”。
额度缓存中跨过重置时间的旧窗口不再证明可用，发送业务请求前仍需有效预检。

显式账号头优先；未指定时保留合格账号上的会话亲和，新会话优先考虑
`MIROFISH_DEFAULT_ACCOUNT`，否则使用唯一固定策略：**48 小时内 `7d` 优先重置，按 1 小时
分档 → 同档 `7d_fable` 已用比例更高 → 最久未分配**。更远或未知的重置时间不获重置优先。
没有三种模式或活跃会话数权重，活跃会话数只用于展示。

会话优先使用 `X-Mirofish-Session` 等明确协议会话标识，其次是正文会话元数据，
否则由首条 user 内容推导，忽略共用的 system 提示词。闲置超过 `MIROFISH_SESSION_TTL`
（默认 1800 秒）过期。亲和账号不再满足门禁时，自动请求可改派到合格账号，
显式指定账号则不替换。`X-Mirofish-Account` 是 relay 本地请求/响应头，绝不转发上游；
响应还可包含 `X-Mirofish-Quota-7d-Utilization` / `-Reset`。

### 冷却与健康恢复

- 停用账号保留凭据，但不参与自动调度或手动批量额度刷新；显式业务请求返回 403。
- 业务遇 429 后合并强制刷新额度，再按 `credit_exhausted_*` 的具体窗口冷却至重置；
  不做每小时恢复探测。仅 `7d_fable` 用尽不连坐其它模型，仅 `7d_claude` 用尽不连坐
  非 Claude 模型；`5h`/`7d` 是共同约束。普通短暂 429 冷却 60 秒。
  可自动换合格账号，不能换该账号代理或回退直连。
- 已记录的健康 401 必须重新登录。永久 403 标记 `suspended`，不自动调度或批量刷新；
  临时 403 使用上游返回的真实恢复 deadline，不猜固定冷却时长。
- 仅真正的 `overloaded_error` 503 记录账号级过载：一天后允许实际业务重试，不启动探针；
  也可手动执行真实模型调用，成功完成后恢复健康。普通 edge/HTML 503 不记作额度耗尽
  或账号级过载。
- 资料、limits、model roster、`count_tokens` 成功均不能自愈模型健康，重新登录也不将
  403/503 当作已恢复；打开 HTTP/SSE 流不算模型成功，必须成功完成真实模型响应。

### 模型目录

`GET /v1/models` 明确请求时，使用账号的 signed `/v1/model-roster`，按账号缓存 600 秒；
没有后台 roster 轮询或模型 probe。返回 OpenAI `object: "list"`，
`data` 是含 `id`、`object: "model"`、`created`、`owned_by` 的对象数组，不是字符串数组。
能力只来自该账号的 roster；Claude 条目 `contextWindow >= 1_000_000` 才提供 `[1m]` 变体，
不以静态列表虚构权限，读取失败不伪造成功目录。模型出现在目录里不代表当前有可用容量。

调用模型示例（流式）：

    curl -N http://127.0.0.1:8787/v1/messages \
      -H 'Content-Type: application/json' \
      -H 'X-Mirofish-Proxy-Key: <proxy-key>' \
      -H 'X-Mirofish-Account: main' \
      -d '{"model":"gpt-5.6-luna","max_tokens":128,"stream":true,"messages":[{"role":"user","content":"你好"}]}'

OpenAI 兼容请求省略 `model` 时使用 `MIROFISH_DEFAULT_MODEL`（默认 `gpt-5.6-luna`）。上游模型容量会变化，
`/v1/models` 的目录也不保证每个条目此刻都有可用后端。旧客户端发送
`claude-haiku-4-5-20251001` 时会规范化为当前目录 ID `claude-haiku-4-5`。对于当前上游会误报
`no upstream available for model` 的第三方 Claude 极简请求，relay 会在缺少官方 Claude Agent
SDK system 标记时补一个独立兼容块；原 system 内容保留，官方客户端请求和非 Claude 模型的
请求体不会被这项兼容逻辑改写。

## 从旧版（单文件 relay）升级

数据卷名 `mirofish-data` 与 `/data/secrets.enc`、`/data/accounts.sqlite3`、`/data/proxy.key`
路径不变。SQLite 自动迁移，`secrets.enc` v1 自动升级为 v2；已存在的每账号设备密钥保留，
缺失时首次使用创建。升级不会擅自修复代理绑定：旧 Mihomo 节点、空绑定或损坏配置会拒绝出站，
需在 WebUI 手动配置有效端点并改绑，或明确选择 `direct`。
保留主密钥与数据卷后执行 `docker compose up -d --build`，并在宿主机配置 journald 持久化。

## 注意

- Docker 镜像使用仓库中的 `uv.lock` 做 `--locked --no-dev` 安装；修改
  `pyproject.toml` 依赖后必须同步更新并提交锁文件，否则镜像构建会直接失败。
- Compose 默认端口映射为 `127.0.0.1:8787:8787`，容器内服务仍监听 `0.0.0.0`。
  如主动改为公网监听，必须增加 TLS 与额外鉴权（反向代理 / Cloudflare Access）。
- Mirofish 没有精确余额接口；WebUI 显示套餐层级与到期时间、用量日志与 relay 返回的
  7 天配额利用率。
- `/v1/models` 和模型请求会先申请设备 ticket；如果升级上游协议，可通过
  `MIROFISH_RELAY_BASE` 覆盖默认 relay 地址，通过
  `MIROFISH_MIRASIM_CLIENT_VERSION` 覆盖客户端版本标识，通过
  `MIROFISH_MIRASIM_LOCALE` 覆盖默认的 `zh-HK` locale。
- 调用方自带 `claude-cli/...` User-Agent 时，其 SDK 指纹按抓包顺序原样透传。其他调用方
  （OpenAI 兼容、第三方 SDK）会被补全为完整的官方指纹：`User-Agent`、`x-stainless-*`、
  `x-app: cli`、`accept-encoding: gzip, deflate, br, zstd`，并加上路由所需的
  `anthropic-beta: claude-code-20250219`；`x-claude-code-session-id` 与
  `x-mirasim-session` 取相同值，和官方客户端一致。指纹字段是整体覆盖而不是逐项补默认值，
  避免出现 `lang: python` 与 `runtime: node` 并存这种任何真实客户端都不会发出的组合；
  只有 `anthropic-version` 和 `anthropic-beta` 这两个会改变请求语义的选项保留调用方的值。
  `MIROFISH_CLAUDE_CLI_USER_AGENT` 可覆盖 User-Agent。
- 设备身份按账号隔离：每个账号的 Ed25519 密钥惰性创建并持久保留，同一账号更新凭据
  只使旧授权缓存失效，不擅自 rotate 设备。机器层面的字段仍是全安装共用：
  非 CLI 调用方统一补全为抓包中的
  `arm64 / MacOS` Claude CLI 组合；真实 `claude-cli/...` 调用方的 arch / os 原样保留。
  `x-mirasim-device` 是该账号公钥派生的 22 字符设备 ID，签名与（旧版标识下的）无签名降级发出的是同一个值；
  降级路径不会改用形状不同的替代标识，否则单看这个字段就能区分两条路径。
- 没有桌面端后台行为回放、`/events` 遥测或更新检查；也没有后台资料、额度、roster 刷新。
- `MIROFISH_TLS_IMPERSONATE=off` 为默认值，TLS ClientHello 出自 OpenSSL，官方客户端是 Electron 的
  BoringSSL：cipher 列表、扩展顺序与 GREASE 由 TLS 库决定，要对齐 JA3 得换掉 TLS 栈，
  配置 OpenSSL 做不到。因此 ALPN 扩展被刻意保留（官方也带这个扩展，去掉反而更显眼），
  只有在 Python 3.13+ 上会把 supported_groups 收窄成浏览器那三个曲线，去掉 OpenSSL 3.5
  默认的 X25519MLKEM768 与 ffdhe。回环抓包测试覆盖这些字段。
  Docker 镜像包含 `tls-impersonate` extra，可显式设 `MIROFISH_TLS_IMPERSONATE=chrome136`
  使用可选 Chromium/BoringSSL 风格 profile；这不改变固定代理绑定，也不保证与官方 TLS 完全等价。
- 上游会话标识（`x-mirasim-session`）现在是裸 v4 UUID，不再带 `mirofish_` 前缀：该值同时用作
  `x-claude-code-session-id`。映射包含账号：同账号同会话确定，跨账号不同；
  真实 CLI 的 session 头、Codex session/thread 头与正文会话元数据同样按账号改写。
  消息内容、tool ID、`previous_response_id` 不改写；改写在最终序列化和签名前完成。
- Claude 模型请求会自动补上官方客户端的 prompt cache 断点（Agent SDK 标记块、最后一个
  system 块、最后一个 user 轮次的末尾块，共 3 个，与抓包一致；`tools` 不打断点）。
  调用方自己带了任何 `cache_control` 时整体不改动，因为上游最多只接受 4 个断点。
  最后一个 user 轮次如果是字符串内容，会展开为等价的 text block 以便承载断点。
- 上游固定使用 HTTP/1.1 连接池；`MIROFISH_KEEPALIVE_EXPIRY`（默认 75 秒）、
  `MIROFISH_MAX_CONNECTIONS`（默认 100）和 `MIROFISH_MAX_KEEPALIVE_CONNECTIONS`
  （默认 20）控制连接复用，`MIROFISH_STREAM_READ_TIMEOUT`（默认 600 秒）控制流式读取超时。
  `MIROFISH_MAX_BODY_BYTES`（默认 8388608）同时限制压缩输入与解压后的正文，防止解压炸弹。
- CLI `status <alias>` 只刷新资料，旧 `--probe`/`status?probe=1` 也不读额度。
  `models <alias>` 是目录读取，不是模型探测；`--scan` 已删除。
- `max_tokens<=1` 的 Messages 请求默认完全由本地合成：返回 `content` 为空、
  `stop_reason` 为 `max_tokens` 的合法 Messages 信封，`usage.input_tokens` 只作本地估算。
  不选账号、不做额度/roster 读取、不申请 ticket，也不调用上游 `count_tokens` 或模型。
  响应头带 `X-Mirofish-Probe: short-circuit` 与 `X-Mirofish-Synthetic: true`，
  并记录调用方 `user-agent` 便于定位来源；不计入用量日志，不改变健康状态。
  `/v1/chat/completions` 同理（`max_tokens` 为 0 或 1 都会被翻译成同一形状）。需要恢复原样
  转发时设置 `MIROFISH_ONE_TOKEN_SHORT_CIRCUIT=0`。合成响应、额度读取、目录及 token 计数
  都不证明模型可用；只有真实模型调用成功才是恢复依据。
- 删除账号只清除本地凭证，不注销远端账号。
