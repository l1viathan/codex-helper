# Codex session extractor

列出当前用户的本地 Codex session：

```bash
./codex-helper.py list
```

输出`LAST`、`NAME`、`UUID`、`WORKDIR`。完整UUID可用于name重复时无歧义地执行
`codex resume <UUID>`。默认按最近活动优先，`--reverse`改为最早活动优先。名称优先
读取`state_*.sqlite`的`threads.name`，未执行`/rename`时回退到自动生成并截短的
`title`；同时兼容仍将名称保存在`session_index.jsonl`的Codex版本。guardian 等内部
subagent rollout 不会作为独立session列出。输出前会统计各列的最大terminal display width，
使用space补齐列，因此中文或长name不会让`WORKDIR`错位。

自动标题只用于在列表中辨认会话，不能作为 `codex resume` 的选择器；这类会话请使用
同一行的完整 UUID，例如 `codex resume <UUID>`。

例如，持续恢复名为 `ebooknt` 的同一个 session：

```bash
codex resume ebooknt
./codex-helper.py list
./codex-helper.py extract ebooknt
```

使用完整 ID、唯一的 ID 前缀、精确的显式名称或 JSONL 文件路径导出对话：

```bash
./codex-helper.py extract 019fb4a8
./codex-helper.py extract ebooknt
./codex-helper.py extract ebooknt --reverse
```

`list` 和 `extract` 在 stdout 连接 terminal 时默认使用 pager。默认pager是 `less`，
也可通过 `$PAGER` 指定；stdout连接pipe或redirect时不会启动pager：

```bash
PAGER='less -R' ./codex-helper.py extract ebooknt
./codex-helper.py extract ebooknt | grep teardown
```

交互式pager下会按显示顺序分块读取session：正序从文件开头读取，`--reverse`
从文件末尾读取，按对话组倒序显示。连续的用户输入会保持原顺序，并排在该组
Codex回复之前；多条回复也保持原顺序。两种顺序都不会在显示首屏前构造完整消息list；
退出pager后即停止读取。

输出采用 Codex TUI 的简洁对话形式：`›` 表示用户 prompt，`•` 表示 Codex 回复。
最终回复和进度 commentary 都会输出，inline code 的 markdown 反引号会被移除，
相邻正文与 inline code 之间会保留一个空格作为原TUI颜色边界，便于直接使用
`grep` 检索。warning、工具调用、推理、系统或 developer 指令均不会进入 stdout。
若只需要最终回复：

```bash
./codex-helper.py extract 019fb4a8 --final-only
```

新版Codex（0.157起）把对话写在`event_msg`/`item_completed`里，文本位于
`payload.item.content[]`且不再有`phase`字段，因此`--final-only`改为在turn边界取
`task_complete.last_agent_message`；该字段为空（例如turn被中断）时退化为该turn最后
一条非空agent消息。旧版`user_message`/`agent_message`+`phase`格式仍然支持。

使用 `-w` 按 terminal display width 插入硬换行，续行使用两个空格与正文对齐。
英文单词不会被拆开；中文使用 `jieba` 按词边界换行：

```bash
python3 -m pip install -r requirements.txt
./codex-helper.py extract ebooknt -w 128
```

默认数据目录是 `$CODEX_HOME`，未设置时使用 `~/.codex`。也可显式指定：

```bash
./codex-helper.py --codex-home /path/to/.codex list
```
