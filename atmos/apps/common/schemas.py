"""共享的 API 请求/响应模型。

仅对 **写入型** 接口定义 Pydantic 模型以做强校验；
读取型接口直接返回服务层构造的字典（已按视图语义整形），
避免为每个视图再维护一层重复的 DTO。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CollectScope = Literal["all", "collect", "derive", "reference"]
OutlierMethod = Literal["zscore", "iqr"]
AggregateMode = Literal["hourly", "daily"]


class CollectRequest(BaseModel):
    """触发采集管道（分析平台：刷新数据源）。"""

    scope: CollectScope = Field("all", description="采集范围：all / collect / derive / reference")
    cities: list[str] | None = Field(None, description="限定城市 slug；为空表示全部启用城市")
    include_archive: bool = Field(True, description="是否包含历史归档回补")
    quality_after: bool = Field(True, description="采集后是否立即执行质量评测")
    wait: bool = Field(False, description="是否同步等待完成（默认立即返回任务 ID）")

    @field_validator("cities")
    @classmethod
    def _clean_cities(cls, value: list[str] | None) -> list[str] | None:
        if not value:
            return None
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned or None


class QualityRequest(BaseModel):
    """触发质量评测（治理平台）。"""

    assets: list[str] | None = Field(None, description="限定资产 key；为空表示全部资产")
    window_hours: int | None = Field(None, ge=1, le=8760, description="评测回溯窗口（小时）")
    wait: bool = Field(False, description="是否同步等待完成")


class CityCreateRequest(BaseModel):
    """新增城市主数据（治理平台）。"""

    slug: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name_zh: str = Field(..., min_length=1, max_length=64)
    name_en: str = Field(..., min_length=1, max_length=96)
    country: str = Field(..., min_length=1, max_length=64)
    country_code: str = Field(..., min_length=2, max_length=8)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    timezone: str = Field(..., min_length=1, max_length=64)
    admin1: str | None = None
    elevation: float | None = None
    population: int | None = Field(None, ge=0)
    climate_zone: str | None = None
    tags: list[str] = Field(default_factory=list)
    is_active: bool = True


class CityUpdateRequest(BaseModel):
    """更新城市（部分字段，治理平台）。"""

    name_zh: str | None = None
    name_en: str | None = None
    timezone: str | None = None
    elevation: float | None = None
    population: int | None = Field(None, ge=0)
    is_active: bool | None = None
    tags: list[str] | None = None


class ApiMessage(BaseModel):
    """统一的操作结果包装。"""

    ok: bool = True
    message: str = ""
    data: dict[str, Any] | None = None


__all__ = [
    "CollectScope",
    "OutlierMethod",
    "AggregateMode",
    "CollectRequest",
    "QualityRequest",
    "CityCreateRequest",
    "CityUpdateRequest",
    "ApiMessage",
]
