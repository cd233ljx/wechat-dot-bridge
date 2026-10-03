# 安全边界与恢复行为

## 私有身份边界

本桥接面向一个可信本机用户、一个微信 owner 和一个既有 dot。微信来信必须同时匹配已绑定 account、owner、私聊、完整消息；群聊、其他用户及机器人回声不入队。文字、引用、图片、文件、视频和语音按各自类型处理。

MCP 经官方 Secure MCP Tunnel 访问，本项目不运行公开 HTTP MCP 监听。Tunnel / 插件必须仅本人可访问；Tunnel 的组织和工作区关联需要按 [官方权限说明](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)核验。这里没有从共享 Tunnel 的每次请求中区分多个组织用户，所以**不能作为多用户服务分享给组织或团队**。

一条有效订阅对应一个 callback 目的地。另一个目的地在原订阅仍有效时会收到 `subscription_conflict`；先在原 dot 取消订阅才能切换。该约束防止同时通知不同聊天，不能证明某个请求一定来自你期望的 dot；真正的访问授权来自私人 Tunnel / 插件权限。

## 数据和凭据

消息正文的业务去向是腾讯 iLink、当前用户的私人 SQLite，以及经私人 Tunnel 访问的 dot。Events 回调载荷只有 opaque `message_id`，没有消息正文、微信 owner、route、context token 或登录 token。正文经 `read_weixin_message` 提供给有权调用该插件的 dot。

| 数据 | 保存位置 / 对外行为 |
| --- | --- |
| 微信 token 与 context token | 私人 SQLite，仅收发模块使用 |
| 来信与不可变回复 | 私人 SQLite；正文仅指定工具可读 |
| callback URL 与签名 secret | 私人订阅表；状态不返回完整 URL 或 secret |
| Tunnel 运行密钥 | 操作者选定的私人文件，由官方 Tunnel client 读取 |
| 待确认二维码 | 私人登录状态文件；确认或过期后清除状态文件中的二维码内容；可选本地 PNG 仍需操作者保管 |
| 手机验证码 | 隐藏交互输入，不持久保存 |

启用 `callback_proxy` 后，本机 HTTP 代理转发 Events 的端到端 HTTPS 连接，可以观察目标公网 IP、TLS 主机名等连接元数据，桥接不会向它发送明文 callback 路径、签名 secret 或事件 body。启用 `callback_dns: "google"` 后，Google Public DNS 仅收到回调 hostname 的 DNS 查询，不收到 callback URL / path、secret、消息 ID 或消息正文。微信绑定凭据不会进入 Events 请求；微信收发的网络路径不受这两个配置项影响。

专用状态目录使用 `0700`，数据库、运行密钥和私人配置使用 `0600`。禁止用认证缓存作为配置输入，不扫描任何 agent 的认证目录。不要将运行目录、配置文件、数据库、二维码、密钥或 callback URL 放入 Git、聊天、工单附件或公开日志。SQLite 不是加密数据库；有本机同用户或管理员访问权的人可以读取它，备份必须继承相同保密边界。

运行密钥通过 `scripts/save_runtime_key.py` 在交互终端隐藏输入；脚本拒绝管道和覆盖。已有密钥文件应继续引用原文件。Bridge 不调用模型 API，但密钥仍可能属于具有其他权限的 OpenAI 账号，不能声称它没有权限风险或没有用量成本。

网络和协议错误只暴露固定类别。状态工具没有正文或秘密。需要注意，dot 工具读取结果与 dot 回复本身会进入你的 OpenAI 会话；私人 Tunnel 不会让正常产品级记录或会话存储消失。

## 出站请求校验

微信请求限定在源码认可的腾讯 HTTPS origins；不接受调用者任意指定目标。Events callback 采用配置中的**精确 hostname**，先检查 allowlist，再执行 DNS 和连接：

- 只允许 HTTPS 和 443；拒绝 userinfo、fragment、IP 字面量与通配符。
- 每次请求重新解析 DNS；任何私有、本机、非公共或 IPv6 地址封装答案都拒绝，不能混合公共与私有答案。选择 Google DoH 也不跳过这些检查。
- 直连 socket 或代理 `CONNECT` 都使用已经校验的公网 IP，不让代理再次解析回调 hostname；TLS 证书校验、SNI 和 HTTP `Host` 保留原 hostname。
- 不跟随 HTTP redirect，不继承环境变量代理。不修改系统 DNS、环境代理或其他服务的网络配置。
- DNS 与 HTTPS 请求有限时，请求 / 响应大小有限制。

`callback_proxy` 未配置时 Events 直连；配置时只接受 `http://127.0.0.1:端口`，不接受远程代理、认证信息、路径、query 或其他 scheme。该代理必须由本人管理，桥接不会新建或修改代理服务。它与官方 Tunnel client 的控制面代理是两个独立配置。

`callback_dns` 可选 `system`（默认）或 `google`。`system` 使用本机解析器；`google` 使用固定 `8.8.8.8:443`，TLS 核验 `dns.google`，通过 HTTPS 请求 Google Public DNS，避免先用本机 DNS 解析 DoH 服务。当启用 `callback_proxy` 时，DoH 也经该显式代理 `CONNECT 8.8.8.8:443`。解析或 TLS 失败时拒绝请求，不回退到未经校验的地址。

