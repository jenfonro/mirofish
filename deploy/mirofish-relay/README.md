# Mirofish Relay — Docker + WebUI

容器化运行 `mirofish/` Python 包：多账号管理、邮箱验证码登录、凭证加密持久化、
按账号固定 Mihomo 节点（多槽位并发出口）、Anthropic-compatible `/v1/messages` 真流式中转、
OpenAI-compatible `/v1/chat/completions` 翻译（含 tool calls / 图片 / 流式）、Codex Responses
透明代理，以及内置 Vue 管理 WebUI。

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
查看每个账号绑定的节点、套餐资料（套餐层级徽章、到期日与剩余天数、持有人姓名；悬停徽章可见
用户 ID、租户、邀请升级进度与各窗口预算，数据来自上游 `/auth/me` 与 `/auth/referral`，
登录与「刷新」时读取，后台扫描每天自动补新）、配额利用率、**用量额度卡片**（来自上游 `/v1/limits` 的
5 小时 / 7 天 / 7 天 Fable / 30 天窗口：已用百分比、匀速线平均参照、超前 / 落后、剩余额度、
重置倒计时；不消耗额度。其中 **7 天 Fable 窗口**由上游把所有 fable 模型合并计量、只给一个
总数，卡片会在该窗口下额外列出 `fable-5` 与 `fable-5-1` 各自的请求数与 token 消耗——这份
拆分来自本地用量日志，且只统计当前窗口区间内的记录，因此**跟着窗口重置一起归零**，不需要
清理数据；鼠标悬停可看输入 / 输出 / 缓存读写明细）、近 24 小时用量图表、流式测试调用模型、
删除本地账号。WebUI 支持浅色 / 深色主题，
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

模型 relay 还要求设备签名：0.0.272 客户端在整个安装范围内持久化一个 Ed25519 密钥，
并按「账号 × 出口」申请约 15 分钟的 device ticket，再为每个请求生成 `mrs-sig-v2` 签名（覆盖
method、pathname、时间戳、nonce、设备 ID、客户端版本，以及凭证、relay 元数据与请求体的摘要）。
私钥与账号 token 一样只进入加密凭证存储，不写入 SQLite 或日志。旧版按账号保存的设备密钥会
自动迁移一个到安装级槽位。当前默认客户端标识为抓包确认的 `0.0.272`。ticket 会提前 120 秒
刷新；申请端点返回 404/501 时缓存 15 分钟“不支持签名”，其他失败按 1–30 秒退避。0.0.272 的
模型请求没有 ticket 时直接返回 503（`device_session_required`，处于退避期时附带 `retry_after` 秒数），不会改用
账号 token——那会把中转故障变成对用户自有账号的意外扣费；只有把
`MIROFISH_MIRASIM_CLIENT_VERSION` 固定在 0.0.272 以下时才保留旧的账号 token 降级（不发送伪签名）。
ticket 恢复后自动继续。

0.0.272 的模型请求还会把 relay 自己的 `x-mirasim-*` 元数据（明文保留的
`x-mirasim-client` 除外）封装到 `x-mirasim-enc`。封装使用临时 X25519 + HKDF-SHA256
(`mrs-seal-v1`) + ChaCha20-Poly1305，并把上游 pathname 与 HTTP method 放进 AAD；因此
session、账号、设备和签名字段不会在网络上明文出现。默认公钥内置于 relay，也可用
`MIROFISH_MIRASIM_SEAL_PUBLIC_KEY` 或 `MIRASIM_SEAL_PUBKEY` 覆盖。封装失败会停止该请求，
不会降级为明文；与旧的自托管端点联调时可显式设置 `MIROFISH_MIRASIM_SEAL_METADATA=0`。

