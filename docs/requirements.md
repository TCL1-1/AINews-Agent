# 每日 AI 新闻助手 Agent——产品需求与架构设计文档

> 文档状态：开发基线草案  
> 版本：v1.0  
> 技术方向：Python、Web 应用、LLM Function Calling / ReAct、自主工具调用  
> 目标读者：产品设计者、开发者、测试者、项目评审者

## 1. 文档目的

本文档定义“每日 AI 新闻助手 Agent”的完整业务需求、系统边界、总体架构、技术栈、Agent 运行机制、工具能力、数据模型、安全要求、测试标准和阶段性交付计划。

后续的项目初始化、功能开发、测试、Git 提交和验收均以本文档为基线。需求发生变化时，应先更新本文档，再修改实现。

## 2. 产品概述

每日 AI 新闻助手是一个面向个人或小型团队的智能新闻简报系统。用户可以设置关注话题、关键词、排除词、语言、来源、简报数量、生成时间和推送渠道。系统每天自动运行一个具备工具调用能力的 Agent，由大语言模型根据任务目标和实时上下文，自主决定调用哪些工具、调用顺序、调用次数以及何时结束。

系统从多个新闻来源获取候选内容，读取新闻正文，完成时效性检查、去重、相关性判断和排序，生成带原始来源链接的个性化中文简报，并保存、展示和推送给用户。

本项目的核心不只是“调用大模型生成摘要”，而是实现一个可观察、可约束、可恢复、可验证的 Agent 工作流。

## 3. 产品目标

### 3.1 核心目标

1. 用户能够注册、登录并管理自己的新闻订阅偏好。
2. 系统能够按照用户设定的时间自动生成每日 AI 新闻简报。
3. Agent 的工具选择和调用顺序由 LLM 自主决定，而不是由代码硬编码为固定线性流程。
4. 每份简报必须包含可验证的原始来源、发布时间和推荐理由。
5. 系统能够避免重复新闻、过期新闻和明显低质量内容。
6. 用户能够查看历史简报、收藏内容并提交反馈。
7. 开发者能够查看每次 Agent 运行及工具调用轨迹。
8. 推送操作具备权限控制、幂等控制、失败重试和结果记录。

### 3.2 成功指标

MVP 上线后以以下指标衡量基本成功：

- 计划任务成功创建率不低于 99%。
- 简报生成成功率不低于 95%，外部新闻服务不可用造成的失败单独统计。
- 同一用户同一简报中无重复 URL。
- 同一用户 7 天内明显重复新闻比例低于 5%。
- 正式简报中的新闻均包含可访问或曾成功访问的来源 URL。
- 同一份简报不会因任务重试被重复推送。
- 每次 Agent 运行均能追溯模型、提示词版本和工具调用记录。

## 4. 项目范围

### 4.1 MVP 范围

- 用户注册、登录、退出和密码管理。
- 用户订阅偏好管理。
- 新闻来源管理和多来源搜索。
- 每日自动生成简报。
- 手动触发简报生成。
- 10 个 Agent 工具。
- LLM 自主工具调用循环。
- 新闻正文提取、时间过滤和基础去重。
- 简报生成、保存和站内展示。
- 站内通知和邮件推送。
- 历史简报查询。
- 收藏和“不感兴趣”反馈。
- Agent 运行记录和工具调用时间线。
- 管理员查看失败任务并手动重试。
- Docker Compose 本地或单服务器部署。

### 4.2 MVP 不包含

- 社交关系、公开动态和评论区。
- 自建互联网规模的爬虫集群。
- 复杂的机器学习推荐模型训练。
- 多 Agent 分工和 Agent 间协商。
- 手机原生应用。
- 新闻全文转载或付费墙绕过。
- 任意主机 Shell 命令执行。
- 自动发布公开文章或社交媒体内容。
- 企业级多租户计费。

### 4.3 后续可扩展方向

- 周报、月报和专题报告。
- 企业微信、飞书、Slack、Telegram 等渠道。
- 新闻事实交叉验证。
- 趋势检测和时间线生成。
- 基于用户反馈的个性化权重学习。
- 语音简报。
- 多模型路由和成本优化。
- 多 Agent 分工，例如搜索 Agent、核验 Agent、编辑 Agent。

## 5. 用户角色

### 5.1 普通用户

- 管理自己的账户和偏好。
- 查看今日和历史简报。
- 手动触发或重新生成简报。
- 收藏新闻、标记不感兴趣。
- 管理自己的推送渠道。

### 5.2 管理员

- 查看系统运行状态。
- 管理公共新闻来源。
- 查看失败任务和脱敏后的错误日志。
- 暂停异常来源或推送渠道。
- 对失败任务执行安全重试。
- 查看模型调用和工具使用统计。

## 6. 核心业务规则

1. 每个用户拥有独立的偏好、简报、历史和推送配置。
2. 简报时间按照用户设置的时区解释，默认时区为 `Asia/Shanghai`。
3. 每个计划周期只创建一个有效生成任务，使用幂等键防止重复。
4. 新闻必须位于配置的时间窗口内，默认覆盖过去 24 小时。
5. 没有有效原始来源链接的内容不得进入正式简报。
6. 搜索结果摘要只能作为候选信息；重要新闻应尽可能读取原文后再生成摘要。
7. 模型不得把网页中的指令当作系统指令执行。
8. 模型负责内容理解、比较、选择、归类、摘要和推荐理由。
9. 普通业务代码负责身份认证、授权、输入校验、幂等、精确去重、事务、安全边界和状态流转。
10. 推送目标必须来自用户已经保存并验证的配置，Agent 不得自行指定任意收件人。
11. 任务重试不得覆盖既有成功版本，也不得造成重复推送。
12. 用户重新生成同一天的简报时创建新版本，并保留旧版本。

