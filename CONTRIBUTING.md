# 贡献指南

当前 0.1.0 为实验性 / Early Access。支持 Linux、systemd 用户服务、本人单账号私聊，将微信接到已有 dot。先阅读 [README](README.md)、[安全边界](docs/SECURITY.md)及目录内的 [AGENTS.md](AGENTS.md)。

欢迎修复协议兼容、消息与媒体传输、故障恢复、安装体验、文档和本地验证。模型 / 模型 API、Codex CLI、新助手、群聊、多用户、跨账号历史迁移和无来信无限制主动推送不在本项目范围。斜杠消息和类似 Shell 的文本交给原 dot，不在桥接内执行。

## 开发环境

使用 Python 3.11+ 和专用虚拟环境，不修改已有服务或认证配置：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Node 24+ 用于可选 Markdown 解析和本地音频转换。音频安装见 [INSTALL](docs/INSTALL.md)；没有编解码依赖时应如实记录相关测试跳过，不能声称已完成转换验收。

测试使用临时专用目录、内存 / 模拟 transport 和合成消息；不导入真实绑定，不读取生产 SQLite，不发送微信或访问真实 callback。测试凭据使用明显的 `fixture` / `test` 占位值。需要真实微信验证时由部署者在明确的测试窗口执行 [验收清单](docs/ACCEPTANCE.md)，只记录布尔结果、计数和固定错误类别。

## 技术栈与复用原则

优先采用社区基础设施和成熟设计，避免长期维护自己的通用框架。当前实现仍有明显的原型特征，以下是源码盘点，不代表目标架构已经落地。

| 层 | 当前实现 | 后续方向 |
| --- | --- | --- |
| 语言 / 打包 | Python 3.11+、setuptools、venv / pip；Python 无锁文件 | 保留 Python；采用 uv 锁定应用运行与开发环境，保留可发布的 pyproject 元数据 |
| dot 连接 | 官方 Tunnel client、stdio、systemd 用户服务 | 保留官方 Tunnel 和 systemd |
| MCP | 手写 JSON-RPC 分发、Schema、校验、协议版本与错误映射 | 优先验证官方 Python MCP SDK v2；标准部分交给 SDK，Events 差异放在薄适配层 |
| 网络 | http.client、socket、ssl，分别实现 API / 回调网络与 DNS，媒体复用回调传输 | 评估 HTTPX 传输接口，收敛统一适配器；保留 IP 固定、TLS 核验、禁用环境代理、有限响应和不确定发送不重试 |
| 并发 | 手写 threading worker、锁、轮询和退出协调 | 随 SDK 迁移验证 AnyIO 任务组与取消机制，保持明确数据库事务边界 |
| 存储 | 标准库 sqlite3、显式 SQL、原子迁移、数据库中的入站 / 出站队列 | 保留单机 SQLite；先补齐处理轮次的持久状态 |
| Webhook | 按 Standard Webhooks 手写 HMAC 与轮换签名 | 优先采用 standardwebhooks 参考实现或共享测试向量，验证精确签名字节和密钥轮换兼容 |
| 媒体 | cryptography、Pillow；可选 Node 24+、silk-wasm、ffmpeg；腾讯 Markdown 过滤器 | 保留现有社区复用，集中维护许可与固定版本 |
| 验证 | unittest、模拟 transport、升级 / 打包 / 秘密扫描脚本，仓库包含 GitHub Actions | 保留有效测试；补跨层故障场景、SDK 合约测试、锁文件检查，逐步加入 Ruff / 类型检查 |

官方 SDK 已提供 MCP 2026-07-28 的发现和 stdio 支持，但其标准订阅与本项目使用的 webhook Events 不是同一个接口。必须先证明 `server/discover` 的 `events` 能力、三个 `events/*` 方法、回调验证 / 签名及现有 11 个工具兼容；不能只因 SDK 能启动就宣称迁移成功。确认默认日志、验证错误和遥测不会带出正文或凭据。

选型验收顺序是：固定版本与许可 → 最小离线兼容实验 → 数据 / 状态迁移与故障回归 → 原有部署路径验收。每个自研例外要写出社区方案无法覆盖的具体能力。当前单账号场景继续使用 SQLite 和 systemd；引入 Redis、外部任务队列或 Web 框架应由明确需求驱动。

详细依据、已复现问题和迁移顺序见 [UPSTREAM_AUDIT](docs/UPSTREAM_AUDIT.md)。以上是选型方向，本轮审查未安装或切换生产依赖。

## 模块职责

| 位置 | 职责 |
| --- | --- |
| `cli.py`、`__main__.py` | 本地命令、配置校验、stdio runtime 与进程锁 |
| `store.py`、`migrations.py` | 持久消息 / 回复状态、去重、事务与版本化数据库升级 |
| `transport.py`、`login.py` | 微信协议、轮询 / 发送、明确选择的绑定导入与二维码流程 |
| `mcp.py`、`events.py` | dot 工具定义、事件订阅、验证、签名与有限投递重试 |
| `attachment_store.py`、`media.py`、`artifacts.py` | 私有附件存储、媒体传输与明确请求的公网 HTTPS 文件导入 |
| `typing.py`、`timing.py` | 有限输入提示与不含正文的阶段计时 |
| `audio.py`、`message_format.py`、`codecs/` | 可选本地编码转换和微信文本适配 |
| `operations.py`、`maintenance.py` | 本地诊断、备份恢复、容量统计与显式历史清理 |
| `scripts/`、`tests/` | 安装 / 发布辅助、本地回归与模拟升级演练 |

