# QUQ Alpha 公开扫描服务

任何人可以上传 BSC 地址列表，扫描当天 QUQ 交易磨损数据。

## 快速部署

```bash
# 设置 BSCScan API Keys（逗号分隔）
export BSCSCAN_API_KEYS="key1,key2,key3"

# Docker 部署
docker compose up -d --build

# 或直接运行
pip install -r requirements.txt
BSCSCAN_API_KEYS="key1,key2" uvicorn main:app --host 0.0.0.0 --port 8000
```

访问 `http://localhost:8000`

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/scan` | POST | 提交扫描任务，body: `{"addresses": [...], "day": "2026-03-20"}` |
| `/api/status/{task_id}` | GET | 查询进度 |
| `/api/results/{task_id}` | GET | 获取结果 |

## 限制

- 单次最多 1000 个地址
- 全局最多 3 个并发扫描任务
- 结果保留 1 小时后自动清理