Anthropic 请求会保留 `?beta=true` 与白名单内的 Claude SDK 特征头；Codex 的 `/v1/responses`
和 `/backend-api/codex/responses` 都映射到上游 `/v1/responses`，`/v1/alpha/search` 与
`/backend-api/codex/alpha/search` 映射到上游 `/v1/alpha/search`；两组路径各自用自己的上游
pathname 签名，保留查询串但签名只包含 pathname。
压缩的 Codex 请求体先有界解压，再以最终精确字节计算长度、哈希和签名。调用方的
`Authorization` / `X-Api-Key` 绝不会转发给上游。Codex 请求发出时与官方桌面端内置 Codex 的抓包
一致：`user-agent` 改写为 `MIROFISH_CODEX_USER_AGENT`（默认为抓包值），`originator` 固定为
`mirasim`，不转发 `openai-beta` 与 `accept-encoding`，调用方 cookie 丢弃，改为回传 relay 自己
按「账号 × 出口」收到的 Cloudflare cookie。所有请求头的顺序与大小写按抓包写到线上（`Host`、
`Connection` 在最后；签名的 `/v1/models` 用小写 `authorization`）。模型流量默认发往
官方客户端当前使用的 `https://relay.mirasim.ai`；旧的 `mirasim-relay.mirofish.ai` 分发可能仍返回
模型目录；当前观察到它可能对同一 Claude 请求返回 `no upstream available for model`。

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
                                       # 7d_fable 窗口附带 models[]：各 fable 模型本窗口用量
    GET    /api/limits                 # 全部账号并发拉取用量额度（不计费）
    GET    /proxies
    GET    /v1/models                # 按账号缓存 5 分钟；data 为标准 OpenAI 模型对象列表，
                                     # 便利字段以 mirofish_ 前缀命名（mirofish_model_ids）
    POST   /v1/messages              # Anthropic Messages；"stream":true 为真 SSE 透传
    POST   /v1/messages/count_tokens # Anthropic token 计数（转发上游，不计费；失败则本地估算）
    POST   /v1/chat/completions      # OpenAI 兼容；支持 tools/图片/流式
                                     # 注意：上游以 thinking 模式服务，仅接受 temperature=1
                                     # 且不接受 top_p；其他采样参数会被自动丢弃而非转发
    POST   /v1/responses             # Codex Responses 原始字节/状态/响应头透传
    POST   /backend-api/codex/responses # Codex 原生路径，映射到 /v1/responses
    POST   /v1/alpha/search          # Codex 检索透传
    POST   /backend-api/codex/alpha/search # Codex 原生路径，映射到 /v1/alpha/search
    POST   /api/login/start          # {"alias","email"} 发送验证码
    POST   /api/login/finish         # {"alias","code"} 完成登录
    POST   /api/accounts/<alias>/enabled # {"enabled":true|false} 面板启用/停用开关
    DELETE /api/accounts/<alias>     # 删除本地账号及凭证
    GET    /api/usage?hours=24       # 用量统计（按小时 × 账号聚合）
    GET    /api/schedule             # 当前账号调度模式与用量上限
    POST   /api/schedule             # {"mode":"balanced"|"reset_first"|"fable_first","max_utilization":0.98}

验证码一旦验证成功，access/refresh token 会先写入加密存储，再读取套餐、租户等展示资料。
如果后续资料接口临时失败，`/api/login/finish` 仍返回成功并标记 `profile_pending=true`；这样不会因
重复提交已经消费的验证码而出现先 502、后 401。稍后在账号列表点击「刷新」即可补齐资料。

账号选择顺序：请求头 `X-Mirofish-Account` > `MIROFISH_DEFAULT_ACCOUNT` > **会话亲和** > 轮询。

自动调度会跳过**任一相关窗口已用满**的账号。窗口是层层收窄的，一个请求同时花掉它们：

| 请求 | 5h | 7d | 7d_claude | 7d_fable |
|---|---|---|---|---|
| `claude-fable-5` / `-5-1` | ✓ | ✓ | ✓ | ✓ |
| `claude-opus-*` / `sonnet` / `haiku` | ✓ | ✓ | ✓ | — |
| `gpt-*` / `kimi-*`（Codex 路径） | ✓ | ✓ | — | — |

取其中最紧的那个——花费同时落在这几个窗口上，所以
5 小时窗口用满的账号哪怕周额度还剩很多也一样发不出请求。（此前只看了 7 天窗口，导致调度反复
选中 5 小时窗口已满的账号，被上游以 `credit_exhausted_5h` 拒绝，累积后触发封停。）所有账号都
用满时仍会照常发出请求，由上游做最终判定，不会因此拒绝服务。

