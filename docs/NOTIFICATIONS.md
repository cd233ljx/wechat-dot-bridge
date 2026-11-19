# 本机微信通知接口

已有 `serve` 服务会同时提供本接口。独立使用时，先在源码目录运行 `uv sync --locked --all-extras`，按[微信绑定说明](INSTALL.md#3-导入绑定或扫码)选择导入或扫码。只使用通知时可以跳过 Tunnel 配置，直接指定状态目录。确认原接收端已停止后运行：

```bash
export WDB_STATE="$HOME/.local/share/wechat-dot-bridge"
uv run --locked wechat-dot-bridge --state-dir "$WDB_STATE" resume --confirm-single-consumer
uv run --locked wechat-dot-bridge --state-dir "$WDB_STATE" serve-local
```

先在微信向已绑定会话发一条消息，服务需要该会话的上下文。已有服务使用配置中的实际状态目录。两种启动模式共用进程锁与唯一发送队列。独立模式消费来信以更新发送上下文，期间收到的来信不会排入 dot 待办。需要同时聊天时使用原有的 `serve`。

### 提交

业务程序负责验证自己的 webhook、决定发送时机并生成正文。以桥接服务的系统用户，通过私有目录（0700）中的 Unix socket（0600）发送 HTTP 请求。没有 TCP 监听。

```bash
curl --unix-socket "$WDB_STATE/notify.sock" http://localhost/v1/notifications \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: backup-job-001' \
  -d '{"text":"服务器备份完成"}'
```

请求仅接受 `text`（最多 64 KiB UTF-8）和可选的 `expires_at`（Unix 秒，每段发送前检查）。接收方固定为当前绑定的本人。每个事件使用唯一的 `Idempotency-Key`，长度 1–128，允许 ASCII 字母、数字和 `_.:-`。同键同内容及相同截止时间返回原记录，冲突返回 409。请求超时可用完全相同的请求查询或恢复提交。

### 核对结果

202 表示已排队或正在发送。用响应中的 `id` 查询：

```bash
curl --unix-socket "$WDB_STATE/notify.sock" "http://localhost/v1/notifications/$NOTIFICATION_ID"
```

`accepted` 表示微信上游受理了全部分段，手机是否显示需另行确认。`expired` 表示剩余内容已过期。`uncertain` 表示发送结果不明，服务不会自动重发。通过 `queue` 可找回记录；核对手机后停止服务，用 `resolve-send ID --outcome delivered` 或 `not-delivered` 标记当前分段。若尚有后续分段，结果为 `partial`，后续分段不会补发。

缺少上下文返回 409，未绑定或暂停返回 503，通知队列已满返回 429。正文、签名凭据与微信身份均无需出现在 URL 或访问日志中。停止服务即关闭 socket，队列与接收上下文保存在私有 SQLite 中。升级到数据库版本 10 前按[运维说明](OPERATIONS.md)备份。
