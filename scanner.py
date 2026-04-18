"""Core scanning logic extracted from quq-monitor.py for public web service."""
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

BSC_RPC = os.environ.get('BSC_RPC', 'https://bsc-dataseed1.binance.org')
DEX_ADDYS = {
    '0xb300000b72deaeb607a12d5f54773d1c19c7028d',
    '0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c',
    '0xee50b711cfc25d4e700825d5632d8c6e2907f83d',
    '0xf351e2befbe9c3744d890304d1af0e4987568d05',
    '0xee57c25eaa31d453be7da41f70805e7b5e6ef83d',
}


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


def _fetch_all_token_txs(addr, ts_start, ts_end, api_keys, contract=None):
    results = []
    keys_cycle = list(api_keys)
    session = req.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0'
    for page in range(1, 80):
        got_page = False
        max_retries = len(api_keys) * 2 + 5
        for retry in range(max_retries):
            api_key = keys_cycle[retry % len(keys_cycle)]
            try:
                contract_param = f'&contractaddress={contract}' if contract else ''
                url = (f'https://api.etherscan.io/v2/api?chainid=56&module=account&action=tokentx'
                       f'{contract_param}&address={addr}&sort=desc&page={page}&offset=200&apikey={api_key}')
                resp = session.get(url, timeout=25)
                data = resp.json()
                rows = data.get('result', []) or []
                if isinstance(rows, str):
                    time.sleep(2 + retry * 0.5)
                    continue
                if not rows:
                    return results
                past_window = False
                for tx in rows:
                    ts = int(tx.get('timeStamp', 0))
                    if ts_start <= ts < ts_end:
                        results.append(tx)
                    if ts < ts_start:
                        past_window = True
                if past_window:
                    return results
                if len(rows) < 200:
                    return results
                got_page = True
                time.sleep(0.15)
                break
            except Exception:
                time.sleep(1.5 + retry * 0.5)
        if not got_page:
            break
    return results


def _fetch_normal_txs(addr, ts_start, ts_end, api_keys):
    """Fetch normal (BNB) transactions for an address in the time window."""
    results = []
    keys_cycle = list(api_keys)
    session = req.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0'
    for page in range(1, 80):
        got_page = False
        max_retries = len(api_keys) * 2 + 5
        for retry in range(max_retries):
            api_key = keys_cycle[retry % len(keys_cycle)]
            try:
                url = (f'https://api.etherscan.io/v2/api?chainid=56&module=account&action=txlist'
                       f'&address={addr}&sort=desc&page={page}&offset=200&apikey={api_key}')
                resp = session.get(url, timeout=25)
                data = resp.json()
                rows = data.get('result', []) or []
                if isinstance(rows, str):
                    time.sleep(2 + retry * 0.5)
                    continue
                if not rows:
                    return results
                past_window = False
                for tx in rows:
                    ts = int(tx.get('timeStamp', 0))
                    if ts_start <= ts < ts_end:
                        results.append(tx)
                    if ts < ts_start:
                        past_window = True
                if past_window:
                    return results
                if len(rows) < 200:
                    return results
                got_page = True
                time.sleep(0.15)
                break
            except Exception:
                time.sleep(1.5 + retry * 0.5)
        if not got_page:
            break
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


def query_address(addr, ts_start, ts_end, api_keys, retries=3):
    a_lower = addr.lower()
    for attempt in range(retries):
        try:
            all_usdt = _fetch_all_token_txs(addr, ts_start, ts_end, api_keys, contract=USDT)
            all_quq = _fetch_all_token_txs(addr, ts_start, ts_end, api_keys, contract=QUQ)
            all_normal = _fetch_normal_txs(addr, ts_start, ts_end, api_keys)

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
