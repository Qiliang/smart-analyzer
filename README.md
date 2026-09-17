# smart-voice 分析工具

按日从 ES 抽样拉取会话日志，统计账号分布、火山 STT 定稿延迟、Mpaas Agent 首字延迟、TTS 聚合/首音延迟。

## 运行

```bash
# 安装依赖（可编辑包）
uv sync

# Web UI（内置定时调度）：http://127.0.0.1:8080/
# 默认 Basic Auth：hollycrm / hollycrm（可用 BASIC_AUTH_USER / BASIC_AUTH_PASSWORD 覆盖）
uv run fastapi run --port 8080

# 命令行分析
uv run python -m smart_analyzer --date 2026-09-16
uv run python -m smart_analyzer --date 2026-09-17 --from-cache --sample-rate 10

# 仅调度进程（无 UI；一般不需要，FastAPI 已内嵌调度）
uv run python schedule.py
```

界面：
1. **定时任务**：创建/启停/删除 cron 任务，查看执行记录，可立刻跑昨天
2. **文件浏览**：浏览 `session/` 下 logs、reports、jobs

日志：`session/logs/YYYY-MM-DD/*.txt`（头部写 session_id/uuid，正文去掉重复 `[ID: ...]`；仍兼容旧 `.jsonl`）  
报表：`session/reports/YYYY-MM-DD/report.txt` 与 `summary.json`  
任务：`session/jobs/schedules.json`、`runs.jsonl`
