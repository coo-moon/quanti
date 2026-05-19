# Pool Sync Progress Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为股票池同步添加实时进度显示，刷新页面不丢失。

**Architecture:** 新增 sync_jobs 表存储进度，POST 创建 job 后台异步执行，GET 返回当前进度，前端轮询。

**Tech Stack:** SQLite (sync_jobs table), asyncio, FastAPI, Vue 3

---

## Task 1: 数据库添加 sync_jobs 表

**File:** Modify: `quanti/data/database.py`

**Step 1: 添加表创建**

在 `_create_tables()` 方法中，在 `pool_stocks` 表定义后添加：

```python
CREATE TABLE IF NOT EXISTS sync_jobs (
    job_id TEXT PRIMARY KEY,
    pool_name TEXT NOT NULL,
    total INTEGER NOT NULL,
    current INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    errors_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
```

**Step 2: 添加 sync_jobs CRUD 方法**

在 Database 类中添加：

```python
def create_sync_job(self, job_id: str, pool_name: str, total: int) -> None:
    from datetime import datetime
    self.conn.execute(
        "INSERT INTO sync_jobs (job_id, pool_name, total, current, status, errors_json, created_at) VALUES (?, ?, ?, 0, 'running', '{}', ?)",
        (job_id, pool_name, total, datetime.now().isoformat()),
    )
    self.conn.commit()

def update_sync_job(self, job_id: str, current: int, status: str, errors: dict) -> None:
    import json
    self.conn.execute(
        "UPDATE sync_jobs SET current=?, status=?, errors_json=? WHERE job_id=?",
        (current, status, json.dumps(errors), job_id),
    )
    self.conn.commit()

def get_sync_job(self, job_id: str) -> dict | None:
    row = self.conn.execute(
        "SELECT job_id, pool_name, total, current, status, errors_json, created_at FROM sync_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    import json
    return {
        "job_id": row[0], "pool_name": row[1], "total": row[2],
        "current": row[3], "status": row[4],
        "errors": json.loads(row[5]), "created_at": row[6],
    }
```

**Step 3: Commit**
```bash
git add quanti/data/database.py
git commit -m "feat: add sync_jobs table for pool sync progress"
```

---

## Task 2: 后端 API - GET /sync/status 接口

**File:** Modify: `quanti/api/routes.py`

**Step 1: 添加 SyncStatusResponse model**

在已有的 `PoolCreateRequest` 等 model 附近添加：

```python
class SyncStatusResponse(BaseModel):
    job_id: str
    current: int
    total: int
    status: str  # running, done, error
    errors: dict
    message: str
```

**Step 2: 添加 status 路由**

在 `sync_pool_stocks` 路由下方添加：

```python
@router.get("/pools/{name}/sync/status")
async def get_sync_status(name: str, job_id: str, request: Request):
    """Get sync job progress."""
    db = request.app.state.db
    job = db.get_sync_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    if job["pool_name"] != name:
        return {"error": "Job does not belong to this pool"}
    current = job["current"]
    total = job["total"]
    status = job["status"]
    err_count = len(job["errors"])
    if status == "running":
        message = f"已同步 {current}/{total}"
    elif status == "done":
        message = f"同步完成，共 {total} 只"
    else:
        message = f"同步结束，{err_count} 只失败"
    return SyncStatusResponse(
        job_id=job_id, current=current, total=total,
        status=status, errors=job["errors"], message=message
    )
```

**Step 3: Commit**
```bash
git add quanti/api/routes.py
git commit -m "feat: add GET /pools/{name}/sync/status endpoint"
```

---

## Task 3: 后端 API - POST /pools/{name}/sync 改为异步

**File:** Modify: `quanti/api/routes.py`

**Step 1: 重写 sync_pool_stocks 路由**

原有路由改为：

```python
@router.post("/pools/{name}/sync")
async def sync_pool_stocks(name: str, request: Request):
    """Start async sync for pool stocks. Returns job_id immediately."""
    import uuid
    from datetime import date, timedelta
    from quanti.data.akshare_adapter import AkShareAdapter

    db = request.app.state.db
    if not db.pool_exists(name):
        return {"error": f"股票池 '{name}' 不存在"}

    codes = db.get_pool_codes(name)
    if not codes:
        return {"error": "股票池为空"}

    job_id = str(uuid.uuid4())[:8]
    db.create_sync_job(job_id, name, len(codes))

    # Start async task
    asyncio.create_task(_run_pool_sync(job_id, name, codes, db))

    return {"job_id": job_id}
```

**Step 2: 添加 _run_pool_sync 异步函数**

在路由文件顶部导入后添加：

