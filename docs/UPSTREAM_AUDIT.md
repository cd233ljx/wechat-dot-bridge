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
以下保留修复前的设计审查和复现基线。五项问题已在后续实现中修复，
当前代码、社区组件采用和新增验收见本文件末尾“本轮实施结果”；
本文件不把本地测试等同于生产已部署或微信客户端已确认。
复现只用临时数据库、合成字节和模拟 callback，没有读取生产正文或发送微信。

### 修复前已复现的问题

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

审查时依赖入口是 `pyproject.toml` 和 `codecs/package.json`：主进程为 Python 3.11+，
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

以上是实施前的审查记录，以下记录当前代码实际完成的变更。生产升级与本人手机验收
按独立的部署记录确认，不由这份设计文档推断。

## 本轮实施结果（2026-10-04）

参考仍固定为 cc-connect `dfad19415a38b00b2c5c288610784d1a7eef337f`，
腾讯通道参考为 `24de5c9`。复用对象是微信 adapter、实际处理事件与会话成员的设计；
本项目仍是已有 dot 的通道，没有引入它的 Agent Engine、模型执行器或第二个助手。

### 原五项复现的修复结果

| 修复前问题 | 当前行为与回归 |
| --- | --- |
| 进度回复进入 outbox 后任务从恢复列表消失 | `message_work` 持久保存处理轮次；`final:false` 不终结任务，已接受进度仍能由 `list_pending_weixin_messages` 找到。进程重启保留中断任务，发送状态独立 |
| 重放旧进度开启 typing | reply 幂等同时校验正文、附件、`final` 和显式消息范围，去重分支不更新处理状态；typing 只同步持久轮次。普通迟到 heartbeat / stop 不改变完成终态；旧版本被拒绝 |
| 同批事件按哈希排序 | 首次投递按持久入站 `messages.rowid`；未就绪媒体可被越过，就绪后进入当前首次投递序列。退避中的重试不阻塞后续消息，到期重试按期限有界优先，上限仍为五次 |
| UTF-8 跨块损坏 | `attachment_content.py` 用 Python 标准增量 UTF-8 codec 重建边界；完整字符留在下一块文本。字节 offset / next_offset 和 blob 保持精确，文本范围独立标注；中文、表情、无效 UTF-8、乱序读取与重试均有回归 |
| 限流后追加状态通知 | `rate_limited` 保留失败状态和本机诊断，不追加微信通知；同来信较早的其他失败不能绕过此保护。`uncertain` 仍不提醒、不重发 |

本轮还补齐了审查指出的处理关联：读取返回轮次 ID 和 revision，后续进度与回复
可携带版本；合并处理只覆盖显式指定、已读取的本人来信。缩小成员集合会分离轮次，
之后默认完成仅影响当前成员，不会误收尾被排除的旧文件。分组恢复同步各成员的有限租期。
回复与 `get_weixin_send_status` 返回当前 `work`，调用者可取得合组后的新版本。

收信后异步预热 typing 票据，真正读取后才显示输入提示。成功读取活跃轮次的附件续期，
诊断或旧附件读取不会自动认领任务。输入状态仍是 best effort；微信接口成功不能保证
手机恒显示，也不能据此推断 dot 的模型内部事件。

### 已采用的社区基础设施

当前应用环境由 `uv.lock` 固定。以下版本来自本轮锁文件，不是对上游最新版本的声明：

