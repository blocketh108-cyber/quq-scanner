import importlib
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
main = importlib.import_module('main')
ADDR = '0x1111111111111111111111111111111111111111'
CASE_ADDR = '0xabcdefabcdefabcdefabcdefabcdefabcdefabcd'


class MainQqqbTests(unittest.TestCase):
    def tearDown(self):
        main.tasks.clear()

    def test_API接受QQQB算法(self):
        req = main.ScanRequest(addresses=[ADDR], day='2026-07-31', algo='qqqb')
        with patch.object(main.threading.Thread, 'start'):
            response = main.start_scan(req)
        self.assertEqual(main.tasks[response['task_id']]['algo'], 'qqqb')

    def test_扫描前按地址忽略大小写去重(self):
        duplicate = CASE_ADDR.upper().replace('0X', '0x')
        req = main.ScanRequest(addresses=[CASE_ADDR, duplicate, CASE_ADDR], day='2026-07-31', algo='qqqb')
        with patch.object(main.threading, 'Thread') as thread_cls:
            response = main.start_scan(req)

        self.assertEqual(response['total'], 1)
        self.assertEqual(main.tasks[response['task_id']]['total'], 1)
        self.assertEqual(thread_cls.call_args.kwargs['args'][1], [CASE_ADDR])

    def test_余额刷新前同样按地址忽略大小写去重(self):
        duplicate = CASE_ADDR.upper().replace('0X', '0x')
        req = main.RefreshRequest(addresses=[CASE_ADDR, duplicate, CASE_ADDR])
        with patch.object(main, 'query_balances', return_value={'fullAddr': CASE_ADDR}) as query:
            response = main.refresh_balances(req)

        self.assertEqual(response['count'], 1)
        query.assert_called_once_with(CASE_ADDR)

    def test_旧QUQ参数兼容映射到QQQB(self):
        req = main.ScanRequest(addresses=[ADDR], day='2026-07-31', algo='quq')
        with patch.object(main.threading.Thread, 'start'):
            response = main.start_scan(req)
        self.assertEqual(main.tasks[response['task_id']]['algo'], 'qqqb')

    def test_健康接口版本已更新(self):
        self.assertEqual(main.health()['version'], '2.4-dual-token')

    def test_前端展示QUQ加QQQB并包含总交易量(self):
        html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('QUQ + QQQB/USDT 扫描器', html)
        self.assertIn("startScan('qqqb')", html)
        self.assertIn('id="statVolume"', html)
        self.assertIn('<th>QUQ</th><th>QQQB</th>', html)
        self.assertIn("Object.prototype.hasOwnProperty.call(r,legacy)?r[legacy]:null", html)
        self.assertNotIn("Object.prototype.hasOwnProperty.call(r,legacy)?r[legacy]:0", html)
        self.assertNotIn('r[legacy]??0', html)
        self.assertNotIn('QUQ Alpha 扫描器', html)
        self.assertNotIn('scanBtnQuq', html)
        self.assertIn('const unique=new Map()', html)
        self.assertIn("const key=addr.toLowerCase()", html)


if __name__ == '__main__':
    unittest.main()
