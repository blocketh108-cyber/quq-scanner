import importlib
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
main = importlib.import_module('main')
ADDR = '0x1111111111111111111111111111111111111111'


class MainQqqbTests(unittest.TestCase):
    def tearDown(self):
        main.tasks.clear()

    def test_API接受QQQB算法(self):
        req = main.ScanRequest(addresses=[ADDR], day='2026-07-31', algo='qqqb')
        with patch.object(main.threading.Thread, 'start'):
            response = main.start_scan(req)
        self.assertEqual(main.tasks[response['task_id']]['algo'], 'qqqb')

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
        self.assertIn("Object.prototype.hasOwnProperty.call(r,legacy)?r[legacy]:0", html)
        self.assertNotIn('r[legacy]??0', html)
        self.assertNotIn('QUQ Alpha 扫描器', html)
        self.assertNotIn('scanBtnQuq', html)


if __name__ == '__main__':
    unittest.main()
