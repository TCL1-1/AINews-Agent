# 每日 AI 新闻助手 Agent

一个面向 LLM、AI Agent、半导体、芯片、自动驾驶与机器人领域的垂直资讯系统。
DeepSeek 模型会自主决定工具调用顺序，搜索、读取、去重并生成带来源的每日简报，
同时区分已经发生的新闻、持续进展和官方确认的未来事件。

完整产品与架构基线见 [`docs/requirements.md`](docs/requirements.md)。

## 已实现能力

- 注册、登录和 Session 认证
- 主题、关键词、排除词、生成时间和条数配置
- 11 个受控 Agent 工具
- DeepSeek Chat Completions Function Calling 循环
- 工具预算、超时、重复调用检测和审计轨迹
- RSS + 开放网络混合搜索、模型公司官网检索、候选评分和历史去重
- X、Reddit、GitHub、ChatGPT、YouTube 公开内容发现（社区内容按低可信线索处理）
- 分阶段工具预算、搜索停止条件、正文抓取和 SSRF 防护
- 按主题栏目和事件状态生成简报
- 站内推送、邮件适配器和推送幂等
- 手动生成、每日调度、历史简报和用户反馈
- SQLite 本地运行与 PostgreSQL Docker Compose 配置

## 运行前提

- Git。
- Python 3.12 或更高版本。
- 能访问所配置的大模型与新闻来源；生成简报必须配置 DeepSeek API Key。
- 推荐安装 `uv` 并使用仓库中的 `uv.lock` 还原依赖；也可以使用标准 `venv + pip`。
- 使用容器部署时，只需要 Docker Engine（或 Docker Desktop）和 Docker Compose。

## 从全新克隆开始（Windows PowerShell）

### 1. 克隆并安装依赖

在 GitHub 仓库页面复制克隆地址，然后执行：

```powershell
git clone <repository-url>
Set-Location AINews-Agent
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data,runtime | Out-Null
uv sync --frozen --extra dev
```

没有安装 `uv` 时可以改用：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 2. 配置环境变量

编辑根目录中的 `.env`，至少完成以下两项：

```dotenv
DEEPSEEK_API_KEY=你的_deepseek_api_key
APP_SECRET_KEY=随机长字符串
```

可以用下面的命令生成 `APP_SECRET_KEY`：

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

未配置 `DEEPSEEK_API_KEY` 时，注册、登录和页面浏览仍可使用，但生成简报会失败。开发环境默认
使用 `data/ai_news.db` 中的 SQLite 数据库；不要把 `.env`、数据库或运行日志提交到 Git。

### 3. 初始化数据库并启动

首次安装以及每次拉取包含数据库迁移的更新后，都应先运行：

```powershell
uv run alembic upgrade head
```

启动 Web 服务：

```powershell
uv run uvicorn app.main:app --reload
```

访问 <http://127.0.0.1:8000>，注册账户、保存偏好后点击“立即生成简报”。可用以下命令检查
服务是否存活：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

预期返回 `status` 为 `ok` 的 JSON。手动生成简报不依赖独立调度器；如需按照用户设置的时间
自动生成，请另开一个终端并保持下列进程运行：

```powershell
uv run python -m app.scheduler
```

如果使用的是 `venv + pip`，对应命令如下：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
.\.venv\Scripts\python.exe -m app.scheduler
```

### macOS / Linux 命令差异

除创建环境文件和目录外，其余 `uv` 命令与 Windows 相同：

```bash
cp .env.example .env
mkdir -p data runtime
uv sync --frozen --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

## 测试和检查

提交代码前运行完整测试、静态检查和类型检查：

```powershell
uv run pytest
uv run ruff check .
uv run mypy app
```

测试默认使用本地模拟或隔离数据，不要求配置真实的 DeepSeek、TwitterAPI.io 或 SMTP 密钥。

## 配置

