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
