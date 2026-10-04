# 安装（Linux / systemd 用户服务）

当前版本面向本人单账号私聊，仍处于实验阶段。先确认已有 dot、私人插件和 Tunnel 权限，具体要求见 [官方 Tunnel 指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。Tunnel 使用权限与 ChatGPT 开发者插件权限分开；dot / Work 的用量与费用按对应产品规则计算。

## 1. 准备源码环境

在新目录检出源码，准备 Python 3.11+ 和 uv：

```bash
uv sync --locked --all-extras
uv run --locked --all-extras python3 -m unittest discover -s tests -v
```

开发、CI 和源码部署使用同一份 `uv.lock`。`--locked` 会拒绝过期锁文件，部署时不重新解析版本。需要指定最低 Python 版本时，给 `uv sync` 加 `--python 3.11`。

可选的 Markdown 解析和音频转换需要 Node 24+、npm：

```bash
bash scripts/install-codecs.sh
```

没有 Node 时 Markdown 使用简化降级。没有音频转换依赖时，仍可接收原始音频和微信提供的转写、回传已有 AMR/SILK，不能转换其他音频。SILK 使用 package-lock.json；转换脚本把 imageio-ffmpeg 0.6.0 装进独立环境，FFmpeg 不包含在本项目发行包内。

## 2. 生成私人配置

从 [官方发行包](https://github.com/openai/tunnel-client/releases/latest)下载适合系统的 tunnel-client，保留随附许可，并按官方指南核对校验和与 Tunnel identity。此服务的访问控制依赖私人 Tunnel；不要把插件分享给其他账号或组织。

以下变量在自己的终端设置；程序路径和 identity 需要替换成你实际取得的值：

```bash
WDB_SOURCE="$PWD"
WDB_CONFIG="$HOME/.config/wechat-dot-bridge"
WDB_STATE="$HOME/.local/share/wechat-dot-bridge"
WDB_TUNNEL_BIN="/absolute/path/to/tunnel-client"
WDB_TUNNEL_ID="YOUR_TUNNEL_ID"
WDB_KEY_FILE="$WDB_CONFIG/tunnel-runtime-key"
```

已有运行密钥文件时，把 `WDB_KEY_FILE` 指向它。需要新建文件时，通过隐藏输入保存：

```bash
uv run --locked --all-extras python scripts/save_runtime_key.py --path "$WDB_KEY_FILE"
```

脚本只接受交互输入，不覆盖已有文件。不要把密钥放进参数、聊天、截图或 Git。

```bash
uv run --locked --all-extras python scripts/install_local.py \
  --source-dir "$WDB_SOURCE" \
  --config-dir "$WDB_CONFIG" \
  --state-dir "$WDB_STATE" \
  --tunnel-bin "$WDB_TUNNEL_BIN" \
  --tunnel-id "$WDB_TUNNEL_ID" \
  --key-file "$WDB_KEY_FILE"
```

安装器生成 `~/.local/bin/wechat-dot-bridge`、私人配置和 systemd 用户 unit，包装器使用当前虚拟环境的 Python。它不覆盖已有安装，也不启用或启动服务。可用 `--bin-dir`、`--unit-dir` 指定其他位置。

专用配置、状态目录为 `0700`，私人文件为 `0600`，拒绝符号链接目标。共享程序 / unit 目录可以保留安全的 `0755` 权限。`bridge.json` 初始的 `callback_hosts` 为空，回调主机在 dot 接入时核验。

## 3. 导入绑定或扫码

同一个绑定只能有一个接收者。先停止旧 OpenClaw / cc-connect 接收服务，包括其他主机上的实例，再进行接收验证或切换；本机锁不能阻止其他程序消费相同绑定。

已有可用凭据时，导入自己明确选择的 OpenClaw account JSON 或 cc-connect 微信配置：

```bash
"$HOME/.local/bin/wechat-dot-bridge" --config "$WDB_CONFIG/bridge.json" \
  import-binding /private/path/to/selected-account.json
```

cc-connect 配置有多个绑定时，用 `--account-id` 指定。导入只读取所选来源，不扫描认证目录、不修改来源，完成后保持暂停。已有 bridge 不能更换 account / owner；微信绑定、历史消息和 dot 会话不会一起迁移。

没有可导入凭据时，在服务未运行的情况下扫码：

```bash
"$HOME/.local/bin/wechat-dot-bridge" --config "$WDB_CONFIG/bridge.json" login-start
uv run --locked --all-extras python scripts/render_login_qr.py \
  --state-dir "$WDB_STATE" \
  --output "$WDB_STATE/weixin-login-qr.png"
```

二维码只保存在私人目录。通过可信的本机或 SSH 文件访问显示 PNG，用手机微信确认，再查询登录状态：

```bash
"$HOME/.local/bin/wechat-dot-bridge" --config "$WDB_CONFIG/bridge.json" login-poll
```

等待中可以再查询。返回 `requires_phone_verification: true` 时使用隐藏输入：

```bash
"$HOME/.local/bin/wechat-dot-bridge" --config "$WDB_CONFIG/bridge.json" \
  login-poll --verification-code
```

`confirmed` 后凭据写入私有 SQLite，接收仍暂停。二维码过期可重新 `login-start`，再次渲染前删除旧 PNG；渲染工具拒绝覆盖。`already_bound` 不表示本机已有可用凭据。重新扫码也不能保证仍是原来的微信机器人或对话。

## 4. 启动 Tunnel，连接 dot

先检查密钥，再以暂停接收的状态启动服务：

```bash
uv run --locked --all-extras python scripts/check_runtime.py \
  --key-file "$WDB_KEY_FILE" --state-dir "$WDB_STATE" \
  --config "$WDB_CONFIG/bridge.json" --key-only
systemctl --user daemon-reload
systemctl --user start wechat-dot-bridge.service
uv run --locked --all-extras python scripts/check_runtime.py \
  --key-file "$WDB_KEY_FILE" --state-dir "$WDB_STATE" \
  --config "$WDB_CONFIG/bridge.json"
```

预期 `key_ready`、`tunnel_healthy` 和 `tunnel_ready` 为 `true`。启动前完整检查返回非零属正常，届时只用 `--key-only`。包装器不在默认位置时，完整检查加 `--command /your/wrapper/path`。

按 [dot 接入说明](DOT_SETUP.md)创建仅自己使用的插件、核验精确回调主机并订阅 `weixin.message`。原 dot 应发现 11 个工具和一个事件。配置完成、确认没有第二个接收者后，开启接收：

```bash
systemctl --user stop wechat-dot-bridge.service
"$HOME/.local/bin/wechat-dot-bridge" --config "$WDB_CONFIG/bridge.json" \
  resume --confirm-single-consumer
systemctl --user start wechat-dot-bridge.service
```

发一条有唯一标记的短文字，确认原 dot 读取并回复，最后在同一个微信对话确认收到一次回复。API 的 `accepted` 不代表微信实际投递或已读。后续验收见 [验收清单](ACCEPTANCE.md)。

需要开机持续运行时，自行执行 `systemctl --user enable wechat-dot-bridge.service`。退出登录后的运行策略取决于主机的 systemd 用户配置，安装器不修改 linger。

## 可选网络配置

默认使用本机 DNS 和直接 HTTPS。确有需要时，在私人配置里设置 `callback_proxy` / `callback_dns` 或 `media_proxy` / `media_dns`；代理仅接受现有的 `http://127.0.0.1:端口`，DNS 可选 `system` 或 `google`。Tunnel 控制面代理由安装器的 `--proxy` 独立设置，微信 API 仍直连。

Google DoH 只收到主机名查询；本机代理可观察连接元数据。精确主机、公网 IP 和 TLS 校验仍保持启用，不继承环境代理。配置字段与适用边界见 [安全说明](SECURITY.md)；不要复制他人的 callback URL 或放宽 allowlist。

## 停止与回滚

修改绑定、登录或启停标志前停止运行中的服务：

```bash
systemctl --user stop wechat-dot-bridge.service
"$HOME/.local/bin/wechat-dot-bridge" --config "$WDB_CONFIG/bridge.json" pause
```

撤销常驻可执行 `systemctl --user disable wechat-dot-bridge.service`。保留私人状态，以便备份和回滚；恢复旧接收者前确认本桥接已停止。完整步骤见 [维护指南](OPERATIONS.md)。

## 发布检查

在 Git checkout 中运行：

```bash
uv run --locked --all-extras python scripts/check_release.py
```

检查包括完整回归、历史秘密扫描、升级恢复、源码包 / wheel 及许可资源。源码压缩包没有 Git 历史时，单独的 `scripts/verify-upgrade.py` 仍可运行；完整检查需要先建立 Git 仓库，只能检查实际存在的历史。

wheel 安装检查在源码目录外运行，复用已有依赖，只验证发行包内容与导入，不代替首次干净安装或微信端验收。更详细的发布流程见 [贡献指南](../CONTRIBUTING.md)。