| 组件 | 锁定版本 / 职责 | 保留适配的具体原因 |
| --- | --- | --- |
| 官方 MCP Python SDK | 2.3.0；JSON-RPC 类型、解析、分发、stdio 与取消 | OpenAI 的三个 webhook `events/*` 方法和既有 Tunnel 请求形态需要薄适配；不能用 SDK 资源订阅冒充 webhook |
| AnyIO | 4.15.1；协议与 worker 任务组、退出协调 | 同步 SQLite 和网络工作通过受控线程执行；业务事务、唯一进程锁与 uncertain 仍由桥接维护 |
| jsonschema | 4.26.0；工具 Schema 校验 | 本人权限、处理成员、幂等与发送语义仍需业务层验证 |
| HTTPCore | 1.0.9；统一 HTTP framing、CONNECT、响应解析 | 默认 backend 不提供本项目要求的公网 IP 固定、原 hostname TLS、总体期限和所有请求的取消；`net_transport.py` 提供受测的有限适配 |
| Standard Webhooks | 1.1.0；精确原始 body 字节签名 | 桥接保留协议指定 header、UTC 时间与短期双签名轮换 |
| uv | 开发、CI、源码生产部署统一 `uv sync --locked --all-extras` | 部署时拒绝过期锁文件，不现场重新解析另一套版本；升级依赖需显式更新锁并验证 |
| SQLite / systemd / 媒体组件 | 保留标准库 SQLite、用户服务、cryptography、Pillow、silk-wasm、ffmpeg 与腾讯 Markdown 过滤器 | 当前单账号无需新增外部队列、数据库服务或 Web 框架；Node 使用原 package-lock，编解码器仍是可选本地依赖 |

选择 HTTPCore 直接承担共同 HTTP 机制，而不是在每个通道继续复制 HTTP 客户端。
微信 API、回调、登录、媒体 CDN、公开文件导入共用该边界；端点授权和固定错误类别
仍由各业务入口控制。库设置 `retries=0`，原有媒体上传的有界重试不扩展为微信发送自动重试。
写出请求后超时或退出取消继续保留 uncertain；跨进程重启不重发。

MCP 薄适配有一个明确的维护例外：SDK 的公开自定义 reader 接口不能同时提供桥接的
2 MiB 帧限制和子进程 stdin 隔离，因而复用锁定 2.3 系列中的
`mcp.server.stdio._claim_fd` / `_open_stdin_diversion`。它使 handler 子进程得到 EOF，
不会消费 MCP 帧，并在退出时恢复描述符。依赖范围固定为 `>=2.3.0,<2.4`，当前锁到
2.3.0；更新时必须审查私有入口并重跑 stdio、帧限制、EOF、取消与错误脱敏合约。
这是已验证的适配差异，不把 SDK 升级兼容性当作自动保证。

### 验证与部署边界

回归使用临时私有 SQLite、合成消息和 mock / 本地网络，不访问生产正文或真实 callback。
新增覆盖进度后恢复、幂等重放、迟到版本、多消息合并 / 缩组、附件跨 UTF-8 字符、
限流通知、取消窗口、已受理分段与 uncertain。官方 SDK 线协议覆盖 discovery、旧 initialize、
工具清单 / 参数、webhook Events、帧上限、子进程 EOF、通知和取消，错误 / 日志保持脱敏。

CI 从提交的锁文件安装依赖，在 Python 3.11 / 3.14 运行 Ruff 和共享发布检查；
发布检查包括 required unittest、模拟升级、秘密扫描、源码包 / wheel 资源与许可、
checkout 外安装 smoke，sdist 包含 uv.lock。生产沿用原绑定、原订阅和私有运行目录，
先停唯一服务并备份，再同步锁定环境。部署成功和本人微信端实际显示效果分别验证。

## 最终架构核验（2026-10-04）

核验基线为 bridge `28adf13`，下述修复随本轮后续提交交付。
cc-connect 固定为 `dfad19415a38b00b2c5c288610784d1a7eef337f`；再次核对远端 HEAD
仍为该版本，本地参考工作区无修改。审查完整微信 Platform、Engine、Session 及相关测试，
不是只摘取 typing 或发送片段。当前环境没有 Go，本轮读取上游测试，不声称运行过它们。

**结论：在本人单账号、已有 dot、私人 MCP 桥接的范围内，无需整体重构。**
应保持社区基础设施和持久事务，修正生命周期边界，后续按实际扩展整理接口。

