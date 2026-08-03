"""Core scanning logic extracted from quq-monitor.py for public web service.
Migrated from Etherscan V2 to Ankr Advanced API (2026-04-20)."""
import math, time, random, os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import requests as req

utc8 = timezone(timedelta(hours=8))

USDT = '0x55d398326f99059ff775485246999027b3197955'
USDC = '0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d'
USD1 = '0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d'
QUQ  = '0x4fa7c69a7b69f8bc48233024d546bc299d6b03bf'
QQQB = '0x205812cdbed920aff76c6580abd681a46d11efc7'
TRADED_TOKENS = {'quq': QUQ, 'qqqb': QQQB}
BNB_PLACEHOLDER = 'BNB'

# 有效凭据只允许由部署环境注入，禁止写入公开仓库。
ANKR_URL = os.environ.get('ANKR_URL', '').strip()
BSC_RPC = os.environ.get('BSC_RPC', 'https://bsc-dataseed.bnbchain.org').strip()
DEX_ADDYS = {
    '0xb300000b72deaeb607a12d5f54773d1c19c7028d',
    '0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c',
    '0xee50b711cfc25d4e700825d5632d8c6e2907f83d',
    '0xf351e2befbe9c3744d890304d1af0e4987568d05',
    '0xee57c25eaa31d453be7da41f70805e7b5e6ef83d',
}

# --- swap 供应商分类（按交易内部 verbose log 命中的聚合器/池子归类，不看入口 to）---
# 已链上实测锁定（BSC chain 56）：同一入口 b30000 壳 + 同一 selector 0x810c705b，
# 既可能内部走 LiFi 也可能走 Liquidmesh，唯一可靠依据是逐笔 verbose log 命中哪个聚合器合约。
LIFI_DIAMOND   = '0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae'  # LiFi 官方 LiFiDiamond
LM_ROUTER      = '0x3d90f66b534dd8482b181e24655a9e8265316be9'  # Liquidmesh 官方 Router（全 EVM 统一）
PANCAKE_POOLS  = {
    '0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c',  # PancakeV3 USDT/QUQ 主池
    '0x28e2ea090877bf75740558f6bfb36a5ffee9e9df',  # PancakeInfinity PoolManager（底层成交）
}
_VENDOR_CACHE = {}  # txhash -> 'LiFi' | 'Liquidmesh' | 'Pancake' | None
VENDOR_CLASSIFY = os.environ.get('QUQ_CLASSIFY_VENDORS', '0') == '1'
VENDOR_WORKERS = max(1, int(os.environ.get('QUQ_VENDOR_WORKERS', '2')))


def classify_swap_vendor(txhash):
    """对单笔 swap 交易拉 RPC receipt logs，按内部命中的聚合器合约判定供应商。
    返回 'LiFi' / 'Liquidmesh' / 'Pancake' / None（未命中已知聚合器）。结果按 hash 缓存。
    """
    h = txhash.lower()
    if not VENDOR_CLASSIFY:
        return None
    if h in _VENDOR_CACHE:
        return _VENDOR_CACHE[h]
    addrs = _receipt_log_addresses(h)
    vendor = None
    if LIFI_DIAMOND in addrs:
        vendor = 'LiFi'
    elif LM_ROUTER in addrs:
        vendor = 'Liquidmesh'
    elif addrs & PANCAKE_POOLS:
        # 未经聚合器、直连 Pancake 池成交
        vendor = 'Pancake'
    _VENDOR_CACHE[h] = vendor
    return vendor


def prewarm_vendor_cache(hashes, workers=10):
    """并发预拉一批 swap 交易的 verbose log 填充 _VENDOR_CACHE。
    主循环随后调用 classify_swap_vendor 时全部命中缓存，避免串行慢。
    _VENDOR_CACHE 单 key 赋值在 GIL 下线程安全。
    """
    if not VENDOR_CLASSIFY:
        return
    workers = min(workers, VENDOR_WORKERS)
    todo = [h.lower() for h in hashes if h.lower() not in _VENDOR_CACHE]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(classify_swap_vendor, h) for h in todo]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass


# QUQ v6 算法常量
PRINCIPAL_SRC = '0x4201e0e98fa3b33483fcd009149b390302760d67'  # 团队本金注入源
TEAM_RELAY    = '0xb300000b72deaeb607a12d5f54773d1c19c7028d'  # 团队中转
PRINCIPAL_MIN_USD   = 500
PRINCIPAL_MAX_QUQ   = 5_000_000
RELAY_SKIP_QUQ      = 400_000

_QUQ_PRICE_CACHE = {'price': None, 'ts': 0}

