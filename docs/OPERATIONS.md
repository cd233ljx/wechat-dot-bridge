# 运行、诊断和恢复

以下命令中的配置路径属于部署者自己的安装，示例使用 `CONFIG` 占位。不要输出配置全文、原始网络日志或数据库内容。

## 日常检查（服务运行期间可用）

```bash
wechat-dot-bridge --config CONFIG status
wechat-dot-bridge --config CONFIG diagnose
wechat-dot-bridge --config CONFIG storage
wechat-dot-bridge --config CONFIG subscriptions
wechat-dot-bridge --config CONFIG queue
```

`diagnose` 返回消息 ID、固定错误类别和等待时长，并报告容量告警。`storage` 只返回数据库 / 附件字节、可复用空间、磁盘可用字节、各表计数与固定告警，不含路径、正文、绑定、回调 URL 或密钥。`subscriptions` 返回不含回调地址和密钥的订阅 ID、状态与期限。`queue` 返回尚未解决的回复 ID，不含正文。

来信超过五分钟未处理、且 dot 没有报告有效的处理进度时，桥接会在原微信对话发送一次“桥接状态”提示。已明确失败的回复也可触发提示。一条来信最多一次提示，跨重启去重；每轮最多入队一条。不调用模型，不推送无来信通知，不把提示当作 dot 已完成，也不清空待处理来信。

若微信绑定不可用、回复上下文已失效或整个服务没运行，微信提示本身也可能无法送达，此时必须使用本机检查。平台任务是否启用仍须在原 dot 检查；不能由 acknowledged 推断模型已经执行。

## 修改状态（先停止唯一服务）

```bash
systemctl --user stop wechat-dot-bridge.service
wechat-dot-bridge --config CONFIG backup /PRIVATE/NEW-snapshot.sqlite3
wechat-dot-bridge --config CONFIG cancel-subscription SUBSCRIPTION_ID
# 或：
wechat-dot-bridge --config CONFIG subscription-forever
wechat-dot-bridge --config CONFIG retry-send REPLY_ID
wechat-dot-bridge --config CONFIG retry-attachment ATTACHMENT_ID
systemctl --user start wechat-dot-bridge.service
```

按实际需要选择命令，不要逐行执行整个示例。修改命令取得 worker 锁；服务仍在运行时会拒绝，不会启动第二个收信服务。取消订阅仅清理所选订阅及其待投递通知，不清空微信绑定、来信、回复或附件。

`uncertain` 表示服务器无法确定一次发送是否已经到达。核对微信里**当前不确定的那一段**后，使用：

```bash
wechat-dot-bridge --config CONFIG resolve-send REPLY_ID --outcome delivered
# 确认没收到才用：
wechat-dot-bridge --config CONFIG resolve-send REPLY_ID --outcome not-delivered
```

`delivered` 仅确认当前未确认分段，后续未发送段继续排队；不把整条长文误判完成。`not-delivered` 允许重试该段。这个人工决定不开放给 dot 自动猜测。

## 升级与回滚

1. 在独立 checkout / worktree 安装依赖并运行完整测试。
2. 停止唯一服务，保存私有新备份，记录旧 commit。
3. 快进生产源码；新可选依赖安装在专用目录，不升级全局 Python/Node。
4. 启动原服务，核对绑定、订阅数量、不过期策略与队列；用微信验证。
5. 优先前向修复。需要回滚时先停服务，在新恢复目录使用与旧 commit 兼容的升级前快照，保留升级后原库并人工核对新来信与发送结果；不要让旧代码直接打开已升级的生产库，也不要用旧快照覆盖升级后收到的消息。旧版可能不理解 `is_notice`、媒体 / 分段或去重墓碑语义，不能仅凭“新增表 / 列”判定可直接回退。

启动时 `migrations.py` 使用 `PRAGMA user_version` 记录数据库版本。所有尚未应用的迁移和版本标记在一个事务提交；故障或进程中断回滚，重启从上次已提交版本恢复。未版本化旧库在首次迁移补齐旧回填。高于当前支持范围的版本会以 `database_schema_unsupported` 拒绝打开；遇到该类别保留数据库并使用兼容版本，不能修改版本号绕过检查。

仓库提供 `python3 scripts/verify-upgrade.py --old-revision 4d87a28`，在临时目录用模拟数据验证旧模式升级、已发送分段、不确定状态及备份恢复；不连接微信和真实数据库。

## 从备份恢复到新目录

```bash
wechat-dot-bridge --state-dir /PRIVATE/EMPTY-RECOVERY restore-backup /PRIVATE/snapshot.sqlite3
wechat-dot-bridge --state-dir /PRIVATE/EMPTY-RECOVERY diagnose
wechat-dot-bridge --state-dir /PRIVATE/EMPTY-RECOVERY queue
```

