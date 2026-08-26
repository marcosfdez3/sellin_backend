from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    APP_NAME: str = "InsiderTrack API"
    DEBUG: bool = False

    # Database — usa SQLite en dev, PostgreSQL en prod
    DATABASE_URL: str = "sqlite+aiosqlite:///./insider_track.db"

    # EDGAR
    EDGAR_BASE_URL: str = "https://www.sec.gov"
    EDGAR_DATA_URL: str = "https://data.sec.gov"
    # La SEC exige un User-Agent identificable: "Nombre Apellido email@ejemplo.com"
    EDGAR_USER_AGENT: str = "InsiderTrack Dev dev@insidertrack.com"
    EDGAR_RATE_LIMIT_DELAY: float = 0.11  # ~9 req/s, bajo el límite de 10

    # Scheduler — cada cuántos minutos se sincronizan nuevos Form 4
    SYNC_INTERVAL_MINUTES: int = 60

    # Señales — umbrales para generar señales relevantes
    MIN_TRANSACTION_VALUE_USD: float = 0  # sin mínimo por ahora
    CLUSTER_DAYS_WINDOW: int = 7
    CLUSTER_MIN_INSIDERS: int = 2

    class Config:
        env_file = ".env"


settings = Settings()