网络错误只能返回固定类别，禁止把异常字符串、响应体、URL、密钥或消息原文带进状态 / 日志。消息原文仅通过指定读工具进入本人 dot。新增状态字段与诊断必须同时检查隐私边界。

跨进程发送中断必须保留 `uncertain`；不能因为重启、工具重试或订阅变化自动重发。`accepted` 是 API 受理，不能标记为微信已投递 / 已读。本人同一来信可多次回复，需保留幂等键和已接受分段。

专用目录为 `0700`、私人文件为 `0600`；拒绝符号链接目标。共享安装目录的权限兼容仅用于不含秘密的程序 / unit 文件，不能扩大到配置、状态或备份。健康监听仅绑定本机回环。

## 数据库变更

数据库版本使用 `PRAGMA user_version`，迁移集中在 `migrations.py`。已发布迁移保持不变，新变更追加新版本。所有待迁移步骤及版本标记在同一个事务提交；失败或中断不留下“已建表但未回填”的完成标记。旧未版本化库需兼容升级，超出支持范围的版本必须以 `database_schema_unsupported` 拒绝。

迁移测试应验证历史回填、故障注入后的回滚 / 重启、已受理分段、`uncertain` 和去重依据。清理已完成历史时保留待处理、未确认发送及去重墓碑，不能把旧消息重新当作新消息。

升级前停唯一服务，保存私人备份。回滚采用兼容旧版本的备份副本并人工核对升级后新增状态，不直接让旧程序打开升级后的生产库，不用旧快照覆盖新来信。具体步骤见 [OPERATIONS](docs/OPERATIONS.md)。

## 提交与本地检查

每个变更说明触发条件、之前 / 之后的行为、验证结果及兼容性影响。提交信息使用简体中文；发布给使用者的变更记入 [CHANGELOG](CHANGELOG.md) 的 Unreleased。代码修复应添加能复现真实失败的测试；文档等低影响修改不需要机械复制实现的测试，但仍运行完整项目回归。

在仓库根目录执行完整发布检查：

```bash
.venv/bin/python -m pip install 'setuptools>=77'
.venv/bin/python scripts/check_release.py --old-revision 4d87a28
```

检查通过已安装的 setuptools PEP 517 backend 构建源码包和 wheel，不依赖 `build` 前端。依赖准备完成后的检查过程不联网。它汇总语法检查、完整回归、模拟升级、秘密扫描、差异检查、打包 / 许可资源检查与 checkout 外的 wheel 安装 smoke；无需重复运行已经包含的步骤。`--old-revision` 默认 `4d87a28`，指定版本必须存在于本地 Git 历史。

只需要单独验证某一步时，可以运行：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/verify-upgrade.py --old-revision 4d87a28
.venv/bin/python scripts/scan_secrets.py
```

秘密扫描覆盖本地 refs 可达历史（含 commit / tag 文字）和当前非忽略工作区，报告类别、文件 / 行、Git 对象与计数，不打印匹配值。它不能证明未拉取的远端 refs、不可达对象、Git LFS / 外部附件、二进制内容或所有形式的秘密均安全；超出扫描大小预算会返回 `scan_limit`，需要另行处理。发现问题先通过私密渠道处理，不把命中内容贴到工单。

项目不要求 CI。发布依靠本地检查及明确记录的真人验收；功能依赖微信 / dot 产品侧能力时，应保留相应“待验证”的边界。

## 发布

1. 更新 CHANGELOG、版本与支持边界，确认文档列出当前 11 个工具。
2. 运行本地发布检查，检查源码包与 wheel 只包含代码、必要文档和许可，没有运行数据、绑定、callback URL / secret、运行 key 或 node_modules。
3. 检查脚本在临时虚拟环境从 checkout 外安装 wheel，使用 `--no-index --no-deps` 并复用已安装运行依赖；此步骤验证打包与导入，不验证干净环境的依赖解析安装。发布前另按 [INSTALL](docs/INSTALL.md) 在干净环境验证依赖安装，并记录实际环境。Python 3.11 是声明的最低版本，应单独验证。
4. 按 [SECURITY](SECURITY.md) 启用仓库的私密漏洞报告，或在该文件填入已经确认的私密报告渠道；不使用占位邮箱发布。
5. 停服、私人备份并演练升级；真人测试只在部署者自己的单账号环境执行。仅上传源码和发布产物，保留原运行目录。

变更未打标签前记在 Unreleased；不要把构建成功或本地模拟成功写成所有账户均可用。暂不承诺固定响应时间或维护周期。

## 问题与漏洞

普通问题使用仓库的 bug / feature 模板，提供版本、复现步骤、预期行为、实际固定错误类别与是否有真人确认。仅使用合成示例，不上传原始日志、配置、数据库或私人媒体。

可能涉及凭据泄露、越权访问、目标校验绕过或私人消息暴露的问题，按 [SECURITY](SECURITY.md) 私密报告，不建公开 Issue 或 PR。