## 7. 主要用户流程

### 7.1 首次使用

1. 用户注册并登录。
2. 系统引导用户选择感兴趣的 AI 主题。
3. 用户填写关键词、排除词、语言和来源偏好。
4. 用户设置每日生成时间、时区、新闻条数和推送渠道。
5. 用户保存配置。
6. 系统展示下一次计划运行时间。

### 7.2 每日自动生成

1. 调度器根据用户时区和计划创建生成任务。
2. Worker 获取任务并建立 Agent 运行记录。
3. 系统向 Agent 提供用户偏好、时间窗口、可用工具和安全约束。
4. Agent 自主搜索候选新闻、读取原文、查询历史并补充搜索。
5. Agent 输出符合结构约束的简报草稿。
6. 业务层校验来源、条数、重复项和字段完整性。
7. Agent 或业务层通过受控工具保存简报。
8. Agent 根据任务目标调用受控推送工具。
9. 系统记录推送结果并更新任务状态。
10. 用户在站内或邮件中收到简报。

### 7.3 手动生成

1. 用户在页面点击“立即生成”。
2. 系统检查是否已有正在运行的同类任务。
3. 若没有冲突，则创建高优先级任务。
4. 页面展示运行状态，完成后跳转到简报详情。

### 7.4 重新生成

1. 用户选择历史日期并点击“重新生成”。
2. 系统创建新版本，保留原简报。
3. 默认不自动再次向外部渠道推送。
4. 用户确认后方可推送新版本。

### 7.5 用户反馈

1. 用户对新闻执行收藏、喜欢或不感兴趣操作。
2. 系统保存反馈及时间。
3. MVP 中反馈只影响后续的确定性权重。
4. 后续版本可将聚合后的反馈摘要提供给 Agent，但不得直接把未经清洗的用户输入当作系统指令。

## 8. 功能需求

### 8.1 账户与权限

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-AUTH-001 | 用户可以注册、登录和退出 | P0 |
| FR-AUTH-002 | 密码必须使用强哈希保存 | P0 |
| FR-AUTH-003 | 用户只能访问自己的偏好、简报、反馈和推送记录 | P0 |
| FR-AUTH-004 | 用户可以修改密码和时区 | P0 |
| FR-AUTH-005 | 管理员权限与普通用户权限分离 | P1 |
| FR-AUTH-006 | 登录和敏感操作应有频率限制 | P1 |

### 8.2 订阅偏好

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-PREF-001 | 用户可以选择预设话题 | P0 |
| FR-PREF-002 | 用户可以添加自定义关键词和排除关键词 | P0 |
| FR-PREF-003 | 用户可以设置语言、地区和来源范围 | P0 |
| FR-PREF-004 | 用户可以设置每份简报的最大条数 | P0 |
| FR-PREF-005 | 用户可以设置生成时间、时区和周末策略 | P0 |
| FR-PREF-006 | 用户可以暂停和恢复订阅 | P0 |
| FR-PREF-007 | 用户可以选择新闻类型，如产品、研究、融资或政策 | P1 |
| FR-PREF-008 | 系统保存偏好修改时间和版本 | P1 |

### 8.3 新闻来源

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-SOURCE-001 | 系统支持 RSS/Atom 来源 | P0 |
| FR-SOURCE-002 | 系统支持至少一种联网搜索来源 | P0 |
| FR-SOURCE-003 | 新闻提供方通过统一 Provider 接口接入 | P0 |
| FR-SOURCE-004 | 每个来源可设置语言、分类、可信度和启用状态 | P1 |
| FR-SOURCE-005 | 系统记录来源请求成功率和最近错误 | P1 |
| FR-SOURCE-006 | 管理员可以暂停异常来源 | P1 |

### 8.4 Agent 运行

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-AGENT-001 | Agent 必须使用 Function Calling 或 ReAct 循环 | P0 |
| FR-AGENT-002 | 工具选择、顺序和调用次数由 LLM 决定 | P0 |
| FR-AGENT-003 | 系统不得用固定代码规定完整的搜索到推送顺序 | P0 |
| FR-AGENT-004 | Agent 必须支持最多步骤、总超时和费用上限 | P0 |
| FR-AGENT-005 | 每一次工具调用均需记录名称、参数摘要、状态和耗时 | P0 |
| FR-AGENT-006 | Agent 最终输出必须通过结构化 Schema 校验 | P0 |
| FR-AGENT-007 | 相同工具和相同参数连续重复时应触发循环检测 | P0 |
| FR-AGENT-008 | 工具异常结果必须返回模型，由模型决定重试、更换工具或结束 | P0 |
| FR-AGENT-009 | 模型、提示词和工具 Schema 必须有版本记录 | P1 |
| FR-AGENT-010 | 工具调用轨迹仅保存在后台审计数据中，不得在用户前端展示 | P1 |

### 8.5 简报

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-DIGEST-001 | 简报包含标题、日期、概览和新闻列表 | P0 |
| FR-DIGEST-002 | 每条新闻包含标题、摘要、来源、原文链接和发布时间 | P0 |
| FR-DIGEST-003 | 每条新闻包含推荐理由和分类 | P0 |
| FR-DIGEST-004 | 简报条数遵循用户设置 | P0 |
| FR-DIGEST-005 | 同一简报内不得包含重复新闻 | P0 |
| FR-DIGEST-006 | 支持同一日期的多个版本 | P0 |
| FR-DIGEST-007 | 用户可以查看今日和历史简报 | P0 |
| FR-DIGEST-008 | 用户可以收藏或标记不感兴趣 | P1 |
| FR-DIGEST-009 | 可选生成“今日趋势”概览 | P1 |

