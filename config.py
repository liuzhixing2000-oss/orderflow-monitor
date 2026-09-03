from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT"
    api_key: str = ""
    bybit_ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    bybit_rest_url: str = "https://api.bybit.com"
    book_depth: int = 50
    large_trade_usd: float = 250_000
    history_seconds: int = 86_400
    data_path: str = "/data/orderflow.db"
    snapshot_interval_seconds: int = 60
    round_trip_cost_pct: float = 0.12
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def symbol_list(self) -> list[str]:
        return [x.strip().upper() for x in self.symbols.split(",") if x.strip()]


settings = Settings()
