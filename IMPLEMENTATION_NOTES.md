# Liquidation Aggregation & Regime Detection Implementation

## Overview
Comprehensive multi-exchange liquidation aggregation with real-time regime detection for BTC/ETH/SOL order-flow analysis.

## Key Features

### 1. Multi-Exchange Liquidation Aggregation
- **Bybit**: Preserves `allLiquidation` WebSocket stream (SELL closes longs, BUY closes shorts)
- **Binance USD-M**: Free public `!forceOrder@arr` stream with independent reconnect/failure isolation
  - No additional API keys required
  - Isolated failure handling: Binance disconnects do not affect Bybit feeds
  - Automatic reconnection with 5s backoff

### 2. Liquidation Events with Exchange Tagging
```python
add_liquidation(exchange: str, ts: float, side: str, usd: float) -> None
```
- Each liquidation event stores the source exchange
- Supports per-exchange metrics and breakdowns
- Enables cross-exchange comparison and analysis

### 3. Time-Window Liquidation Aggregation
Snapshots expose liquidations for:
- **1m**: Latest minute
- **5m**: 5-minute rolling window
- **15m**: 15-minute rolling window

Each window aggregates across all supported exchanges.

### 4. Liquidation Regime Detection
`identify_liquidation_regime()` function distinguishes three regimes using:
- **15-minute price change** (±0.5% threshold)
- **15-minute OI change** (±10% threshold)
- **Directional liquidation activity** (long vs short)

#### Regimes
1. **long_liquidation_deleveraging**
   - Downside price move (-0.5%) + OI decline (-10%) + short liquidations
   - Interpretation: Shorts covering, longs being trapped/deleveraged
   - Confidence score: 0.30–0.95 based on magnitude

2. **short_squeeze**
   - Upside price move (+0.5%) + OI decline (-10%) + short liquidations
   - Interpretation: Shorts forced to close while market rises
   - Confidence score: 0.30–0.95 based on magnitude

3. **fresh_position_building**
   - Strong directional move (±1.0%) + OI increase (+10%) + any liquidations
   - Interpretation: New traders entering, weak longs/shorts washing out
   - Confidence score: 0.25–0.90 based on magnitude and OI growth

Regimes only return non-null values when conditions are clearly supported (confidence > 0.25).

### 5. Snapshot Enhancements
Snapshot structure now includes:

```json
{
  "liquidations_1m": {"long_usd": 0.0, "short_usd": 0.0},
  "liquidations_5m": {"long_usd": 0.0, "short_usd": 0.0},
  "liquidations_15m": {"long_usd": 0.0, "short_usd": 0.0},
  "liquidations_by_exchange": {
    "exchange_breakdown_5m": {
      "bybit": {"long_usd": 0.0, "short_usd": 0.0},
      "binance": {"long_usd": 0.0, "short_usd": 0.0}
    },
    "bybit": {
      "long_liquidations_5m_usd": 0.0,
      "short_liquidations_5m_usd": 0.0
    },
    "binance": {
      "long_liquidations_5m_usd": 0.0,
      "short_liquidations_5m_usd": 0.0
    }
  },
  "liquidation_regime": {
    "regime": "short_squeeze" | "long_liquidation_deleveraging" | "fresh_position_building" | null,
    "confidence": 0.0–1.0,
    "evidence": ["evidence strings"],
    "source_data": {
      "price_change_15m_pct": float,
      "oi_change_15m_pct": float,
      "liquidations_15m": {...},
      "aggregate_liquidations_1m": {...}
    }
  }
}
```

### 6. MCP Tool Updates
`get_liquidation_squeeze_context()` now exposes:
- `liquidations_1m`, `liquidations_5m`, `liquidations_15m`
- `liquidations_by_exchange` (per-exchange breakdown)
- `liquidation_regime` (regime detection + confidence)
- `binance_status` (feed health indicator)

### 7. Health Check Enhancement
`/health` endpoint now reports:
- Liquidation feeds (Bybit required, Binance optional/free)
- CoinGlass map status (optional)
- Feed status for each symbol

## Configuration
No new environment variables required:
- **Bybit**: Uses existing `BYBIT_WS_URL` and `BYBIT_REST_URL`
- **Binance**: Free public stream, no API key needed
- **CoinGlass**: Optional (existing `COINGLASS_API_KEY`)

## Version Bump
- **Main API**: `0.4.0` → `0.5.0`
- **Score Version**: `0.2.0-research` → `0.3.0-liquidation-regime`

## Backward Compatibility
- All existing endpoints remain functional
- New fields added to snapshot without breaking existing consumers
- Legacy liquidation fields available under original key names

## Read-Only Guarantee
- ✅ No order execution capability
- ✅ No account balance access
- ✅ No position modification
- ✅ Public market data only

## Notes
- CoinGlass API key is optional (liquidation map gracefully degrades)
- Binance connection failures isolated (do not affect Bybit)
- Regime detection only fires on clear, supported conditions
- All confidence scores are heuristic-based; not statistical calibrations

