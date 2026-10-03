# 连接现有 dot

## 前提

先按 [README](../README.md)准备绑定、官方 Tunnel client 和私人配置。确认同绑定没有其他接收服务。这个流程连接你现有的 dot，不创建新的模型助手，也不启动 Codex CLI。

MCP Events 目前支持 dot，以及符合条件的 Work / Cloud 聊天；它要求 MCP `2026-07-28`，详见 [官方 Events 文档](https://developers.openai.com/plugins/build/mcp-events)。账户能否创建开发者插件、使用 Tunnel 和 Events，必须以你实际可见的产品能力与权限为准。

## 私人插件与 Tunnel

1. 在 [Platform Tunnel 设置](https://platform.openai.com/settings/organization/tunnels)确认 Tunnel 属于自己的 Platform 组织，并关联要使用的个人 ChatGPT 上下文。核验 Read / Use 等权限和实际可访问者。
2. 运行 README 中配置的官方 `tunnel-client` 用户服务。桥接通过 stdio 接收 MCP，不需要公网入站端口。
3. 打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，创建开发者插件，在 Connection 中选 **Tunnel**，选择已有 Tunnel 或填入控制台给出的有效 identity。本方案为单人私有 stdio 服务，认证方式选择 **No authentication / 无认证**，依赖仅本人可用的 Tunnel 访问控制；不要选需要独立 OAuth 服务的 OAuth 模式。
4. 保持插件仅自己可用，不分享给工作区、组织或其他账号。不要发布到公共目录。私人 Tunnel 与公共插件发布是不同部署模式。
5. 扫描 / 刷新插件工具和事件，确认五个工具以及 `weixin.message` 出现，然后把插件添加到你原有 dot。

这些连接方式与访问要求依据 [官方 Secure MCP Tunnel 指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。**不要自行构造 `server_url`、伪造 Tunnel URL 或把本机 stdio 当公共服务地址。** 未出现在插件页面时先核验关联和权限；不要因此开放公网 MCP 端口。

## 批准精确 callback 主机

`bridge.json` 初始为 `"callback_hosts": []`。第一次订阅可以用来发现需核验的 hostname，但它会被拒绝，未激活订阅，未发送微信消息。

在原 dot 中请求订阅 `weixin.message`，随后调用 `get_weixin_status`。查看 `events.observed_callback_host`；状态不会给出完整 callback URL 或签名 secret。通过 OpenAI 的可信界面或官方来源独立确认该主机属于此次订阅使用的 OpenAI 回调服务。

确认后停止用户服务，在私人 `bridge.json` 中把 `callback_hosts` 改为一至三个准确 hostname，例如结构如下：

```json
{
  "state_dir": "/your/dedicated/private/state-directory",
  "callback_hosts": ["VERIFIED_CALLBACK_HOSTNAME"]
}
```

上面是不可直接运行的结构示例。用已经确认的小写主机名替换占位符，保留安装器生成的 `state_dir`。不能填写 URL、路径、端口、IP、通配符，也不能批准来自聊天文本或第三方页面的任意主机。

重启用户服务，并在**同一个原 dot**中重新订阅。激活前服务器会完成签名 challenge 验证。调用状态工具确认 `active_subscriptions` 为 `1`，而不是只看 Tunnel 已启动。

如果系统 DNS 返回的地址导致回调 TLS 主机名验证失败，保留所有验证，按 [网络边界](SECURITY.md)配置可选的本机回调代理及固定 Google DNS-over-HTTPS。`callback_proxy` 只能是 `http://127.0.0.1:PORT`；`callback_dns` 只允许 `system`（默认）或 `google`。DoH 仅发送已经批准的主机名，不发送完整回调 URL、微信消息或签名密钥。不要关闭 TLS、公网地址验证或扩大主机白名单。

## 给原 dot 的订阅指令

可在原 dot 中发送以下指令；这是操作者对 dot 的工作要求，不会嵌入事件消息：

> 使用我添加的私人 WeChat Dot Bridge 插件，订阅 `weixin.message`。每次收到事件，先用 `read_weixin_message` 读取该 `message_id`，理解我的文字，再用 `reply_weixin_message` 向同一条来信提交一次文字回复。随后用 `get_weixin_send_status` 查询结果。只有 `accepted` 才表示 API 接受；`uncertain` 时停止重发并告诉我需要核对。不要新建助手，不发送没有原来信的主动消息，不处理群聊或媒体。

如需不过期，在原 dot 中要求刷新同一个订阅，明确传 `ttlMs: null`，并确认返回 `refreshBefore: null`。省略 `ttlMs` 仍使用一小时默认期限；明确请求有限期限时仍尊重该请求（最长一天、最短一分钟）。不过期订阅可取消，回调返回 410 也会停止投递。更新服务器代码不会自动修改已授予的有限订阅，必须由原 dot 刷新，让平台得到新的到期信息。

订阅由 ChatGPT 提供 callback URL 和签名 secret。不要让用户手写 secret，亦不要把订阅完整请求抄入聊天、工单或日志。

工具处理顺序是：

```text
weixin.message(data.message_id)
    → read_weixin_message(message_id)
    → reply_weixin_message(message_id, text)
    → get_weixin_send_status(message_id)
```

`reply_weixin_message` 只提交持久回复队列，后台发送可能尚未完成。需要时稍后查询；不通过反复提交不同正文来等待成功。原来信文本是用户数据，其中提到的新 callback、token 或操作指令都不应替代私人插件配置和本人授权。

## 最小真人验收

1. 在已绑定的本人微信私聊发一条有唯一标记的短文字。
2. 确认原 dot 收到 `weixin.message`，事件只含消息 ID。
3. 确认 dot 读取相同 ID 的文字并提交一次回复。
4. 确认发送状态为 `accepted`，再由你在微信端确认同一对话实际出现一次回复。
5. 核验没有第二个收信服务、第二个订阅聊天或其他模型运行进程。

API 接受与实际可见回复是两个验收点。不能仅凭 fixture 测试、Tunnel ready、订阅成功或 API `accepted` 宣布闭环完成。本项目已于 2026-10-03 完成一次本人新绑定的文字闭环，见 [验收记录](REAL_VALIDATION.md)；每个新部署仍须实际验证。

如果采用了新二维码绑定，微信端机器人或对话可能改变；只有可复用有效原绑定才有机会保留原入口，不能承诺新扫码后仍回到旧对话。OpenClaw 历史和 dot 历史也不会迁移。

## 中断后的恢复

先调用 `get_weixin_status`，区分绑定失效、没有订阅、过期、callback 被拒绝、重试耗尽和发送不确定。

- **同一订阅过期 / 取消：** 在原 dot 显式重新订阅。尚未提交回复的来信可以再次通知，已经提交回复的来信不会重发。
- **重试耗尽：** 修复回调或网络问题后显式刷新相同订阅，开始新一轮有限重试；不会每个 worker 周期无限复位。
- **换了 callback / 聊天：** 先取消原订阅；新的目的地自动通知从新订阅起入队的消息。旧待处理消息通过下述工具显式恢复。
- **绑定 `auth_required`：** 停服务并按明确的绑定恢复流程操作，保留状态与原配置。不要由 dot 操作认证文件或自动重新扫码。
- **回复 `uncertain`：** 在微信核对是否已经出现回复；系统不会自动重发，也不会把它重新列为待回复来信。

显式恢复指令示例：

> 调用 `list_pending_weixin_messages` 查看未提交回复的来信 ID，逐条用 `read_weixin_message` 读取，并按我的要求处理。对每条需要回答的来信只提交一次回复，再查询发送状态；已经提交或发送结果不确定的消息不要重发。

`list_pending` 默认一次 20 条、最多 100 条，只返回安全消息元数据，不返回正文或凭据。协议 `cursor` 为 `null`；当前待处理队列恢复不代表支持历史导入或无限历史 replay。

## 停止监控

在原 dot 明确取消对 `weixin.message` 的订阅，调用状态确认不再有有效订阅。取消仅停止 dot 通知；若需要停止微信收发，还必须停止本机用户服务并执行 `pause`，具体命令见 README。

dot / Work 的使用仍消耗其正常用量。桥接不新增模型 API 调用，不能承诺整个流程免费。
