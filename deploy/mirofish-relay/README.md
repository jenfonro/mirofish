# Mirofish Relay — Docker + WebUI

容器化运行 `mirofish/` Python 包：多账号管理、邮箱验证码登录、凭证加密持久化、
按账号固定 Mihomo 节点（多槽位并发出口）、Anthropic-compatible `/v1/messages` 真流式中转、
OpenAI-compatible `/v1/chat/completions` 翻译（含 tool calls / 图片 / 流式），以及内置 Vue 管理 WebUI。

relay 与 Mihomo 代理引擎打包在**同一个容器**里，由入口脚本先生成 Mihomo 配置并启动引擎，
再启动 relay（未配置订阅时跳过 Mihomo，纯直连）；任一进程退出即整体重启。不再有独立的
sidecar 与 init 容器。

## 快速开始

    cd deploy/mirofish-relay
    cp .env.example .env
    # 编辑 .env，设置 MIROFISH_MASTER_KEY（至少 16 字符，可用 openssl rand -base64 32 生成）
    # 设置 MIROFISH_PROXY_SUBSCRIPTION_URL；不要把带 token 的链接提交到仓库
    docker compose up -d --build

打开管理页面：

    http://127.0.0.1:8787/

为避免 bearer 密钥进入 `docker logs` 或外部日志平台，容器不会打印完整代理密钥。
密钥保存在权限为 `0600` 的数据卷文件 `/data/proxy.key`（compose 服务名为 `mirofish`）：

    docker compose exec mirofish cat /data/proxy.key

在 WebUI 中输入密钥后即可：配置代理订阅、添加账号（发送邮箱验证码 → 输入验证码 → 完成登录）、
查看每个账号绑定的节点、plan / 配额利用率、**用量额度卡片**（来自上游 `/v1/limits` 的
5 小时 / 7 天 / 30 天窗口：已用百分比、匀速线平均参照、超前 / 落后、剩余额度、重置倒计时；
不消耗额度）、近 24 小时用量图表、流式测试调用模型、删除本地账号。WebUI 支持浅色 / 深色主题，
并内置可开关的 Miku 二次元皮肤（默认开启，顶栏可切回标准皮肤）：皮肤本体是纯配色，
把透明 PNG 角色图放进 `webui/public/miku/`（文件名与尺寸见该目录 README）并重新构建前端后，
顶栏头像、登录页立绘与右下角看板娘才会显示；缺图时自动隐藏，不影响功能。

## 凭证存储

容器内没有 macOS Keychain，因此使用加密文件后端：

- token 和 relay 设备私钥保存在 `/data/secrets.enc`，使用 `MIROFISH_MASTER_KEY` 经 scrypt 派生密钥、
  AES-256-GCM 加密（v2 格式）；旧版单文件 relay 写入的 v1 格式（PBKDF2 + HMAC）
  首次读取时自动迁移为 v2，主密钥不变；
- SQLite `/data/accounts.sqlite3` 只保存元数据（邮箱、plan、租户、用量日志）；
- 丢失主密钥将无法解密已有账号凭证，需要重新登录。

模型 relay 还要求设备签名：每个本地账号首次发起模型请求时生成一个 Ed25519 设备密钥，
并为当前出口申请约 15 分钟的 device ticket，再为每个请求生成 `mrs-sig-v1` 签名。私钥与账号
token 一样只进入加密凭证存储，不写入 SQLite 或日志。当前默认客户端标识为抓包确认的
`0.0.220`。Anthropic 请求会保留 `?beta=true` 与白名单内的 Claude SDK 特征头；查询串不会
错误地并入签名，调用方的 `Authorization` / `X-Api-Key` 也绝不会转发给上游。

## 代理池

容器内置的 Mihomo 引擎负责订阅的下载、解析和建立连接，因此 SS、VMess、VLESS、Trojan、
Hysteria、TUIC 等 Mihomo 支持的节点都可以使用。配置与 provider 缓存保存在数据卷的
`/data/mihomo/` 下，relay 通过容器内回环地址 `127.0.0.1:9090`（控制器）/ `127.0.0.1:7890`（代理）
与引擎通信。

订阅地址由 `.env` 的 `MIROFISH_PROXY_SUBSCRIPTION_URL` 配置；它优先于 WebUI 曾保存的地址。
服务会定期读取 Mihomo 的节点列表，并把每个账号选中的节点 ID 写入 SQLite，因此同一账号会持续
使用同一个出口节点。以下情况会触发重新选择；只有传输层节点故障才累计全局失败次数（达到
`MIROFISH_PROXY_FAILURE_THRESHOLD` 后停用）：

- **节点网络失败**：连接超时、拒绝等传输层错误。
- **上游不服务该账号的当前出口区域**：上游返回 429 `shared_quota_unavailable`（「云端中转未在
  当前网络区域提供服务」）。可用性取决于账号套餐与出口的组合；服务按账号暂记该节点并尝试其他
  出口，不会把节点全局停用。该账号被所有可用出口拒绝后会进入冷却，并自动换用其他账号。
