# Mirofish Relay — Docker + WebUI

把 `scripts/general/mirofish_relay.py` 容器化运行：多账号管理、邮箱验证码登录、
凭证加密持久化、按账号固定 Mihomo 节点、Anthropic-compatible `/v1/messages` 中转，以及内置管理 WebUI。

## 快速开始

    cd deploy/mirofish-relay
    cp .env.example .env
    # 编辑 .env，设置 MIROFISH_MASTER_KEY（至少 16 字符，可用 openssl rand -base64 32 生成）
    # 设置 MIROFISH_PROXY_SUBSCRIPTION_URL；不要把带 token 的链接提交到仓库
    docker compose up -d --build

打开管理页面：

    http://127.0.0.1:8787/

首次进入会显示容器生成的本地代理密钥（只显示一次）。密钥也保存在数据卷
`/data/proxy.key`：

    docker compose exec mirofish-relay cat /data/proxy.key

在 WebUI 中输入密钥后即可：配置代理订阅、添加账号（发送邮箱验证码 → 输入验证码 → 完成登录）、
查看每个账号绑定的节点、plan/用量/配额、测试调用模型、删除本地账号。

## 凭证存储

容器内没有 macOS Keychain，因此使用加密文件后端：

- token 保存在 `/data/secrets.enc`，使用 `MIROFISH_MASTER_KEY` 派生密钥加密
  （PBKDF2-HMAC-SHA256 + HMAC 完整性校验），主密钥不写入数据卷；
- SQLite `/data/accounts.sqlite3` 只保存元数据（邮箱、plan、租户、最近用量）；
- 丢失主密钥将无法解密已有账号凭证，需要重新登录。

## 代理池

Docker Compose 会启动一个 Mihomo sidecar。订阅由 Mihomo 自身下载、解析和建立连接，Python relay 不再尝试解析节点协议；
因此 SS、VMess、VLESS、Trojan、Hysteria、TUIC 等 Mihomo 支持的节点都可以使用。

订阅地址由 `.env` 的 `MIROFISH_PROXY_SUBSCRIPTION_URL` 配置；它优先于 WebUI 曾保存的地址。服务会定期读取 Mihomo 的节点列表，
并把每个账号选中的节点 ID 写入 SQLite，因此同一账号会持续使用同一个出口节点。节点网络失败时，服务会保存并切换到下一个节点。
为避免并发请求在切换 Mihomo 全局选择器时串用 IP，不同账号的上游请求会短暂串行；个人使用场景下这能保证账号与出口 IP 的对应关系。
节点数量少于账号数量时，系统会在没有空闲节点后按最少账号数复用节点；无法保证超过订阅节点数量的账号各自拥有独立 IP。

订阅请求默认使用 `mihomo/1.19.0` 的 User-Agent；如果你的订阅服务要求特定客户端标识，可在 `.env` 设置
`MIROFISH_PROXY_SUBSCRIPTION_USER_AGENT` 后重建容器。

如果服务器无法访问订阅站，可改用静态文件：在能够下载订阅的机器保存原始订阅内容，上传到
`deploy/mirofish-relay/mihomo-input/subscription.yaml`，然后在 `.env` 清空
`MIROFISH_PROXY_SUBSCRIPTION_URL` 并设置 `MIROFISH_PROXY_SUBSCRIPTION_FILE=/input/subscription.yaml`。
初始化容器会把它复制到 Mihomo 允许读取的 `/config` 数据卷。该文件含节点凭据，应设置为仅自己可读且不要提交到版本库；静态文件模式需要手动更新该文件后重建 Mihomo。

相关接口：

    GET  /proxies                    # 立即返回缓存状态，不拉取网络
    POST /api/proxies/subscription  # {"url":"https://..."}，保存并刷新
    POST /api/proxies/refresh        # 请求 Mihomo 主动更新订阅并读取节点，失败会返回 502/503

如果该接口返回 `503 Mihomo controller request timed out`，说明 relay 到 Mihomo
侧车的控制端口 `mihomo:9090` 无响应；先执行 `docker compose logs mihomo` 检查订阅下载和配置错误。
`MIROFISH_MIHOMO_CONTROLLER_TIMEOUT` 默认 5 秒，可在 `.env` 中按需调整。

## API

所有接口需要请求头 `X-Mirofish-Proxy-Key`：

    GET    /health
    GET    /accounts
    GET    /accounts/<alias>/status[?probe=1]
    GET    /proxies
    GET    /v1/models
    POST   /v1/messages            # 可加 X-Mirofish-Account 选择账号
    POST   /api/login/start        # {"alias","email"} 发送验证码
    POST   /api/login/finish       # {"alias","code"} 完成登录
    DELETE /api/accounts/<alias>   # 删除本地账号及凭证

调用模型示例：

    curl http://127.0.0.1:8787/v1/messages \
      -H 'Content-Type: application/json' \
      -H 'X-Mirofish-Proxy-Key: <proxy-key>' \
      -H 'X-Mirofish-Account: main' \
      -d '{"model":"claude-haiku-4-5-20251001","max_tokens":128,"messages":[{"role":"user","content":"你好"}]}'

## 注意

- compose 默认只绑定 127.0.0.1；如需对外暴露，请自行加反向代理与鉴权。
- Mirofish 没有精确余额接口；WebUI 显示 plan、最近 usage 与 relay 返回的 7 天配额利用率。
- 删除账号只清除本地凭证，不注销远端账号。
