# 每日 AI 新闻助手 Agent

一个面向 LLM、AI Agent、半导体、芯片、自动驾驶与机器人领域的垂直资讯系统。
DeepSeek 模型会自主决定工具调用顺序，搜索、读取、去重并生成带来源的每日简报，
同时区分已经发生的新闻、持续进展和官方确认的未来事件。

完整产品与架构基线见 [`docs/requirements.md`](docs/requirements.md)。

## 已实现能力

- 注册、登录和 Session 认证
- 主题、关键词、排除词、生成时间和条数配置
- 10 个受控 Agent 工具
- DeepSeek Chat Completions Function Calling 循环
- 工具预算、超时、重复调用检测和审计轨迹
- RSS 多来源搜索、正文抓取、SSRF 防护和历史去重
- 按主题栏目和事件状态生成简报
- 站内推送、邮件适配器和推送幂等
- 手动生成、每日调度、历史简报和用户反馈
- SQLite 本地运行与 PostgreSQL Docker Compose 配置

## 本地启动（Windows PowerShell）

项目已经创建 `.venv`。安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

初始化数据库迁移：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

启动 Web：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

另开终端启动调度器：

```powershell
.\.venv\Scripts\python.exe -m app.scheduler
```

访问 <http://127.0.0.1:8000>，注册账户、保存偏好后点击“立即生成简报”。

## 测试和检查

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

## 配置

运行配置位于 `.env`，示例字段见 `.env.example`。`.env` 已被 Git 忽略。
正式部署前至少需要：

- 轮换已经暴露过的 DeepSeek API 密钥。
- 使用随机长字符串替换 `APP_SECRET_KEY`。
- 配置 HTTPS、PostgreSQL 和可靠的邮件服务。
- 根据用途确认新闻来源条款和抓取策略。

## Docker Compose

```powershell
docker compose up --build
```

Compose 会启动 PostgreSQL、Web 和独立调度器。生产环境应使用秘密管理替代明文 `.env`。