def get_quq_price():
    """从 DexScreener 拉 QUQ/USDT 现价。缓存 60s。失败回退 0.002。"""
    now = time.time()
    if _QUQ_PRICE_CACHE['price'] and now - _QUQ_PRICE_CACHE['ts'] < 60:
        return _QUQ_PRICE_CACHE['price']
    try:
        r = req.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{QUQ}',
            timeout=8,
        )
        if r.status_code == 200:
            pairs = (r.json().get('pairs') or [])
            pairs = [p for p in pairs if p.get('chainId') == 'bsc']
            pairs.sort(key=lambda p: -(p.get('liquidity', {}).get('usd') or 0))
            if pairs:
                price = float(pairs[0]['priceUsd'])
                _QUQ_PRICE_CACHE['price'] = price
                _QUQ_PRICE_CACHE['ts'] = now
                return price
    except Exception:
        pass
    return _QUQ_PRICE_CACHE['price'] or 0.002


def trading_window(day_text=None):
    now = datetime.now(utc8)
    if day_text:
        d = datetime.strptime(day_text, '%Y-%m-%d').date()
        start = datetime.combine(d, datetime.min.time().replace(hour=8)).replace(tzinfo=utc8)
        end = start + timedelta(days=1)
        return d, int(start.timestamp()), int(end.timestamp())
    d = now.date() if now.hour >= 8 else (now.date() - timedelta(days=1))
    start = datetime.combine(d, datetime.min.time().replace(hour=8)).replace(tzinfo=utc8)
    return d, int(start.timestamp()), int(now.timestamp())


def traded_tokens_for_window(ts_start):
    """所有交易日都同时扫描 QUQ 与 QQQB；两种币可能在同一窗口并存。"""
    return dict(TRADED_TOKENS)


# Transfer(address,address,uint256) event topic
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
# 稳定币合约列表
STABLE_CONTRACTS = [USDT, USDC, USD1, QUQ, QQQB]
BSC_RPCS = [
    # Ankr BSC RPC 优先；公共节点仅作临时兜底。
    BSC_RPC,
    'https://bsc-dataseed.bnbchain.org',
    'https://bsc-dataseed1.defibit.io',
    'https://bsc.publicnode.com',
]
_rpc_idx = 0


def _next_rpc():
    global _rpc_idx
    url = BSC_RPCS[_rpc_idx % len(BSC_RPCS)]
    _rpc_idx += 1
    return url


def _rpc_call(method, params, retries=3):
    for attempt in range(retries):
        for rpc_url in BSC_RPCS:
            try:
                resp = req.post(rpc_url, json={
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                    "id": 1,
                }, timeout=20)
                data = resp.json()
                if 'error' in data:
                    continue
                return data.get('result')
            except Exception:
                continue
        time.sleep(0.8 * (attempt + 1))
    return None


def _receipt_log_addresses(txhash):
    receipt = _rpc_call('eth_getTransactionReceipt', [txhash]) or {}
    addrs = set()
    for lg in receipt.get('logs', []) or []:
        a = (lg.get('address') or '').lower()
        if a:
            addrs.add(a)
    return addrs


def _ts_to_block(ts):
    """时间戳转 BSC block number（估算，BSC 当前约 0.45s/block）"""
    # 精确锚点：block 101686331 = ts 1780315365 (2026-06-01 19:29 CST)
    anchor_ts = 1780315365
    anchor_block = 101686331
    diff = ts - anchor_ts
    return max(0, anchor_block + int(diff / 0.45))


def _get_block_by_ts(ts, closest='before'):
    """通过 BSC RPC 获取精确 block number（二分查找太慢，用估算+修正）"""
    return _ts_to_block(ts)


def _rpc_get_logs(contract, from_block, to_block, addr_topic, topic_position='to'):
    """通过 BSC RPC eth_getLogs 拉 Transfer 事件"""
    addr_padded = '0x000000000000000000000000' + addr_topic[2:].lower()
    # topic_position: 'from' = topic[1], 'to' = topic[2]
    if topic_position == 'from':
        topics = [TRANSFER_TOPIC, addr_padded, None]
    else:
        topics = [TRANSFER_TOPIC, None, addr_padded]

    all_logs = []
    # Ankr 要求单次 getLogs 控制范围；每段最多 2000 块。
    chunk = 2000
    # 构建所有 chunk 请求
    chunks = []
    cur = from_block
    while cur <= to_block:
        end = min(cur + chunk, to_block)
        chunks.append((cur, end))
        cur = end + 1

    def fetch_chunk(block_range):
        start, end = block_range
        # 单 chunk 失败时轮换节点并退避重试，规避单点限流/抖动。
        for attempt in range(3):
            for rpc_url in BSC_RPCS:
                try:
                    resp = req.post(rpc_url, json={
                        "jsonrpc": "2.0",
                        "method": "eth_getLogs",
                        "params": [{
                            "fromBlock": hex(start),
                            "toBlock": hex(end),
                            "address": contract,
                            "topics": topics,
                        }],
                        "id": 1
                    }, timeout=20)
                    data = resp.json()
                    if 'error' in data:
                        continue
                    logs = data.get('result', [])
                    if isinstance(logs, list):
                        return logs
                except Exception:
                    continue
            time.sleep(0.8 * (attempt + 1))
        return None

    with ThreadPoolExecutor(max_workers=min(10, len(chunks))) as pool:
        futures = [pool.submit(fetch_chunk, chunk_range) for chunk_range in chunks]
        for future in as_completed(futures):
            chunk_logs = future.result()
            if chunk_logs is None:
                raise RuntimeError('BSC RPC 日志分段连续失败')
            all_logs.extend(chunk_logs)

    return all_logs


