from datetime import datetime
from pydantic import BaseModel


class UsageSummary(BaseModel):
    total_requests: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    active_models: int
    active_api_keys: int
    # JWT-attributed requests (SPA / browser sessions). Counted separately
    # because these rows have api_key_id IS NULL and won't inflate active_api_keys.
    web_ui_requests: int = 0


class ChartDataSeries(BaseModel):
    name: str
    data: list[int]


class ChartDataResponse(BaseModel):
    timestamps: list[int]
    series: list[ChartDataSeries]


class TopModelUsage(BaseModel):
    model_id: int
    model_name: str
    model_type: str
    total_tokens: int
    total_requests: int


class TopUserUsage(BaseModel):
    user_id: int
    username: str
    total_tokens: int
    total_requests: int


class TopDepartmentUsage(BaseModel):
    department_id: int | None = None
    department_name: str
    total_tokens: int
    total_requests: int