| 对照点 | CC Connect 的完整设计 | 本桥接的适配与判断 |
| --- | --- | --- |
| 收信与媒体 | 微信 Platform 负责轮询、上下文、媒体与 typing，再交给 Engine | `IlinkClient` / worker 接收并持久保存消息和 cursor，媒体就绪后通知；保留 |
| 处理与完成 | Engine 拥有 AgentSession，非终结结果不结束 turn，终结后推进 pending | dot 拥有执行器，桥接保存 `message_work`，进度与完成分开；不复制本地执行队列 |
| 旧轮次隔离 | Session lock generation 防止旧 holder 影响新 turn | 成员调整时，所有来源组及遗留成员的旧标识一起失效；本轮补齐 |
| 停机取消 | context 取消与执行失败分开；输入停止使用独立三秒 background context | 通知 / GET 下载保持可恢复，停止输入有独立总期限；本轮补齐 |
| 发送结果 | Platform 发往通道，Engine 管其执行结果；sendMessage 接受 HTTP 200 空响应 | outbox、已受理分段和 `uncertain` 独立持久保存；补齐空响应兼容 |
| 基础设施 | 平台、会话与执行器各有职责 | 官方 MCP SDK、AnyIO、HTTPCore、jsonschema、Standard Webhooks、SQLite 与 systemd 已承担通用机制；保留 |

本轮修复四项已复现缺口：

1. A/B/C 拆为 A/B 后，C 的旧 token 还能重并并完成新版 A/B。现在新范围和所有来源组
   的遗留成员都更换轮次标识；遗留状态、租期与读取记录保留，回复和分组变化一起原子提交。
   参考 [Session generation fencing](https://github.com/chenhg5/cc-connect/blob/dfad19415a38b00b2c5c288610784d1a7eef337f/core/session.go#L65)。
2. callback / CDN 的取消类别被当成永久失败，未完成 claim 又消耗重试预算。
   现在取消和进程中断保留同一通知 ID / body 或待下载附件，并退回未完成 claim 的次数；
   真实超时 / 临时错误仍有限重试。微信 POST 不参与该恢复，仍为 `uncertain`。
3. 全局取消后 `typing.close` 的停止请求必然被拦住。现在仅固定 `sendtyping/status=2`
   可在独立的三秒总期限内做清理，DNS 和请求共享期限，业务取消信号不被重置。
   刷新被取消时保留待清理目标，避免普通停止请求失败后提前丢掉最后一次清理机会。
   参考 [微信输入状态清理](https://github.com/chenhg5/cc-connect/blob/dfad19415a38b00b2c5c288610784d1a7eef337f/platform/weixin/weixin.go#L681)。
4. sendmessage 的 HTTP 200 空或纯空白响应原来被解析成 `response_invalid` / `uncertain`。
   按上游只对该发送接口接受空响应，持久记为 `accepted`；轮询 / 上传 URL 获取仍要求 JSON，
   非空错误响应仍校验，且 `accepted` 只表示接口受理，不表示微信端确认送达。
   参考 [sendMessage 响应处理](https://github.com/chenhg5/cc-connect/blob/dfad19415a38b00b2c5c288610784d1a7eef337f/platform/weixin/client.go#L175)。

剩余维护项可以渐进处理：Events 表结构的后续变更纳入统一版本迁移；通用 DNS / 代理
辅助从 Events 私有方法收敛到网络层；为 Store mixin 补内部宿主契约；减少 SDK / 工具定义
和运维 helper 的延迟导入回边。当前未复现这些耦合导致的运行故障，不把它们列为开源阻碍。
Store 的 outbox / parts / work 原子事务必须保留，不能拆成各自提交的仓库。

通知 ACK 不等于 dot 开始或完成执行。无轮次 token 的旧调用维持兼容语义，不能声称能识别
旧执行者；附件读取只续期当前关联的活跃本人工作，不完成或复活任务。已由旧版写成失败的
记录不能自动推断为中断，以上修复不追溯重放它们，也不承诺微信客户端恒显示输入提示。

本轮最终验证：Python 3.11 / 3.14 各 315 项 unittest 通过、零跳过；锁文件、Ruff、
升级演练、完整本地历史秘密扫描、源码包 / wheel 检查与 checkout 外安装 smoke 均通过。
新增默认 HTTPCore 路径、EOF 协调退出及跨重启回归；仅替换合成网络端点，不访问生产。
本次修复在本地完成验证，尚未部署生产，也未发布公共仓库。