响应头返回
`X-Mirofish-Account` 与 `X-Mirofish-Quota-7d-Utilization` / `-Reset`。

**停用与账号级 429 冷却**：在 WebUI 停用的账号保留凭证但不参与任何自动分配；显式用
`X-Mirofish-Account` 指定它会返回 403。上游对某个账号返回 429 时（属于出口的
`shared_quota_unavailable` 区域拒绝除外），该请求会自动换一个账号重试（显式指定的账号
不替换），被拒账号进入冷却期，期间自动分配会避开它，其活跃会话也会改派到其他账号——
否则会话亲和会把客户端的重试一直送回刚刚 429 的那个账号。冷却时长按错误区分：

- **额度耗尽**：上游把它放在 `error.code`（`credit_exhausted_5h` / `_7d` / `_shared`），而
  `error.type` 跟短暂限速一样都是 `rate_limit_error`——**只看 `type` 是分不出来的**。曾经因此
  把所有额度耗尽误判成短暂限速，只冷却 60 秒就放回调度，结果账号一再被拒，累积到最后被上游
  以「反复触发限流」为由封停 24 小时。现在按 `code` 识别，并且 code 里就带着窗口名，所以直接
  用那个窗口的 `reset_at` 作为冷却时长（下限 10 分钟，上限 1 小时——7 天窗口可能好几天才重置，
  真按它冷却会看不到中途提额或提前重置）。
  **冷却只针对被判定用满的那个窗口**：`7d_fable` 用满时只挡 fable 模型，opus 等照常调度——
  上游自己的提示就写着「可切换其它模型继续 / other models still work」。曾经把整个账号冷却，
  结果因为每个账号的 fable 额度都远早于 7 天窗口用完，82 个账号里 81 个同时出列，连 opus 也没
  账号可用。没有指明窗口的 429 仍然冷却整个账号。

  **全池该窗口用满时，relay 直接本地返回 429，不再打上游**：错误体沿用上游的写法
  （`type: rate_limit_error`、`code: credit_exhausted_<窗口>`）并给出大致恢复时间。这一点很
  重要——额度已经确定用完了，再把请求发出去只会每次多挨一次拒绝，而反复被拒正是触发上游封停
  账号 24 小时的原因。只有在「所有可用账号都恰好卡在同一个窗口上」时才这样回答；如果是停用、
  异常等混合原因导致无账号可选，仍然返回原来的 503。
- **其他 429**：通常是短暂限速，只冷却 60 秒。Anthropic、OpenAI 兼容与 Codex Responses 三条路径行为一致。
这些错误是账号属性而非出口属性，因此不会触发代理节点轮换。

**账号状态（正常 / 异常）**：账号列表有「状态」列。上游以 401（凭证或签名会话被拒）、
403（反复触发限流后被临时封停）或 503 `overloaded_error`（该账号当前没有可用上游）拒绝
某个账号时，该账号被标记为**异常**并记录状态码、上游原因和时间，随后从自动调度中移除，
其活跃会话改派到其他账号，请求本身自动换账号重试（显式用 `X-Mirofish-Account` 指定的
账号不替换，直接把错误返回给调用方）。

**只看状态码是不够的**：判定依据是上游返回体里的 `overloaded_error`，不是 503 本身。
relay 自己也会返回 503（`device_session_required`、代理池没有可用节点），上游前面的边缘
节点抖动时还会返回 503 的 HTML 错误页或没有错误信封的空响应——这些跟“当时恰好承载请求的
那个账号”没有任何关系。把它们也算作账号异常的代价是：一次共享故障就能把整个账号池停调一天
（线上真实发生过，82 个账号里 81 个被误停）。因此这类 503 既不标记异常也不进冷却，调用方
直接重试即可。

两种拒绝会不会自己好，处理方式不同：

- **401（凭证失效）**：不会自愈，没有重试期限。状态列显示「需重新登录」，必须重新登录该账号，
  或在测试台指定它成功发送一次请求。
