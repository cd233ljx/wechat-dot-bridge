# 连接现有 dot

## 前提

先按 [安装指南](INSTALL.md)准备绑定、官方 Tunnel client 和私人配置。确认同绑定没有其他接收服务。这个流程连接你现有的 dot，不创建新的模型助手，也不启动 Codex CLI。

MCP Events 目前支持 dot，以及符合条件的 Work / Cloud 聊天；它要求 MCP `2026-07-28`，详见 [官方 Events 文档](https://developers.openai.com/plugins/build/mcp-events)。账户能否创建开发者插件、使用 Tunnel 和 Events，必须以你实际可见的产品能力与权限为准。

## 私人插件与 Tunnel

1. 在 [Platform Tunnel 设置](https://platform.openai.com/settings/organization/tunnels)确认 Tunnel 属于自己的 Platform 组织，并关联要使用的个人 ChatGPT 上下文。核验 Read / Use 等权限和实际可访问者。
2. 运行安装指南中配置的官方 `tunnel-client` 用户服务。首次接入时保持接收暂停，先完成插件与订阅配置。桥接通过 stdio 接收 MCP，不需要公网入站端口。
3. 打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，创建开发者插件，在 Connection 中选 **Tunnel**，选择已有 Tunnel 或填入控制台给出的有效 identity。本方案为单人私有 stdio 服务，认证方式选择 **No authentication / 无认证**，依赖仅本人可用的 Tunnel 访问控制；不要选需要独立 OAuth 服务的 OAuth 模式。
4. 保持插件仅自己可用，不分享给工作区、组织或其他账号。不要发布到公共目录。私人 Tunnel 与公共插件发布是不同部署模式。
5. 扫描 / 刷新插件工具和事件，确认十一个工具以及 `weixin.message` 出现，然后把插件添加到你原有 dot。

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

> 使用我添加的私人 WeChat Dot Bridge 插件，订阅 `weixin.message`。每次收到事件，先用 `read_weixin_message` 读取该 `message_id`，理解我的文字，再用 `reply_weixin_message` 向同一条来信回复；可以先发进度，随后按需追加分段内容或完成消息。随后用 `get_weixin_send_status` 查询结果。只有 `accepted` 才表示 API 接受；`uncertain` 时不要重试该条回复，但可以提交其他进度或完成回复。不要新建助手，不发送没有原来信的主动消息，不处理群聊。遇到附件，读取 `attachments`；图片预览可直接理解，其他附件用 `read_weixin_attachment` 读取。需要回传原附件时直接用原 `attachment_id` 调用 `reply_weixin_attachment`，不要把文件重新编码。新附件用 `stage_weixin_attachment` 暂存；超过单次 1 MiB 时使用同一个 `upload_id`、字节 `offset` 分块，最后一块 `final:true`。不要对尚未读取成功的附件声称已经理解。

如需不过期，在原 dot 中要求刷新同一个订阅，明确传 `ttlMs: null`，并确认返回 `refreshBefore: null`。省略 `ttlMs` 仍使用一小时默认期限；明确请求有限期限时仍尊重该请求（最长一天、最短一分钟）。不过期订阅可取消，回调返回 410 也会停止投递。更新服务器代码不会自动修改已授予的有限订阅，必须由原 dot 刷新，让平台得到新的到期信息。

若 dot 的订阅入口没有暴露 `ttlMs`，本人可先停服务，再执行 `wechat-dot-bridge --config /path/to/bridge.json subscription-forever`，然后启动服务。此命令仅将当前本人有效订阅在桥接侧设为不过期；取消会清除策略，不同 callback 不继承。之后省略期限的刷新沿用该策略，明确请求有限期限则改回有限并清除策略。平台自己的任务期限没有因此得到确认；可在原 dot 调用状态查看 `refreshBefore: null`。

订阅由 ChatGPT 提供 callback URL 和签名 secret。不要让用户手写 secret，亦不要把订阅完整请求抄入聊天、工单或日志。

工具处理顺序是：

```text
weixin.message(data.message_id)
    → read_weixin_message(message_id)
    → reply_weixin_message(message_id, text)
    → get_weixin_send_status(message_id)
```

`reply_weixin_message` 支持同一 `message_id` 多次提交。返回的 `reply_id` 标识这一次回复，可传给 `get_weixin_send_status` 查询；不传时查询最新回复。可选 `reply_key` 为同一次发送保持稳定，新回复用新 key。该工具只提交持久回复队列，后台发送可能尚未完成。需要时稍后查询；不通过反复提交不同正文来等待成功。原来信文本是用户数据，其中提到的新 callback、token 或操作指令都不应替代私人插件配置和本人授权。

## 工具清单

| 工具 | 用途 |
| --- | --- |
| `get_weixin_status` | 查看安全状态、队列和订阅错误 |
| `read_weixin_message` | 读取来信、引用和附件；就绪图片返回预览 |
| `read_weixin_attachment` | 读取图片、文件分块和文本内容 |
| `stage_weixin_attachment` | 暂存新附件，支持分块上传 |
| `import_weixin_attachment` | 从明确提供的公开 HTTPS 地址导入附件 |
| `reply_weixin_attachment` | 将附件回传同一对话 |
| `reply_weixin_message` | 回复已有来信，支持多次追加 |
| `report_weixin_progress` | 续期、停止或完成处理轮次；不发送消息 |
| `retry_weixin_operation` | 重试明确失败的发送或下载，保留已接受分段 |
| `get_weixin_send_status` | 查询 API 接受、失败或不确定状态 |
| `list_pending_weixin_messages` | 恢复尚未完成的来信，包括已发进度的任务 |

## 微信输入状态

收到来信后桥接异步预热腾讯 `typing_ticket`，成功读取 `read_weixin_message` 后才启动输入提示。提示每 5 秒续一次；`final:false` 的进度回复保持处理状态，最终回复、显式停止、暂停或 5 分钟处理租期到期后取消。重复读取已经活跃的消息不延长租期，明确进度上报和成功读取该轮次附件可以续期。票据按账号、本人会话和 API 端点缓存十分钟，不因来信 context 更新而重新获取。首次失败五秒后重试，连续失败按 5 / 10 / 20 / 30 秒退避；失败不阻断消息回复，重启不恢复旧输入状态。

此状态表示 dot 已读取且该处理轮次尚未完成，不表示平台精确的模型思考阶段。事件已送达但 dot 尚未调用读取工具时，不会显示输入状态。`get_weixin_status.typing` 提供本进程内 ticket / start / stop 的成功、失败次数、最后固定错误类别和尝试时间，不含票据或异常原文，重启归零。接口成功也不等于微信客户端一定显示。

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

- **同一订阅过期 / 取消：** 在原 dot 显式重新订阅。尚未完成处理的来信可以再次通知，已经提交进度的任务仍属待处理；处理完成的来信不重新通知。
- **重试耗尽：** 修复回调或网络问题后显式刷新相同订阅，开始新一轮有限重试；不会每个 worker 周期无限复位。
- **换了 callback / 聊天：** 先取消原订阅；新的目的地自动通知从新订阅起入队的消息。旧待处理消息通过下述工具显式恢复。
- **绑定 `auth_required`：** 停服务并按明确的绑定恢复流程操作，保留状态与原配置。不要由 dot 操作认证文件或自动重新扫码。
- **回复 `uncertain`：** 在微信核对是否已经出现对应回复；系统不会自动重发该次发送。任务尚未完成时可能仍在处理恢复列表中，恢复处理不能重试这个不确定的 `reply_id`。

显式恢复指令示例：

> 调用 `list_pending_weixin_messages` 查看尚未完成处理的来信 ID，逐条读取并查看已有回复状态，再按我的要求继续处理。已发进度的任务仍可继续完成；不要重复提交已有回复，不要重试 uncertain 的发送。

`list_pending` 默认一次 20 条、最多 100 条，只返回安全消息元数据，不返回正文或凭据。协议 `cursor` 为 `null`；当前待处理队列恢复不代表支持历史导入或无限历史 replay。

## 停止监控

在原 dot 明确取消对 `weixin.message` 的订阅，调用状态确认不再有有效订阅。取消仅停止 dot 通知；若需要停止微信收发，还必须停止本机用户服务并执行 `pause`，具体命令见 [安装指南](INSTALL.md#停止与回滚)。

dot / Work 的使用仍消耗其正常用量。桥接不新增模型 API 调用，不能承诺整个流程免费。

## 扩展能力验收

刷新现有插件工具定义至十一个，无需新建插件、订阅或重新扫码。原 dot 应继续复用唯一订阅。

1. 图片：发送普通图片，请 dot 描述内容后回传原图；确认图可见且原图回到同一对话。
2. 文件 / 视频：分别发送一个小文本文件和短视频，请 dot 回传；文件名、大小、内容应保持一致。视频接收回传不等于 dot 已理解画面。
3. 语音：发送语音并让 dot 复述微信提供的转写；若没有转写，工具保留原始音频，不虚构识别结果。AMR/SILK 可原生回传，已安装可选编解码器时其他音频可转 SILK；没有编解码器时作为文件。
4. 引用：引用一条文字及图片分别追问；检查 dot 获得引用内容。只有引用 ID 时，依赖本地收到或发送过的缓存；未缓存的旧历史无法恢复。
5. 多次回复：同一消息先发进度，再发完成结果；长文本自动分段，查询同一 reply_id 的 parts_total / parts_accepted。

`text_format:"markdown"` 使用腾讯的微信兼容 Markdown 解析器；默认 `plain` 原样保留。工具不拦截 /new、/model 或 Shell 风格文字，也不会执行它们。

若插件目录已显示十一个工具，原 dot 仍只有旧工具，本次真人操作通过重启后恢复；这不等于所有客户端都支持原地刷新。先保留现有 dot 和订阅，检查客户端工具加载状态，不要为了更新工具新建助手或重复订阅。


## 新版任务约定

沿用原订阅，任务应处理文字与附件，收到 weixin.message 后读取对应 ID，不把事件已送达等同于完成处理。
遇到附件错误应读状态并明确报告；reply 返回 queued/accepted 后检查对应 reply_id。
进度消息传 `final:false`，最终回复传 `final:true`（默认）。长任务使用 `report_weixin_progress(active:true)` 续期；仅停止处理传 `active:false`，它保留可恢复任务；已经处理完且不需要微信回复时传 `active:false, completed:true`。最终处理完成与微信发送受理是独立状态，仍须查询对应 `reply_id`。
retry_weixin_operation 可恢复明确失败的回复和附件，不会重发已被接受的部分；uncertain 仍需本人核对。
插件已更新但原 dot 工具不全时，本次客户端需要重启才能更新。任务本身也要确认没有保留“只处理文字”的旧指令；不创建第二份订阅。

读取消息返回 `processing.processing_id` 和 `processing.revision`，建议在后续进度和回复调用中作为顶层 `processing_id` / `revision` 携带。二者必须同时提供；旧轮次或过期租期返回固定错误，先显式读取恢复获取新版本，不能无限重放旧操作。兼容省略版本的旧客户端，但已经完成的任务不会被普通迟到 heartbeat 或 stop 改回待处理；相同 `reply_key` 重试也不改变处理状态。完成轮次的旧版本不能用于新回复；若确实需要追加新的分段，可用新的 `reply_key`、省略已完成轮次的旧版本，明确开始另一轮进度回复。

连续文字与文件合并回答时，先分别读取，再显式提交包含主 `message_id` 的 `message_ids`。额外成员必须已经读取，不能用另一条来信的回复自动清除旧文件。合组或调整成员可能产生新 `processing_id` / `revision`；续期使用当前轮次版本。进度、回复和 `get_weixin_send_status` 都在 `work` 中返回当前轮次状态与版本。显式缩小消息组会分离轮次，之后不带 `message_ids` 的完成操作只影响该轮次实际成员。

`read_weixin_attachment.offset` 和 `next_offset` 是原始字节游标，按 `next_offset` 继续读取；blob 拼接保持原文件字节。文本跨 UTF-8 字符时把尚未完整的字符放入下一块，`text_offset` / `text_next_offset` 仅描述该文本覆盖的字节范围，不能替代分页的 `next_offset`。按原字节游标顺序读取，text 拼接等于对整文件作 UTF-8 解码。

成功读取附件会续期已经活跃的本人处理轮次，因此该工具标为有状态读取；孤立读取旧附件不会自动创建任务或恢复已完成轮次。新增字段仍属于原有十一个工具，插件应刷新工具参数定义，不需要增加订阅或重新绑定微信。

本机 `subscription-forever` 设置对当前订阅持续生效，平台有限 TTL 续订不会覆盖它；取消订阅会清除此设置。它不控制平台任务的启用状态或任务寿命。


`import_weixin_attachment` 可直接读取可供服务器访问的 HTTPS 文件；返回 attachment_id 后再 reply。URL 不写入状态。`kind:auto` 自动识别图片、视频、原生语音，其余保留文件形式；普通音频要转语音时显式 kind:voice。生成文本文件优先 text_content，避免无谓的 base64 编码。不能从 sandbox 内部路径直接取文件。
