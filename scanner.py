"""Core scanning logic extracted from quq-monitor.py for public web service.
Migrated from Etherscan V2 to Ankr Advanced API (2026-04-20)."""
import math, time, random, os
from datetime import datetime, timezone, timedelta
import requests as req

utc8 = timezone(timedelta(hours=8))

USDT = '0x55d398326f99059ff775485246999027b3197955'
USDC = '0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d'
USD1 = '0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d'
QUQ  = '0x4fa7c69a7b69f8bc48233024d546bc299d6b03bf'
BNB_PLACEHOLDER = 'BNB'
ROUTER = '0xb300000b72deaeb607a12d5f54773d1c19c7028d'

ANKR_URL = os.environ.get('ANKR_URL', 'https://rpc.ankr.com/multichain/363197ae9fdac895e127b44fbce5c17e0ea17c7d71ae64ba6f58384db63e5167')
BSC_RPC = os.environ.get('BSC_RPC', 'https://bsc-dataseed1.binance.org')
DEX_ADDYS = {
    '0xb300000b72deaeb607a12d5f54773d1c19c7028d',
    '0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c',
    '0xee50b711cfc25d4e700825d5632d8c6e2907f83d',
    '0xf351e2befbe9c3744d890304d1af0e4987568d05',
    '0xee57c25eaa31d453be7da41f70805e7b5e6ef83d',
}

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


def _ankr_post(method, params, retries=3):
    """Call Ankr Advanced API with retries."""
    for attempt in range(retries):
        try:
            resp = req.post(ANKR_URL, json={
                "jsonrpc": "2.0", "method": method, "params": params, "id": 1
            }, timeout=30)
            data = resp.json()
            if 'error' in data:
                time.sleep(1 + attempt)
                continue
            return data.get('result', {})
        except Exception:
            time.sleep(1 + attempt)
    return {}


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
    }


def _fetch_all_token_txs(addr, ts_start, ts_end, api_keys=None, contract=None):
    """Fetch token transfers via Ankr API with cursor pagination."""
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


def _fetch_normal_txs(addr, ts_start, ts_end, api_keys=None):
    """Fetch normal (BNB) transactions via Ankr API."""
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
    a_lower = addr.lower()
    for attempt in range(retries):
        try:
            all_usdt = _fetch_all_token_txs(addr, ts_start, ts_end, contract=USDT)
            all_quq = _fetch_all_token_txs(addr, ts_start, ts_end, contract=QUQ)
            all_normal = _fetch_normal_txs(addr, ts_start, ts_end)

            by_hash = {}
            for tx in all_usdt:
                h = tx['hash'].lower()
                frm, to = tx['from'].lower(), tx['to'].lower()
                val = float(tx['value']) / 1e18
                by_hash.setdefault(h, {'usdt_in': 0, 'usdt_out': 0, 'quq_in': 0, 'quq_out': 0})
                if to == a_lower:   by_hash[h]['usdt_in'] += val
                if frm == a_lower:  by_hash[h]['usdt_out'] += val
            for tx in all_quq:
                h = tx['hash'].lower()
                frm, to = tx['from'].lower(), tx['to'].lower()
                val = float(tx['value']) / 1e18
                by_hash.setdefault(h, {'usdt_in': 0, 'usdt_out': 0, 'quq_in': 0, 'quq_out': 0})
                if to == a_lower:   by_hash[h]['quq_in'] += val
                if frm == a_lower:  by_hash[h]['quq_out'] += val

            buy, sell = 0.0, 0.0
            quq_hashes = set()
            for h, v in by_hash.items():
                if v['quq_in'] > 0 or v['quq_out'] > 0:
                    quq_hashes.add(h)
                    buy += v['usdt_out']
                    sell += v['usdt_in']

            # Rebate detection
            non_quq_by_cp = {}
            for tx in all_usdt:
                h = tx['hash'].lower()
                if h in quq_hashes:
                    continue
                val = float(tx['value']) / 1e18
                if val < 1000:
                    continue
                frm, to = tx['from'].lower(), tx['to'].lower()
                cp = frm if to == a_lower else to
                non_quq_by_cp.setdefault(cp, {'in': 0, 'out': 0})
                if to == a_lower:
                    non_quq_by_cp[cp]['in'] += val
                else:
                    non_quq_by_cp[cp]['out'] += val
            for cp, v in non_quq_by_cp.items():
                if v['in'] > 0 and v['out'] > 0 and abs(v['in'] - v['out']) < 100:
                    sell += v['in'] - v['out']

            # BNB gas consumption from normal transactions (sent by this address)
            bnb_tx_count = 0
            bnb_gas_used = 0.0
            for tx in all_normal:
                if tx.get('from', '').lower() == a_lower:
                    bnb_tx_count += 1
                    gas_used = int(tx.get('gasUsed', 0))
                    gas_price = int(tx.get('gasPrice', 0))
                    bnb_gas_used += (gas_used * gas_price) / 1e18
            # Also count gas from token transfers (these are in txlist too but let's
            # also count internal/token tx gas from the normal tx list which covers all)

            return {
                'addr': a_lower,
                'fullAddr': addr,
                'usdt_in': buy,
                'usdt_out': sell,
                'wear': sell - buy,
                'points': int(math.floor(math.log2(buy / 2)) + 1) if buy >= 2 else 0,
                'bnb_tx_count': bnb_tx_count,
                'bnb_gas_used': bnb_gas_used,
            }
        except Exception:
            pass
        time.sleep(0.5)
    return {'addr': a_lower, 'fullAddr': addr, 'usdt_in': 0, 'usdt_out': 0, 'wear': 0, 'points': 0, 'bnb_tx_count': 0, 'bnb_gas_used': 0}