def _logs_to_etherscan_format(logs, contract, decimals=18):
    """将 RPC getLogs 结果转为 Etherscan 兼容格式"""
    results = []
    for log in logs:
        topics = log.get('topics', [])
        if len(topics) < 3:
            continue
        from_addr = '0x' + topics[1][-40:]
        to_addr = '0x' + topics[2][-40:]
        raw_data = log.get('data', '0x0')
        try:
            value = int(raw_data, 16)
        except (ValueError, TypeError):
            value = 0
        block_hex = log.get('blockNumber', '0x0')
        try:
            block_num = int(block_hex, 16)
        except (ValueError, TypeError):
            block_num = 0
        # 用 block number 估算时间戳
        ts_est = 1780162099 + (block_num - 101346147) * 3

        results.append({
            'hash': log.get('transactionHash', ''),
            'from': from_addr.lower(),
            'to': to_addr.lower(),
            'value': str(value),
            'contractAddress': contract.lower(),
            'tokenDecimal': str(decimals),
            'timeStamp': str(ts_est),
            'blockNumber': str(block_num),
            'logIndex': str(int(log.get('logIndex', '0x0'), 16) if isinstance(log.get('logIndex'), str) else 0),
        })
    return results


_ankr_available = True  # 标记 Ankr 是否可用，避免反复超时
_ankr_fail_ts = 0  # 上次标记不可用的时间


