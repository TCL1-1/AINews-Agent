# X 关注账号与 TwitterAPI 测试要求

## 用户填写位置

登录后进入左侧导航的“订阅偏好”，在“X 账号关注”区域填写最多 3 个账号。
每个输入框接受 `@sama`、`sama` 或 `https://x.com/sama` 格式。保存设置后，账号列表会进入
后续 Agent Run 的偏好快照；已开始的任务不受后续修改影响。

## 运行要求

1. 当偏好快照中的 `x_accounts` 非空时，Agent 必须在通用新闻发现前调用
   `search_x_accounts`，并完整传入全部配置账号和本次简报覆盖时间。
2. 工具必须使用 TwitterAPI.io Advanced Search，通过 `X-API-Key` 请求头认证，查询使用
   `from:<handle> since_time:<unix> until_time:<unix>`、`queryType=Latest` 和空首游标。
3. 最多配置 3 个账号，每个账号单独请求，避免单个高频账号挤占其他账号结果。
4. 每个在覆盖时段内返回候选的账号，最终简报至少包含 1 条该账号内容。
5. 账号无新帖、API 未配置、请求失败或内容无法核验时允许缺省，但简报 warnings 必须说明。
6. X 帖子可以证明账号自身表达或公告；涉及第三方的重要事实仍需官网或独立来源核验。

## 回归测试

- 账号输入支持 `@handle`、纯用户名和 X 主页 URL，并执行去重与最多 3 个限制。
- TwitterAPI 查询使用账号过滤和精确 Unix 时间窗口，不把 API Key 写入结果或审计参数。
- 3 个账号分别发起请求，结果保留命中的账号标识、发布时间、URL 和互动量。
- 偏好保存后，`x_accounts` 出现在 Agent Run 的偏好快照中。
- 未调用 `search_x_accounts` 时不能保存配置了关注账号的简报。
- 有窗口内候选但未将相应账号内容纳入简报时，保存必须失败并允许 Agent 修正。
- 无候选或 Provider 不可用时允许生成其他来源简报，并输出明确 warning。
