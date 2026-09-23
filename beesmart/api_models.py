"""Public response contracts for clients and the generated OpenAPI schema.

Only diagnostics, event payloads, and additional tariff columns are variable.
Use ``response_model_exclude_unset=True`` when returning these models so absent
optional fields remain absent from the existing wire format.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


ChannelCode = Literal["push", "sms", "digital_ads", "call"]
ArpuSegment = Literal["LOW", "MID", "HIGH"]
DataSegment = Literal["NON_USER", "LITE", "HEAVY"]
CallSegment = Literal["LOW", "MEDIUM", "HIGH"]


class ApiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ErrorResponse(ApiResponse):
    detail: str


class HealthResponse(ApiResponse):
    status: Literal["ok"]


class AgentInfo(ApiResponse):
    engine: Literal["local_python", "openai_python"]
    model: str | None
    llm_calls: bool
    evaluation: Literal["organizer_mock"]
    paid_calls: bool
    provider: Literal["local", "openai"] = "local"
    ready: bool = True
    status: Literal["disabled", "ready", "needs_configuration", "storage_error"] = "disabled"
    budget_usd: float = Field(default=5.0, ge=0)
    estimated_spend_usd: float = Field(default=0.0, ge=0)
    reserved_usd: float = Field(default=0.0, ge=0)
    request_attempts: int = Field(default=0, ge=0)
    completed_requests: int = Field(default=0, ge=0)


class LLMRunInfo(ApiResponse):
    provider: Literal["local", "openai"]
    model: str | None
    status: Literal["disabled", "pending", "planning", "cached", "completed", "failed"]
    cache_hit: bool
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    reserved_usd: float = Field(ge=0)
    summary: str = Field(max_length=1000, description="Untrusted model text; render with textContent, never innerHTML. Hypothesis rationale, not a measured result.")
    hypotheses: int = Field(ge=0, le=14)
    error_code: str | None


class DatasetRef(ApiResponse):
    source: Literal["bundled", "uploaded"]
    id: str | None = None
    customers: int | None = Field(default=None, ge=0)
    history_rows: int | None = Field(default=None, ge=0)
    tariffs: int | None = Field(default=None, ge=0)


class Campaign(ApiResponse):
    campaign_name: str
    target_tariff: str
    channel: ChannelCode
    filter_current_tariff: str | None = None
    filter_arpu_segment: ArpuSegment | None = None
    filter_data_segment: DataSegment | None = None
    filter_call_segment: CallSegment | None = None


class Event(ApiResponse):
    event: str
    data: dict[str, JsonValue]


class CampaignDetail(ApiResponse):
    name: str
    channel: ChannelCode
    cost: float
    n_contacts: int = Field(ge=0)
    gross_lift: float
    n_negative: int = Field(ge=0)
    capped_at_campaign_limit: bool
    capped_at_reach_budget: bool
    capped_at_money_budget: bool


class Metrics(ApiResponse):
    team_id: str
    baseline_total_arpu: float
    gross_arpu_lift: float
    total_cost: float
    net_arpu_gain: float
    total_arpu_after: float
    growth_vs_baseline_pct: float
    status: Literal["PASS", "FAIL"]
    n_campaigns: int = Field(ge=0, description="All scored campaigns, including pilots.")
    total_contacts: int = Field(ge=0)
    unique_customers_targeted: int = Field(ge=0)
    coverage_pct: float
    avg_gain_per_customer: float
    roi: float | None = Field(description="Gross lift divided by cost; null when the ratio is not finite.")
    risk_score_pct: float
    budget_used_pct: float
    campaigns_detail: list[CampaignDetail]
    n_pilots: int = Field(ge=0)
    n_final_campaigns: int = Field(ge=0)


class RunRecord(ApiResponse):
    id: str
    status: Literal["queued", "running", "completed", "failed"]
    seed: int = Field(ge=0, le=4_294_967_295)
    created_at: str
    finished_at: str | None
    duration_seconds: float | None
    error: str | None
    dataset: DatasetRef
    events: list[Event]
    campaigns: list[Campaign]
    metrics: Metrics | None
    diagnostics: dict[str, JsonValue]
    llm: LLMRunInfo | None = None


class DatasetQuality(ApiResponse):
    missing_arpu_segment: int | None = Field(default=None, ge=0)
    missing_data_segment: int | None = Field(default=None, ge=0)
    missing_call_segment: int | None = Field(default=None, ge=0)


class DatasetSummary(ApiResponse):
    status: Literal["ready", "missing", "error"]
    customers: int = Field(ge=0)
    tariffs: int = Field(ge=0)
    baseline_arpu: float | None
    cells: int = Field(ge=0)
    missing_files: list[str]
    message: str
    quality: DatasetQuality


class RuntimeInfo(ApiResponse):
    ready: bool
    missing_files: list[str]
    message: str


class SegmentSummary(ApiResponse):
    current_tariff: str
    arpu_segment: ArpuSegment
    customers: int = Field(ge=0)
    arpu_sum: float
    arpu_mean: float


class TariffSummary(ApiResponse):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    tariff_plan_code: str
    price_tariff: float
    Data_in_PKG: float | None = None
    Min_another_operator_in_PKG: float | None = None
    Min_another_operator_and_city_in_PKG: float | None = None
    description: str | None = None


class ProjectInfo(ApiResponse):
    name: str
    owner: str
    case_name: str


class LimitsInfo(ApiResponse):
    total_budget: float = Field(ge=0)
    max_total_contacts: int = Field(ge=0)
    max_campaigns: int = Field(ge=0)
    max_pilots: int = Field(ge=0)
    max_customers_per_campaign: int = Field(ge=0)


class ProviderInfo(ApiResponse):
    id: str
    label: str
    budget_usd: float = Field(ge=0)
    app_spend_usd: float = Field(ge=0)
    account_balance_usd: float | None
    runtime_calls: bool


class DataFileInfo(ApiResponse):
    id: str
    name: str
    bytes: int = Field(ge=0)
    url: str


class OverviewResponse(ApiResponse):
    project: ProjectInfo
    limits: LimitsInfo
    dataset: DatasetSummary
    runtime: RuntimeInfo
    segments: list[SegmentSummary]
    tariffs: list[TariffSummary]
    providers: list[ProviderInfo]
    files: list[DataFileInfo]
    agent: AgentInfo