### 8.6 调度和任务

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-JOB-001 | 按用户时区创建每日任务 | P0 |
| FR-JOB-002 | 同一用户、日期和计划只创建一个有效任务 | P0 |
| FR-JOB-003 | 用户可以手动触发任务 | P0 |
| FR-JOB-004 | 用户可以查看任务状态 | P0 |
| FR-JOB-005 | 系统支持安全重试和指数退避 | P0 |
| FR-JOB-006 | 运行中的任务可以被管理员取消 | P1 |
| FR-JOB-007 | 调度器与 Web 服务分离，避免多 Worker 重复调度 | P0 |
| FR-JOB-008 | 手动生成任务在用户离开状态页后继续后台运行；运行期间再次点击概览页生成按钮应返回同一任务状态页，不得重复创建任务 | P0 |

### 8.7 推送

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-DELIVERY-001 | 支持站内简报 | P0 |
| FR-DELIVERY-002 | 支持邮件推送 | P0 |
| FR-DELIVERY-003 | 推送必须有幂等键 | P0 |
| FR-DELIVERY-004 | 记录发送状态、尝试次数和错误信息 | P0 |
| FR-DELIVERY-005 | 推送失败不应删除已经生成的简报 | P0 |
| FR-DELIVERY-006 | 支持手动重试失败推送 | P1 |
| FR-DELIVERY-007 | 后续可通过适配器增加 Webhook 和即时通信渠道 | P1 |

### 8.8 管理与可观察性

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-OPS-001 | 管理员可查看任务和 Agent 运行列表 | P1 |
| FR-OPS-002 | 可按状态、用户和日期筛选失败运行 | P1 |
| FR-OPS-003 | 展示 token、模型调用次数和估算费用 | P1 |
| FR-OPS-004 | 日志中的密钥、Cookie 和敏感字段必须脱敏 | P0 |
| FR-OPS-005 | 系统提供存活和就绪检查 | P1 |

## 9. Agent 工具设计

系统提供 10 个首期工具。每个工具均使用严格参数 Schema、明确返回结构、超时限制和权限控制。

### 9.1 基础文件与运行工具

#### T01 `list_dir`

- 作用：列出 Agent 工作目录中的文件和子目录。
- 输入：相对路径。
- 输出：名称、类型、大小和修改时间。
- 限制：只能访问任务工作目录；禁止路径逃逸和符号链接逃逸。

#### T02 `read_file`

- 作用：读取允许目录中的文本文件。
- 输入：相对路径、可选字符上限。
- 输出：文件内容、编码、是否截断。
- 限制：禁止读取密钥、环境文件、数据库文件和主机配置。

#### T03 `search_content`

- 作用：在指定工作目录下搜索关键词。
- 输入：关键词、目录、可选文件类型。
- 输出：文件、行号和匹配片段。
- 限制：限制搜索范围、文件数量和返回总长度。

#### T04 `write_file`

- 作用：保存中间分析、导出简报或调试报告。
- 输入：相对路径、内容和写入模式。
- 输出：最终路径、写入字节数和内容哈希。
- 限制：只允许写入任务输出目录；默认不覆盖现有文件。

#### T05 `bash`

- 作用：在隔离环境中执行经过允许的辅助命令。
- 输入：命令文本。
- 输出：退出码、标准输出、标准错误和耗时。
- 限制：必须在容器沙箱中运行；使用命令白名单；限制 CPU、内存、时间和输出；不得访问主机目录、密钥或任意网络。

### 9.2 新闻业务工具

#### T06 `search_news`

- 作用：根据查询词、时间窗口、语言和来源搜索候选新闻。
- 输入：查询词、开始时间、结束时间、来源、语言和数量上限。
- 输出：候选新闻标题、URL、来源、发布时间、摘要片段和语言。
- 业务价值：Agent 可以根据结果质量主动修改搜索词并再次搜索。

#### T07 `fetch_article`

- 作用：抓取并提取指定 URL 的主要正文和元数据。
- 输入：URL 和最大正文长度。
- 输出：规范 URL、标题、作者、来源、发布时间、正文、内容哈希和抓取状态。
- 限制：执行 SSRF 防护、域名与 IP 校验、跳转限制、大小限制和超时。

#### T08 `query_news_history`

- 作用：查询用户近期已处理、已保存或已推送的新闻。
- 输入：用户标识、URL、标题哈希、关键词和查询天数。
- 输出：历史新闻及出现日期、简报版本和推送状态。
- 业务价值：辅助跨日去重，避免重复推送旧事件。

#### T09 `save_digest`

- 作用：将结构化简报保存为数据库记录。
- 输入：用户、简报日期、标题、概览、新闻项和运行标识。
- 输出：简报标识、版本、状态和保存条数。
- 限制：业务层重新校验用户权限、来源、条数、重复项和字段长度。

#### T10 `push_digest`

- 作用：通过指定的受控渠道推送已保存简报。
- 输入：简报标识和渠道标识。
- 输出：发送记录标识、渠道、状态和发送时间。
- 限制：收件人来自用户验证配置；执行幂等检查；Agent 不得传入任意地址。

### 9.3 不应包装为工具的能力

以下能力优先由模型推理或确定性业务代码完成：

- 新闻摘要、标题生成和内容分类：由模型完成。
- 固定评分公式：由普通业务代码完成。
- URL 精确去重和字段校验：由业务服务完成。
- JSON Schema 校验：由数据校验层完成。
- 身份认证、授权和事务：由 Web 与服务层完成。

### 9.4 后续候选工具

- `verify_claim`：跨来源核验重要事实。
- `get_trending_topics`：获取趋势主题。
- `get_user_feedback`：读取聚合后的用户反馈。
- `get_source_reputation`：查询来源可信度。
- `translate_article`：调用独立翻译服务。
- `generate_audio_digest`：生成语音简报。

## 10. Agent 运行机制

### 10.1 运行目标

