# 开源发布

仓库名用 **`wechat-dot-bridge`**，展示名为 **WeChat Dot Bridge**，与 Python 包和命令名一致。

GitHub 简介：

> 在微信里和已有的 OpenAI dot 聊天，支持文字、图片和文件。

Topics：`wechat`、`weixin`、`openai`、`mcp`、`mcp-events`、`python`。

首次发行标题可用 `v0.1.0 · 实验性版本`。说明用途、安装入口和支持范围即可，不把自动化测试写成所有账号均已通过微信端验收。

发布前按 [贡献指南](../CONTRIBUTING.md#发布)检查产物、许可与实际安装，按 [安全报告说明](../SECURITY.md#开源仓库设置)准备私密报告入口。正式打标签时把 CHANGELOG 的 Unreleased 整理为该版本记录。

公开的是桥接源码。每个部署仍使用自己的私人 Tunnel 和插件，运行目录、凭据、绑定和消息留在本机。
