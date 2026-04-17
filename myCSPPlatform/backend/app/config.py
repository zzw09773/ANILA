from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "CSP Platform"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql://csp:csp_password@localhost:5432/csp"

    # JWT
    SECRET_KEY: str = "your-secret-key-change-this-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Admin Account
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # Proxy Timeouts (seconds)
    EMBEDDING_TIMEOUT: int = 30
    LLM_TIMEOUT: int = 120

    # Proxy Retry
    PROXY_MAX_RETRIES: int = 3
    PROXY_RETRY_BASE_DELAY: float = 0.5

    # Health Check
    HEALTH_CHECK_INTERVAL: int = 60

    # Usage Writer
    USAGE_BATCH_SIZE: int = 100
    USAGE_FLUSH_INTERVAL: int = 5

    # Site URL (for external access, used by platform links)
    SITE_URL: str = "http://localhost"

    # Static files
    STATIC_DIR: str = str(Path(__file__).parent / "static")

    # Auto-register models on startup (JSON string)
    # Format: '[{"name":"llama3-70b","display_name":"Llama 3 70B","model_type":"llm","endpoint_url":"http://vllm:8000","api_version":"v1"}]'
    AUTO_REGISTER_MODELS: str = ""

    # Auto-register platform links on startup (JSON string)
    # Format: '[{"name":"n8n","url":"http://n8n:5678","icon":"workflow","description":"自動化工作流程"}]'
    AUTO_REGISTER_LINKS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
