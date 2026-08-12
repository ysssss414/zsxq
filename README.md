# 知识星球 A 股公司只读检索报告工具

这个工具用于在本地通过 `zsxq-cli` 搜索某家 A 股公司相关内容，并调用 DeepSeek API 生成 Markdown 投研报告。

## 安全边界

工具内部只会调用以下 `zsxq-cli` 命令：

- `zsxq-cli auth status`
- `zsxq-cli group +list`
- `zsxq-cli group +topics`（仅在传入 `--recent-pages > 0` 时启用，用于只读扫描最近主题流）
- `zsxq-cli topic +search`
- `zsxq-cli topic +detail`

不会执行发帖、评论、点赞、删除、导出 token 等写入或敏感操作。

工具不会读取、打印或保存知识星球 token。`.env` 只读取 DeepSeek key、zsxq-cli 命令入口和参数模板。终端日志只输出进度、数量、topic_id 和文件路径，不输出知识星球原文全文。

## 环境准备

需要：

- Python 3.10+
- Node/npm
- 已安装并可运行的 `zsxq-cli`
- DeepSeek API key

示例：

```bash
npm install -g zsxq-cli
```

或者在项目内安装后使用默认的 `npx --no-install zsxq-cli`。如果你的机器上已经有全局命令，可以在 `.env` 里设置：

```env
ZSXQ_CLI=zsxq-cli
```

请先按 `zsxq-cli` 自身方式在本机完成登录。本工具运行时只做 `auth status` 检查。

## 配置

复制配置样例：

```bash
copy config.example.env .env
```

填写：

```env
DEEPSEEK_API_KEY=sk-your-deepseek-api-key
```

如果你的 `zsxq-cli` 参数和默认模板不同，只调整 flags，不需要改代码：

```env
ZSXQ_GROUP_LIST_ARGS=--json
ZSXQ_TOPIC_SEARCH_ARGS=--group-id {group_id} --query {keyword} --json
ZSXQ_TOPIC_DETAIL_ARGS=--topic-id {topic_id} --json
```

如果 Codex 沙箱无法读取 `C:\Users\<你>\.config\zsxq-cli\config.json`，可以把 zsxq-cli 的运行 home 放到项目目录：

```env
ZSXQ_CLI=./node_modules/.bin/zsxq-cli.cmd
ZSXQ_RUNTIME_HOME=.runtime-home
```

然后在 PowerShell 里用同一个运行 home 登录：

```powershell
cd D:\二级\quant\codex\zsxq
$env:HOME = "$PWD\.runtime-home"
$env:USERPROFILE = "$PWD\.runtime-home"
.\node_modules\.bin\zsxq-cli.cmd auth login
.\node_modules\.bin\zsxq-cli.cmd auth status
```

`.runtime-home/` 已加入 `.gitignore`。它可能包含 zsxq-cli 自己保存的登录凭据，不要提交或分享。

模板变量：

- `{group_id}`
- `{keyword}`
- `{topic_id}`

## 使用

### 网页端

本机单用户网页端会复用当前 `.env`、`zsxq-cli` 登录态和 `reports/` 输出目录：

```powershell
python web_app.py
```

默认访问：

```text
http://127.0.0.1:8765
```

网页端会在页面顶部提示：检索到的知识星球内容会发送到 DeepSeek API，用于生成报告。

### 命令行

按 group_id：

```bash
python main.py --company 宁德时代 --group-id 123456789 --days 30 --output-dir ./reports
```

按 group_name：

```bash
python main.py --company 宁德时代 --group-name 目标星球名称 --days 30 --output-dir ./reports
```

可选限制：

```bash
python main.py --company 宁德时代 --group-id 123456789 --output-dir ./reports --max-keywords 30 --max-topics 100
```

如果 `topic +search` 漏掉了时间范围内的内容，可以额外扫描最近主题流：

```bash
python main.py --company 宁德时代 --group-id 123456789 --days 30 --output-dir ./reports --max-keywords 30 --max-topics 100 --recent-pages 5
```

`--recent-pages 5` 表示额外按时间流最多扫描 5 页，每页默认 30 条。工具会在本地仅用公司名、全称、别名、股票代码等身份类关键词匹配最近主题流，并优先拉取 `days` 范围内的候选，避免行业泛词带来过多噪声。

