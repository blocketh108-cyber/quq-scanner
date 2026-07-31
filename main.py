"""FastAPI web service for QQQB/USDT public scanning."""
import os, re, uuid, time, threading, json
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scanner import trading_window, query_address, query_address_quq_v6, query_balances, _get_bnb_price, get_quq_price, scan_batch, ANKR_URL, _ankr_available
import scanner as _scanner_mod

app = FastAPI(title="QQQB/USDT Scanner")


@app.on_event("startup")
def _probe_ankr_on_startup():
    """启动时探测 Ankr 可用性，避免第一个扫描请求浪费 15 秒超时"""
    import requests, threading
    def _probe():
        try:
            r = requests.post(ANKR_URL, json={
                "jsonrpc": "2.0", "method": "ankr_getTokenTransfers",
                "params": {"blockchain": "bsc", "address": ["0xCD0D720cAB1B92fDbaf1470C51C3958bd92e151A"], "pageSize": 1},
                "id": 1
            }, timeout=10)
            data = r.json()
            if "result" not in data or not data["result"].get("transfers"):
                _scanner_mod._ankr_available = False
                _scanner_mod._ankr_fail_ts = time.time()
        except Exception:
            _scanner_mod._ankr_available = False
            _scanner_mod._ankr_fail_ts = time.time()
    threading.Thread(target=_probe, daemon=True).start()

# --- Config ---
# Ankr API - no API keys needed, URL configured in scanner.py
MAX_ADDRESSES = int(os.environ.get("MAX_ADDRESSES", "1000"))
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
TASK_STALE_SECONDS = int(os.environ.get("TASK_STALE_SECONDS", "300"))
TASK_MAX_SECONDS = int(os.environ.get("TASK_MAX_SECONDS", "1800"))
TASK_RETENTION_SECONDS = int(os.environ.get("TASK_RETENTION_SECONDS", "3600"))

# --- Task store ---
tasks: dict = {}  # task_id -> {status, progress, total, results, error, created}
tasks_lock = threading.RLock()
task_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_TASKS)


def _release_task_slot(task_id: str) -> bool:
    """只释放一次并发槽，避免取消/超时/线程退出重复 release。"""
    with tasks_lock:
        task = tasks.get(task_id)
        if not task or not task.get('slot_acquired') or task.get('slot_released'):
            return False
        task['slot_released'] = True
        task['slot_released_at'] = time.time()
    try:
        task_semaphore.release()
    except ValueError:
        return False
    return True


def _task_counts() -> dict:
    with tasks_lock:
        running = sum(1 for t in tasks.values() if t.get('status') == 'running')
        queued = sum(1 for t in tasks.values() if t.get('status') == 'queued')
        active_slots = sum(1 for t in tasks.values() if t.get('slot_acquired') and not t.get('slot_released'))
        done = sum(1 for t in tasks.values() if t.get('status') == 'done')
        error_count = sum(1 for t in tasks.values() if t.get('status') == 'error')
        cancelled_count = sum(1 for t in tasks.values() if t.get('status') == 'cancelled')
        timeout_count = sum(1 for t in tasks.values() if t.get('status') == 'timeout')
    return {
        'running': running,
        'queued': queued,
        'active_slots': active_slots,
        'available_slots': max(0, MAX_CONCURRENT_TASKS - active_slots),
        'done_count': done,
        'error_count': error_count,
        'cancelled_count': cancelled_count,
        'timeout_count': timeout_count,
    }


def _public_task(task_id: str, task: dict, now: Optional[float] = None) -> dict:
    now = now or time.time()
    created = task.get('created') or now
    started = task.get('started') or created
    updated = task.get('updated') or created
    last_progress = task.get('last_progress_ts') or started
    return {
        'task_id': task_id,
        'status': task.get('status'),
        'progress': task.get('progress', 0),
        'total': task.get('total', 0),
        'error': task.get('error'),
        'day': task.get('day'),
        'algo': task.get('algo', 'u'),
        'created': created,
        'started': task.get('started'),
        'updated': updated,
        'finished': task.get('finished'),
        'age_seconds': int(now - created),
        'running_seconds': int(now - started) if task.get('status') == 'running' else 0,
        'idle_seconds': int(now - last_progress) if task.get('status') == 'running' else 0,
    }