def _ankr_post(method, params, retries=2):
    """调用 Ankr Advanced API；失败必须抛出，调用方决定是否完整回退。"""
    global _ankr_available, _ankr_fail_ts
    if not ANKR_URL:
        _ankr_available = False
        _ankr_fail_ts = time.time()
        raise RuntimeError('Ankr端点未配置')
    # 每 5 分钟重试一次 Ankr
    if not _ankr_available and time.time() - _ankr_fail_ts > 300:
        _ankr_available = True
    if not _ankr_available:
        raise RuntimeError('Ankr端点暂不可用')
    last_error = None
    for attempt in range(retries):
        try:
            resp = req.post(ANKR_URL, json={
                "jsonrpc": "2.0", "method": method, "params": params, "id": 1
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if 'error' in data:
                last_error = RuntimeError('Ankr返回RPC错误')
                err_msg = str(data.get('error', {}).get('message', ''))
                if 'No nodes available' in err_msg:
                    _ankr_available = False
                    _ankr_fail_ts = time.time()
                    break
                time.sleep(1 + attempt)
                continue
            return data.get('result', {})
        except Exception as exc:
            last_error = exc
            time.sleep(1 + attempt)
    _ankr_available = False
    _ankr_fail_ts = time.time()
    error_type = type(last_error).__name__ if last_error else '未知错误'
    raise RuntimeError(f'Ankr调用失败（{method}，{error_type}）') from last_error


def _ankr_tx_to_etherscan(tx):
    """Convert Ankr token transfer format to Etherscan-compatible dict."""
    # Ankr returns timestamp as ISO string or unix int depending on method
    ts_raw = tx.get('timestamp', 0)
    if isinstance(ts_raw, str):
        try:
            ts_val = int(datetime.fromisoformat(ts_raw.replace('Z', '+00:00')).timestamp())
        except Exception:
            ts_val = int(ts_raw) if ts_raw.isdigit() else 0
    else:
        ts_val = int(ts_raw)

    decimals = int(tx.get('tokenDecimals', 18))
    raw_int = tx.get('valueRawInteger', '0')
    try:
        value_raw = int(raw_int)
    except (ValueError, TypeError):
        value_raw = 0

    return {
        'hash': tx.get('transactionHash', ''),
        'from': tx.get('fromAddress', '').lower(),
        'to': tx.get('toAddress', '').lower(),
        'value': str(value_raw),
        'contractAddress': tx.get('contractAddress', '').lower(),
        'tokenDecimal': str(decimals),
        'timeStamp': str(ts_val),
        'blockNumber': str(tx.get('blockNumber', '') or ''),
        'transactionIndex': str(tx.get('transactionIndex', '') or ''),
        'logIndex': str(tx.get('logIndex', '') or ''),
    }


def _int_position(value, default=(1 << 62)):
    try:
        if isinstance(value, str) and value.startswith('0x'):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return default


def _tx_chain_sort_key(tx):
    """优先按真实链上位置排序；位置缺失时才回退时间戳和 hash。"""
    block = _int_position(tx.get('blockNumber'))
    if block != (1 << 62):
        return (
            0, block,
            _int_position(tx.get('transactionIndex'), 0),
            _int_position(tx.get('logIndex'), 0),
            (tx.get('hash') or '').lower(),
        )
    return (
        1, _int_position(tx.get('timeStamp'), 0),
        (tx.get('hash') or '').lower(), 0, '',
    )



def _fetch_all_token_txs(addr, ts_start, ts_end, api_keys=None, contract=None):
    """抓 token 转账；Ankr 任意页失败时丢弃部分结果并完整回退 getLogs。"""
    try:
        return _fetch_token_txs_ankr(addr, ts_start, ts_end, contract)
    except Exception:
        return _fetch_token_txs_getlogs(addr, ts_start, ts_end, contract)


def _fetch_token_txs_ankr(addr, ts_start, ts_end, contract=None):
    """通过 Ankr 拉 token 转账"""
    results = []
    page_token = None
    for _ in range(200):  # safety limit
        params = {
            "blockchain": ["bsc"],
            "address": [addr],
            "fromTimestamp": ts_start,
            "toTimestamp": ts_end,
            "pageSize": 10000,
            "descOrder": True,
        }
        if contract:
            params["contractAddress"] = contract
        if page_token:
            params["pageToken"] = page_token

        result = _ankr_post("ankr_getTokenTransfers", params)
        transfers = result.get('transfers', []) or []
        if not transfers:
            break

        for tx in transfers:
            # Filter by contract if specified (Ankr may return all tokens)
            if contract and tx.get('contractAddress', '').lower() != contract.lower():
                continue
            eth_tx = _ankr_tx_to_etherscan(tx)
            ts = int(eth_tx['timeStamp'])
            if ts_start <= ts < ts_end:
                results.append(eth_tx)

        page_token = result.get('nextPageToken')
        if not page_token:
            break
        time.sleep(0.1)

    return results


def _fetch_token_txs_getlogs(addr, ts_start, ts_end, contract=None):
    """通过公共 RPC eth_getLogs 拉 token 转账（兜底通道）"""
    from_block = _ts_to_block(ts_start)
    to_block = _ts_to_block(ts_end)

    # 确定要查哪些合约
    contracts_to_check = [contract] if contract else STABLE_CONTRACTS
    decimals_map = {
        USDT: 18, USDC: 18, USD1: 18, QUQ: 18, QQQB: 18,
    }

    results = []
    for c in contracts_to_check:
        dec = decimals_map.get(c, 18)
        # 拉入账（to = addr）
        logs_in = _rpc_get_logs(c, from_block, to_block, addr, 'to')
        results.extend(_logs_to_etherscan_format(logs_in, c, dec))
        # 拉出账（from = addr）
        logs_out = _rpc_get_logs(c, from_block, to_block, addr, 'from')
        results.extend(_logs_to_etherscan_format(logs_out, c, dec))
        time.sleep(0.1)

    # 按时间戳过滤（估算可能有偏差，宽松一点）
    filtered = []
    for tx in results:
        ts = int(tx.get('timeStamp', 0))
        # 允许 ±30 block 的误差（约 90 秒）
        if (ts_start - 100) <= ts < (ts_end + 100):
            filtered.append(tx)

    return filtered


def _fetch_normal_txs(addr, ts_start, ts_end, api_keys=None):
    """抓普通交易；数据源失败返回 None，由上层明确报错而不是显示零 Gas。"""
    try:
        return _fetch_normal_txs_ankr(addr, ts_start, ts_end)
    except Exception:
        return None


def _fetch_normal_txs_ankr(addr, ts_start, ts_end):
    """通过 Ankr 拉 BNB 交易"""
    results = []
    page_token = None
    for _ in range(200):
        params = {
            "blockchain": "bsc",
            "address": [addr],
            "fromTimestamp": ts_start,
            "toTimestamp": ts_end,
            "pageSize": 10000,
            "descOrder": True,
        }
        if page_token:
            params["pageToken"] = page_token

        result = _ankr_post("ankr_getTransactionsByAddress", params)
        txs = result.get('transactions', []) or []
        if not txs:
            break

        for tx in txs:
            ts_raw = tx.get('timestamp', '0')
            if isinstance(ts_raw, str) and ts_raw.startswith('0x'):
                ts_val = int(ts_raw, 16)
            elif isinstance(ts_raw, str):
                try:
                    ts_val = int(ts_raw)
                except ValueError:
                    ts_val = 0
            else:
                ts_val = int(ts_raw)

            gas_used = tx.get('gasUsed', '0')
            gas_price = tx.get('gasPrice', '0')
            value = tx.get('value', '0')
            # Ankr may return hex strings
            if isinstance(gas_used, str) and gas_used.startswith('0x'):
                gas_used = str(int(gas_used, 16))
            if isinstance(gas_price, str) and gas_price.startswith('0x'):
                gas_price = str(int(gas_price, 16))
            if isinstance(value, str) and value.startswith('0x'):
                value = str(int(value, 16))

            eth_tx = {
                'hash': tx.get('hash', ''),
                'from': tx.get('from', '').lower(),
                'to': (tx.get('to') or '').lower(),
                'value': value,
                'gasUsed': gas_used,
                'gasPrice': gas_price,
                'timeStamp': str(ts_val),
            }
            if ts_start <= ts_val < ts_end:
                results.append(eth_tx)

        page_token = result.get('nextPageToken')
        if not page_token:
            break
        time.sleep(0.1)

    return results


def _fetch_normal_txs_empty(addr, ts_start, ts_end):
    """BNB 交易兜底通道 - 返回空（getLogs 拉不到普通转账，BNB gas 对磨损影响极小）"""
    # BNB 普通交易无法通过 getLogs 拉取（不是 event）
    # 但磨损计算主要依赖 token transfer，BNB gas 影响很小
    # 返回空列表，磨损计算中 gas 部分会缺失但不影响主要结果
    return []


def _get_bnb_price():
    """Get BNB/USDT price. Try multiple sources."""
    # Try Binance first
    for url in [
        'https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT',
        'https://api.binance.us/api/v3/ticker/price?symbol=BNBUSDT',
    ]:
        try:
            r = req.get(url, timeout=5)
            if r.status_code == 200:
                return float(r.json()['price'])
        except Exception:
            pass
    # Try CoinGecko
    try:
        r = req.get('https://api.coingecko.com/api/v3/simple/price?ids=binancecoin&vs_currencies=usd', timeout=5)
        if r.status_code == 200:
            return float(r.json()['binancecoin']['usd'])
    except Exception:
        pass
    # Try PancakeSwap on-chain (WBNB/USDT pool)
    try:
        # getReserves() on PancakeSwap V2 WBNB/USDT pair
        pair = '0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae'
        data = '0x0902f1ac'  # getReserves()
        result = _rpc_call('eth_call', [{'to': pair, 'data': data}, 'latest'])
        if result and len(result) >= 130:
            reserve0 = int(result[2:66], 16) / 1e18   # WBNB
            reserve1 = int(result[66:130], 16) / 1e18  # USDT
            if reserve0 > 0:
                return reserve1 / reserve0
    except Exception:
        pass
    return 600.0  # fallback


def query_address(addr, ts_start, ts_end, api_keys=None, retries=3):
    """U 算法：同时汇总 QUQ 与 QQQB 的真实 swap，并保留原返现修正口径。"""
    a_lower = addr.lower()
    active_tokens = traded_tokens_for_window(ts_start)
    token_symbol = 'QUQ+QQQB'
    token_contract = f'{QUQ},{QQQB}'
    last_error = None
    for attempt in range(retries):
        try:
            all_usdt = _fetch_all_token_txs(addr, ts_start, ts_end, contract=USDT)
            all_token_txs = {
                symbol: _fetch_all_token_txs(addr, ts_start, ts_end, contract=contract)
                for symbol, contract in active_tokens.items()
            }
            all_normal = _fetch_normal_txs(addr, ts_start, ts_end)
            if all_normal is None:
                raise RuntimeError('普通交易数据源不可用，无法核对 Gas')

            by_hash = {}

            def ensure_hash(hash_):
                by_hash.setdefault(hash_, {
                    'usdt_in': 0.0,
                    'usdt_out': 0.0,
                    'tokens': {symbol: {'in': 0.0, 'out': 0.0} for symbol in active_tokens},
                })

            for tx in all_usdt:
                h = tx['hash'].lower()
                frm, to = tx['from'].lower(), tx['to'].lower()
                val = float(tx['value']) / 1e18
                ensure_hash(h)
                if to == a_lower:
                    by_hash[h]['usdt_in'] += val
                if frm == a_lower:
                    by_hash[h]['usdt_out'] += val
            for symbol, token_txs in all_token_txs.items():
                for tx in token_txs:
                    h = tx['hash'].lower()
                    frm, to = tx['from'].lower(), tx['to'].lower()
                    val = float(tx['value']) / 1e18
                    ensure_hash(h)
                    if to == a_lower:
                        by_hash[h]['tokens'][symbol]['in'] += val
                    if frm == a_lower:
                        by_hash[h]['tokens'][symbol]['out'] += val

            buy, sell = 0.0, 0.0
            swap_hashes = set()
            for h, values in by_hash.items():
                has_token_in = any(flow['in'] > 0 for flow in values['tokens'].values())
                has_token_out = any(flow['out'] > 0 for flow in values['tokens'].values())
                is_buy = has_token_in and values['usdt_out'] > 0
                is_sell = has_token_out and values['usdt_in'] > 0
                if not (is_buy or is_sell):
                    continue
                swap_hashes.add(h)
                if is_buy:
                    buy += values['usdt_out']
                if is_sell:
                    sell += values['usdt_in']

            # 供应商逐笔计数：仅对真实 swap hash 分类。
            v_lifi = v_lm = v_pancake = v_other = 0
            prewarm_vendor_cache(swap_hashes)
            for h in swap_hashes:
                vendor = classify_swap_vendor(h)
                if vendor == 'LiFi':
                    v_lifi += 1
                elif vendor == 'Liquidmesh':
                    v_lm += 1
                elif vendor == 'Pancake':
                    v_pancake += 1
                else:
                    v_other += 1

            # 返现修正只检查不属于 swap 的 USDT 往返。
            non_swap_by_cp = {}
            for tx in all_usdt:
                h = tx['hash'].lower()
                if h in swap_hashes:
                    continue
                val = float(tx['value']) / 1e18
                if val < 1000:
                    continue
                frm, to = tx['from'].lower(), tx['to'].lower()
                cp = frm if to == a_lower else to
                non_swap_by_cp.setdefault(cp, {'in': 0, 'out': 0})
                if to == a_lower:
                    non_swap_by_cp[cp]['in'] += val
                else:
                    non_swap_by_cp[cp]['out'] += val
            for values in non_swap_by_cp.values():
                if values['in'] > 0 and values['out'] > 0 and abs(values['in'] - values['out']) < 100:
                    sell += values['in'] - values['out']

            bnb_tx_count = 0
            bnb_gas_used = 0.0
            for tx in all_normal:
                if tx.get('from', '').lower() == a_lower:
                    bnb_tx_count += 1
                    gas_used = int(tx.get('gasUsed', 0))
                    gas_price = int(tx.get('gasPrice', 0))
                    bnb_gas_used += (gas_used * gas_price) / 1e18

            return {
                'addr': a_lower,
                'fullAddr': addr,
                'token_symbol': token_symbol,
                'token_contract': token_contract,
                'token_contracts': dict(active_tokens),
                'usdt_in': buy,
                'usdt_out': sell,
                'total_usdt': buy + sell,
                'wear': sell - buy,
                'points': int(math.floor(math.log2(buy / 2)) + 1) if buy >= 2 else 0,
                'bnb_tx_count': bnb_tx_count,
                'bnb_gas_used': bnb_gas_used,
                'v_lifi': v_lifi,
                'v_liquidmesh': v_lm,
                'v_pancake': v_pancake,
                'v_other': v_other,
            }
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    error_type = type(last_error).__name__ if last_error else '未知错误'
    raise RuntimeError(f'地址 {a_lower[:10]}… 双币交易数据获取失败（{error_type}）') from last_error


# --- Balance queries via RPC ---

def query_address_quq_v6(addr, ts_start, ts_end, retries=3):
    """真实磨损算法：双币交易统一汇总，QUQ 专属修正与 QQQB 完全隔离。"""
    a_lower = addr.lower()
    active_tokens = traded_tokens_for_window(ts_start)
    token_symbol = 'QUQ+QQQB'
    token_contract = f'{QUQ},{QQQB}'
    token_price = get_quq_price()
    last_error = None

    for attempt in range(retries):
        try:
            all_usdt = _fetch_all_token_txs(addr, ts_start, ts_end, contract=USDT)
            all_token = []
            for contract in active_tokens.values():
                all_token.extend(_fetch_all_token_txs(addr, ts_start, ts_end, contract=contract))
            all_normal = _fetch_normal_txs(addr, ts_start, ts_end)
            if all_normal is None:
                raise RuntimeError('普通交易数据源不可用，无法核对 Gas')

            by_hash = {}
            for tx in all_usdt + all_token:
                h = tx['hash'].lower()
                by_hash.setdefault(h, []).append(tx)
            ordered_hashes = sorted(
                by_hash,
                key=lambda h: min(_tx_chain_sort_key(tx) for tx in by_hash[h]),
            )

            events = []
            swap_hashes = []
            for h in ordered_hashes:
                ui = uo = 0.0
                flows = {
                    'quq': {'in': 0.0, 'out': 0.0},
                    'qqqb': {'in': 0.0, 'out': 0.0},
                }
                principal_quq_in = 0.0
                relay_quq_out = 0.0
                for tx in by_hash[h]:
                    val = float(tx['value']) / 1e18
                    ca = tx['contractAddress'].lower()
                    fa = tx['from'].lower()
                    ta = tx['to'].lower()
                    if ta == a_lower:
                        if ca == USDT:
                            ui += val
                        elif ca == QUQ:
                            if fa == PRINCIPAL_SRC:
                                principal_quq_in += val
                            else:
                                flows['quq']['in'] += val
                        elif ca == QQQB:
                            flows['qqqb']['in'] += val
                    elif fa == a_lower:
                        if ca == USDT:
                            uo += val
                        elif ca == QUQ:
                            flows['quq']['out'] += val
                            if ta == TEAM_RELAY:
                                relay_quq_out += val
                        elif ca == QQQB:
                            flows['qqqb']['out'] += val

                quq_buy = flows['quq']['in'] > 0 and uo > 0
                quq_sell = flows['quq']['out'] > 0 and ui > 0
                qqqb_buy = flows['qqqb']['in'] > 0 and uo > 0
                qqqb_sell = flows['qqqb']['out'] > 0 and ui > 0
                is_buy = quq_buy or qqqb_buy
                is_sell = quq_sell or qqqb_sell
                is_swap = is_buy or is_sell
                if is_swap:
                    swap_hashes.append(h)
                events.append({
                    'hash': h,
                    'ui': ui,
                    'uo': uo,
                    'quq_buy': quq_buy,
                    'quq_sell': quq_sell,
                    'qqqb_buy': qqqb_buy,
                    'qqqb_sell': qqqb_sell,
                    'is_buy': is_buy,
                    'is_sell': is_sell,
                    'is_swap': is_swap,
                    'principal_quq_in': principal_quq_in,
                    'relay_quq_out': relay_quq_out,
                })

            prewarm_vendor_cache(swap_hashes)
            swap_sell_usdt = swap_buy_usdt = 0.0
            quq_sell_usdt = quq_buy_usdt = 0.0
            relay_qout = 0.0
            principal_usd = 0.0
            principal_n = 0
            first_quq_sell_ui = 0.0
            v_lifi = v_lm = v_pancake = v_other = 0

            for event in events:
                ui, uo = event['ui'], event['uo']
                if event['is_buy']:
                    swap_buy_usdt += uo
                if event['is_sell']:
                    swap_sell_usdt += ui

                # 只有该 hash 的 swap 明确只属于 QUQ 时，才纳入 QUQ 专属修正分量。
                has_quq_swap = event['quq_buy'] or event['quq_sell']
                has_qqqb_swap = event['qqqb_buy'] or event['qqqb_sell']
                if has_quq_swap and not has_qqqb_swap:
                    if event['quq_buy']:
                        quq_buy_usdt += uo
                    if event['quq_sell']:
                        quq_sell_usdt += ui
                        if first_quq_sell_ui == 0.0:
                            first_quq_sell_ui = ui

                principal_quq_in = event['principal_quq_in']
                if principal_quq_in > 0:
                    usd = principal_quq_in * token_price
                    if usd > PRINCIPAL_MIN_USD and principal_quq_in < PRINCIPAL_MAX_QUQ:
                        principal_usd += usd
                        principal_n += 1

                if not event['is_swap']:
                    relay_qout += event['relay_quq_out']

                if event['is_swap']:
                    vendor = classify_swap_vendor(event['hash'])
                    if vendor == 'LiFi':
                        v_lifi += 1
                    elif vendor == 'Liquidmesh':
                        v_lm += 1
                    elif vendor == 'Pancake':
                        v_pancake += 1
                    else:
                        v_other += 1

            wear_v4 = swap_sell_usdt - swap_buy_usdt
            quq_wear_v4 = quq_sell_usdt - quq_buy_usdt
            other_wear_v4 = wear_v4 - quq_wear_v4
            skip_principal = relay_qout >= RELAY_SKIP_QUQ and principal_n > 0
            principal_kept = 0.0 if skip_principal else principal_usd
            # 本金只允许修正明确存在的 QUQ 交易分量；纯 QQQB 交易不能被 QUQ 本金抵扣。
            principal_applied = principal_kept if (quq_buy_usdt > 0 or quq_sell_usdt > 0) else 0.0
            quq_wear_v6 = quq_wear_v4 - principal_applied

            quq_sell_stripped = 0.0
            if quq_wear_v6 > 0 and first_quq_sell_ui > 0:
                quq_sell_stripped = min(quq_wear_v6, first_quq_sell_ui)
                quq_wear_v6 -= quq_sell_stripped
            wear_v6 = other_wear_v4 + quq_wear_v6

            bnb_tx_count = 0
            bnb_gas_used = 0.0
            for tx in all_normal:
                if tx.get('from', '').lower() == a_lower:
                    bnb_tx_count += 1
                    gas_used = int(tx.get('gasUsed', 0))
                    gas_price = int(tx.get('gasPrice', 0))
                    bnb_gas_used += (gas_used * gas_price) / 1e18

            return {
                'addr': a_lower,
                'fullAddr': addr,
                'algo': 'token_v6',
                'token_symbol': token_symbol,
                'token_contract': token_contract,
                'token_contracts': dict(active_tokens),
                'usdt_in': swap_buy_usdt,
                'usdt_out': swap_sell_usdt,
                'total_usdt': swap_buy_usdt + swap_sell_usdt,
                'wear_v4': wear_v4,
                'quq_wear_v4': quq_wear_v4,
                'principal_kept': principal_kept,
                'principal_applied': principal_applied,
                'principal_n': principal_n,
                'principal_skipped': skip_principal,
                'relay_qout': relay_qout,
                'quq_sell_stripped': quq_sell_stripped,
                'wear': wear_v6,
                'points': int(math.floor(math.log2(swap_buy_usdt / 2)) + 1) if swap_buy_usdt >= 2 else 0,
                'bnb_tx_count': bnb_tx_count,
                'bnb_gas_used': bnb_gas_used,
                'quq_price': token_price,
                'token_price': token_price,
                'v_lifi': v_lifi,
                'v_liquidmesh': v_lm,
                'v_pancake': v_pancake,
                'v_other': v_other,
            }
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)

    error_type = type(last_error).__name__ if last_error else '未知错误'
    raise RuntimeError(f'地址 {a_lower[:10]}… 双币真实磨损数据获取失败（{error_type}）') from last_error


# --- Balance queries via RPC ---

def _rpc_call(method, params):
    """调用 BSC JSON-RPC；网络或 RPC 错误必须显式抛出，不能伪装为零余额。"""
    r = req.post(BSC_RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get('error') or body.get('result') is None:
        raise RuntimeError(f'BSC RPC 调用失败：{method}')
    return body['result']


def _erc20_balance(token_contract, wallet_addr):
    """Get ERC20 balance via eth_call (balanceOf)."""
    # balanceOf(address) selector = 0x70a08231
    padded = wallet_addr.lower().replace('0x', '').zfill(64)
    data = '0x70a08231' + padded
    result = _rpc_call('eth_call', [{'to': token_contract, 'data': data}, 'latest'])
    return int(result, 16) / 1e18


def _bnb_balance(wallet_addr):
    """Get native BNB balance."""
    result = _rpc_call('eth_getBalance', [wallet_addr, 'latest'])
    return int(result, 16) / 1e18


def query_balances(addr):
    """查询 USDT、USDC、USD1、QUQ、QQQB 和 BNB 余额；单币失败保留其他币结果。"""
    queries = {
        'usdt': lambda: _erc20_balance(USDT, addr),
        'usdc': lambda: _erc20_balance(USDC, addr),
        'usd1': lambda: _erc20_balance(USD1, addr),
        'quq': lambda: _erc20_balance(QUQ, addr),
        'qqqb': lambda: _erc20_balance(QQQB, addr),
        'bnb': lambda: _bnb_balance(addr),
    }
    result = {'addr': addr.lower(), 'fullAddr': addr}
    errors = []
    for name, fn in queries.items():
        try:
            result[name] = fn()
        except Exception as exc:
            result[name] = None
            errors.append(f'{name}:{type(exc).__name__}')
    result['balance_error'] = '，'.join(errors) if errors else None
    return result


# --- 并发批量扫描 ---
# 每批地址并发。以前写成 15，叠加 5 个任务 + receipt 路由分类时会把 RPC/代理打满。
# 默认 8；如 Railway 资源足够可用 BATCH_CONCURRENCY 环境变量调整。
BATCH_CONCURRENCY = max(1, int(os.environ.get('BATCH_CONCURRENCY', '8')))


def _scan_one(addr, ts_start, ts_end, include_balances=False, algo='u'):
    """扫描单个地址；余额失败只标记余额，不覆盖已经成功的交易结果。"""
    scan_fn = query_address_quq_v6 if algo in ('quq', 'qqqb') else query_address
    r = scan_fn(addr, ts_start, ts_end)
    if include_balances:
        try:
            bal = query_balances(addr)
            for name in ('usdt', 'usdc', 'usd1', 'quq', 'qqqb', 'bnb'):
                r[f'{name}_bal'] = bal.get(name)
            r['balance_error'] = bal.get('balance_error')
        except Exception as exc:
            for name in ('usdt', 'usdc', 'usd1', 'quq', 'qqqb', 'bnb'):
                r[f'{name}_bal'] = None
            r['balance_error'] = f'余额查询失败：{type(exc).__name__}'
    return r


def scan_batch(addresses, ts_start, ts_end, include_balances=False, algo='u',
               progress_cb=None, concurrency=BATCH_CONCURRENCY, cancel_cb=None):
    """并发扫描多个地址，返回按原始顺序排列的结果列表。
    progress_cb(done_count) 每完成一个地址回调一次。
    cancel_cb() 返回 True 时停止等待剩余地址，并尽量取消未开始的 future。
    """
    results: list = [None] * len(addresses)
    done_count = 0
    pool = ThreadPoolExecutor(max_workers=concurrency)
    pending = set()
    future_to_idx = {}
    cancelled = False
    errors = []

    try:
        for i, addr in enumerate(addresses):
            f = pool.submit(_scan_one, addr, ts_start, ts_end, include_balances, algo)
            future_to_idx[f] = i
            pending.add(f)

        while pending:
            if cancel_cb and cancel_cb():
                cancelled = True
                break

            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for f in done:
                idx = future_to_idx[f]
                try:
                    results[idx] = f.result()
                except Exception as exc:
                    errors.append((idx, type(exc).__name__))
                done_count += 1
                if progress_cb:
                    progress_cb(done_count)
    finally:
        if cancelled or (cancel_cb and cancel_cb()):
            for f in pending:
                f.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
        else:
            pool.shutdown(wait=True)

    if cancelled:
        raise RuntimeError('扫描已取消或超时')
    if errors:
        first_idx, error_type = errors[0]
        raise RuntimeError(
            f'{len(errors)} 个地址扫描失败；首个地址 {addresses[first_idx][:10]}…（{error_type}）'
        )
    return results
