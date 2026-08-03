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
    def test_任意交易日都同时扫描QUQ和QQQB(self):
        expected = {'quq': scanner.QUQ, 'qqqb': scanner.QQQB}
        self.assertEqual(scanner.traded_tokens_for_window(PRE_START), expected)
        self.assertEqual(scanner.traded_tokens_for_window(POST_START), expected)

    def test_U算法同时汇总QQQB和QUQ(self):
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
        self.assertEqual(row['token_symbol'], 'QUQ+QQQB')
        self.assertAlmostEqual(row['usdt_in'], 4200)
        self.assertAlmostEqual(row['usdt_out'], 4099)
        self.assertAlmostEqual(row['total_usdt'], 8299)

    def test_真实磨损算法同时汇总QQQB和QUQ(self):
        transfers = {
            scanner.USDT: [
                tx('0xbuy', ADDR, ROUTER, 4100, POST_START + 10, scanner.USDT),
                tx('0xsell', ROUTER, ADDR, 4099, POST_START + 20, scanner.USDT),
            ],
            scanner.QQQB: [
                tx('0xbuy', ROUTER, ADDR, 10, POST_START + 10, scanner.QQQB),
                tx('0xsell', ADDR, ROUTER, 10, POST_START + 20, scanner.QQQB),
            ],
            scanner.QUQ: [],
        }
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=[]), \
             patch.object(scanner, 'prewarm_vendor_cache'), \
             patch.object(scanner, 'classify_swap_vendor', return_value=None):
            row = scanner.query_address_quq_v6(ADDR, POST_START, POST_START + 300, retries=1)
        self.assertEqual(row['token_symbol'], 'QUQ+QQQB')
        self.assertAlmostEqual(row['usdt_in'], 4100)
        self.assertAlmostEqual(row['usdt_out'], 4099)
        self.assertAlmostEqual(row['wear'], -1)
        self.assertEqual(row['principal_kept'], 0)
        self.assertEqual(row['quq_sell_stripped'], 0)

    def test_余额同时返回QUQ和QQQB(self):
        values = {
            scanner.USDT: 1,
            scanner.USDC: 2,
            scanner.USD1: 3,
            scanner.QQQB: 4,
            scanner.QUQ: 5,
        }
        with patch.object(scanner, '_erc20_balance', side_effect=lambda contract, address: values[contract]), \
             patch.object(scanner, '_bnb_balance', return_value=0.5):
            row = scanner.query_balances(ADDR)
        self.assertEqual(row['qqqb'], 4)
        self.assertEqual(row['quq'], 5)

    def test_U算法同一hash双币只计一次USDT(self):
        transfers = {
            scanner.USDT: [tx('0xdual', ADDR, ROUTER, 100, POST_START + 10, scanner.USDT)],
            scanner.QUQ: [tx('0xdual', ROUTER, ADDR, 5, POST_START + 10, scanner.QUQ)],
            scanner.QQQB: [tx('0xdual', ROUTER, ADDR, 10, POST_START + 10, scanner.QQQB)],
        }
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=[]), \
             patch.object(scanner, 'prewarm_vendor_cache'), \
             patch.object(scanner, 'classify_swap_vendor', return_value=None):
            row = scanner.query_address(ADDR, POST_START, POST_START + 300, retries=1)
        self.assertEqual(row['usdt_in'], 100)
        self.assertEqual(row['usdt_out'], 0)

    def test_QUQ正磨损剥离不会抵消QQQB亏损(self):
        transfers = {
            scanner.USDT: [
                tx('0xquqsell', ROUTER, ADDR, 100, POST_START + 10, scanner.USDT),
                tx('0xqqqbbuy', ADDR, ROUTER, 40, POST_START + 20, scanner.USDT),
            ],
            scanner.QUQ: [tx('0xquqsell', ADDR, ROUTER, 5, POST_START + 10, scanner.QUQ)],
            scanner.QQQB: [tx('0xqqqbbuy', ROUTER, ADDR, 10, POST_START + 20, scanner.QQQB)],
        }
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=[]), \
             patch.object(scanner, 'prewarm_vendor_cache'), \
             patch.object(scanner, 'classify_swap_vendor', return_value=None), \
             patch.object(scanner, 'get_quq_price', return_value=0.002):
            row = scanner.query_address_quq_v6(ADDR, POST_START, POST_START + 300, retries=1)
        self.assertEqual(row['wear_v4'], 60)
        self.assertEqual(row['quq_sell_stripped'], 100)
        self.assertEqual(row['wear'], -40)

    def test_同hash的QQQB买入不遮蔽QUQ本金注入(self):
        transfers = {
            scanner.USDT: [tx('0xmixed', ADDR, ROUTER, 40, POST_START + 10, scanner.USDT)],
            scanner.QUQ: [tx('0xmixed', scanner.PRINCIPAL_SRC, ADDR, 300000, POST_START + 10, scanner.QUQ)],
            scanner.QQQB: [tx('0xmixed', ROUTER, ADDR, 10, POST_START + 10, scanner.QQQB)],
        }
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=[]), \
             patch.object(scanner, 'prewarm_vendor_cache'), \
             patch.object(scanner, 'classify_swap_vendor', return_value=None), \
             patch.object(scanner, 'get_quq_price', return_value=0.002):
            row = scanner.query_address_quq_v6(ADDR, POST_START, POST_START + 300, retries=1)
        self.assertEqual(row['principal_n'], 1)
        self.assertEqual(row['principal_kept'], 600)
        self.assertEqual(row['principal_applied'], 0)
        self.assertEqual(row['usdt_in'], 40)
        self.assertEqual(row['wear'], -40)

    def test_首笔QUQ卖出按链上位置而不是hash字典序(self):
        first_usdt = tx('0xzz', ROUTER, ADDR, 100, POST_START + 10, scanner.USDT)
        first_quq = tx('0xzz', ADDR, ROUTER, 5, POST_START + 10, scanner.QUQ)
        second_usdt = tx('0xaa', ROUTER, ADDR, 30, POST_START + 10, scanner.USDT)
        second_quq = tx('0xaa', ADDR, ROUTER, 2, POST_START + 10, scanner.QUQ)
        for item in (first_usdt, first_quq):
            item.update(blockNumber='100', transactionIndex='1', logIndex='1')
        for item in (second_usdt, second_quq):
            item.update(blockNumber='100', transactionIndex='2', logIndex='1')
        transfers = {
            scanner.USDT: [second_usdt, first_usdt],
            scanner.QUQ: [second_quq, first_quq],
            scanner.QQQB: [],
        }
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=[]), \
             patch.object(scanner, 'prewarm_vendor_cache'), \
             patch.object(scanner, 'classify_swap_vendor', return_value=None), \
             patch.object(scanner, 'get_quq_price', return_value=0.002):
            row = scanner.query_address_quq_v6(ADDR, POST_START, POST_START + 300, retries=1)
        self.assertEqual(row['quq_sell_stripped'], 100)
        self.assertEqual(row['wear'], 30)

    def test_Ankr后续分页失败不能返回部分数据(self):
        first_page = {
            'transfers': [tx('0xpage1', ROUTER, ADDR, 1, POST_START + 10, scanner.QQQB)],
            'nextPageToken': '下一页',
        }
        # 转成 Ankr 字段，确保第一页确实会产生一条结果。
        first_page['transfers'][0] = {
            'transactionHash': '0xpage1', 'fromAddress': ROUTER, 'toAddress': ADDR,
            'contractAddress': scanner.QQQB, 'timestamp': POST_START + 10,
            'valueRawInteger': str(10**18), 'tokenDecimals': 18,
        }
        with patch.object(scanner, '_ankr_post', side_effect=[first_page, RuntimeError('分页失败')]):
            with self.assertRaises(RuntimeError):
                scanner._fetch_token_txs_ankr(ADDR, POST_START, POST_START + 300, scanner.QQQB)

    def test_普通交易数据源失败不能显示为零Gas(self):
        transfers = {scanner.USDT: [], scanner.QUQ: [], scanner.QQQB: []}
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=lambda *a, contract=None, **k: transfers[contract]), \
             patch.object(scanner, '_fetch_normal_txs', return_value=None):
            with self.assertRaises(RuntimeError):
                scanner.query_address(ADDR, POST_START, POST_START + 300, retries=1)

    def test_交易数据源异常必须向任务层传播(self):
        with patch.object(scanner, '_fetch_all_token_txs', side_effect=RuntimeError('模拟数据源异常')):
            with self.assertRaises(RuntimeError):
                scanner.query_address(ADDR, POST_START, POST_START + 300, retries=1)
            with self.assertRaises(RuntimeError):
                scanner.query_address_quq_v6(ADDR, POST_START, POST_START + 300, retries=1)

    def test_余额失败保留已成功交易并明确标记(self):
        trade = {'addr': ADDR, 'fullAddr': ADDR, 'usdt_in': 100, 'usdt_out': 99, 'wear': -1}
        with patch.object(scanner, 'query_address', return_value=trade.copy()), \
             patch.object(scanner, 'query_balances', side_effect=RuntimeError('模拟余额异常')):
            row = scanner._scan_one(ADDR, POST_START, POST_START + 300, include_balances=True, algo='u')
        self.assertEqual(row['usdt_in'], 100)
        self.assertEqual(row['wear'], -1)
        self.assertIsNone(row['quq_bal'])
        self.assertIsNotNone(row['balance_error'])


if __name__ == '__main__':
    unittest.main()