`callback_hosts` 默认空数组。首次订阅会被拒绝，状态只保留观察到的 hostname。操作者应通过 OpenAI 的可信界面或官方来源独立确认后，配置准确主机名；**不要把任意被请求的 hostname 都批准，也不要开放 `*.openai.com` 或全域名通配**。本次现有 dot 返回的主机是 `connectors.api.openai.com`；[官方 MCP Events 文档](https://developers.openai.com/plugins/build/mcp-events)没有列出这一精确主机白名单。其核验依据是本人 dot 提供的订阅目标、OpenAI 主域层级、独立 DNS 查询和覆盖该主机的有效 TLS 证书；不是文档背书，也不能据此自动批准其他主机。

激活订阅前发送短期随机 challenge，要求对方成功响应并 constant-time 回显；请求按 [MCP Events 官方要求](https://developers.openai.com/plugins/build/mcp-events)用 Standard Webhooks HMAC 签名。签名覆盖事件 ID、发送时间和确切 body 字节。签名 key 更新持久保存，并在短轮换窗口使用新旧双签名。事件不携带任何要求 dot 执行的用户指令；来信文本是用户数据。

## 可靠性与重复限制

入队、owner context 更新与微信 cursor 在同一 SQLite 事务提交，持久去重跨重启有效。待处理队列有容量上限；满队列失败不推进 cursor，不静默丢弃待处理消息。数据保存在本机，主机或数据库损坏仍需要你的私人备份和恢复策略。

回复针对已有来信，同一来信可提交多条回复。默认相同正文重试返回原回复；不同正文创建追加回复。可用独立 `reply_key` 区分有意重复的相同正文，同一个 key 不可改写。`client_id` 按单条回复确定且持久保存，不能据此承诺腾讯网关提供 exactly-once 语义。

| 状态 | 意义与操作 |
| --- | --- |
| `queued` | 本机已保存，尚未调用腾讯发送接口 |
| `sending` | 正在发送；跨进程中断后保留不确定性 |
| `accepted` | 腾讯 API 接受，未证明投递或已读 |
| `uncertain` | 请求可能已到达，对其不自动重发；由本人核对微信结果 |
| `context_required` | 没有可用 context，或被明确拒绝；等待本人新来信更新 context，恢复次数有限 |
| `auth_required` | 绑定认证失效，暂停接收 / 发送；需要明确处理绑定 |
| `failed` | 已知失败；查看固定错误类别，不把它当成功 |

同绑定只有一个接收者。每个状态目录有 worker 锁；相同 account 还使用本机默认 lock directory 的共享锁。**它们不协调其他程序、其他系统用户或其他机器**。部署前必须停掉原 OpenClaw / cc-connect 接收服务；不要给不同实例配置不同 lock directory 来绕过锁。

## 订阅失败和应用恢复

callback 的 `2xx` 表示 webhook 收到，dot 异步处理不由该确认保证。瞬态失败有限次数指数退避；事件 ID 和 body 保持稳定，每次重签使用新时间。`410` 停止订阅，`413` 不重试。重试耗尽在安全状态中可见，worker 不会无限重置失败队列。

订阅过期或取消会停止通知；微信来信仍可保存在待处理队列。相同订阅 identity 显式恢复时，未提交回复的来信可以重新通知；正常有效订阅刷新不会重发已确认的事件。不同 callback 的新订阅只自动接收其建立后入队的消息，旧消息通过 `list_pending_weixin_messages` 显式读取处理。

协议始终返回 `cursor: null`，不实现历史 replay。上述恢复面向当前应用待处理收件箱；已经 `reply_committed` 的消息不会重新触发回复。回复处于 `uncertain` 的消息也不能靠重新订阅触发第二次发送。

## 报告问题

报告版本、固定错误类别、脱敏状态、复现步骤与 fixture 即可。不要提供真实 token、运行密钥、签名 secret、callback 路径、二维码、完整数据库或私人正文。真人闭环未验收之前，不把该实现视为已验证可替换生产收信服务。

## 媒体和分段

附件引用及解密后的内容保存在同一个私人 SQLite 中。CDN 只允许腾讯 `novac2c.cdn.weixin.qq.com` 的 HTTPS `/c2c/download` 与 `/c2c/upload`，连接绑定已验证公网地址、TLS 核验原主机，无重定向，不发送微信 bearer token。AES-128-ECB / PKCS7 是腾讯媒体线协议要求，不是数据库加密。图片通过 Pillow 解码并生成预览，原始内容仍保留。

当前单附件 20 MiB、暂存单次 1 MiB（支持分块累计至 20 MiB）、缓存 256 MiB、图片解码 2000 万像素。上述数值是实现资源预算，不代表腾讯产品限制。满缓存会明确报告 media_cache_full，不静默删除用户附件；不自动删除数据库或备份。需要更大媒体时须调整部署预算。

文字回复单次工具调用最多 256 KiB，拆成不超过 3800 字符且 8192 UTF-8 字节的分段，每段独立持久确认。剩余分段不因前段成功而被标为成功；中断的不确定分段不自动重发。附件仅随最后一段发送。新增附件、上传进度、引用缓存、分段表属于前向兼容迁移；回滚前先停服，确认没有待发媒体或分段，否则旧代码无法正确处理新队列。

媒体 CDN 网络单独支持 `media_proxy`（仅本人本机 `http://127.0.0.1:端口`）及 `media_dns`（system / google），默认直连及系统 DNS。仅在本机 DNS 或直连不可用时配置；同样保留精确腾讯主机、公共 IP 固定连接、TLS 域名核验和无重定向。DoH 只接收 CDN 主机名，不接收媒体 URL 参数或 AES 密钥；本机代理转发端到端 HTTPS。它不修改全局代理、微信 API 或 Events 回调设置。
