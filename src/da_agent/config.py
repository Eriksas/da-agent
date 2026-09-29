"""读取运行配置：先看环境变量，再看项目根目录的 .env 文件。

密钥用 SecretStr 保存：打印或写日志时显示为 '**********'，只有显式调用
get_secret_value() 才能拿到原文。
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_KEYS = {"", "your-key-here"}


class MissingApiKeyError(RuntimeError):
    """需要真实模型但没有配置密钥。"""


class Settings(BaseSettings):
    """项目配置。字段名对应大写的环境变量，例如 llm_api_key ↔ LLM_API_KEY。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_base_url: str = "https://api.minimax.cn/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "MiniMax-M3"
    llm_max_tool_calls: int = Field(default=12, ge=1, le=50)
    llm_temperature: float | None = Field(default=None, ge=0, le=2)  # 不填使用模型默认值
    llm_timeout_seconds: float = Field(default=120, gt=0, le=600)

    def has_api_key(self) -> bool:
        """是否配置了真实密钥（占位值不算）。"""
        if self.llm_api_key is None:
            return False
        return self.llm_api_key.get_secret_value().strip() not in PLACEHOLDER_KEYS

    def require_api_key(self) -> str:
        """取出密钥原文；没有配置时给出可操作的报错。"""
        if not self.has_api_key():
            raise MissingApiKeyError(
                "未配置 LLM_API_KEY。本地：复制 .env.example 为 .env 并填写；"
                "GitHub Actions：在仓库 Secrets 里添加 LLM_API_KEY。"
                "不需要真实模型时请使用 --llm fake。"
            )
        assert self.llm_api_key is not None
        return self.llm_api_key.get_secret_value()