- **共享额度耗尽不是节点故障**：429 `credit_exhausted_shared` 表示该账号当前不能使用上游共享
  额度。服务不会徒劳地轮换或停用代理节点；自动路由会冷却该账号并换用其他账号，显式指定账号
  时则原样返回错误。需要等待额度恢复或按上游提示接入可用的自有账号。
- **节点在订阅更新后消失**：Mihomo 的 provider 自动更新会重命名全部节点，此时切换选择器会被
  控制器以 400 拒绝。服务会立即重新同步节点列表并重新绑定，不必等到下一次定时刷新。

### 多槽位并发出口

入口脚本为 Mihomo 生成 `MIROFISH_MIHOMO_SLOTS`（默认 8）个独立的槽位监听端口
（从 `MIROFISH_MIHOMO_SLOT_BASE_PORT`，默认 7891 起），每个槽位有自己的选择器组。
relay 把每个账号固定到一个槽位，不同账号的上游请求经由各自槽位并发出站，
互不阻塞（旧版为全局选择器 + 全局锁，所有请求串行）。账号数超过槽位数时，
共享同一槽位的账号会在切换节点时短暂串行，以保证账号与出口 IP 的对应关系。
同一槽位切换节点时，relay 会按「槽位 + 节点」隔离 HTTP 连接池与 device ticket，避免复用
旧节点建立的 HTTPS 隧道，造成看似轮换、实际仍从原出口重试。
若引擎仍在运行不含槽位组的旧配置，relay 会自动退回单选择器兼容模式，
重新 `docker compose up -d --build` 后即启用槽位。

订阅请求默认使用 `mihomo/1.19.0` 的 User-Agent；如果你的订阅服务要求特定客户端标识，
可在 `.env` 设置 `MIROFISH_PROXY_SUBSCRIPTION_USER_AGENT` 后重建容器。

如果服务器无法访问订阅站，可改用静态文件：在能够下载订阅的机器保存原始订阅内容，上传到
`deploy/mirofish-relay/mihomo-input/subscription.yaml`，然后在 `.env` 清空
`MIROFISH_PROXY_SUBSCRIPTION_URL` 并设置 `MIROFISH_PROXY_SUBSCRIPTION_FILE=/input/subscription.yaml`。
入口脚本会把它复制到 Mihomo 允许读取的 `/data/mihomo/` 下。该文件含节点凭据，应设置为仅自己
可读且不要提交到版本库；静态文件模式需要手动更新该文件后重启容器。

相关接口：

    GET  /proxies                    # 立即返回缓存状态，不拉取网络
    POST /api/proxies/subscription   # {"url":"https://..."}，保存并刷新（仅直连模式）
    POST /api/proxies/refresh        # 请求 Mihomo 主动更新订阅并读取节点，失败会返回 502/503

如果该接口返回 `503 Mihomo controller request timed out`，说明 relay 到容器内 Mihomo
引擎的控制端口 `127.0.0.1:9090` 无响应；执行 `docker compose logs mirofish` 检查订阅下载和
配置错误（Mihomo 与 relay 的日志都汇入同一容器 stdout）。`MIROFISH_MIHOMO_CONTROLLER_TIMEOUT`
默认 5 秒，可在 `.env` 中按需调整。

## API

所有接口需要鉴权，三种写法等价：`X-Mirofish-Proxy-Key: <key>`、`X-Api-Key: <key>`、
`Authorization: Bearer <key>`。

    GET    /health
    GET    /accounts
    GET    /accounts/<alias>/status[?probe=1]   # probe=1 同时读取 /v1/limits，不产生模型调用
    GET    /accounts/<alias>/limits    # 单账号用量额度窗口（上游 /v1/limits，不计费）
    GET    /api/limits                 # 全部账号并发拉取用量额度（不计费）
    GET    /proxies
    GET    /v1/models                # 按账号缓存 5 分钟
    POST   /v1/messages              # Anthropic Messages；"stream":true 为真 SSE 透传
    POST   /v1/messages/count_tokens # Anthropic token 计数（转发上游，不计费；失败则本地估算）
    POST   /v1/chat/completions      # OpenAI 兼容；支持 tools/图片/流式
                                     # 注意：上游以 thinking 模式服务，仅接受 temperature=1
                                     # 且不接受 top_p；其他采样参数会被自动丢弃而非转发
    POST   /api/login/start          # {"alias","email"} 发送验证码
    POST   /api/login/finish         # {"alias","code"} 完成登录
    POST   /api/accounts/<alias>/enabled # {"enabled":true|false} 面板启用/停用开关
    DELETE /api/accounts/<alias>     # 删除本地账号及凭证
    GET    /api/usage?hours=24       # 用量统计（按小时 × 账号聚合）

