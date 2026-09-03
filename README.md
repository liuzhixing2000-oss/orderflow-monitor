# BTC / ETH / SOL Order Flow Monitor v0.3

一个独立、只读的 Bybit USDT 永续订单流采集器和 ChatGPT MCP 数据源。它不会下单，也不会发送 Telegram 消息。

## v0.3：ChatGPT 只读 MCP

远程 MCP 地址：

```
https://orderflow-monitor-production.up.railway.app/mcp/
```

提供四个只读工具：

- `get_market_snapshot(symbol)`：读取 BTC、ETH 或 SOL 的最新订单流
- `get_all_market_snapshots()`：读取三个品种的最新状态
- `get_research_status()`：读取样本积累情况
- `get_score_bucket_results(symbol, horizon_minutes, side)`：读取评分分组前向结果

MCP没有下单、改单、撤单或交易所账户工具。当前版本的MCP端点不使用API key；请勿在返回数据中加入任何密钥或账户信息。

## 当前提供的数据

- BTCUSDT、ETHUSDT、SOLUSDT 实时逐笔成交
- 1m / 5m / 15m 主动买卖额、Delta、买方占比
- 服务启动以来的 CVD（重启后重新起算）
- 5m / 15m Open Interest 变化
- 价格上下 0.1% 和 0.5% 的订单簿失衡
- 5m 多空爆仓额、大额主动成交笔数
- 1H / 4H EMA20、EMA50 与趋势背景
- 独立 LONG_SCORE / SHORT_SCORE（研究分数，不触发交易）
- 每分钟保存快照，并统计15/30/60/240分钟后的扣费收益

## HTTP接口

- `/health`
- `/snapshot/BTC`
- `/snapshot/ETH`
- `/snapshot/SOL`
- `/research/status`
- `/research/score-buckets/BTC?horizon=60&side=long`
- `/docs`

普通HTTP接口继续受可选的 `X-API-Key` 保护；MCP接口当前只暴露非敏感的只读市场数据。

## Railway

Start command:

```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

建议变量：

- `SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT`
- `DATA_PATH=/data/orderflow.db`
- 可选 `LARGE_TRADE_USD=250000`
- 可选 `ROUND_TRIP_COST_PCT=0.12`
- 可选 `API_KEY`（仅保护普通HTTP接口）

## 限制

- 分数权重尚未验证，不得把高分直接视为可盈利信号。
- 订单簿来自单一交易所，且挂单可能撤销。
- CVD和实时滚动窗口会随服务重启重新开始。
- 分钟快照高度相关；正式回测需使用阈值穿越、冷却期和样本外数据。
- MCP只解决ChatGPT读取问题，不会自动提高判断准确率。