- **503 `overloaded_error`（上游容量过载）**：上游自己的提示就说明是暂时的、恢复情况在 Discord
  同步，所以带 **1 天**的重试期限，到点自动重新参与调度，无需任何人工操作。状态列显示
  「停调 N 小时」。间隔取一天是因为容量恢复以小时计，更频繁地重试只会白白消耗一次请求又被重新
  停调；而一天足够便宜，也保证账号一定会自己回来。
- **403 限流封停（反复触发限流后被上游临时封禁）**：上游提示里直接给出解封时刻
  （`access resumes at <ISO 时间>`），**直接采用这个时刻**作为重试期限——比它早重试必然失败，
  比它晚重试则白白浪费一个已经恢复的账号。状态列显示「异常」。
- **403 封号（`this account is suspended; contact support`）**：状态列单独显示为
  **「封停」**而不是「异常」，因为两者要做的事完全不同。这种封停没有解封时刻，只有客服能解除，
  所以**不设任何重试期限**，也不参与调度，跨重启保留。曾经把它当作普通异常处理、套用 1 小时
  兜底窗口，结果 46 个被封号的账号每小时都去撞一次上游——既无意义，也可能加重风控。解除后在
  测试台指定该账号成功发送一次请求即可恢复（上游能应答就说明客服已处理，成功比记录更可信）。

注意只有上面两种 403 会停调：面板里手动停用账号时 relay 自己也返回 403，那不是上游判决。

无论哪种，在**测试台**的账号下拉框显式选中该账号发送一次请求并成功，都会立即清除标记并恢复
调度；失败则更新为最新的上游原因。测试台在成功和失败两种结果下都会刷新账号列表，状态立刻可见。
除此之外没有任何定时或后台探测会把异常账号放回调度，标记也跨重启保留，因此故障账号不会悄悄
回到轮询里消耗真实流量。`GET /accounts` 中对应 `healthy`、`health` 与 `health_retry_in`
（距离自动重试的秒数，401 为 `null`）。

注意与上面的 429 冷却区分：429 是**账号可用**但额度耗尽或被限速，冷却 10 分钟 / 60 秒后自动
重试；401、403 封停与 503 `overloaded_error` 则是上游根本不为这个账号服务。

`/v1/models` 不受影响：它是零成本的目录读取，即使所有账号都被停调也照常返回——恰恰在账号
大面积异常时最需要它（要在测试台挑模型去重试）。

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
且刻意忽略 system 提示词，以免所有窗口共用同一提示词而挤到同一账号）。新会话按下方
「账号调度模式」的规则分配，从而在账号间铺开。会话在 `MIROFISH_SESSION_TTL`（默认 1800 秒）无活动后过期。
WebUI 账号表的「活跃会话」列可实时看到每个账号正在服务的窗口数。

**账号调度模式**：WebUI「账号调度」卡片（或 `GET`/`POST /api/schedule`）可在三种模式间
切换，设置持久化在数据卷的 SQLite 中。默认的**均衡分配**把新会话交给活跃会话最少的账号；
**优先重置窗口**在同样的均衡排序上加一点倾斜——7 天窗口将在 48 小时内重置的账号被视为
少扛最多 2 个会话，优先接下新会话，把快清零的额度先花掉，提前量用完就回到正常轮换，
不会把并发都堆到一个账号上。**优先重置窗口 + Fable 已用最高**（`fable_first`）在此基础上，
对非 fable 模型的请求把这点提前量按该账号 `7d_fable` 窗口的已用比例缩放：48 小时内要重置的
账号里，Fable 已用最高的排最前——这些账号的 fable 额度本就用尽（发 fable 请求也会被拒），
而通用额度即将清零，正好先花掉；fable 额度有余量的账号留给 fable 请求。fable 请求本身仍按
「优先重置窗口」分配。用量约束在所有模式下都生效：claude-fable-5 请求同时考虑
该模型独立的 7 天窗口（`7d_fable`），取两者中更满的一个；用量超过可配置上限（默认 98%）
的账号排到所有有余量账号之后；相关窗口已用满（约 100%，上限配得更高时随之抬高）的账号
被自动分配直接跳过，避免把窗口烧到 100% 以上——仅当所有账号都用满时才继续兜底服务，
指定账号请求不受影响，上游 429 仍是最终裁决。额度数据来自后台每 5 分钟一次的
`/v1/limits` 扫描（零模型开销，两种模式都保持刷新，跳过已停用账号，保存调度设置时
立即刷新一次），不在请求路径上探测；已过重置时间的缓存窗口视为无数据，不会误伤刚刚
重置的账号；数据略旧最多让一次请求多试一个账号——上游 429 加自动换号才是真正的兜底。
已开始的对话仍固定在原账号上，不会中途切换（对话所在账号窗口用满时例外：下一轮换到
有余量的账号）。

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

