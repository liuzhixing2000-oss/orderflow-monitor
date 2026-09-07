from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT"
    api_key: str = ""
    bybit_ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    bybit_rest_url: str = "https://api.bybit.com"
    # Bybit linear orderbook supports L200. L50 was often entirely inside 0.1%
    # for BTC/ETH, making the nominal 0.5% depth identical to 0.1%.
    book_depth: int = 200
    large_trade_usd: float = 250_000
    history_seconds: int = 86_400
    data_path: str = "/data/orderflow.db"
    snapshot_interval_seconds: int = 60
    round_trip_cost_pct: float = 0.12

    # Research-only validation controls. A high sample count is not enough for a
    # higher-timeframe model: the observed path must actually span the requested
    # clock time. 95% coverage allows normal scheduling jitter without letting a
    # few minutes of dense ticks masquerade as a 4h history.
    research_min_1h_span_seconds: int = 3_420
    research_min_4h_span_seconds: int = 13_680
    research_min_1h_samples: int = 45
    research_min_4h_samples: int = 180
    research_event_thresholds: str = "50,60,70,80,90"
    research_event_cooldown_minutes: int = 240
    research_price_path_interval_seconds: int = 10
    # Keep enough 10-second path history to evaluate MAE/MFE for weeks of
    # independent events; in-memory trade/order-flow history remains much shorter.
    research_price_path_retention_days: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def symbol_list(self) -> list[str]:
        return [x.strip().upper() for x in self.symbols.split(",") if x.strip()]

    @property
    def research_threshold_list(self) -> list[int]:
        values: list[int] = []
        for raw in self.research_event_thresholds.split(","):
            raw = raw.strip()
            if not raw:
                continue
            value = int(raw)
            if 0 <= value <= 100:
                values.append(value)
        return sorted(set(values)) or [50, 60, 70, 80, 90]


settings = Settings()
