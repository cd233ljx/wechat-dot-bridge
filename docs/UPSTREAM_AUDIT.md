# 通道功能复核（2026-10-04）

本次依据本地已审查源码快照，不宣称覆盖上游未来版本：

- cc-connect dfad194：platform/weixin 下 weixin.go、parse.go、media_inbound.go、
  media_outbound.go、cdn.go、client.go 和对应测试。
- Tencent/openclaw-weixin 24de5c9：src/messaging 下 send.ts、send-media.ts、
  partial-quote.ts、quote-store.ts，src/api/types.ts 与 docs/quote-cache_zh_CN.md。

保留自研 dot 适配、持久队列和 MCP Events；cc-connect 的 Agent Engine、
斜杠命令、模型/会话选择、审批、群聊及主动推送不纳入本人私聊通道。

## 本次补齐

| 上游行为或产品缺口 | 当前处理 |
| --- | --- |
| cc-connect 同一来信 CDN 引用去重 | 重复附件描述只保存一次；内嵌引用与已有引用缓存命中同一附件时共享缓存，不再次下载 |
| Tencent 出站引用按账号与会话隔离 | 新增复合键引用缓存；旧缓存仅在历史证明属于当前唯一账号/会话时迁移，原表保留 |
| cc-connect 图片十六进制密钥无效时尝试媒体密钥 | 兼容两种密钥字段，合法图片不再因冗余坏字段下载失败 |
| 未支持的消息项夹带文字时整条被丢弃 | 保留已支持文字/附件，并返回明确的不支持类型占位，不转发未知字段内容 |

这不是增加额外的发送次数限制。未知类型占位让 dot 可以告知用户当前通道无法解析，
并不等于新增表情、位置、卡片等媒体的实际收发能力。

## 已有能力复核

文字分段、多次回复、输入提示、扫码/旧绑定导入、轮询 cursor、去重、
图片/文件/视频 CDN 加解密与上传、语音 ASR、部分引用与消息 ID 缓存、
Markdown 转纯文本、错误退避、限流冷却、绑定失效冷却均已有实现。
持久发送状态、uncertain 人工核实、不过期订阅策略、备份恢复、
故障提示和分段计时属于桥接自身的运维能力。

本次未增加 MCP 工具，仍为 11 个；不要求刷新插件、重建订阅或重绑微信。

## 保留的差异与验收边界

- OpenClaw 本地 Agent 可直接访问文件；云端 dot 的 sandbox 路径不属于本机。
  现有支持文本内容、分块字节和公开 HTTPS 导入，不能伪装成已打通任意云端文件。
- 微信不提供 ASR 时不新增识别模型。语音回传按本人要求移出必做验收。
- 原始视频回传不是视频理解。原始图片保留，给 dot 的预览会缩放。
- 引用仅能还原桥接见过的内容，服务端没返回出站消息 ID 时无法建立该回复的 ID 缓存。
- 附件保留默认 7 天/256 MiB，单文件 20 MiB；不照搬上游所有资源参数。
  消息正文默认长期保留；当前可显式开启已完成历史清理，待处理 / 未确认发送和去重墓碑保留，不随引用缓存清理而删除。配置见 OPERATIONS.md。
- 延迟优化、主聊天历史同步暂缓。视频、进度后长文、新生成 TXT 文件已由本人确认通过。
- 旧图片引用、文件回传和语音转写已经本人确认，不重复列为未实现。

回滚先停唯一服务，保留升级后原库，在新目录使用与旧版本兼容的升级前副本并核对新增来信 / 发送结果。旧代码未必识别新增引用、提示或分段语义；不要直接打开已升级的生产库，不用旧快照覆盖新消息。推荐前向修复，完整流程见 OPERATIONS.md。

## 等待提醒复核（2026-10-04）