目标必须没有数据库；工具拒绝覆盖现有库或符号链接，备份必须仅本人可读。恢复副本的微信收发默认暂停，保留绑定、订阅与历史。先确认原接收服务停止，解决发送不确定状态，再把部署配置指向恢复目录并显式恢复收发。不要同时运行原库与恢复库。

## 容量与保留策略

默认消息与回复历史长期保留，附件预算不是整个数据库的容量上限。`storage` 的 `database_bytes` 包括 SQLite 主文件及现存 journal / WAL / SHM，`database_reusable_bytes` 是可被后续写入复用的页；删除记录不会保证文件立即缩小或释放同等磁盘空间。私人备份另占空间，不包含在这个数据库计数中。

`maintenance_policy` 在私人 `bridge.json` 中可选配置；下例表示保留符合清理条件的已完成历史至少 30 天：

```json
{
  "maintenance_policy": {
    "history_days": 30,
    "disk_warning_mib": 512,
    "database_warning_mib": 1024
  }
}
```

| 参数 | 默认与含义 |
| --- | --- |
| `history_days` | `0`，关闭自动消息历史清理；可设 `1`–`3650` 天 |
| `disk_warning_mib` | `512`，可用磁盘低于阈值返回 `disk_space_low` |
| `database_warning_mib` | `1024`，数据库字节大于阈值返回 `database_large` |

告警不停止收发，不是数据库硬上限。配置变更先停服并备份，重启后 `storage` 核对策略。服务每小时执行附件维护；`history_days > 0` 时同时分批清理已完成历史。

手动操作先停止唯一服务并保存私人备份。默认只预览，追加 `--apply` 才执行；`--days` 覆盖本次天数，不改配置：

```bash
wechat-dot-bridge --config CONFIG cleanup-history --days 30
wechat-dot-bridge --config CONFIG cleanup-history --days 30 --apply
wechat-dot-bridge --config CONFIG storage
```

每轮最多处理 500 条。只选择超龄且所有回复已 `accepted` 的已完成来信；近期回复 / 时间线、待处理、非 `accepted`（包括 `uncertain` / 失败）发送、活跃进度、待投递事件、共享附件、最新 context 和最高消息 rowid 均保留。清理移除对应正文 / 历史及可删除附件，保留不含正文或凭据的 opaque hash 去重墓碑，旧微信消息不会重新入队。引用缓存可能返回 `cache_expired`；不能从被清理记录恢复旧内容。

去重墓碑会随历史消息继续少量增长；保留条件也可能使某些很旧记录无法删除，不能承诺绝对容量上限。清理不删除旧私人备份中的内容，也不等于安全擦除 SQLite / 磁盘上历史字节。备份及磁盘预算仍由部署者管理。

## 私人备份轮换

单次 `backup /PRIVATE/NEW-snapshot.sqlite3` 保留原有手工快照行为。希望轮换时使用专用 `0700` 目录，先停服务：

```bash
wechat-dot-bridge --config CONFIG backup-managed /PRIVATE/bridge-backups --keep 7
```

先生成并校验新的 `0600` SQLite 快照，成功后才保留最新 7 份并移除多余的本工具 `bridge-backup-*.sqlite3` 文件；失败不轮换旧备份。其他命名的手工备份（例如 `before-*`）保留，不在专用目录存放其他恰好匹配本工具命名的文件。拒绝符号链接、不安全权限或硬链接文件，结果只报告完整性、保留 / 移除计数，不输出快照路径或内容。

备份不会自动定时创建。恢复时由操作者在私人目录选定快照，并使用上面的 `restore-backup` 流程；生产与恢复实例不能同时收信。

## 分段延迟测量

`wechat-dot-bridge --config CONFIG latency --limit 10` 或追加
`--message-id MESSAGE_ID`，只输出关联 ID、时间和状态，不输出正文、凭据或回调 URL。
记录长轮询返回、事件排队/投递/每次回调结果、成功读工具开始/结束、
回复入队、发送 worker 开始及首次微信接口受理。工具处理时间使用单调时钟；
阶段时间使用本机时钟。微信消息创建时间来自另一时钟，不用于本地阶段差值。
事件到 ACK 包含重试等待，单次回调耗时见 timeline.elapsed_ms。
发送 worker 开始到受理可能包含媒体上传；首次受理不代表所有分段送达。
工具处理时间不包含平台调度、Tunnel 传输或客户端渲染。
旧消息缺失阶段为 null，不反推或伪造时间。读工具关键时间另存 timeline，
不受最近 1000 条工具诊断记录轮换影响。

先连续测三条纯文字，每条收到回复再发下一条；比较 receive_to_event、
callback_to_read、read_server、reply_tool_server、queue_to_send 等阶段。
计时只增加观测，不调整轮询/批处理/重试策略。回滚遵循上面的兼容副本流程，
保留原库和升级后消息，不用旧快照覆盖生产数据。
