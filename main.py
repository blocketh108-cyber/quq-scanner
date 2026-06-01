"""FastAPI web service for QUQ Alpha public scanning."""
import os, re, uuid, time, threading, json
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scanner import trading_window, query_address, query_address_quq_v6, query_balances, _get_bnb_price, get_quq_price, scan_batch, ANKR_URL, _ankr_available

app = FastAPI(title="QUQ Alpha Scanner")

# --- 备注存储 ---
NOTES_FILE = os.path.join(os.path.dirname(__file__), 'data', 'address-notes.json')
_notes_lock = threading.Lock()

def _load_notes() -> dict:
    """加载地址备注，返回 {address_lower: note_text}"""
    if not os.path.exists(NOTES_FILE):
        return {}
    try:
        with open(NOTES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def _save_notes(notes: dict):
    """保存地址备注"""
    os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
    with open(NOTES_FILE, 'w', encoding='utf-8') as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)

# --- Config ---
# Ankr API - no API keys needed, URL configured in scanner.py
MAX_ADDRESSES = 1000
MAX_CONCURRENT_TASKS = 5

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
        tasks[task_id]['error'] = '服务器繁忙，请稍后再试（最多同时 5 个扫描任务）'
        return
    try:
        tasks[task_id]['status'] = 'running'
        trading_day, ts_start, ts_end = trading_window(day)
        tasks[task_id]['day'] = str(trading_day)
        tasks[task_id]['algo'] = algo

        def on_progress(done):
            tasks[task_id]['progress'] = done
            tasks[task_id]['results'] = [r for r in results_ref[0][:done] if r is not None]

        # 用并发批量扫描（8 地址并发）
        results_ref = [None]  # 用列表包装以便闭包引用

        def progress_cb(done):
            tasks[task_id]['progress'] = done

        results = scan_batch(
            addresses, ts_start, ts_end,
            include_balances=include_balances,
            algo=algo,
            progress_cb=progress_cb,
        )
        tasks[task_id]['results'] = results
        tasks[task_id]['progress'] = len(addresses)
        tasks[task_id]['status'] = 'done'
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
    finally:
        task_semaphore.release()


@app.get("/api/health")
def health():
    return {"keys_loaded": 0, "api": "ankr+rpc_fallback", "max_addresses": MAX_ADDRESSES, "version": "2.1"}


@app.get("/api/diag-ankr")
def diag_ankr():
    """诊断：测试所有数据源连通性"""
    import requests as _req, time as _t
    import scanner as _sc
    url = ANKR_URL
    results = {}
    # 状态
    results["ankr_available"] = _sc._ankr_available
    results["moralis_available"] = _sc._moralis_available
    results["ankr_fail_ts"] = _sc._ankr_fail_ts
    results["moralis_fail_ts"] = _sc._moralis_fail_ts
    # 测试 Ankr getTokenTransfers
    t0 = _t.time()
    try:
        r = _req.post(url, json={"jsonrpc":"2.0","method":"ankr_getTokenTransfers","params":{"blockchain":"bsc","address":["0xCD0D720cAB1B92fDbaf1470C51C3958bd92e151A"],"pageSize":5},"id":1}, timeout=15)
        data = r.json()
        results["ankr_getTokenTransfers"] = {"ms": int((_t.time()-t0)*1000), "status": r.status_code, "ok": "result" in data and bool(data["result"].get("transfers"))}
    except Exception as e:
        results["ankr_getTokenTransfers"] = {"ms": int((_t.time()-t0)*1000), "error": str(e)}
    # 测试 Moralis
    t0 = _t.time()
    try:
        headers = {'accept': 'application/json', 'X-API-Key': _sc.MORALIS_API_KEY}
        r = _req.get(f'{_sc.MORALIS_BASE}/0xCD0D720cAB1B92fDbaf1470C51C3958bd92e151A/erc20/transfers', headers=headers, params={'chain':'bsc','limit':3}, timeout=20)
        results["moralis_transfers"] = {"ms": int((_t.time()-t0)*1000), "status": r.status_code, "count": len(r.json().get("result",[])) if r.status_code==200 else 0}
    except Exception as e:
        results["moralis_transfers"] = {"ms": int((_t.time()-t0)*1000), "error": str(e)}
    # 测试 BSC RPC eth_getLogs
    t0 = _t.time()
    try:
        r = _req.post(_sc.BSC_RPCS[0], json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=10)
        block = int(r.json()["result"], 16)
        results["bsc_rpc"] = {"ms": int((_t.time()-t0)*1000), "latest_block": block}
    except Exception as e:
        results["bsc_rpc"] = {"ms": int((_t.time()-t0)*1000), "error": str(e)}
    return results


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


# --- 备注 API ---
class NoteRequest(BaseModel):
    address: str
    note: str


@app.get("/api/notes")
def get_notes():
    """获取所有地址备注"""
    with _notes_lock:
        return _load_notes()


@app.post("/api/notes")
def save_note(req: NoteRequest):
    """保存单个地址备注"""
    addr = req.address.strip().lower()
    if not re.fullmatch(r'0x[a-fa-f0-9]{40}', addr):
        raise HTTPException(400, "无效地址")
    with _notes_lock:
        notes = _load_notes()
        if req.note.strip():
            notes[addr] = req.note.strip()
        else:
            notes.pop(addr, None)  # 空备注=删除
        _save_notes(notes)
    return {"ok": True}


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