def _mark_stale_tasks():
    """把长时间无进度/超长运行任务标成 timeout，并释放并发槽。"""
    now = time.time()
    stale_ids = []
    with tasks_lock:
        for task_id, task in tasks.items():
            if task.get('status') != 'running' or task.get('slot_released'):
                continue
            started = task.get('started') or task.get('created') or now
            last_progress = task.get('last_progress_ts') or started
            idle = now - last_progress
            age = now - started
            if idle >= TASK_STALE_SECONDS or age >= TASK_MAX_SECONDS:
                task['status'] = 'timeout'
                task['error'] = f'扫描任务超时释放（无进度 {int(idle)} 秒 / 总运行 {int(age)} 秒）'
                task['cancel_requested'] = True
                task['updated'] = now
                task['finished'] = now
                stale_ids.append(task_id)
    for task_id in stale_ids:
        _release_task_slot(task_id)
    return stale_ids


class ScanRequest(BaseModel):
    addresses: list[str]
    day: Optional[str] = None  # YYYY-MM-DD, default today
    include_balances: bool = False  # Also fetch balances
    algo: Optional[str] = "u"  # "u" = 返现算法；"qqqb" = 真实磨损算法（旧 quq 值兼容）


class RefreshRequest(BaseModel):
    addresses: list[str]


def _run_scan(task_id: str, addresses: list[str], day: Optional[str], include_balances: bool = False, algo: str = "u"):
    _mark_stale_tasks()
    acquired = task_semaphore.acquire(timeout=0)
    if not acquired:
        counts = _task_counts()
        with tasks_lock:
            task = tasks.get(task_id)
            if task:
                task['status'] = 'error'
                task['error'] = f"服务器繁忙，请稍后再试（当前运行 {counts['active_slots']}/{MAX_CONCURRENT_TASKS}，最多同时 {MAX_CONCURRENT_TASKS} 个扫描任务）"
                task['updated'] = time.time()
                task['finished'] = task['updated']
        return

    try:
        now = time.time()
        with tasks_lock:
            task = tasks.get(task_id)
            if not task:
                task_semaphore.release()
                return
            task['slot_acquired'] = True
            task['slot_released'] = False
            task['started'] = now
            task['updated'] = now
            task['last_progress_ts'] = now
            if task.get('cancel_requested') or task.get('status') == 'cancelled':
                task['status'] = 'cancelled'
                task['error'] = '任务已取消'
                task['finished'] = now
                return
            task['status'] = 'running'

        trading_day, ts_start, ts_end = trading_window(day)
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]['day'] = str(trading_day)
                tasks[task_id]['algo'] = algo

        def is_cancelled():
            with tasks_lock:
                task = tasks.get(task_id)
                if not task:
                    return True
                return task.get('cancel_requested') or task.get('status') in ('cancelled', 'timeout')

        def progress_cb(done):
            if is_cancelled():
                raise RuntimeError('扫描已取消或超时')
            now2 = time.time()
            with tasks_lock:
                task = tasks.get(task_id)
                if task and task.get('status') == 'running':
                    task['progress'] = done
                    task['updated'] = now2
                    task['last_progress_ts'] = now2

        results = scan_batch(
            addresses, ts_start, ts_end,
            include_balances=include_balances,
            algo=algo,
            progress_cb=progress_cb,
            cancel_cb=is_cancelled,
        )
        now3 = time.time()
        with tasks_lock:
            task = tasks.get(task_id)
            if task and task.get('status') == 'running':
                task['results'] = results
                task['progress'] = len(addresses)
                task['status'] = 'done'
                task['updated'] = now3
                task['finished'] = now3
    except Exception as e:
        now4 = time.time()
        with tasks_lock:
            task = tasks.get(task_id)
            if task and task.get('status') not in ('cancelled', 'timeout'):
                task['status'] = 'error'
                task['error'] = str(e)
                task['updated'] = now4
                task['finished'] = now4
    finally:
        _release_task_slot(task_id)