# --- Balance queries via RPC ---

def query_address_quq_v6(addr, ts_start, ts_end, retries=3):
    """QUQ v6 算法（按地址按日窗口的精简版）：
    1) wear_v4 = 同 hash 同时含 USDT+QUQ 的 swap 内 (sell_usdt - buy_usdt)
    2) 本金扣除：从 PRINCIPAL_SRC 单笔接收 QUQ，且 qty<5M 且 qty*vwap>$500，
       但若当窗口内有 ≥400k QUQ 转出到 TEAM_RELAY，则跳过本金扣除（钱内部调度）
    3) 首笔 QUQ 卖出收益剥离：若 wear_v6 > 0 且首笔 swap 卖 QUQ 当日 swap_ui > 0，
       剥离 min(wear_v6, first_swap_sell_ui)；不会让地址从亏变盈
    """
    a_lower = addr.lower()
    quq_price = get_quq_price()  # 本窗口 VWAP 近似用现价
    for attempt in range(retries):
        try:
            all_usdt = _fetch_all_token_txs(addr, ts_start, ts_end, contract=USDT)
            all_quq  = _fetch_all_token_txs(addr, ts_start, ts_end, contract=QUQ)
            all_normal = _fetch_normal_txs(addr, ts_start, ts_end)

            # 分组到 hash
            by_hash = {}
            for tx in all_usdt + all_quq:
                h = tx['hash'].lower()
                by_hash.setdefault(h, []).append(tx)

            # 顺序按 timestamp（同 hash 第一笔）排序，便于"首笔 swap 卖出"识别
            ordered_hashes = sorted(by_hash.keys(), key=lambda h: int(by_hash[h][0]['timeStamp']))

            swap_ui = swap_uo = 0.0          # swap 内的 USDT 收/付
            relay_qout = 0.0                 # 转给 TEAM_RELAY 的 QUQ
            principal_usd = 0.0              # 本金注入折U
            principal_n = 0
            first_sell_swap_ui = 0.0         # 首笔卖 QUQ 的 swap 内 ui

            for h in ordered_hashes:
                items = by_hash[h]
                ui = uo = qi = qo = 0.0
                src_quq = None
                relay_q_in_hash = 0.0
                for tx in items:
                    val = float(tx['value']) / 1e18
                    ca = tx['contractAddress'].lower()
                    fa = tx['from'].lower()
                    ta = tx['to'].lower()
                    if ta == a_lower:
                        if ca == USDT:
                            ui += val
                        elif ca == QUQ:
                            qi += val
                            src_quq = fa
                    elif fa == a_lower:
                        if ca == USDT:
                            uo += val
                        elif ca == QUQ:
                            qo += val
                            if ta == TEAM_RELAY:
                                relay_q_in_hash += val

                is_swap = (ui > 0 or uo > 0) and (qi > 0 or qo > 0)
                if is_swap:
                    swap_ui += ui
                    swap_uo += uo
                    if qo > 0 and ui > 0 and first_sell_swap_ui == 0.0:
                        first_sell_swap_ui = ui
                else:
                    relay_qout += relay_q_in_hash
                    # 本金注入识别
                    if qi > 0 and src_quq == PRINCIPAL_SRC:
                        usd = qi * quq_price
                        if usd > PRINCIPAL_MIN_USD and qi < PRINCIPAL_MAX_QUQ:
                            principal_usd += usd
                            principal_n += 1

            wear_v4 = swap_ui - swap_uo
            skip_principal = (relay_qout >= RELAY_SKIP_QUQ and principal_n > 0)
            principal_kept = 0.0 if skip_principal else principal_usd
            wear_v6 = wear_v4 - principal_kept

            # 首笔 QUQ 卖出剥离（仅修正空领-空卖伪盈利）
            quq_sell_stripped = 0.0
            if wear_v6 > 0 and first_sell_swap_ui > 0:
                quq_sell_stripped = min(wear_v6, first_sell_swap_ui)
                wear_v6 -= quq_sell_stripped

            # 积分仍按买入计（与 U 算法保持一致口径）
            buy = swap_ui

            # BNB gas
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
                'algo': 'quq_v6',
                'usdt_in': swap_ui,
                'usdt_out': swap_uo,
                'wear_v4': wear_v4,
                'principal_kept': principal_kept,
                'principal_n': principal_n,
                'principal_skipped': skip_principal,
                'relay_qout': relay_qout,
                'quq_sell_stripped': quq_sell_stripped,
                'wear': wear_v6,
                'points': int(math.floor(math.log2(buy / 2)) + 1) if buy >= 2 else 0,
                'bnb_tx_count': bnb_tx_count,
                'bnb_gas_used': bnb_gas_used,
                'quq_price': quq_price,
            }
        except Exception:
            pass
        time.sleep(0.5)
    return {
        'addr': a_lower, 'fullAddr': addr, 'algo': 'quq_v6',
        'usdt_in': 0, 'usdt_out': 0, 'wear_v4': 0, 'principal_kept': 0,
        'principal_n': 0, 'principal_skipped': False, 'relay_qout': 0,
        'quq_sell_stripped': 0, 'wear': 0, 'points': 0,
        'bnb_tx_count': 0, 'bnb_gas_used': 0, 'quq_price': quq_price,
    }


# --- Balance queries via RPC ---

def _rpc_call(method, params):
    """Call BSC JSON-RPC."""
    r = req.post(BSC_RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=15)
    return r.json().get('result')


def _erc20_balance(token_contract, wallet_addr):
    """Get ERC20 balance via eth_call (balanceOf)."""
    # balanceOf(address) selector = 0x70a08231
    padded = wallet_addr.lower().replace('0x', '').zfill(64)
    data = '0x70a08231' + padded
    result = _rpc_call('eth_call', [{'to': token_contract, 'data': data}, 'latest'])
    if result:
        return int(result, 16) / 1e18
    return 0.0


def _bnb_balance(wallet_addr):
    """Get native BNB balance."""
    result = _rpc_call('eth_getBalance', [wallet_addr, 'latest'])
    if result:
        return int(result, 16) / 1e18
    return 0.0


def query_balances(addr):
    """Query USDT, USDC, USD1 and BNB balances for a single address."""
    return {
        'addr': addr.lower(),
        'fullAddr': addr,
        'usdt': _erc20_balance(USDT, addr),
        'usdc': _erc20_balance(USDC, addr),
        'usd1': _erc20_balance(USD1, addr),
        'bnb': _bnb_balance(addr),
    }
