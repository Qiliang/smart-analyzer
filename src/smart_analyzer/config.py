"""应用配置：ES、账号 API、采样率、本地路径。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录（src/smart_analyzer/config.py → 上三级）
ROOT_DIR = Path(__file__).resolve().parents[2]
ALLOWED_SAMPLE_RATES = (5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100)


class Settings(BaseSettings):
    """从环境变量 / .env 读取，缺省值对齐生产 ES 与账号接口。"""

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    elasticsearch_host: str = Field(
        default="http://10.188.65.125:32068",
        alias="ELASTICSEARCH_HOST",
    )
    elasticsearch_index: str = Field(
        default="k8s-smart-voice-*",
        alias="ELASTICSEARCH_INDEX",
    )
    elasticsearch_namespace: str = Field(
        default="prod-app",
        alias="ELASTICSEARCH_NAMESPACE",
    )
    elasticsearch_page_size: int = Field(default=100, alias="ES_PAGE_SIZE")
    elasticsearch_retries: int = Field(default=6, alias="ES_RETRIES")

    account_info_url: str = Field(
        default="http://10.188.64.179:1000/home/accountInfo",
        alias="ACCOUNT_INFO_URL",
    )
    account_env: str = Field(default="prod", alias="ACCOUNT_ENV")

    pull_sample_rate: int = Field(default=100, alias="PULL_SAMPLE_RATE")
    session_root: Path = Field(default=ROOT_DIR / "session", alias="SESSION_ROOT")

    basic_auth_user: str = Field(default="hollycrm", alias="BASIC_AUTH_USER")
    basic_auth_password: str = Field(default="hollycrm", alias="BASIC_AUTH_PASSWORD")

    @property
    def logs_root(self) -> Path:
        return Path(self.session_root) / "logs"

    @property
    def reports_root(self) -> Path:
        return Path(self.session_root) / "reports"

    def normalized_sample_rate(self) -> int:
        rate = int(self.pull_sample_rate)
        if rate not in ALLOWED_SAMPLE_RATES:
            # 夹到最近档位，避免配置写错直接崩溃
            return min(ALLOWED_SAMPLE_RATES, key=lambda x: abs(x - rate))
        return rate


@lru_cache
def get_settings() -> Settings:
    return Settings()