针对连续文字和文件分别触发相同等待提示，复核 cc-connect
[dfad194 的 core/engine.go](https://github.com/chenhg5/cc-connect/blob/dfad19415a38b00b2c5c288610784d1a7eef337f/core/engine.go)：

- `SetEventIdleTimeout` / `processInteractiveEvents` 的事件循环在收到实际 Agent
  事件时重置 idle timer；绝对 turn deadline 是独立机制。超时处理与所管理运行的终止相连。
- `EventResult` 处理完成结果后推进 `pendingMessages`；`EventError` 和事件通道关闭
  有明确失败处理。连续来信由会话执行状态管理，而非逐条按来信年龄推断执行失败。
- `platform/weixin/weixin.go` 的 `StartTyping` 返回停止函数，进度展示与实际执行生命周期关联。

本桥接只接收 dot 的工具调用，既没有完整 Agent 事件流，也不拥有其执行器，不能照搬
cc-connect 的本地 Agent 会话或假装能终止超时任务。因此移除由 `message_unread` /
`processing_stalled` 自动触发微信提示的路径，保留这两项诊断及待处理消息；
只对明确的回复发送失败保留一次性提示，继续排除 uncertain。没有新增会话冷却阈值，
没有把另一条消息的回复作为旧任务完成依据。这是借鉴上游生命周期边界后的桥接适配，
不是声称已实现 cc-connect 的完整 Agent Engine。


## 输入提示启动复核（2026-10-04）

继续参考同一 cc-connect 快照 `platform/weixin/weixin.go` 的 `getTypingTicket` /
`StartTyping`：按会话缓存十分钟票据，每五秒续发。桥接此前把 context token 纳入
缓存键，导致连续来信反复获取票据；现改为账号、本人会话、API 端点范围复用。
保留桥接的独立异步 worker；五分钟有界处理租期仍从成功读取或显式进度开始，
不能把收到事件冒充 dot 已开始处理。

cc-connect 首次输入请求失败会结束该次输入提示；本桥接保留有界重试，但从原来
固定等待三十秒改为五秒起步、最高三十秒的指数退避，避免短回复期间无法恢复。
这是桥接自身适配，不是上游的重试参数。获取票据后重新检查处理租期，防止已经
结束或到期的任务启动输入状态。新增仅含固定类别和计数的进程内诊断，便于区分
尚未读取与输入接口失败，既不记录凭据，也不影响消息回复。

## 完整链路与技术栈审查（2026-10-04）

审查基线：bridge `9ffdd66`；cc-connect `dfad19415a38b00b2c5c288610784d1a7eef337f`。
本轮通过远端 HEAD 核对，本地 cc-connect 快照与查询时的远端一致。
对照范围包含微信 adapter 的收发 / 媒体 / typing / context / 限流、Engine 的
会话锁 / 等待队列 / EventResult / EventError / 取消，以及相关测试。
这是设计审查，不是“已按上游完成全部重构”的声明；以下问题尚未修复。
复现只用临时数据库、合成字节和模拟 callback，没有读取生产正文或发送微信。

### 已复现的问题

| 优先级 | 触发与当前结果 | 根因与修改方向 |
| --- | --- | --- |
| P1 | `final=False` 的进度回复一入队就从 `list_pending` 消失；发送受理后消息状态变成 `replied`。续期到期或进程中断后，恢复入口不能列出这个未完成任务 | `store.py:list_pending / queue_reply / finish_send` 将是否提交回复、是否受理发送当成任务状态。持久化独立处理轮次和完成标记；恢复应能列出已经发过进度但没有终结的任务 |
| P1 | 先提交进度，再提交完成回复，再重放原进度的同一 reply_key；发送被去重，但 typing 重新变为可启动 | `queue_reply` 的去重分支之后仍无条件调用 `report_progress`，MCP 又更新内存租期。幂等需要覆盖全部状态副作用，旧进度重试不能反向覆盖新完成状态；保存轮次 / 版本及 final 语义 |
| P2 | 同一批十条 ready 来信，模拟回调的投递位置为 `5,10,2,7,6,9,4,3,8,1` | `events.py:deliver_once` 对相同 next_attempt 按 event_id 的哈希排序。首次投递需保留入站序号；重试和未就绪媒体的越过策略应显式定义。平台 ACK 只证明接收通知，仍不等于执行顺序 |
| P2 | 合成 UTF-8 文件在第 131072 字节跨越一个汉字，拼接各块 text 不能还原原文；拼接 blob 可以完全还原 | `mcp.py:read_weixin_attachment` 每块单独 `decode(errors='replace')`。文本分块需有字符边界或可续接的解码语义，二进制 offset 与文本游标不能混淆；同时减少 text + base64 的重复上下文负担 |
| P2 | 发送明确返回 rate_limited 后，HealthMonitor 又给同条来信排入一条状态消息 | `operations.py:HealthMonitor.tick` 只检查 reply_failed 类别，没有排除 rate_limited。worker 的三十秒冷却仅延迟该通知；应与 cc-connect 一样避免在限流失败后追加失败通知 |

复现结果摘要：`interim_reply_missing_from_recovery=True`、
`interim_reply_state=replied`、`replaying_old_progress_reopens_typing=True`、
`rate_limit_generates_extra_notice=True`、`utf8_text_roundtrip_matches=False`、
`binary_roundtrip_matches=True`、`batch_callback_order_matches_ingest=False`。
这些是修复前观察，不是预期验收值，也不代表已证明它们都是当天生产现象的原因。

### 生命周期层缺口

cc-connect 的 [Engine](https://github.com/chenhg5/cc-connect/blob/dfad19415a38b00b2c5c288610784d1a7eef337f/core/engine.go)
先创建可接收等待消息的会话状态再抢执行锁；繁忙时只排队，收到终结 `EventResult`
再推进下一项，非终结结果不触发完成副作用。它还覆盖启动时来信、释放锁与排队竞争、
会话死亡后的等待消息处理。我们目前只有逐消息通知和逐回复 outbox，没有等价的
“哪一次处理认领了哪些来信、是否仍在运行、是否终结”的持久层。

因此，连续文字和文件由 dot 合并回答时，桥接没有显式记录合并关系的入口；
另一条消息的回复不能安全证明旧文件任务已完成。先前移除等待提醒只解决误提示，
没有解决这层关联关系。不能通过“同会话最近回过话”自动清空旧来信。

建议建立三套独立状态，而非继续复用 messages.state：

1. 来信 / 事件：已持久接收、媒体准备、待通知、通知已 ACK；带稳定入站序号。
2. 处理轮次：待领取、处理中、已完成 / 失败 / 中断；明确关联 message_ids，保存
   领取版本与租期。dot 可显式说明一次处理覆盖了哪些已经读取的来信；终结必须幂等。
3. 微信发送：queued / sending / accepted / failed / uncertain；分段和幂等键沿用。
   处理完成与发送被受理分别报告，不能把 accepted 当送达。

领取与续期失效后应进入可恢复状态，不能凭超时自动宣布 dot 失败或启动第二个模型。
租期和终结操作需要版本检查，避免迟到进度复活旧轮次。事件通知可以继续作为唤醒信号；
在没有显式领取 / 完成协议前，不能贸然通过“只发第一条事件”把后面的消息永久堵住。
这部分是适配云端 dot 的方案，不是声称 cc-connect 的内存队列可以直接作为我们的持久队列。

### 输入与附件的剩余差异

[微信 adapter](https://github.com/chenhg5/cc-connect/blob/dfad19415a38b00b2c5c288610784d1a7eef337f/platform/weixin/weixin.go)
在收到来信后预热 typing ticket，Engine 在真正处理时启动输入状态并管理停止函数。
我们上一轮只完成票据复用与重试，尚未做收信后的异步预热；附件分块读取也没有关联
处理轮次来续期。应由明确的处理轮次统一输入状态和有效活动，保留有限期限，避免
诊断读取 / 旧消息读取被误认为新的任务处理。客户端最终是否显示仍不由桥接保证。

cc-connect 的 [SaveFilesToDisk](https://github.com/chenhg5/cc-connect/blob/dfad19415a38b00b2c5c288610784d1a7eef337f/core/message.go)
把附件保存为消息范围内的独立文件供本地 Agent 使用。云端 dot 无法读取本机路径，
我们仍需附件工具，但应提供明确的内容类型、大小、完整性与分页语义；先修文本边界，
再验证平台支持的原生文件资源方式，不能捏造本机与云端 sandbox 互通。

### 技术栈判断与社区组件

当前依赖入口是 `pyproject.toml` 和 `codecs/package.json`：主进程为 Python 3.11+，
存储使用标准库 SQLite；并发、MCP 分发、参数校验、HTTP/DNS 与 webhook 签名主要手写。
第三方必需 Python 运行依赖仅 cryptography 和 Pillow。已有官方 Tunnel、systemd、
silk-wasm、ffmpeg 和腾讯 Markdown 过滤器复用，并不是所有底层都自行实现。
问题在于通用协议基础设施承担得过多，同时业务状态模型未单独建立。

- **MCP SDK 优先替换手写协议层。** 官方 [Python SDK v2](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
  已明确支持 2026-07-28、server/discover 和 stdio，不能再以“新协议必然只能手写”作为默认理由。
  本轮另外检查 SDK 源码 `2118f14f8a19bc158d8a1cf90af58d85d187f849`：
  `src/mcp/server/extension.py` 有新增方法扩展；但未找到本项目的 `events/subscribe` /
  `events/list` webhook 实现，默认能力生成也没有 events 项。与
  [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events) 的差异
  需要薄适配与合约测试，不能拿 SDK 的 subscriptions/listen 冒充 webhook。
- **网络与退出协调应集中。** [HTTPX 的 transport 接口](https://www.python-httpx.org/advanced/transports/)
  允许定制网络实现，可评估统一连接池、超时与测试入口；IP 固定和代理 CONNECT
  等安全约束仍需验证，不能换成默认客户端就算兼容。微信发送在写出后超时必须保留 uncertain，
  不挂通用自动重试装饰器。配合 SDK 的 AnyIO 统一任务取消，避免继续累积独立线程退出规则。
- **依赖需可重现。** Python 目前只有版本范围，Node 已有 package-lock；采用
  [uv 的锁定 / 同步机制](https://docs.astral.sh/uv/concepts/projects/sync/)
  固定开发与部署环境，更新通过验证后显式推进；锁住应用依赖不等于必须给库依赖全写死。
- **签名优先参考维护中的库。** [Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks)
  有 Python 参考实现；其价值包括互操作测试。采用前测试 UTF-8 原始字节、UTC 时间与双签名轮换，
  不为替换二十行 HMAC 而引入不同协议行为。
- **SQLite 与 systemd 可以保留。** 它们本来就是成熟基础设施，单账号场景不需要仅为“框架化”
  引入 Redis / Celery / Kubernetes。数据库原子入站 + cursor、持久 outbox、uncertain 保护和
  私有权限检查值得保留；应在其上建立独立业务状态，而非从头重写存储。

### 可审查的实施顺序

1. 先将本轮五个合成复现固化为修复回归，修正排序、UTF-8 和限流通知；不改生产绑定或订阅。
2. 定义处理轮次及其与来信 / 回复的关系，覆盖进度回复后崩溃、重复工具请求、迟到续期、
   多消息合并完成和 uncertain。只有这层明确，才能统一 typing 与恢复入口。
3. 在隔离环境用官方 MCP SDK 跑现有工具和 webhook 的线协议合约测试；确认日志与遥测脱敏，
   再迁移分发与校验。自研仅保留确实缺失的 Events 适配和微信业务边界。
4. 引入锁文件，逐步收敛网络和运行时；一次只迁移一个边界，不在同次升级叠加状态库、SDK、
   网络库全部替换。每步都有模拟重启 / 升级验收后再更新生产。

本轮修改仅记录审查结果和社区优先原则，没有对生产做上述迁移，也没有关闭功能来冒充设计完善。