@app.get("/api/health")
def health():
    _mark_stale_tasks()
    return {
        "keys_loaded": 0,
        "api": "ankr+rpc_fallback",
        "max_addresses": MAX_ADDRESSES,
        "version": "2.3-qqqb-switch",
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
        "task_stale_seconds": TASK_STALE_SECONDS,
        "task_max_seconds": TASK_MAX_SECONDS,
        **_task_counts(),
    }


@app.get("/api/diag-ankr")
def diag_ankr():
    """诊断：测试所有数据源连通性"""
    import requests as _req, time as _t
    import scanner as _sc
    url = ANKR_URL
    results = {}
    # 状态
    results["ankr_available"] = _sc._ankr_available
    results["ankr_fail_ts"] = _sc._ankr_fail_ts
    # 测试 Ankr getTokenTransfers
    t0 = _t.time()
    try:
        r = _req.post(url, json={"jsonrpc":"2.0","method":"ankr_getTokenTransfers","params":{"blockchain":"bsc","address":["0xCD0D720cAB1B92fDbaf1470C51C3958bd92e151A"],"pageSize":5},"id":1}, timeout=15)
        data = r.json()
        results["ankr_getTokenTransfers"] = {"ms": int((_t.time()-t0)*1000), "status": r.status_code, "ok": "result" in data and bool(data["result"].get("transfers"))}
    except Exception as e:
        results["ankr_getTokenTransfers"] = {"ms": int((_t.time()-t0)*1000), "error": str(e)}
    # 测试 BSC RPC eth_blockNumber：逐个 RPC 尝试，避免单个节点返回 HTML/空响应导致诊断接口误报 500
    t0 = _t.time()
    rpc_checks = []
    for rpc_url in _sc.BSC_RPCS:
        try:
            r = _req.post(rpc_url, json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=10)
            try:
                data = r.json()
            except Exception as je:
                rpc_checks.append({"url": rpc_url, "status": r.status_code, "error": f"非JSON响应: {str(je)}", "body": r.text[:120]})
                continue
            if data.get("result"):
                block = int(data["result"], 16)
                results["bsc_rpc"] = {"ms": int((_t.time()-t0)*1000), "latest_block": block, "url": rpc_url, "checks": rpc_checks}
                break
            rpc_checks.append({"url": rpc_url, "status": r.status_code, "error": data.get("error") or "无result"})
        except Exception as e:
            rpc_checks.append({"url": rpc_url, "error": str(e)})
    else:
        results["bsc_rpc"] = {"ms": int((_t.time()-t0)*1000), "error": "所有BSC RPC均不可用", "checks": rpc_checks}
    return results


@app.get("/api/bnb-price")
def bnb_price():
    return {"price": _get_bnb_price()}


@app.post("/api/scan")
def start_scan(req: ScanRequest):
    _mark_stale_tasks()
    # Validate addresses
    addrs = []
    for a in req.addresses:
        a = a.strip()
        if not a:
            continue
        if not re.fullmatch(r'0x[a-fA-F0-9]{40}', a):
            raise HTTPException(400, f"无效地址: {a}")
        addrs.append(a)
    if not addrs:
        raise HTTPException(400, "请提供至少一个地址")
    if len(addrs) > MAX_ADDRESSES:
        raise HTTPException(400, f"最多支持 {MAX_ADDRESSES} 个地址")

    algo = (req.algo or 'u').lower()
    if algo == 'quq':
        algo = 'qqqb'
    if algo not in ('u', 'qqqb'):
        raise HTTPException(400, "algo 必须为 'u' 或 'qqqb'")
    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        'status': 'queued',
        'progress': 0,
        'total': len(addrs),
        'results': [],
        'error': None,
        'created': time.time(),
        'updated': time.time(),
        'started': None,
        'finished': None,
        'last_progress_ts': None,
        'slot_acquired': False,
        'slot_released': True,
        'cancel_requested': False,
        'day': req.day,
        'algo': algo,
    }
    t = threading.Thread(target=_run_scan, args=(task_id, addrs, req.day, req.include_balances, algo), daemon=True)
    t.start()
    return {"task_id": task_id, "total": len(addrs), "algo": algo, "max_concurrent_tasks": MAX_CONCURRENT_TASKS, **_task_counts()}


