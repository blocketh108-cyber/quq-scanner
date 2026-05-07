"""FastAPI web service for QUQ Alpha public scanning."""
import os, re, uuid, time, threading
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scanner import trading_window, query_address, query_address_quq_v6, query_balances, _get_bnb_price, get_quq_price

app = FastAPI(title="QUQ Alpha Scanner")

# --- Config ---
# Ankr API - no API keys needed, URL configured in scanner.py
MAX_ADDRESSES = 1000
MAX_CONCURRENT_TASKS = 3

# --- Task store ---
tasks: dict = {}  # task_id -> {status, progress, total, results, error, created}
task_semaphore = threading.Semaphore(MAX_CONCURRENT_TASKS)


class ScanRequest(BaseModel):
    addresses: list[str]
    day: Optional[str] = None  # YYYY-MM-DD, default today
    include_balances: bool = False  # Also fetch balances
    algo: Optional[str] = "u"  # "u" = USDT 返现算法（旧）, "quq" = QUQ v6 算法


class RefreshRequest(BaseModel):
    addresses: list[str]


def _run_scan(task_id: str, addresses: list[str], day: Optional[str], include_balances: bool = False, algo: str = "u"):
    acquired = task_semaphore.acquire(timeout=0)
    if not acquired:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = '服务器繁忙，请稍后再试（最多同时 3 个扫描任务）'
        return
    try:
        tasks[task_id]['status'] = 'running'
        trading_day, ts_start, ts_end = trading_window(day)
        tasks[task_id]['day'] = str(trading_day)
        tasks[task_id]['algo'] = algo
        scan_fn = query_address_quq_v6 if algo == 'quq' else query_address
        results = []
        for i, addr in enumerate(addresses):
            r = scan_fn(addr, ts_start, ts_end)
            if include_balances:
                bal = query_balances(addr)
                r['usdt_bal'] = bal['usdt']
                r['usdc_bal'] = bal['usdc']
                r['usd1_bal'] = bal['usd1']
                r['quq_bal'] = bal['quq']
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
    return {"keys_loaded": 0, "api": "ankr", "max_addresses": MAX_ADDRESSES}


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

    algo = (req.algo or 'u').lower()
    if algo not in ('u', 'quq'):
        raise HTTPException(400, "algo 必须为 'u' 或 'quq'")
    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        'status': 'queued',
        'progress': 0,
        'total': len(addrs),
        'results': [],
        'error': None,
        'created': time.time(),
        'day': req.day,
        'algo': algo,
    }
    t = threading.Thread(target=_run_scan, args=(task_id, addrs, req.day, req.include_balances, algo), daemon=True)
    t.start()
    return {"task_id": task_id, "total": len(addrs), "algo": algo}


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
        "algo": task.get('algo', 'u'),
        "results": task['results'],
        "error": task['error'],
    }


@app.get("/api/quq-price")
def quq_price():
    return {"price": get_quq_price()}


@app.post("/api/refresh")
def refresh_balances(req: RefreshRequest):
    """One-shot balance refresh for up to MAX_ADDRESSES addresses (synchronous, fast)."""
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