数据卷完全兼容：SQLite 结构自动迁移（新增用量日志表），`secrets.enc` v1 自动升级为 v2，
旧版账号设备密钥首次使用时迁移为安装级密钥，账号与节点绑定关系保留。直接
`docker compose up -d --build` 即可。

## 注意

- Docker 镜像使用仓库中的 `uv.lock` 做 `--locked --no-dev` 安装；修改
  `pyproject.toml` 依赖后必须同步更新并提交锁文件，否则镜像构建会直接失败。
- compose 当前把 `8787` 绑定到 `0.0.0.0`（公网可达）。任何能访问该端口的人只需本地代理密钥即可调用；
  公网部署强烈建议在前面加 TLS 与额外鉴权（反向代理 / Cloudflare Access）。改回仅本机：把 ports 设为
  `127.0.0.1:8787:8787`。
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
- 设备与机器字段遵循 0.0.272 的安装级边界，不按账号虚构。非 CLI 调用方统一补全为抓包中的
  `arm64 / MacOS` Claude CLI 组合；真实 `claude-cli/...` 调用方的 arch / os 原样保留。
  `x-mirasim-device` 始终是公钥派生的 22 字符安装 ID，签名与（旧版标识下的）无签名降级发出的是同一个值；
  降级路径不会改用形状不同的替代标识，否则单看这个字段就能区分两条路径。
- 仿真范围只到请求头与请求体。TLS ClientHello 出自 OpenSSL，官方客户端是 Electron 的
  BoringSSL：cipher 列表、扩展顺序与 GREASE 由 TLS 库决定，要对齐 JA3 得换掉 TLS 栈，
  配置 OpenSSL 做不到。因此 ALPN 扩展被刻意保留（官方也带这个扩展，去掉反而更显眼），
  只有在 Python 3.13+ 上会把 supported_groups 收窄成浏览器那三个曲线，去掉 OpenSSL 3.5
  默认的 X25519MLKEM768 与 ffdhe。回环抓包测试固定了这些字段，依赖升级不会悄悄改掉。
- 上游会话标识（`x-mirasim-session`）现在是裸 v4 UUID，不再带 `mirofish_` 前缀：该值同时用作
  `x-claude-code-session-id`，官方客户端在这里发的一直是 UUID。对同一对话仍然是确定性的，
  会话亲和行为不变。
- Claude 模型请求会自动补上官方客户端的 prompt cache 断点（Agent SDK 标记块、最后一个
  system 块、最后一个 user 轮次的末尾块，共 3 个，与抓包一致；`tools` 不打断点）。
  调用方自己带了任何 `cache_control` 时整体不改动，因为上游最多只接受 4 个断点。
  最后一个 user 轮次如果是字符串内容，会展开为等价的 text block 以便承载断点。
- 上游固定使用 HTTP/1.1 连接池；`MIROFISH_KEEPALIVE_EXPIRY`（默认 75 秒）、
  `MIROFISH_MAX_CONNECTIONS`（默认 100）和 `MIROFISH_MAX_KEEPALIVE_CONNECTIONS`
  （默认 20）控制连接复用，`MIROFISH_STREAM_READ_TIMEOUT`（默认 600 秒）控制流式读取超时。
  `MIROFISH_MAX_BODY_BYTES`（默认 8388608）同时限制压缩输入与解压后的正文，防止解压炸弹。
- `status?probe=1` 使用 `/v1/limits`，不产生模型调用；显式模型扫描会发送最小工作请求，
  可能消耗少量额度。
- 删除账号只清除本地凭证，不注销远端账号。