Agent 每次接收一个目标导向任务，而不是固定的步骤列表。任务上下文包括：

- 用户偏好快照。
- 时间窗口和时区。
- 新闻条数要求。
- 当前可用工具定义。
- 工具安全边界。
- 最近简报和已推送内容摘要。
- 输出 Schema。
- 最大步骤、超时和成本预算。
- 对网页提示注入的防御指令。

### 10.2 自主决策边界

Agent 可以自主决定：

- 使用哪些关键词搜索。
- 是否扩大、缩小或改写查询。
- 是否读取某篇新闻全文。
- 是否查询更长的历史范围。
- 候选新闻的相关性、重要性和分类。
- 最终选择哪些新闻。
- 何时已经获得足够信息并结束。

Agent 不可以自主决定：

- 绕过身份认证或权限。
- 访问工作目录以外的文件。
- 修改用户账户和推送目标。
- 绕过来源、重复项和字段校验。
- 向未验证地址发送内容。
- 提高自己的步骤、费用或时间上限。
- 直接执行任意主机命令。

### 10.3 循环控制

运行时负责重复执行“请求模型—解析工具调用—执行工具—回传结果—请求模型”，直到满足停止条件。运行时只控制安全和资源边界，不硬编码业务工具顺序。

默认限制：

- 最大模型轮次：15。
- 最大工具调用次数：25。
- 单次运行总时限：5 分钟。
- 单工具默认超时：30 秒；抓取工具可单独配置。
- 相同工具和相同参数连续调用上限：2 次。
- 单次运行最多成功推送一次同一简报和同一渠道。
- 模型 token 和估算费用设置每次运行上限。

### 10.4 最终输出结构

Agent 的最终输出为 `DigestDraft`，至少包含：

- `title`：简报标题。
- `digest_date`：用户时区下的简报日期。
- `coverage_start`、`coverage_end`：覆盖时间范围。
- `overview`：当日整体概览。
- `items`：新闻列表。
- `warnings`：来源不足、抓取失败等非致命问题。

每个新闻项至少包含：

- 原始标题和生成标题。
- 原始 URL 和规范 URL。
- 来源名称。
- 发布时间。
- 中文摘要。
- 推荐理由。
- 分类和关键词。
- 相关性分数。
- 用于生成摘要的证据片段或内容标识。

### 10.5 运行状态

Agent 运行状态：

`PENDING → RUNNING → DRAFTED → VALIDATING → SAVED → DELIVERING → SUCCEEDED`

异常状态：

- `RETRYING`：存在可重试错误。
- `PARTIAL_SUCCESS`：简报已保存但部分渠道推送失败。
- `FAILED`：运行未生成可用简报。
- `CANCELLED`：被用户或管理员取消。

## 11. 新闻处理策略

### 11.1 时间过滤

- 默认只收录过去 24 小时内发布的新闻。
- 用户可配置 12、24、48 或 72 小时时间窗口。
- 无法确认发布时间的新闻默认降低优先级，不作为头条。
- 所有内部时间使用 UTC 保存，展示时转换为用户时区。

### 11.2 去重策略

MVP 采用三层策略：

1. 规范化 URL 精确匹配。
2. 归一化标题哈希匹配。
3. 标题与来源、时间和关键词的组合相似性判断。

后续可增加正文向量相似度。确定性去重必须在保存前再次执行，不能只依赖 Agent 判断。

### 11.3 质量过滤

以下候选默认排除：

- 不在时间窗口内。
- 命中用户排除关键词。
- 无来源或 URL 无效。
- 正文为空、过短或主要为广告。
- 已经向该用户推送。
- 来源处于禁用名单。
- 内容与 AI 主题没有实质关系。
- 明显由其他媒体转载且无法找到更原始来源。

### 11.4 推荐排序

默认确定性评分建议：

| 因素 | 默认权重 |
|---|---:|
| 主题匹配度 | 35% |
| 关键词匹配度 | 20% |
| 来源可信度 | 15% |
| 新鲜度 | 15% |
| 内容重要性 | 15% |

模型可以对评分相近的候选进行综合比较，但不得绕过硬性过滤条件。

## 12. 系统总体架构

### 12.1 逻辑架构

```text
用户浏览器
    │
    ▼
Web / API 服务
    ├── 认证与权限
    ├── 用户偏好
    ├── 简报与历史
    ├── 用户反馈
    └── 管理页面
           │
           ▼
      关系型数据库
           ▲
           │
调度器 ─→ 任务队列/任务表 ─→ Worker
                              ├── Agent Runtime
                              ├── LLM Provider
                              ├── Tool Registry
                              ├── News Providers
                              ├── File/Shell Sandbox
                              └── Delivery Adapters
```

### 12.2 组件职责

#### Web / API 服务

- 处理账户、偏好、简报和反馈请求。
- 执行认证、授权、CSRF 防护和输入校验。
- 创建手动任务，但不在请求线程中执行 Agent。

#### 调度器

- 根据用户时区和计划创建任务。
- 使用幂等键避免重复任务。
- 仅负责创建任务，不执行 Agent。

#### Worker

- 领取任务并执行 Agent。
- 管理超时、取消、重试和状态更新。
- 保存模型和工具调用轨迹。

#### Agent Runtime

- 组装上下文和工具 Schema。
- 与模型交互。
- 解析工具调用并调用 Tool Registry。
- 执行循环检测、预算控制和最终结构校验。

#### Tool Registry

- 注册工具及版本。
- 校验调用参数。
- 执行权限、超时和审计策略。
- 统一处理工具成功与失败结果。

#### News Provider

- 统一封装 RSS、搜索服务和未来的新闻 API。
- 将不同来源转换为统一候选格式。

#### Delivery Adapter

- 统一封装站内、邮件和未来的即时通信渠道。
- 管理渠道配置、重试和提供方消息标识。