验证码一旦验证成功，access/refresh token 会先写入加密存储，再读取套餐、租户等展示资料。
如果后续资料接口临时失败，`/api/login/finish` 仍返回成功并标记 `profile_pending=true`；这样不会因
重复提交已经消费的验证码而出现先 502、后 401。稍后在账号列表点击「刷新」即可补齐资料。

账号选择顺序：请求头 `X-Mirofish-Account` > `MIROFISH_DEFAULT_ACCOUNT` > **会话亲和** > 轮询
（轮询会自动跳过 7 天配额利用率已达 100% 的账号）。响应头返回
`X-Mirofish-Account` 与 `X-Mirofish-Quota-7d-Utilization` / `-Reset`。

**停用与共享额度冷却**：在 WebUI 停用的账号保留凭证但不参与任何自动分配；显式用
`X-Mirofish-Account` 指定它会返回 403。当上游以 `credit_exhausted_shared`（共享额度耗尽）
拒绝某个账号时，该请求会自动换一个账号重试（显式指定的账号不替换），被拒账号进入
10 分钟冷却期，期间自动分配会避开它，其活跃会话也会改派到其他账号；冷却结束后自动
重新尝试。这个错误是账号属性而非出口属性，因此不会触发代理节点轮换。

**按名称排除节点**：设置 `MIROFISH_PROXY_NODE_EXCLUDE`（正则，如 `香港|HK|🇭🇰`）后，
命中的节点在 Mihomo provider（`exclude-filter`）和中转节点列表两层都被排除，完全不
参与分配。正则同时交给 Python 和 Mihomo（Go RE2）使用，请保持简单的字面量/或写法。
修改后需重建容器。

**订阅 DNS 直通**：生成 Mihomo 配置时会抓取订阅并把其中的顶层 `dns:` 段原样并入——
部分机场的节点入口域名只有订阅指定的私有 DNS（`nameserver-policy`）能解析出真实地址，
公共 DNS 返回占位 IP（如 `127.127.127.x`），没有这段配置节点会全部拨号失败。订阅没有
`dns:` 段或启动时抓取失败则不写入，行为与旧版一致。更换订阅后需要重建容器让新的 DNS
生效。

**区域拒绝按账号记忆**：`shared_quota_unavailable`（区域不服务）取决于账号的上游套餐——
plus 账号能用的节点，共享额度账号可能整池被拒。因此区域拒绝只记在「账号 × 节点」维度
（30 分钟），不影响该节点对其他账号的可用性。一个账号被所有出口都拒绝时，按账号冷却
处理并把请求转移到其他账号，而不是继续扫描节点池。

**会话亲和**：同一个对话（窗口）始终路由到同一个账号，不同对话才分配到不同账号——
避免「一个会话被多账号轮流服务」这种明显的中转特征。会话标识按优先级取：请求头
`X-Mirofish-Session` > 请求体 `metadata.user_id` > 首条 user 消息的哈希（对话追加轮次时保持不变，
且刻意忽略 system 提示词，以免所有窗口共用同一提示词而挤到同一账号）。新会话会分配到
当前承载会话最少的账号，从而在账号间铺开。会话在 `MIROFISH_SESSION_TTL`（默认 1800 秒）无活动后过期。
WebUI 账号表的「活跃会话」列可实时看到每个账号正在服务的窗口数。

调用模型示例（流式）：

    curl -N http://127.0.0.1:8787/v1/messages \
      -H 'Content-Type: application/json' \
      -H 'X-Mirofish-Proxy-Key: <proxy-key>' \
      -H 'X-Mirofish-Account: main' \
      -d '{"model":"claude-haiku-4-5-20251001","max_tokens":128,"stream":true,"messages":[{"role":"user","content":"你好"}]}'

## 从旧版（单文件 relay）升级

数据卷完全兼容：SQLite 结构自动迁移（新增用量日志表），`secrets.enc` v1 自动升级为 v2，
账号与节点绑定关系保留。直接 `docker compose up -d --build` 即可。

## 注意

- compose 当前把 `8787` 绑定到 `0.0.0.0`（公网可达）。任何能访问该端口的人只需本地代理密钥即可调用；
  公网部署强烈建议在前面加 TLS 与额外鉴权（反向代理 / Cloudflare Access）。改回仅本机：把 ports 设为
  `127.0.0.1:8787:8787`。
- Mirofish 没有精确余额接口；WebUI 显示 plan、用量日志与 relay 返回的 7 天配额利用率。
- `/v1/models` 和模型请求会先申请设备 ticket；如果升级上游协议，可通过
  `MIROFISH_MIRASIM_CLIENT_VERSION` 覆盖客户端版本标识，通过
  `MIROFISH_MIRASIM_LOCALE` 覆盖默认的 `zh-HK` locale。
- `status?probe=1` 使用 `/v1/limits`，不产生模型调用；显式模型扫描会发送最小工作请求，
  可能消耗少量额度。
- 删除账号只清除本地凭证，不注销远端账号。
