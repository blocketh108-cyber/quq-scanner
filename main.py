"""FastAPI web service for QUQ Alpha public scanning."""
import os, re, uuid, time, threading
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scanner import trading_window, query_address, query_balances, _get_bnb_price

app = FastAPI(title="QUQ Alpha Scanner")

# --- Config ---
API_KEYS = [k.strip() for k in os.environ.get("BSCSCAN_API_KEYS", "").split(",") if k.strip()]
if not API_KEYS:
    print("WARNING: BSCSCAN_API_KEYS not set. Scanning will fail.")
else:
    print(f"Loaded {len(API_KEYS)} API key(s)")
MAX_ADDRESSES = 50
MAX_CONCURRENT_TASKS = 3

# --- Task store ---
tasks: dict = {}  # task_id -> {status, progress, total, results, error, created}
task_semaphore = threading.Semaphore(MAX_CONCURRENT_TASKS)


class ScanRequest(BaseModel):
    addresses: list[str]
    day: Optional[str] = None  # YYYY-MM-DD, default today
    include_balances: bool = False  # Also fetch balances


class RefreshRequest(BaseModel):
    addresses: list[str]


def _run_scan(task_id: str, addresses: list[str], day: Optional[str], include_balances: bool = False):
    acquired = task_semaphore.acquire(timeout=0)
    if not acquired:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = '服务器繁忙，请稍后再试（最多同时 3 个扫描任务）'
        return
    try:
        tasks[task_id]['status'] = 'running'
        trading_day, ts_start, ts_end = trading_window(day)
        tasks[task_id]['day'] = str(trading_day)
        results = []
        for i, addr in enumerate(addresses):
            r = query_address(addr, ts_start, ts_end, API_KEYS)
            if include_balances:
                bal = query_balances(addr)
                r['usdt_bal'] = bal['usdt']
                r['usdc_bal'] = bal['usdc']
                r['usd1_bal'] = bal['usd1']
                r['bnb_bal'] = bal['bnb']
            results.append(r)
            tasks[task_id]['progress'] = i + 1
            tasks[task_id]['results'] = results
        tasks[task_id]['status'] = 'done'
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
    finally:
        task_semaphore.release()


@app.get("/api/health")
def health():
    return {"keys_loaded": len(API_KEYS), "max_addresses": MAX_ADDRESSES}


@app.get("/api/bnb-price")
def bnb_price():
    return {"price": _get_bnb_price()}


@app.post("/api/scan")
def start_scan(req: ScanRequest):
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

    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        'status': 'queued',
        'progress': 0,
        'total': len(addrs),
        'results': [],
        'error': None,
        'created': time.time(),
        'day': req.day,
    }
    t = threading.Thread(target=_run_scan, args=(task_id, addrs, req.day, req.include_balances), daemon=True)
    t.start()
    return {"task_id": task_id, "total": len(addrs)}


@app.get("/api/status/{task_id}")
def get_status(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {
        "status": task['status'],
        "progress": task['progress'],
        "total": task['total'],
        "error": task['error'],
        "day": task.get('day'),
    }


@app.get("/api/results/{task_id}")
def get_results(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {
        "status": task['status'],
        "progress": task['progress'],
        "total": task['total'],
        "day": task.get('day'),
        "results": task['results'],
        "error": task['error'],
    }


@app.post("/api/refresh")
def refresh_balances(req: RefreshRequest):
    """One-shot balance refresh for up to 50 addresses (synchronous, fast)."""
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
    results = []
    for addr in addrs:
        results.append(query_balances(addr))
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


# Cleanup old tasks periodically (>1h)
def _cleanup():
    while True:
        time.sleep(600)
        cutoff = time.time() - 3600
        expired = [k for k, v in tasks.items() if v['created'] < cutoff]
        for k in expired:
            tasks.pop(k, None)

threading.Thread(target=_cleanup, daemon=True).start()
