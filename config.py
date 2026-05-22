from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    AGENT_NAME: str = "analytics"
    MANAGER_URL: str = "http://127.0.0.1:8100"
    API_PORT: int = 8102
    WP_URL: str = "https://pethubonline.com"
    WP_USER: str = "jasonsarah2026"
    WP_APP_PASSWORD: str = "EIul 3KqI 3fY7 yLbk Ltva aPnj"
    GA4_TAG: str = "GT-KV6JDR72"
    HEARTBEAT_INTERVAL: int = 120
    COLLECTION_HOUR: int = 4
    STALE_DAYS: int = 30
    DB_PATH: str = "/var/lib/freelancer/projects/40416335/analytics-agent/data/analytics_data.json"

    class Config:
        env_file = ".env"


settings = Settings()
