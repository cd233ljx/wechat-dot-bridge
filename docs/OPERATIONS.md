# 运行、诊断和恢复

以下命令中的配置路径属于部署者自己的安装，示例使用 `CONFIG` 占位。不要输出配置全文、原始网络日志或数据库内容。

## 日常检查（服务运行期间可用）

```bash
wechat-dot-bridge --config CONFIG status
wechat-dot-bridge --config CONFIG diagnose
wechat-dot-bridge --config CONFIG subscriptions
wechat-dot-bridge --config CONFIG queue
```

`diagnose` 只返回消息 ID、固定错误类别和等待时长。`subscriptions` 返回不含回调地址和密钥的订阅 ID、状态与期限。`queue` 返回尚未解决的回复 ID，不含正文。

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
5. 回滚先停止服务，再切回兼容旧 commit。**新增 `is_notice` 后，不要直接切回不认识该列语义的旧版：旧代码可能把桥接提示当成 dot 回复。**先在副本演练，或优先前向修复。不要恢复旧库覆盖升级后收到的消息。

仓库提供 `python3 scripts/verify-upgrade.py --old-revision 4d87a28`，在临时目录用模拟数据验证旧模式升级、已发送分段、不确定状态及备份恢复；不连接微信和真实数据库。

## 从备份恢复到新目录

```bash
wechat-dot-bridge --state-dir /PRIVATE/EMPTY-RECOVERY restore-backup /PRIVATE/snapshot.sqlite3
wechat-dot-bridge --state-dir /PRIVATE/EMPTY-RECOVERY diagnose
wechat-dot-bridge --state-dir /PRIVATE/EMPTY-RECOVERY queue
```

目标必须没有数据库；工具拒绝覆盖现有库或符号链接，备份必须仅本人可读。恢复副本的微信收发默认暂停，保留绑定、订阅与历史。先确认原接收服务停止，解决发送不确定状态，再把部署配置指向恢复目录并显式恢复收发。不要同时运行原库与恢复库。
