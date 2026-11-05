from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    database_url: str = 'mysql+pymysql://gridmind:CHANGE_ME@127.0.0.1:3307/gridmind'
    event_backend: str = 'local'
    event_directory: str = 'data/events'
    model_directory: str = 'data/models'
    raw_directory: str = 'data/raw'
    api_key: str = ''
    aws_region: str = 'ap-south-1'
    dynamodb_table: str = 'GridMindSimulationEvents'
    dynamodb_endpoint_url: str | None = None
    live_context_url: str = ''
    live_context_token: str = ''
    live_context_name: str = 'Configured measured-energy feed'
    live_context_region: str = 'Custom'
    live_context_timezone: str = 'UTC'

    def path(self, value: str) -> Path:
        result = Path(value)
        return result if result.is_absolute() else ROOT / result


settings = Settings()
