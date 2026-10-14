import secrets

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
    SYNC_INTERVAL_MINUTES: int = 10
    # Páginas de 100 filings del feed que se leen como máximo en cada sync
    # (se para antes al llegar a filings ya procesados)
    SYNC_MAX_FEED_PAGES: int = 20
    # Días hacia atrás que se recuperan con el índice diario de EDGAR
    # al arrancar y cada mañana (rellena huecos si el backend estuvo parado)
    BACKFILL_DAYS: int = 7

    # Feed de señales — días que se muestran por defecto en la app
    FEED_DEFAULT_DAYS: int = 30

    # Señales — umbrales para generar señales relevantes
    MIN_TRANSACTION_VALUE_USD: float = 0  # sin mínimo por ahora
    CLUSTER_DAYS_WINDOW: int = 7
    CLUSTER_MIN_INSIDERS: int = 2

    # Auth — define JWT_SECRET en .env / Render. Si falta, se genera uno aleatorio
    # en cada arranque (válido en desarrollo, pero cierra todas las sesiones al reiniciar).
    JWT_SECRET: str = ""
    JWT_EXPIRE_MINUTES: int = 60 * 24 * 30  # 30 días

    # Límite de intentos de login
    LOGIN_MAX_FAILED_PER_EMAIL: int = 5   # fallos seguidos antes de bloquear ese email
    LOGIN_MAX_ATTEMPTS_PER_IP: int = 20   # intentos totales por IP
    LOGIN_WINDOW_MINUTES: int = 15        # ventana y duración del bloqueo

    # Login con Google — ID de cliente OAuth de tipo "Aplicación web"
    # (Google Cloud Console). Vacío = login con Google desactivado.
    GOOGLE_CLIENT_ID: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
if not settings.JWT_SECRET:
    settings.JWT_SECRET = secrets.token_urlsafe(32)
