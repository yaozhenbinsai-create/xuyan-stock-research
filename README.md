# 序研 · AI 股票研究工作台

序研把 A 股研究中的行情、公告和政策原文、取证过程、研究结论与后续复盘保存在一套本地工作台。可以按交易日检查一份结论当时用了哪些资料，再回看后续走势和信号变化。本仓库基于 `research-relational-20261120.2-preview.1`，使用 FastAPI、浏览器前端和独立 MySQL 8.4 实例。

| 可以做什么 | 对应位置 |
| --- | --- |
| 浏览证券名录、日线、公告、财务与政策记录，查看不同时间采集的修订 | 数据资产、数据源 |
| 组织选股、按时间截点冻结证据，查阅模型调用及其结果 | 研究工作台、机会发现、使用记录 |
| 查看关注列表和持仓，按日或按月复盘已保存结论 | 跟踪、复盘 |

## 在 Windows 上首次运行

准备 64 位 Windows、Python 3.14、PowerShell 和官方 MySQL Community Server 8.4 的 Windows x64 ZIP。将项目放在使用 ASCII 字符的路径下，例如 `C:\dev\xuyan-stock-research`。MySQL 程序解压到仓库的 `tools/mysql/`，使其结构为 `tools/mysql/mysql-8.4.<补丁号>-winx64/bin/mysqld.exe`。可在官方页面核对 ZIP 的签名。[MySQL 下载与签名](https://dev.mysql.com/downloads/mysql/8.4.html)

打开项目根目录的 PowerShell，执行以下命令。

```powershell
py -3.14 -m venv .venv
$python = (Resolve-Path '.venv\Scripts\python.exe').Path
& $python -m pip install -r requirements.lock.txt
& $python tools\admin_account.py
& $python tools\mysql_instance.py initialize
& $python run.py
```

管理员初始化会隐藏输入，要求自行设置 12 到 160 个字符的密码。首次安装只有 **ADMIN** 一个应用账号，登录地址为 `http://127.0.0.1:8751/`。密码校验值保存在 `data/web-admin.json`。重复运行管理员初始化会保留密码。会话在退出、服务重启或八小时后失效。API 客户端可向同源 `/login` 提交 `username`、`password` 表单并保存响应中的 Cookie，用它访问后续接口。

MySQL 默认使用本机端口 19317，在 `data/mysql/` 建立专用实例。程序会生成独立的数据库服务凭据，使用当前 Windows 用户的 DPAPI 加密保存。应用和数据库分别使用不同的端口。两项默认值都在 `deployment.json` 中，首次建库前可改用空闲端口，也可以执行 `tools/mysql_instance.py initialize --port 19318` 为新实例选端口。后续启动会核对实例 UUID。关闭工作台可在前台窗口按 Ctrl+C，MySQL 保留数据以供下次使用。

## 第一次打开工作台

- 首页显示零条行情、零只证券和零次研究。数据源页列出 19 种适配器定义，包括 BaoStock 交易日历、证券名录和日线，以及公告、财务、报价和政策来源。每个来源初始处于停用、待验证状态。
- 若要开始采集，先在“数据源”选择有使用权限的来源并启用，在“采集任务”里选择范围。手动提交的任务也需要后台执行器。在 `deployment.json` 将 `workers_enabled` 改为 `true`，重启工作台后在页面查看执行状态。
- AI 设置初始有零个模型、零个执行方案和零把 API 密钥。使用 API 时，由 ADMIN 在本实例填写自己的地址、模型标识和密钥，也可以显式指定自己的环境变量名。启动只读取本仓库新建的数据库配置。
- 如果选择官方账号通道，先安装官方 Codex CLI，再运行 `& $python tools\official_login.py login` 登录。用 `& $python tools\official_login.py status` 查看本实例的登录状态。CLI 会话保存在本机 `data/official-codex/`，运行命令时不使用其他应用的登录目录、代理地址或 API Key。[Codex 官方认证说明](https://developers.openai.com/codex/auth/)
- 运行采集、研究或启用计划时，可能访问所选外部服务并按该服务规则计量。首次安装关闭后台执行器、研究时钟和外部推送。要启用现有 Webhook 交付功能，可在本实例的 `deployment.json` 中明确设 `enable_webhook_environment` 为 `true`，再按需设置自己的 `STOCK_RESEARCH_WEBHOOK` 和 `STOCK_RESEARCH_WEBHOOK_ENABLED=1`。
- 私有内容归入 `data/`，其中包括本地 MySQL、账号校验文件、加密连接资料和运行记录。复制数据库到其他 Windows 用户时需要处理 DPAPI 用户绑定。请使用各自新建的本地开发实例参与测试。

## 参与完善

欢迎提交功能建议、复现问题或发起合并请求。第一次贡献可以从下面几项中选一项。[Issues](https://github.com/yaozhenbinsai-create/xuyan-stock-research/issues) · [Pull requests](https://github.com/yaozhenbinsai-create/xuyan-stock-research/pulls)

- **取证和数据质量**。为撤回公告、缺字段的行情或不同发布日期添加脱敏样例，说明应落在哪个数据状态。适配器在 `foundation/sources.py`，记录规则在 `foundation/assets.py`。
- **研究与复盘**。为 `research/` 中的证据边界和时段计算补小型回归。当前预览版有两个可独立复现的失败用例，分别涉及盘中量能数据不完整时的类型匹配，以及午盘决策在首个下午报价出现时的结论。
- **前端空状态**。检查无持仓、无行情、无模型时的界面，将复现路径、浏览器版本、期望结果和去标识化截图写入 Issue。页面在 `foundation/static/`。

提交 PR 时请注明改动关联的页面或模块、已执行的命令和观测结果。Issue、提交和附件请采用自造样例。实际持仓、交易流水、受限行情、API Key 和本地账号文件保存在自己的实例。

数据库初始化后可运行全套测试。用例中需要 MySQL 的部分会新建随机命名的测试库。以下 PowerShell 命令在前面配置的虚拟环境中执行。

```powershell
& $python -m unittest discover -s tests
```

在项目根目录执行以下命令可单独复现两项已知失败。

```powershell
$env:PYTHONPATH = (Resolve-Path 'tests').Path
& $python -m unittest test_rises.RiseTests.test_partial_quotes_do_not_supply_complete_confirmation_or_daily_volume test_session_workflow.SessionWorkflowTests.test_noon_formal_decision_requires_evidence_and_uses_first_afternoon_quote
```

## 依赖与数据使用

Python 库按 `requirements.lock.txt` 安装。FastAPI 与 PyMySQL 使用 MIT，Starlette、Uvicorn、HTTPX 和 pypdf 使用 BSD-3-Clause，cryptography 可按 Apache-2.0 或 BSD-3-Clause 使用。BaoStock 的软件包声明 BSD License。MySQL Community Server 由安装者从 Oracle 获取并遵循其 GPL 及随附许可。[PyMySQL](https://github.com/PyMySQL/PyMySQL) · [pypdf](https://pypi.org/project/pypdf/) · [BaoStock 软件包](https://pypi.org/project/baostock/) · [MySQL Community Edition](https://www.mysql.com/products/community/)

开源库的软件许可和外部数据的使用权分别由各提供方确定。计划公开行情、公告全文、研究附件或衍生数据时，请核对相应来源的使用与转发条款。仓库提供适配器和人工构造的测试输入，实际市场数据由部署者自行获取。[BaoStock](https://baostock.com/) · [巨潮资讯](https://www.cninfo.com.cn/)

