# 从干净环境部署（Linux / systemd 用户服务）

要求 Python 3.11+；微信收发不运行模型。可选 Node 24+ 用于腾讯 Markdown 解析器及 SILK 编解码。OpenAI 插件和私有 Tunnel 权限由本人账户提供，不保证 dot / Work 用量免费。

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

配置精确的实际平台 callback 主机；不要复制他人的带签名 URL 或关闭主机校验。代理如有需要仅配置本机回环地址。微信新扫码、已有绑定导入及私有插件连接步骤见 README、DOT_SETUP.md。

确认同一绑定只有一个接收服务后，再显式启动 / 启用 systemd 用户服务。默认不开放公网端口。重启登录维持运行所需的 linger 应由部署者明确配置。

## 发布验证

`.github/workflows/ci.yml` 配置 Python 3.11 / 3.14 回归、本地语音转换、wheel 构建及 checkout 外安装检查。仓库尚未推送时，不宣称远端 CI 已运行。

本次已在本机独立 Python 3.11 环境安装 wheel，并从源码目录外验证导入和测试。wheel 携带腾讯 / SILK 许可与编解码辅助源码，不携带 node_modules、凭据或运行数据库。外部 FFmpeg 二进制不包含在仓库内，重新分发须遵守对应构建许可。
