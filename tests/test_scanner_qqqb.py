import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('scanner_under_test', ROOT / 'scanner.py')
assert SPEC is not None and SPEC.loader is not None
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)

ADDR = '0x1111111111111111111111111111111111111111'
ROUTER = '0x2222222222222222222222222222222222222222'
POST_START = int(datetime(2026, 7, 31, 8, 0, tzinfo=scanner.utc8).timestamp())
PRE_START = int(datetime(2026, 7, 25, 8, 0, tzinfo=scanner.utc8).timestamp())


def tx(hash_, from_, to, amount, timestamp, contract):
    return {
        'hash': hash_,
        'from': from_,
        'to': to,
        'value': str(int(amount * 1e18)),
        'timeStamp': str(timestamp),
        'contractAddress': contract,
    }


class QqqbSwitchTests(unittest.TestCase):
    def test_交易日按切换日选择唯一币种(self):
        self.assertEqual(scanner.traded_token_for_window(PRE_START), ('quq', scanner.QUQ))
        self.assertEqual(scanner.traded_token_for_window(POST_START), ('qqqb', scanner.QQQB))

    def test_U算法切换后只统计QQQB并排除残留QUQ(self):
        transfers = {
            scanner.USDT: [
                tx('0xbuy', ADDR, ROUTER, 4100, POST_START + 10, scanner.USDT),
                tx('0xsell', ROUTER, ADDR, 4099, POST_START + 20, scanner.USDT),
                tx('0xold', ADDR, ROUTER, 100, POST_START + 30, scanner.USDT),
            ],
            scanner.QQQB: [
                tx('0xbuy', ROUTER, ADDR, 10, POST_START + 10, scanner.QQQB),
                tx('0xsell', ADDR, ROUTER, 10, POST_START + 20, scanner.QQQB),
            ],
            scanner.QUQ: [tx('0xold', ROUTER, ADDR, 5, POST_START + 30, scanner.QUQ)],
        }
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=[]), \
             patch.object(scanner, 'prewarm_vendor_cache'), \
             patch.object(scanner, 'classify_swap_vendor', return_value=None):
            row = scanner.query_address(ADDR, POST_START, POST_START + 300, retries=1)
        self.assertEqual(row['token_symbol'], 'QQQB')
        self.assertAlmostEqual(row['usdt_in'], 4100)
        self.assertAlmostEqual(row['usdt_out'], 4099)
        self.assertAlmostEqual(row['total_usdt'], 8199)

    def test_真实磨损算法切换后使用QQQB且不套旧QUQ本金修正(self):
        transfers = {
            scanner.USDT: [
                tx('0xbuy', ADDR, ROUTER, 4100, POST_START + 10, scanner.USDT),
                tx('0xsell', ROUTER, ADDR, 4099, POST_START + 20, scanner.USDT),
            ],
            scanner.QQQB: [
                tx('0xbuy', ROUTER, ADDR, 10, POST_START + 10, scanner.QQQB),
                tx('0xsell', ADDR, ROUTER, 10, POST_START + 20, scanner.QQQB),
            ],
        }
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=[]), \
             patch.object(scanner, 'prewarm_vendor_cache'), \
             patch.object(scanner, 'classify_swap_vendor', return_value=None):
            row = scanner.query_address_quq_v6(ADDR, POST_START, POST_START + 300, retries=1)
        self.assertEqual(row['token_symbol'], 'QQQB')
        self.assertAlmostEqual(row['usdt_in'], 4100)
        self.assertAlmostEqual(row['usdt_out'], 4099)
        self.assertAlmostEqual(row['wear'], -1)
        self.assertEqual(row['principal_kept'], 0)
        self.assertEqual(row['quq_sell_stripped'], 0)

    def test_余额统一返回QQQB而不是QUQ(self):
        values = {
            scanner.USDT: 1,
            scanner.USDC: 2,
            scanner.USD1: 3,
            scanner.QQQB: 4,
        }
        with patch.object(scanner, '_erc20_balance', side_effect=lambda contract, address: values[contract]), \
             patch.object(scanner, '_bnb_balance', return_value=0.5):
            row = scanner.query_balances(ADDR)
        self.assertEqual(row['qqqb'], 4)
        self.assertNotIn('quq', row)


if __name__ == '__main__':
    unittest.main()
