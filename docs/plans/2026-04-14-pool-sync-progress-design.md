# 股票池同步进度功能设计

## 概述

为股票池同步（K线数据）添加实时进度显示，刷新页面不丢失进度。

## 设计

### 数据库

新增 `sync_jobs` 表（SQLite）：

```sql
CREATE TABLE sync_jobs (
    job_id TEXT PRIMARY KEY,
    pool_name TEXT NOT NULL,
    total INTEGER NOT NULL,
    current INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',  -- running, done, error
    errors_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
```

### API

**POST /pools/{name}/sync**
- 创建 sync_job 记录
- 后台异步执行同步（`asyncio.create_task`）
- 返回：`{job_id: "xxx"}`

**GET /pools/{name}/sync/status?job_id=xxx**
- 返回：`{current: 12, total: 50, status: "running", errors: {"000001": "error"}, message: "已同步 12/50"}`

### 前端

- 点同步 → POST → 获取 job_id → 显示进度条
- 每秒轮询 GET status
- 完成后显示最终结果

### 行为

- 同一股票池同时只允许一个同步任务
- 刷新页面：前端用 job_id 轮询恢复进度
- 同步失败：status=error，errors 记录失败的股票代码