## 13. 技术栈

### 13.1 推荐技术选型

| 类别 | 选型 | 说明 |
|---|---|---|
| 编程语言 | Python 3.12+ | 类型支持成熟，适合 Agent 和 Web 开发 |
| 包与环境管理 | uv | 依赖安装快，并提交锁文件 |
| Web 框架 | FastAPI | API、异步 I/O 和类型集成良好 |
| 数据校验 | Pydantic | 工具输入、输出和配置 Schema |
| ORM | SQLAlchemy | 统一数据库访问 |
| 数据库迁移 | Alembic | 所有结构变化可追踪 |
| 本地数据库 | SQLite | 仅用于开发和演示 |
| 正式数据库 | PostgreSQL | 支持并发、事务和扩展 |
| 页面 | Jinja2 + HTMX | 满足简单前端，降低工程复杂度 |
| 样式 | 简单 CSS 或轻量组件库 | 不以视觉设计为重点 |
| HTTP 客户端 | httpx | 异步抓取和超时控制 |
| RSS 解析 | feedparser | 读取 RSS/Atom |
| 网页提取 | BeautifulSoup + 正文提取库 | 清洗 HTML 和提取正文 |
| 调度 | APScheduler 独立进程 | MVP 定时任务 |
| 分布式任务 | 后续采用 Redis + RQ/Dramatiq | 多实例部署时启用 |
| LLM 接口 | Provider 抽象 | 避免业务层绑定单一厂商 |
| 默认 Agent 实现 | OpenAI Responses API Function Calling | 手写循环便于展示自主调用和审计 |
| 测试 | pytest | 单元、集成和端到端测试 |
| 静态检查 | Ruff、mypy | 格式、规范和类型检查 |
| 部署 | Docker Compose | Web、Worker、Scheduler、PostgreSQL 分离 |

### 13.2 LLM 接入原则

- 第一版实现统一 `LLMProvider` 抽象。
- 默认实现基于支持严格函数调用的 Responses 接口。
- 模型名称、超时、重试和预算全部通过配置提供。
- 业务服务不得直接依赖具体模型返回的非标准字段。
- 后续可以增加兼容其他模型供应商的 Provider。
- OpenAI 官方 Function Calling 指南：<https://developers.openai.com/api/docs/guides/function-calling>
- OpenAI Agents SDK 参考：<https://developers.openai.com/api/docs/guides/agents/sdk>

## 14. 数据模型

### 14.1 `users`

- `id`
- `email` 或 `username`
- `password_hash`
- `role`
- `timezone`
- `is_active`
- `created_at`
- `updated_at`

### 14.2 `preference_profiles`

- `id`
- `user_id`
- `topics`
- `keywords`
- `excluded_keywords`
- `languages`
- `regions`
- `content_types`
- `source_preferences`
- `max_items`
- `coverage_hours`
- `schedule_time`
- `weekend_enabled`
- `enabled`
- `version`
- `updated_at`

### 14.3 `source_configs`

- `id`
- `name`
- `provider_type`
- `base_url`
- `config`
- `language`
- `category`
- `trust_level`
- `enabled`
- `last_success_at`
- `last_error`

### 14.4 `articles`

- `id`
- `canonical_url`
- `original_url`
- `title`
- `normalized_title`
- `title_hash`
- `source_name`
- `author`
- `published_at`
- `fetched_at`
- `language`
- `content_excerpt`
- `content_hash`
- `fetch_status`
- `created_at`

### 14.5 `digest_jobs`

- `id`
- `user_id`
- `job_type`
- `scheduled_for`
- `idempotency_key`
- `status`
- `attempt_count`
- `next_retry_at`
- `created_at`
- `started_at`
- `finished_at`
- `error_code`
- `error_message`

### 14.6 `agent_runs`

- `id`
- `job_id`
- `user_id`
- `status`
- `model_provider`
- `model_name`
- `prompt_version`
- `toolset_version`
- `preference_snapshot`
- `started_at`
- `finished_at`
- `model_call_count`
- `tool_call_count`
- `input_tokens`
- `output_tokens`
- `estimated_cost`
- `stop_reason`
- `error_message`

### 14.7 `tool_calls`

- `id`
- `run_id`
- `sequence_number`
- `tool_name`
- `tool_version`
- `arguments_redacted`
- `result_summary`
- `status`
- `started_at`
- `finished_at`
- `duration_ms`
- `error_code`

### 14.8 `digests`

- `id`
- `user_id`
- `run_id`
- `digest_date`
- `version`
- `status`
- `title`
- `overview`
- `coverage_start`
- `coverage_end`
- `warnings`
- `created_at`

唯一约束建议为 `user_id + digest_date + version`。

### 14.9 `digest_items`

- `id`
- `digest_id`
- `article_id`
- `rank`
- `generated_title`
- `generated_summary`
- `recommendation_reason`
- `category`
- `keywords`
- `relevance_score`
- `evidence_reference`

### 14.10 `delivery_channels`

- `id`
- `user_id`
- `channel_type`
- `display_name`
- `encrypted_config`
- `verified_at`
- `enabled`
- `created_at`

### 14.11 `deliveries`

- `id`
- `digest_id`
- `channel_id`
- `idempotency_key`
- `status`
- `attempt_count`
- `provider_message_id`
- `last_error`
- `sent_at`
- `created_at`

### 14.12 `user_feedback`

- `id`
- `user_id`
- `article_id`
- `digest_id`
- `feedback_type`
- `created_at`

## 15. 页面和交互需求

### 15.1 登录和注册页

- 用户名或邮箱、密码。
- 清晰显示校验错误。
- 不暴露账户是否存在的敏感细节。

### 15.2 今日简报页