```python
async def _run_pool_sync(job_id: str, pool_name: str, codes: list[str], db: Database) -> None:
    from datetime import date, timedelta
    from quanti.data.akshare_adapter import AkShareAdapter

    end_d = date.today()
    start_d = end_d - timedelta(days=365)
    adapter = AkShareAdapter(db)
    errors = {}

    for i, code in enumerate(codes):
        try:
            count = adapter.sync_daily_quotes(code, start=start_d, end=end_d, repair_gaps=False)
            if count == 0:
                errors[code] = "未获取到数据"
        except Exception as e:
            errors[code] = str(e)
        db.update_sync_job(job_id, i + 1, "running", errors)

    final_status = "error" if errors else "done"
    db.update_sync_job(job_id, len(codes), final_status, errors)
```

**Step 3: 确保 asyncio 导入**

确认文件顶部有 `import asyncio`。

**Step 4: Commit**
```bash
git add quanti/api/routes.py
git commit -m "feat: make pool sync async with job progress tracking"
```

---

## Task 4: 前端 - API 添加 job polling

**File:** Modify: `web/src/api/client.ts`

**Step 1: 添加 sync status 类型和函数**

```typescript
export interface SyncStatus {
  job_id: string;
  current: number;
  total: number;
  status: string;
  errors: Record<string, string>;
  message: string;
}

export const fetchPoolSyncStatus = (poolName: string, jobId: string) =>
  api.get<SyncStatus>(`/pools/${encodeURIComponent(poolName)}/sync/status`, {
    params: { job_id: jobId },
  });
```

**Step 2: Commit**
```bash
git add web/src/api/client.ts
git commit -m "feat: add pool sync status API client"
```

---

## Task 5: 前端 - Pool.vue 进度条 UI

**File:** Modify: `web/src/views/Pool.vue`

**Step 1: 添加 progress ref 和轮询逻辑**

```typescript
const syncJobId = ref<string | null>(null);
const syncProgress = ref({ current: 0, total: 0, status: "", errors: {} as Record<string, string> });
let pollTimer: ReturnType<typeof setInterval> | null = null;

function startPolling(jobId: string) {
  syncJobId.value = jobId;
  pollTimer = setInterval(async () => {
    try {
      const res = await fetchPoolSyncStatus(selectedPool.value, jobId);
      syncProgress.value = res.data;
      if (res.data.status !== "running") {
        stopPolling();
        await selectPool(selectedPool.value);
        await loadPools();
        syncMsg.value = res.data.message;
        syncError.value = res.data.status === "error";
      }
    } catch (e) {
      console.error("Poll error:", e);
    }
  }, 1000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}
```

**Step 2: 修改 syncPool 函数**

```typescript
async function syncPool() {
  if (!selectedPool.value || syncingPool.value) return;
  syncingPool.value = true;
  syncMsg.value = "";
  syncError.value = false;
  try {
    const res = await syncPoolStocks(selectedPool.value);
    if (res.data.job_id) {
      startPolling(res.data.job_id);
    }
  } catch (e) {
    syncMsg.value = "同步失败";
    syncError.value = true;
    syncingPool.value = false;
  }
}
```

**Step 3: 模板添加进度条**

在 detail-actions 区域，sync 按钮下方添加：

```html
<div v-if="syncJobId && syncProgress.status === 'running'" class="progress-bar-wrap">
  <div class="progress-info">
    <span>已同步 {{ syncProgress.current }}/{{ syncProgress.total }}</span>
    <span v-if="Object.keys(syncProgress.errors).length" class="progress-errors">
      {{ Object.keys(syncProgress.errors).length }} 只失败
    </span>
  </div>
  <div class="progress-bar">
    <div class="progress-fill" :style="{ width: (syncProgress.current / syncProgress.total * 100) + '%' }"></div>
  </div>
</div>
```

**Step 4: 添加进度条样式**

```css
.progress-bar-wrap {
  padding: 12px 16px;
  background: rgba(0, 113, 227, 0.06);
  border-radius: var(--radius-md);
}

.progress-info {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
}

.progress-errors {
  color: var(--color-red);
}

.progress-bar {
  height: 6px;
  background: rgba(0, 113, 227, 0.15);
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-accent);
  border-radius: 3px;
  transition: width 0.3s ease;
}
```

**Step 5: Commit**
```bash
git add web/src/views/Pool.vue
git commit -m "feat: add pool sync progress bar UI"
```

---

## Task 6: 验证

**Step 1: TypeScript 检查**

```bash
cd web && npx vue-tsc --noEmit
```

Expected: no errors

**Step 2: 构建**

```bash
npm run build
```

Expected: build success

**Step 3: 重启服务并测试**

```bash
lsof -ti:8000 | xargs kill -9; sleep 2
cd /Users/coo/Library/Mobile\ Documents/com~apple~CloudDocs/cloudSource/quanti
.venv/bin/python -m quanti.cli serve --host 0.0.0.0 --port 8000
```

**Step 4: 功能测试**

1. 打开 http://localhost:8000/pool
2. 创建或选择一个股票池
3. 添加几只股票（如 000001, 000002）
4. 点击「同步数据」
5. 观察进度条是否实时更新
6. 刷新页面，确认进度不丢失

Expected: 刷新后进度条从断点继续

---

## Task 7: Push**

```bash
git push
```
