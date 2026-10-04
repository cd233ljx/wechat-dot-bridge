# WeChat Dot Bridge

在微信里和你已有的 OpenAI dot 聊天。

你发来文字、图片或文件，桥接把来信交给 dot，再把它的回复送回同一个微信对话。模型能力仍由原 dot 提供，桥接本身只负责消息收发。

## 能做什么

- 文字聊天，同一条来信可以先回进度，再回结果；长回复自动分段。
- 收取和回传图片、文件、视频，读取文本附件，处理引用内容。
- 接收微信语音及平台提供的转写；其他音频转换需要可选编解码器。
- 保存待处理消息和发送状态，提供输入提示、备份与故障恢复。

当前是 **0.1.0 实验性版本**，面向 Linux / systemd 和本人单账号私聊。具体能力和微信端确认情况见 [能力说明](docs/CAPABILITIES.md)。

## 开始使用

接入需要已有 dot、个人开发者插件权限，以及仅自己可用的 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 和 [MCP Events](https://developers.openai.com/plugins/build/mcp-events) 能力。先确认账户具备这些条件，再准备 Python 3.11+ 和 uv。

在源码目录安装锁定的依赖：

```bash
uv sync --locked --all-extras
```

接下来按 [安装指南](docs/INSTALL.md)准备本机服务和微信绑定，再按 [dot 接入说明](docs/DOT_SETUP.md)连接你原来的 dot。安装脚本会生成配置，服务由你确认后启动。

插件应只供自己使用。消息和附件保存在本机私有目录；dot 读取后的内容仍会进入你的 OpenAI 会话。发送结果不确定时会保留 `uncertain`，由你核对后处理。

## 文档

- [安装](docs/INSTALL.md) · [连接 dot 与工具清单](docs/DOT_SETUP.md)
- [排查、升级和备份](docs/OPERATIONS.md) · [微信端验收](docs/ACCEPTANCE.md)
- [贡献指南](CONTRIBUTING.md) · [变更记录](CHANGELOG.md) · [安全报告](SECURITY.md)

## 实现与致谢

Python、官方 MCP SDK、AnyIO、HTTPCore、Standard Webhooks 和 SQLite 负责协议与运行机制，依赖版本保存在 `uv.lock`。

微信协议参考 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)，微信通道和会话生命周期参考 [cc-connect](https://github.com/chenhg5/cc-connect) 的固定版本；参考版本、调用链和适配差异见 [上游审查](docs/UPSTREAM_AUDIT.md)。

原创代码采用 [MIT](LICENSE)，第三方来源见 [NOTICE](NOTICE.txt)。本项目与 OpenAI、腾讯无隶属或背书关系。