- 展示当前日期和覆盖时间范围。
- 展示新闻标题、摘要、来源、发布时间和推荐理由。
- 支持跳转原文；简报详情页不显示收藏和不感兴趣操作入口。
- 显示当前简报版本和推送状态。

### 15.3 偏好设置页

- 主题多选。
- 关键词和排除词编辑。
- 语言、地区和来源配置。
- 每日时间、时区、条数和周末策略。
- 推送渠道启用或停用。
- 保存后显示下一次运行时间。

### 15.4 历史简报页

- 按日期分页。
- 显示生成状态和版本。
- 可进入详情和发起重新生成。

### 15.5 运行详情页

- 展示任务和 Agent 状态。
- 展示模型、提示词版本和工具集版本。
- 不展示工具调用时间线、调用次数、工具名称、耗时或结果状态；相关轨迹仅供后台审计。
- 展示 token 和估算费用。
- 对错误信息进行脱敏。

### 15.6 管理页

- 查看运行列表、失败任务和来源状态。
- 启停来源。
- 对失败任务执行重试。
- 查看整体成功率和推送失败率。

## 16. API 边界

MVP 至少提供以下逻辑接口，实际路径可在接口设计阶段确定：

- 账户：注册、登录、退出、查看当前用户。
- 偏好：读取、更新、暂停、恢复。
- 简报：今日简报、历史列表、详情、重新生成。
- 任务：手动创建、查询状态、取消。
- 反馈：收藏、喜欢、不感兴趣、撤销反馈。
- 渠道：创建、验证、启用、停用、删除。
- 管理：任务列表、运行详情、来源管理、失败重试。
- 健康检查：存活检查、数据库和依赖就绪检查。

写接口必须执行 CSRF 防护或等效机制。所有资源访问都需要进行对象级权限检查。

## 17. 安全设计

### 17.1 身份与数据安全

- 密码使用 Argon2 或等效强哈希。
- Session Cookie 使用 `HttpOnly`、`Secure` 和合理的 `SameSite` 设置。
- 所有写操作进行权限和 CSRF 校验。
- 邮箱、推送地址和 Provider 配置视为敏感数据。
- 外部服务密钥通过环境变量或秘密管理系统注入。
- `.env`、密钥和生产数据禁止提交 Git。

### 17.2 网页和提示注入防护

- 新闻网页内容始终标记为不可信数据。
- 系统提示明确禁止执行网页中的命令或角色指令。
- HTML 清洗后只向模型提供必要正文和元数据。
- 不把网页内容拼接到系统指令区域。
- 工具授权由运行时执行，不能因网页文本改变。

### 17.3 SSRF 防护

- 只允许 HTTP 和 HTTPS。
- 阻止 localhost、私网、链路本地和云元数据地址。
- DNS 解析后再次校验目标 IP。
- 限制重定向次数，并校验每次重定向地址。
- 限制响应体大小、MIME 类型和请求时间。

### 17.4 文件和 Shell 安全

- 文件工具只能访问每次任务独立的工作目录。
- 使用解析后的绝对路径确认目标仍位于允许根目录。
- Bash 工具在一次性沙箱或受限容器运行。
- 命令白名单优先于黑名单。
- 禁止读取环境变量、密钥挂载和宿主机文件。
- 禁止执行软件安装、提权和破坏性命令。

### 17.5 推送安全

- 推送目标不能由模型自由填写。
- 渠道配置必须属于任务用户。
- 使用 `digest_id + channel_id` 生成幂等键。
- 重试前检查提供方消息标识和本地发送状态。

## 18. 非功能需求

### 18.1 性能

- 普通 Web 页面在不含外部调用时，95% 请求在 500 毫秒内返回。
- 简报历史分页查询在常规数据量下 1 秒内返回。
- 单次 Agent 默认在 5 分钟内完成。
- 抓取和模型调用不得阻塞 Web 请求线程。

### 18.2 可靠性

- 所有长期任务具有持久化状态。
- 服务重启后可识别未完成任务并恢复或标记失败。
- 外部调用使用明确超时、有限重试和指数退避。
- 数据库写入和状态转换使用事务。

### 18.3 可维护性

- 业务层、Provider、工具和 Web 层分离。
- 工具输入输出均有类型定义和版本。
- 关键逻辑具备单元测试。
- 数据库变化必须通过迁移脚本。
- 配置和代码分离。

### 18.4 可观察性

- 使用结构化日志和关联标识。
- Job、Run、模型请求和工具调用使用同一条追踪链。
- 记录运行耗时、成功率、工具错误率和推送失败率。
- 记录 token 与估算费用，但不记录敏感正文和密钥。

### 18.5 可移植性

- 开发环境支持 Windows。
- 正式运行环境优先使用 Linux 容器。
- 通过 Docker Compose 启动主要服务。
- 文件路径使用跨平台路径处理方式。

## 19. 测试方案

### 19.1 单元测试

- 用户时区和时间窗口计算。
- 偏好规则合并。
- URL 规范化。
- 标题归一化和哈希。
- 精确去重和跨日去重。
- 新闻评分公式。
- Agent 最大步骤和总超时。
- 重复工具调用检测。
- 工具参数 Schema 校验。
- 路径逃逸和符号链接逃逸。
- SSRF 地址识别。
- 推送幂等键。
- 任务状态转换。

### 19.2 工具集成测试

- 每个工具的正常返回、超时和错误返回。
- News Provider 返回统一数据结构。
- 正文提取对常见页面结构有效。
- 数据库查询只返回当前用户数据。
- `save_digest` 拒绝无来源或重复项。
- `push_digest` 拒绝未验证渠道和重复发送。
- 工具调用记录在后台持久化且保持顺序，但任务详情页不包含调用轨迹、次数或详情。
- Bash 沙箱无法访问宿主机敏感路径。

### 19.3 Agent 场景测试

