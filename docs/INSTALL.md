# 从干净环境部署（Linux / systemd 用户服务）

当前 0.1.0 为实验性 / Early Access，支持 Linux / systemd 用户服务、本人单账号私聊。要求 Python 3.11+；微信收发不运行模型。可选 Node 24+ 用于腾讯 Markdown 解析器及 SILK 编解码。OpenAI 插件和私有 Tunnel 权限由本人账户提供，不保证 dot / Work 用量免费。

## 安装源码与可选音频

在自己选择的新目录检出仓库，创建独立环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[setup]'
.venv/bin/python -m unittest discover -s tests -v
```

音频可选安装（要求现有 Node 24+、npm、uv）：

```bash
bash scripts/install-codecs.sh
```

未安装音频转换依赖时仍可传文件和已有 AMR/SILK，不能宣称已验证通用音频转码。Node 缺失时 Markdown 使用简化降级。Python 依赖装在独立环境，SILK 依赖使用 package-lock.json；FFmpeg 使用独立的 imageio-ffmpeg 0.6.0 环境。

## 准备服务，不自动启动

从 OpenAI 官方发布下载匹配系统的 tunnel-client，核对发布校验和；自行创建仅本人使用的 Tunnel。运行 key 通过隐藏输入脚本保存到本人私有目录，禁止写入仓库或聊天。

```bash
.venv/bin/python scripts/install_local.py \
  --config-dir /PRIVATE/bridge-config \
  --state-dir /PRIVATE/bridge-data \
  --tunnel-bin /ABSOLUTE/tunnel-client \
  --tunnel-id TUNNEL_ID \
  --key-file /PRIVATE/runtime.key
```

该脚本生成包装器、配置和用户 unit，不启动或启用服务，不覆盖已有安装。用虚拟环境 Python 运行，确保生成包装器引用该环境。首次绑定默认暂停。已有绑定只导入本人指定文件；重新扫码须由本人决定。

已有的共享程序 / systemd unit 目录允许保留安全的 `0755` 权限；安装器不修改其权限。专用配置、状态目录使用 `0700`，私人文件使用 `0600`，符号链接目标和他人可写目录会被拒绝。

配置精确的实际平台 callback 主机；不要复制他人的带签名 URL 或关闭主机校验。代理如有需要仅配置本机回环地址。微信新扫码、已有绑定导入及私有插件连接步骤见 README、DOT_SETUP.md。

确认同一绑定只有一个接收服务后，再显式启动 / 启用 systemd 用户服务。默认不开放公网端口。重启登录维持运行所需的 linger 应由部署者明确配置。

## 发布验证

发布验证在本地完成，不要求 CI。完整回归、源码包 / wheel 构建、安装产物检查以及 Git 历史扫描的步骤见 [贡献指南](../CONTRIBUTING.md)。真人微信验收单独按 [当前清单](ACCEPTANCE.md)进行，本地测试不能代替微信端确认。

本地打包要求当前 Python 环境已安装 `setuptools>=77`，使用项目配置的 PEP 517 backend，不需要 `build` 前端：

```bash
.venv/bin/python -m pip install 'setuptools>=77'
.venv/bin/python scripts/check_release.py
```

依赖准备完成后，发布检查不联网。临时 wheel 安装 smoke 使用 `--no-index --no-deps`，复用已有运行依赖，仅验证产物在 checkout 外的导入、资源和新库初始化；不能把它当作新的干净环境依赖安装验证。首次部署仍需执行上面的干净环境安装步骤。

发布产物应携带腾讯 / SILK 许可与编解码辅助源码，不携带 node_modules、凭据或运行数据库。外部 FFmpeg 二进制不包含在仓库内，重新分发须遵守对应构建许可。
