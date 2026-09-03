# BTC / ETH / SOL Order Flow Monitor v0.2

一个独立、只读的 Bybit USDT 永续订单流采集器。它不会下单，也不会发送 Telegram 消息。

## 当前提供的数据

- BTCUSDT、ETHUSDT、SOLUSDT 实时逐笔成交
- 1m / 5m / 15m 主动买卖额、Delta、买方占比
- 服务启动以来的 CVD（重启后重新起算）
- 5m / 15m Open Interest 变化
- 价格上下 0.1% 和 0.5% 的订单簿失衡
- 5m 多空爆仓额、大额主动成交笔数
- 1H / 4H EMA20、EMA50 与趋势背景
- 可解释的即时订单流摘要（不是交易信号）
- 独立 LONG_SCORE / SHORT_SCORE（研究分数，不触发交易）
- 每分钟保存快照，并统计15/30/60/240分钟后的扣费收益

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开：

- `http://localhost:8000/health`
- `http://localhost:8000/snapshot/BTC`
- `http://localhost:8000/snapshot/ETH`
- `http://localhost:8000/snapshot/SOL`
- `http://localhost:8000/docs`
- `http://localhost:8000/research/status`
- `http://localhost:8000/research/score-buckets/BTC?horizon=60&side=long`

## Railway 部署

1. 把 `orderflow-monitor` 作为一个全新的 GitHub 仓库上传，不要放进 BTC Tide Watch。
2. Railway 新建 Project → Deploy from GitHub repo。
3. Root Directory 留空；程序已包含 `railway.json` 和 `Procfile`。
4. 在 Variables 添加：
   - `API_KEY`：自己生成的一长串随机字符（推荐）。
   - `SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT`
   - 可选 `LARGE_TRADE_USD=250000`
   - `DATA_PATH=/data/orderflow.db`
   - 可选 `ROUND_TRIP_COST_PCT=0.12`
5. 部署完成后访问 `https://你的域名/health`。

如设置了 `API_KEY`，查询快照时必须放在请求头，不能直接拼在网址中：

```bash
curl -H "X-API-Key: 你的密钥" https://你的域名/snapshot/BTC
```

## 如何让我读取

部署后，把 Railway 的公开域名发给我。初版若启用 API key，普通网页搜索无法自定义请求头；后续可以增加一个短时签名读取端点或自定义连接器。不要把真实 API key 发到聊天里。

## 初版限制

- 订单簿来自单一交易所，不代表全市场。
- CVD 使用 Bybit 主动成交，并在服务重启后从零开始。
- 实时滚动窗口在重启后重新开始；研究快照存入SQLite。
- Railway需添加Volume并挂载到 `/data`，否则重新部署可能丢失研究数据库。
- 盘口失衡会受撤单和诱导挂单影响，不能单独作为入场理由。
- 目前“判断”只做事实压缩；真正的关键位、吸收和背离仍应结合图表解释。
- 分数权重是待检验假设；样本不足前不得用分数自动下单。
