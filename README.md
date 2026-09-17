# BTC / ETH / SOL Order Flow Monitor v0.5

一个独立、只读的 Bybit USDT 永续订单流采集器和 ChatGPT MCP 数据源。它不会下单，也不会发送 Telegram 消息。

## v0.5：Realized Liquidation Analytics

**新增特性：**

- **Multi-exchange liquidation aggregation**: Bybit allLiquidation (native format) + Binance USD-M free public forceOrder stream (isolated, failure-resilient)
- **Time-windowed realized liquidations**: 1m, 5m, 15m, 1h, 4h aggregate long/short liquidation volumes, per-exchange breakdown
- **Liquidation regime classification** (15m basis): 
  - `long_deleveraging`: longs liquidated + OI down 10%+ + price down
  - `short_squeeze`: shorts liquidated + price up
  - `fresh_position_building`: shorts liquidated + OI up 5%+ + positive delta
  - `uncertain`: insufficient evidence
- **New MCP tool**: `get_liquidation_status(symbol)` returns realized liquidations + regime with confidence score
- **New HTTP endpoint**: `GET /liquidations/{symbol}` returns liquidations + regime
- **Updated snapshot**: includes `liquidations` (1m/5m/15m/1h/4h) and `liquidation_regime` classification
- **Updated score_version**: 0.3.0-liquidation-aggregation

## v0.3：ChatGPT 只读 MCP

远程 MCP 地址：

```
https://orderflow-monitor-production.up.railway.app/mcp/
```

提供六个只读工具（新增 `get_liquidation_status`）：

- `get_market_snapshot(symbol)`：读取 BTC、ETH 或 SOL 的最新订单流
- `get_all_market_snapshots()`：读取三个品种的最新状态
- `get_liquidation_squeeze_context(symbol)`：读取清算密集区、到达可能性、挤压阶段、实现清算与当前体制
- `get_liquidation_status(symbol)`：读取实现清算量与清算体制分类
- `get_research_status()`：读取样本积累情况
- `get_score_bucket_results(symbol, horizon_minutes, side)`：读取评分分组前向结果

MCP 没有下单、改单、撤单或交易所账户工具。当前版本的 MCP 端点不使用 API key；请勿在返回数据中加入任何密钥或账户信息。

## 当前提供的数据

- BTCUSDT、ETHUSDT、SOLUSDT 实时逐笔成交
- 1m / 5m / 15m 主动买卖额、Delta、买方占比
- 服务启动以来的 CVD（重启后重新起算）
- 5m / 15m Open Interest 变化
- 价格上下 0.1% 和 0.5% 的订单簿失衡（含股数失衡）
- **1m / 5m / 15m / 1h / 4h 多空爆仓额（Bybit + Binance）**
- **15m 清算体制分类（long_deleveraging / short_squeeze / fresh_position_building）**
- 1H / 4H EMA20、EMA50 与趋势背景
- 可选 CoinGlass 1日聚合清算地图：上下方关键清算区、距离、规模与杠杆构成
- 结合趋势、Delta、OI、盘口和实际清算评估向上逼空/向下多杀多的推进状态
- 独立 LONG_SCORE / SHORT_SCORE（研究分数，不触发交易）
- 每分钟保存快照，并统计15/30/60/240分钟后的扣费收益

## HTTP 接口

- `/health`
- `/snapshot/BTC`
- `/snapshot/ETH`
- `/snapshot/SOL`
- `/liquidations/BTC` — 新增
- `/liquidations/ETH` — 新增
- `/liquidations/SOL` — 新增
- `/research/status`
- `/research/score-buckets/BTC?horizon=60&side=long`
- `/docs`

普通 HTTP 接口继续受可选的 `X-API-Key` 保护；MCP 接口当前只暴露非敏感的只读市场数据。

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
- 可选 `API_KEY`（仅保护普通 HTTP 接口）
- `COINGLASS_API_KEY`（启用清算地图；官方 aggregated-map 接口需要 Professional 或 Enterprise API）
- 可选 `LIQUIDATION_MAP_RANGE=1d`
- 可选 `LIQUIDATION_MAP_REFRESH_SECONDS=300`

## 限制

- 分数权重尚未验证，不得把高分直接视为可盈利信号。
- 订单簿来自单一交易所，且挂单可能撤销。
- CVD 和实时滚动窗口会随服务重启重新开始。
- 分钟快照高度相关；正式回测需使用阈值穿越、冷却期和样本外数据。
- MCP 只解决 ChatGPT 读取问题，不会自动提高判断准确率。
- `upside_reachability_score` / `downside_reachability_score` 是研究启发式分数，不是经过校准的概率。
- **清算体制分类**基于 15m 价格/OI/交易流；confidence < 100 表示证据不足，不可作为单一决策依据。
- Binance USD-M forceOrder 流是独立的、不可靠的补充；如果连接失败，Bybit 清算流继续正常运行。
- 没有 `COINGLASS_API_KEY` 时订单流继续正常运行，清算地图会明确返回 unavailable。

