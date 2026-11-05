from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
from .simulation import Scenario


class AssetInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=100)
    asset_type: Literal['battery','solar','wind','household','industrial','critical','ev']
    capacity_kw: float = Field(gt=0,le=100000,allow_inf_nan=False)
    capacity_kwh: float = Field(default=0,ge=0,le=1000000,allow_inf_nan=False)
    min_soc: float = Field(default=.15,ge=0,le=1,allow_inf_nan=False)
    max_soc: float = Field(default=.95,ge=0,le=1,allow_inf_nan=False)
    metadata_json: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def check_bounds(self):
        if self.min_soc >= self.max_soc or (self.asset_type == 'battery' and self.capacity_kwh <= 0):
            raise ValueError('Battery capacity must be positive and minimum SoC below maximum SoC')
        return self


class TrainInput(BaseModel):
    target: Literal['demand','solar','wind'] = 'demand'
    source_id: str | None = None
    region_id: str | None = None
    metric: str | None = None
    series_key: str | None = None


class PredictInput(BaseModel):
    model_id: str
    horizon: int = Field(default=14,ge=1,le=90)


class RunInput(BaseModel):
    scenario: Scenario = Field(default_factory=Scenario)
    compare: bool = True
    mode: Literal['historical','live-context'] = 'historical'
    source_id: str | None = None
    region_id: str | None = None
    metric: str | None = None
    series_key: str | None = None
    model_id: str | None = None


class ResponseRecord(BaseModel):
    model_config = ConfigDict(extra='allow')


class SourceResponse(ResponseRecord):
    id: str
    name: str
    provider: str
    frequency: str
    units: str
    status: str
    metadata_json: dict


class ObservationResponse(ResponseRecord):
    id: str
    source_id: str
    region_id: str
    observed_at: str
    metric: str
    value: float
    unit: str
    quality_status: str
    value_kind: Literal['observed']


class AssetResponse(AssetInput):
    id: str


class ModelResponse(ResponseRecord):
    id: str
    name: str
    version: str
    target: str
    algorithm: str
    metrics: dict
    metadata_json: dict


class ForecastResponse(ResponseRecord):
    id: str
    model_id: str
    run_id: str | None = None
    generated_at: str
    target_at: str
    value: float
    lower: float | None
    upper: float | None


class RunResponse(ResponseRecord):
    id: str
    comparison_id: str
    name: str
    policy: Literal['baseline','gridmind']
    status: Literal['queued','running','paused','completed','failed','cancelled','interrupted']
    total_steps: int
    completed_steps: int
    config: dict
    input_manifest: dict
    created_at: str
    error: str | None = None


class RunStartResponse(BaseModel):
    comparison_id: str
    runs: list[RunResponse]


class EventResponse(ResponseRecord):
    PK: str
    SK: str
    run_id: str
    step: int
    event_type: Literal['STATE','AGENT_DECISION','SAFETY_CHECK']
    timestamp: str
    schema_version: int
    payload: dict