使用固定新闻数据集和模拟 Provider，确保测试可重复：

- Agent 能自主调用新闻搜索工具。
- 搜索结果不足时会修改查询或补充搜索。
- 重要候选会调用正文抓取工具。
- 会查询历史并剔除已推送内容。
- 工具失败时能够重试、更换策略或正确结束。
- 不同用户偏好会产生不同的工具轨迹和结果。
- 网页中包含提示注入文本时不会执行其指令。
- 最终输出符合 `DigestDraft` Schema。
- 达到工具预算时能够安全停止。

### 19.4 端到端测试

1. 用户注册并登录。
2. 配置主题、关键词、时间、时区、条数和渠道。
3. 手动生成简报。
4. 查看任务运行状态，确认页面不显示工具调用轨迹。
5. 在任务运行中切换到其他标签页，确认任务继续执行；返回概览再次点击生成入口，应回到同一任务状态页且不创建重复任务。
6. 查看生成的简报。
7. 触发站内或测试邮件推送。
8. 查看历史简报。
9. 重新生成并得到新版本。
10. 验证旧版本仍存在。
11. 验证同一简报不会重复推送。

### 19.5 新环境部署验收

在没有既有虚拟环境、`.env` 和数据库的临时目录中，从 Git 跟踪文件开始验证：

1. 按 README 使用 Python 3.12 和 `uv.lock` 安装开发依赖。
2. 从 `.env.example` 创建 `.env`，并确认不填写可选 TwitterAPI.io 与 SMTP 配置时仍能启动。
3. 创建本地数据目录并执行 `alembic upgrade head`，确认 SQLite 数据库可完成迁移。
4. 启动 Web 后访问 `/health`，确认返回 `status=ok`，并完成注册、登录和偏好保存。
5. 在配置测试用模型密钥后手动生成简报；另启调度器，确认进程可正常进入调度循环。
6. 使用 Docker Compose 启动 PostgreSQL、执行迁移并启动 Web 与调度器，确认健康检查可访问。
7. 验证 `docker compose down` 后数据库卷仍存在，并记录彻底删除卷会造成数据丢失。
8. 运行 pytest、Ruff 和 mypy，确认全部通过。
9. 确认 `.env`、SQLite 数据库、运行日志、缓存和真实密钥均不在 Git 跟踪列表中。

## 20. MVP 验收标准

满足以下全部条件方可认定 MVP 完成：

1. 用户可以注册、登录和退出。
2. 用户可以保存完整订阅偏好。
3. 系统可以按计划和手动创建生成任务。
4. Agent 注册并可使用 10 个工具。
5. 工具选择和顺序由模型决定，执行轨迹在后台可审计且不在用户前端展示。
6. 至少存在两个测试场景，产生不同的工具调用顺序。
7. 简报至少能从 RSS 和一种联网搜索来源获取内容。
8. 正式简报包含不少于 3 条新闻，且每条具有有效来源。
9. 同一简报内无重复新闻。
10. 任务重试不产生重复简报或重复推送。
11. 用户可以查看历史简报和版本。
12. 站内推送可用，邮件推送在测试环境可验证。
13. Agent 运行、模型调用和工具调用均有审计记录。
14. 路径逃逸、SSRF、提示注入和推送越权测试通过。
15. 单元测试、集成测试和端到端核心流程通过。
16. 项目可通过文档在新环境完成安装和启动。

## 21. 项目目录规划

