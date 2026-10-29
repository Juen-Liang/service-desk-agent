from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    app_dsn: str = 'postgresql+psycopg://deskflow_app:change-me@127.0.0.1:5432/service_desk_agent'
    demo_dsn: str = 'postgresql+psycopg://deskflow_demo:change-me@127.0.0.1:5432/service_desk_demo'
    demo_url: str = 'http://127.0.0.1:8012'
    demo_service_secret: str = ''
    jwt_secret: str = ''
    encryption_key: str = ''
    data_dir: Path = ROOT / 'data'
    worker_concurrency: int = 2
    lease_seconds: int = 90
    cookie_secure: bool = False
    frontend_origin: str = 'http://127.0.0.1:5178'

    @property
    def checkpoint_dsn(self):
        return self.app_dsn.replace('postgresql+psycopg://', 'postgresql://')


@lru_cache
def settings():
    return Settings()