运行配置位于 `.env`，示例字段见 `.env.example`。`.env` 已被 Git 忽略。
默认 `WEB_SEARCH_PROVIDER=auto`，同一轮会并行使用 GDELT DOC 与 DuckDuckGo，平台检索不足时
再由 Bing 补充；GitHub 和 Reddit 还会使用公开检索入口。连续正文抓取默认最多 4 个并发，
可通过 `AGENT_FETCH_CONCURRENCY` 调整，但不会减少正文核验数量。
`search_news` 支持 `hybrid`、`rss`、`web`、`official` 和 `platform` 五种模式。

### TwitterAPI.io 密钥

在项目根目录的 `.env` 中找到下面这一行，把密钥粘贴到等号后：

```dotenv
TWITTERAPI_API_KEY=你的_api_key
```

密钥只由服务端读取，不会写入页面或日志。配置后，`platform` 模式会优先通过
TwitterAPI.io 搜索 X 的公开内容；未配置时继续使用现有公开网页检索作为降级方案。
`.env.example` 中保留了同名空占位，便于部署时对照配置。

如果本机使用 Clash/TUN 的 Fake-IP 模式，且公开域名被解析到 `198.18.0.0/15`，可在
`.env` 中设置 `ALLOW_PROXY_FAKE_IP=true`。该开关只放行这一段代理保留地址；回环、局域网
和其他非公网地址仍会被正文抓取的 SSRF 防护拒绝。修改 `.env` 后需要重启 Web 服务。

正式部署前至少需要：

- 轮换已经暴露过的 DeepSeek API 密钥。
- 使用随机长字符串替换 `APP_SECRET_KEY`。
- 配置 HTTPS、PostgreSQL 和可靠的邮件服务。
- 根据用途确认新闻来源条款和抓取策略。

## Docker Compose 部署

Compose 会启动 PostgreSQL、Web 和独立调度器。首次启动前同样需要从示例创建 `.env`，并填写
`DEEPSEEK_API_KEY` 与随机的 `APP_SECRET_KEY`。

```powershell
Copy-Item .env.example .env
docker compose build
docker compose up -d db
docker compose run --rm web alembic upgrade head
docker compose up -d web scheduler
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/health
```

查看日志和停止服务：

```powershell
docker compose logs -f web scheduler
docker compose down
```

`docker compose down` 会保留 PostgreSQL 命名卷。不要在需要保留数据时使用
`docker compose down -v`，因为它会删除数据库卷。

拉取新版本后的推荐升级顺序：

```powershell
git pull
docker compose stop web scheduler
docker compose build
docker compose run --rm web alembic upgrade head
docker compose up -d web scheduler
```

当前 Compose 文件中的数据库账号和密码只适合本机开发。公网或正式部署还需要反向代理与
HTTPS、强数据库密码、秘密管理、备份和日志监控；不要直接把 8000 端口暴露到公网。

## 常见问题

- `unable to open database file`：确认已经创建 `data` 目录，并且当前用户有写权限。
- `DEEPSEEK_API_KEY is not configured`：在 `.env` 中填写密钥后重启 Web 和调度器。
- 登录后立即回到登录页：本地运行保持 `APP_ENV=development`；`production` 会要求 HTTPS
  Session Cookie。
- 抓取全部失败：检查外网、代理和 DNS。仅在 Clash/TUN Fake-IP 场景按上文说明开启
  `ALLOW_PROXY_FAKE_IP`。
- 定时任务不执行：确认 `app.scheduler` 进程或 Compose 的 `scheduler` 服务正在运行。

## 推送 GitHub 前检查

```powershell
git status --short
git check-ignore .env data/ai_news.db
uv run pytest
uv run ruff check .
uv run mypy app
```

确认 `.env`、本地数据库、`runtime/` 内容、缓存和真实 API Key 均未进入待提交文件，再推送到
GitHub。首次发布者需要先创建远程仓库并按 GitHub 页面提示添加 `origin`；从 GitHub 克隆的用户
会自动获得该远程地址，无需重复添加。