如果想把最近主题流中 `days` 范围内的所有 topic 都加入候选，让 DeepSeek 再判断是否强相关，可以加：

```bash
--recent-include-all
```

注意：`--recent-include-all` 会绕过身份类关键词过滤，把最近主题流中 `days` 范围内的所有 topic 都加入候选，可能显著增加 detail 拉取和 DeepSeek 分析成本。

默认情况下，DeepSeek 会同时读取 topic detail 和搜索命中上下文：

```bash
--analysis-source detail_search
```

如果发现 detail 正文与搜索命中上下文不一致，或希望只基于 `search_raw.jsonl` 命中的内容整理汇总，可以使用：

```bash
--analysis-source search
```

可选值：

- `detail_search`：topic detail + search hit context，默认
- `detail`：仅使用 topic detail
- `search`：仅使用搜索命中上下文

## 输出文件

每次运行会在 `output_dir` 下创建一个带公司名和时间戳的子目录：

- `keyword_pack.json`：DeepSeek 生成的关键词包和本次选用关键词
- `search_raw.jsonl`：逐关键词搜索的 JSONL，已脱敏 token/cookie/secret 字段和 URL token 参数
- `detail_raw.jsonl`：按去重后的 `topic_id` 拉取的 detail JSONL，含 `in_range` 标记，已脱敏 token/cookie/secret 字段和 URL token 参数
- `topic_analysis.jsonl`：DeepSeek 对每条 topic 的结构化判断
- `report.md`：最终 Markdown 报告
- `run_metadata.json`：运行元信息和计数
- `errors.jsonl`：搜索或 detail 阶段的错误，错误信息已做敏感字段脱敏

## 报告结构

`report.md` 固定包含：

- 核心结论
- 高频主题
- 分条信息汇总
- 待验证清单
- 附录：topic_id、发布时间、作者、关键词来源

每条内容会由 DeepSeek 判断是否可进入报告，并标注信息强度：

- 强相关：公司层面订单、业绩、政策、技术、客户、明确推荐等
- 中等相关：板块逻辑中明确影响公司，或将公司作为核心/受益标的
- 弱相关：标的池、名单、情绪或间接映射
- 无实质关联：不展示在报告正文中

可展示条目会继续分类为：

- 订单
- 业绩
- 产业链
- 技术
- 政策
- 市场情绪
- 传闻
- 其他

可信度评级：

- A：有明确来源或可核验数据
- B：逻辑较强但需要二次验证
- C：弱信号或间接相关
- D：传闻、情绪或无法核验

## 常见问题

如果提示找不到 `npx` 或 `zsxq-cli`，请确认 Node/npm 已加入 PATH，或把 `.env` 中的 `ZSXQ_CLI` 改成可执行文件的完整路径。

如果提示 `zsxq-cli output is not JSON`，说明当前 zsxq-cli 参数没有返回 JSON。请根据你的 zsxq-cli 版本调整 `ZSXQ_*_ARGS`，让 `group +list`、`topic +search`、`topic +detail` 输出 JSON。

如果 group name 匹配到多个星球，工具会停止并提示改用 `--group-id`，避免误查。

## 搜索与截断逻辑

`zsxq-cli topic +search` 当前版本没有 `days`、分页或排序参数。工具会先收集每个关键词返回的候选 topic，按 `topic_id` 去重。

如果传入 `--recent-pages > 0`，工具还会调用只读命令 `group +topics` 扫描最近主题流，并在本地用公司名、全称、别名、股票代码等身份类关键词匹配候选。这个路径可以补足搜索默认排序或第一页结果导致的漏召回，同时减少行业泛词带来的无关候选。

合并候选后，工具会用搜索结果或时间流里的 `create_time` 做候选排序：

- 已知发布时间且在 `days` 范围内的 topic 优先
- 发布时间未知的 topic 其次
- 已知发布时间但超出 `days` 的 topic 最后

排序后才应用 `--max-topics`，再拉取 detail。最终只有 `in_range=true` 的 detail 会进入 DeepSeek 分析。