@app.get("/api/status/{task_id}")
def get_status(task_id: str):
    _mark_stale_tasks()
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    counts = _task_counts()
    public = _public_task(task_id, task)
    return {
        **public,
        **counts,
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
        "task_stale_seconds": TASK_STALE_SECONDS,
        "task_max_seconds": TASK_MAX_SECONDS,
    }


@app.get("/api/results/{task_id}")
def get_results(task_id: str):
    _mark_stale_tasks()
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {
        "status": task['status'],
        "progress": task['progress'],
        "total": task['total'],
        "day": task.get('day'),
        "algo": task.get('algo', 'u'),
        "results": task['results'],
        "error": task['error'],
    }


@app.get("/api/tasks")
def list_tasks():
    """当前任务/并发槽状态：给前端展示，也方便线上诊断。"""
    _mark_stale_tasks()
    now = time.time()
    with tasks_lock:
        recent = sorted(tasks.items(), key=lambda kv: kv[1].get('created', 0), reverse=True)[:20]
        task_list = [_public_task(task_id, task, now) for task_id, task in recent]
    return {
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
        "task_stale_seconds": TASK_STALE_SECONDS,
        "task_max_seconds": TASK_MAX_SECONDS,
        **_task_counts(),
        "tasks": task_list,
    }


@app.post("/api/cancel/{task_id}")
def cancel_task(task_id: str):
    """取消当前任务：释放并发槽；scan_batch 会通过 cancel_cb 尽快停止等待未完成地址。"""
    _mark_stale_tasks()
    now = time.time()
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task.get('status') in ('done', 'error', 'cancelled', 'timeout'):
            return {"status": task.get('status'), "released_slot": False, **_task_counts()}
        task['cancel_requested'] = True
        task['status'] = 'cancelled'
        task['error'] = '任务已取消'
        task['updated'] = now
        task['finished'] = now
    released = _release_task_slot(task_id)
    return {"status": "cancelled", "released_slot": released, **_task_counts()}


@app.get("/api/quq-price")
def quq_price():
    return {"price": get_quq_price()}


@app.post("/api/refresh")
def refresh_balances(req: RefreshRequest):
    """One-shot balance refresh for up to MAX_ADDRESSES addresses (concurrent, fast)."""
    addrs = []
    for a in req.addresses:
        a = a.strip()
        if not a:
            continue
        if not re.fullmatch(r'0x[a-fA-F0-9]{40}', a):
            raise HTTPException(400, f"无效地址: {a}")
        addrs.append(a)
    if not addrs:
        raise HTTPException(400, "请提供至少一个地址")
    if len(addrs) > MAX_ADDRESSES:
        raise HTTPException(400, f"最多支持 {MAX_ADDRESSES} 个地址")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(query_balances, addrs))
    return {"results": results, "count": len(results)}


@app.get("/api/balance/{address}")
def get_balance(address: str):
    """Single address balance query."""
    address = address.strip()
    if not re.fullmatch(r'0x[a-fA-F0-9]{40}', address):
        raise HTTPException(400, "无效地址")
    return query_balances(address)


# Serve static frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")


# Cleanup old finished tasks periodically，同时负责扫描任务 watchdog。
def _cleanup():
    while True:
        time.sleep(10)
        _mark_stale_tasks()
        cutoff = time.time() - TASK_RETENTION_SECONDS
        with tasks_lock:
            expired = [
                k for k, v in tasks.items()
                if v.get('status') in ('done', 'error', 'cancelled', 'timeout')
                and (v.get('finished') or v.get('created') or 0) < cutoff
            ]
            for k in expired:
                tasks.pop(k, None)

threading.Thread(target=_cleanup, daemon=True).start()
