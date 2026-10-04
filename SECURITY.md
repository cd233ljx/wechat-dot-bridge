# 安全报告

当前只维护实验性 / Early Access 的最新代码；没有长期支持分支或承诺的响应周期。0.1.0 只面向 Linux / systemd、本人单账号私聊，不能部署为共享的多用户服务。运行数据、网络目的地、权限和发送恢复边界见 [安全设计说明](docs/SECURITY.md)。

## 私密报告漏洞

请优先使用本仓库 GitHub 的 **Security → Advisories → Report a vulnerability** 入口。该入口由仓库维护者启用；如果不可见，请通过仓库主页已经公布的维护者私密联系方式先请求报告渠道。本文件未指定邮箱，不能假定任何第三方项目的联系方式可用于本桥接。操作说明见 [GitHub 私密报告文档](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)。

没有可用私密通道时，暂缓公开漏洞细节、复现载荷和可能泄露的值；可以提交不含细节的渠道请求，维护者提供私密通道后再发送报告。不要在公开 Issue、PR、讨论、提交、截图或日志中披露凭据、真实消息或可利用的细节。

私密报告建议包括：

- Bridge 版本 / commit、Python 与系统版本。
- 漏洞影响的边界，例如越权读取、凭据暴露、出站目标校验绕过或不确定发送重放。
- 使用合成数据的最小复现、预期与实际行为、固定错误类别。
- 是否已经发现实际暴露；无需提供私人消息、真实 owner、完整配置或数据库。

如需样例，只用虚构 callback 主机、测试绑定与临时目录。不要发送运行 key、token、callback URL / secret、二维码、手机号、签名 header 或原始网络响应。真实秘密已经暴露时，操作者通过对应产品的可信入口处理受影响凭据，报告中仅说明类别和已采取的措施。

维护者在私密渠道确认范围并协商修复与披露时间；只有明确同意后才公开技术细节。不能保证固定响应时限。

## 开源仓库设置

仓库首次公开时，维护者应在 GitHub 仓库 **Settings → Advanced Security** 中启用 **Private vulnerability reporting**，并检查报告入口是否可见。GitHub 此入口面向公开仓库；尚未公开或所用托管服务不支持此功能时，应提供已确认的私密联系渠道。界面位置以 [GitHub 配置说明](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)为准。普通问题模板不接受漏洞细节。