```text
ai-news-agent/
├── app/
│   ├── api/                 # HTTP API
│   ├── web/                 # 页面路由
│   ├── auth/                # 认证和授权
│   ├── agents/              # Runtime、提示词、Schema 和策略
│   ├── tools/               # 10 个工具及注册表
│   ├── providers/
│   │   ├── llm/             # 模型 Provider
│   │   ├── news/            # 新闻 Provider
│   │   └── delivery/        # 推送 Provider
│   ├── services/            # 业务服务
│   ├── jobs/                # Scheduler 和 Worker 任务
│   ├── models/              # ORM 模型
│   ├── repositories/        # 数据访问层
│   ├── templates/           # Jinja2 页面
│   └── static/              # CSS 和少量前端资源
├── migrations/              # Alembic 迁移
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── agent_cases/
│   ├── e2e/
│   └── fixtures/
├── runtime/                 # 本地运行数据，不提交 Git
├── scripts/                 # 开发和运维脚本
├── docs/                    # 需求、架构和运维文档
├── pyproject.toml
├── uv.lock
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

## 22. Git 管理规范

### 22.1 分支策略

- `main`：始终保持可运行和可测试。
- 功能开发使用短期分支，如 `feat/agent-runtime`。
- 修复使用 `fix/delivery-idempotency` 等分支。
- 不建议在小型项目中长期维护复杂的 `develop` 分支。

### 22.2 提交规范

采用 Conventional Commits：

- `feat:` 新功能。
- `fix:` 缺陷修复。
- `test:` 测试。
- `docs:` 文档。
- `refactor:` 不改变行为的重构。
- `chore:` 工具、依赖和维护。

每个提交只处理一个逻辑主题，确保可以独立审查和回退。

### 22.3 合并要求

- Ruff 检查通过。
- mypy 检查通过。
- pytest 通过。
- 不包含密钥、真实用户数据和运行缓存。
- 数据库结构变化包含迁移。
- 用户可见行为变化同步更新文档。

## 23. 开发阶段与提交拆分

### 阶段 0：文档和工程决策

目标：确认需求、架构和关键产品选择。

建议提交：

1. `docs: add product requirements and architecture baseline`
2. `docs: record architecture decisions and security boundaries`

### 阶段 1：工程骨架

目标：项目可以安装、启动、检查和测试。

建议提交：

1. `chore: initialize python project with uv`
2. `chore: configure ruff mypy and pytest`
3. `feat: add application settings and environment template`
4. `feat: add fastapi health endpoints`
5. `chore: add docker compose development services`

### 阶段 2：数据库和账户

目标：完成数据基础、认证和权限。

建议提交：

1. `feat: add database session and initial migration`
2. `feat: add user model and password authentication`
3. `feat: add session based login and logout`
4. `feat: add preference profile management`
5. `test: cover authentication and preference authorization`

### 阶段 3：Agent 工具基础

目标：实现工具注册、校验和五个基础工具。

建议提交：

1. `feat: add versioned tool registry`
2. `feat: add sandboxed filesystem tools`
3. `feat: add restricted shell tool`
4. `test: cover tool validation and sandbox boundaries`

### 阶段 4：新闻 Provider 和业务工具

目标：完成候选新闻获取、正文提取和历史查询。

建议提交：

1. `feat: add news provider abstraction`
2. `feat: add rss news provider`
3. `feat: add web search news provider`
4. `feat: add secure article fetcher`
5. `feat: add article normalization and deduplication`
6. `feat: add news search fetch and history tools`
7. `test: cover news providers and ssrf protection`

### 阶段 5：Agent Runtime

目标：实现真正由模型决定工具调用的循环。

建议提交：

1. `feat: add llm provider abstraction`
2. `feat: add responses function calling provider`
3. `feat: add agent context and digest schemas`
4. `feat: implement autonomous tool calling loop`
5. `feat: add agent budgets and loop detection`
6. `feat: persist agent runs and tool calls`
7. `test: add deterministic agent scenarios`

### 阶段 6：简报与页面

目标：完成简报保存、展示和历史查询。

建议提交：

1. `feat: add digest and article data models`
2. `feat: add validated digest persistence tool`
3. `feat: add digest history and detail services`
4. `feat: add today and history pages`
5. `feat: add feedback and bookmark actions`
6. `test: cover digest versions and ownership`

### 阶段 7：调度和推送

目标：实现完整的每日自动流程。

建议提交：

1. `feat: add persistent digest jobs`
2. `feat: add timezone aware scheduler`
3. `feat: add worker retries and idempotency`
4. `feat: add in app delivery adapter`
5. `feat: add email delivery adapter`
6. `feat: add controlled push digest tool`
7. `test: cover job and delivery idempotency`

### 阶段 8：管理、可观察性和安全加固

目标：达到可演示、可排错和可验收状态。

建议提交：

1. `feat: add agent run timeline page`
2. `feat: add structured logs and metrics`
3. `feat: add source health management`
4. `fix: harden prompt injection boundaries`
5. `fix: enforce redaction and secret handling`
6. `test: add security and end to end suites`
7. `docs: add deployment and operations guide`

## 24. 风险与应对

| 风险 | 影响 | 应对方案 |
|---|---|---|
| 新闻网站结构变化 | 正文提取失败 | Provider 隔离、监控失败率、支持 RSS 回退 |
| 搜索服务不稳定 | 候选不足 | 多 Provider、有限重试、允许部分成功 |
| 模型编造来源 | 简报可信度下降 | URL 必须来自工具结果，保存前确定性校验 |
| 模型陷入循环 | 成本和延迟失控 | 最大步数、重复调用检测、总预算 |
| 网页提示注入 | 非预期工具操作 | 数据与指令隔离、工具权限不受内容影响 |
| 重复推送 | 用户体验受损 | 数据库唯一约束和推送幂等键 |
| 多实例重复调度 | 重复任务 | 独立调度进程和任务唯一约束 |
| Bash 工具权限过大 | 系统安全风险 | 沙箱、白名单、资源限制和禁用主机访问 |
| 邮件进入垃圾箱 | 推送到达率低 | 正确配置邮件域名、保留站内简报 |
| 模型费用过高 | 无法持续运行 | 预算限制、候选截断、模型路由和缓存 |

## 25. 开发前待确认事项

以下选择不会阻止工程骨架开发，但应在对应阶段开始前确认：

1. 默认 LLM 供应商、模型和单次运行费用预算。
2. 首批 RSS 和搜索来源名单。
3. 邮件服务提供方。
4. 是否允许普通用户自定义新闻来源。
5. 用户注册是否开放，还是仅管理员邀请。
6. 简报默认条数和默认生成时间。
7. Bash 工具在最终演示环境中的允许命令清单。
8. 是否需要部署到公网；如果需要，确定域名、HTTPS 和备份策略。

若没有额外指定，MVP 默认决策如下：

- 单 Agent。
- Provider 抽象下使用 OpenAI Responses Function Calling。
- RSS + 一种联网搜索来源。
- 站内 + 邮件推送。
- 开发使用 SQLite，正式环境使用 PostgreSQL。
- 服务端渲染页面使用 Jinja2 + HTMX。
- 默认每天 08:00，时区 `Asia/Shanghai`，最多 10 条新闻。
- 默认搜索过去 24 小时，最多 25 次工具调用，最长运行 5 分钟。
- 注册模式在开发环境开放，正式环境默认管理员邀请。

## 26. 完成定义

项目不仅要“能够运行”，还需满足以下完成定义：

- 本文档中的所有 P0 需求已经实现并通过测试。
- 10 个工具均有参数校验、权限边界、超时和测试。
- 能通过工具轨迹证明调用顺序由 LLM 决定。
- 关键副作用具备幂等性和审计记录。
- 核心安全测试通过。
- 新开发者根据 README 可以完成安装、配置、迁移和启动。
- 演示数据和演示流程稳定可重复。
- 已知限制和未完成的 P1/P2 项目有明确记录。

---

本文档作为第一阶段开发基线。后续若改变工具数量、模型接口、推送渠道、认证方式或部署架构，应先更新相应章节并记录架构决策，再开始实现。
