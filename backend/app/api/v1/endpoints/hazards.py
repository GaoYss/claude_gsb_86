"""隐患登记与整改跟踪接口。"""

import csv
import io
from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response

from app.api.deps import DbSession, PageParams
from app.core.config import settings
from app.core.errors import InvalidOperationError
from app.models.enums import (
    HazardSeverity,
    HazardSource,
    HazardStatus,
    StructurePart,
)
from app.schemas.common import Message, Page, is_overdue
from app.schemas.hazard import (
    HazardCreate,
    HazardDetail,
    HazardRead,
    HazardRectificationCreate,
    HazardTransitionRequest,
    HazardUpdate,
)
from app.services import hazard_service
from app.services.hazard_service import HazardFilter

router = APIRouter(prefix="/hazards", tags=["隐患与整改"])


class HazardFilterParams:
    """隐患列表与导出共用的筛选参数，两处口径必须保持一致。"""

    def __init__(
        self,
        reservoir_id: int | None = Query(default=None, description="按水库过滤"),
        inspection_id: int | None = Query(default=None, description="按来源巡查记录过滤"),
        category: StructurePart | None = Query(default=None, description="隐患类别（部位）"),
        severity: HazardSeverity | None = Query(default=None, description="隐患等级"),
        hazard_status: HazardStatus | None = Query(default=None, alias="status", description="整改状态"),
        source: HazardSource | None = Query(default=None, description="隐患来源"),
        keyword: str | None = Query(default=None, description="按标题 / 编号 / 描述 / 责任人搜索"),
        open_only: bool = Query(default=False, description="只看未销号隐患"),
        overdue_only: bool = Query(default=False, description="只看逾期未整改隐患"),
        discovered_from: date | None = Query(default=None, description="发现日期范围-起（含）"),
        discovered_to: date | None = Query(default=None, description="发现日期范围-止（含）"),
    ) -> None:
        self.value = HazardFilter(
            reservoir_id=reservoir_id,
            inspection_id=inspection_id,
            category=category.value if category else None,
            severity=severity.value if severity else None,
            status=hazard_status.value if hazard_status else None,
            source=source.value if source else None,
            keyword=keyword.strip() or None if keyword else None,
            open_only=open_only,
            overdue_only=overdue_only,
            discovered_from=discovered_from,
            discovered_to=discovered_to,
        )


HazardFilterDep = Annotated[HazardFilterParams, Depends()]


@router.get("", response_model=Page[HazardRead], summary="分页查询隐患台账")
def list_hazards(
    db: DbSession,
    pagination: PageParams,
    filters: HazardFilterDep,
) -> Page[HazardRead]:
    items, total = hazard_service.list_hazards(
        db,
        filters.value,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return Page.build(items=items, total=total, page=pagination.page, page_size=pagination.page_size)


_EXPORT_HEADERS = [
    "隐患编号",
    "隐患标题",
    "所属水库",
    "隐患类别",
    "隐患等级",
    "整改状态",
    "隐患来源",
    "发现日期",
    "发现人",
    "整改期限",
    "整改责任人",
    "是否逾期",
    "销号日期",
    "隐患描述",
    "整改要求",
]


def _export_row(hazard) -> list[str]:
    return [
        hazard.code,
        hazard.title,
        hazard.reservoir.name if hazard.reservoir else "",
        StructurePart.label_of(hazard.category),
        HazardSeverity.label_of(hazard.severity),
        HazardStatus.label_of(hazard.status),
        HazardSource.label_of(hazard.source),
        hazard.discovered_on.isoformat() if hazard.discovered_on else "",
        hazard.discoverer or "",
        hazard.deadline.isoformat() if hazard.deadline else "",
        hazard.assignee or "",
        "逾期" if is_overdue(hazard.deadline, hazard.status) else "",
        hazard.closed_on.isoformat() if hazard.closed_on else "",
        (hazard.description or "").replace("\r\n", "\n").replace("\r", "\n"),
        (hazard.plan or "").replace("\r\n", "\n").replace("\r", "\n"),
    ]


@router.get("/export", summary="按当前筛选条件导出隐患台账（CSV）")
def export_hazards(db: DbSession, filters: HazardFilterDep) -> Response:
    """导出与列表完全同源：同样的筛选参数、同样的 WHERE。

    行数以实际取数为准（多取 1 行探测是否超上限），避免统计与取数之间因数据
    并发变动而出现条数对不上。
    """
    flt = filters.value
    hazards = hazard_service.iterate_hazards(db, flt, limit=settings.export_max_rows + 1)
    if not hazards:
        raise InvalidOperationError("当前筛选条件下没有可导出的隐患，请调整或清空筛选条件后再试")
    if len(hazards) > settings.export_max_rows:
        total = hazard_service.count_hazards(db, flt)
        raise InvalidOperationError(
            f"导出结果共 {total} 条，超过单次导出上限 {settings.export_max_rows} 条，"
            "请增加水库、等级、状态或时间范围等筛选条件后分批导出"
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_EXPORT_HEADERS)
    for hazard in hazards:
        writer.writerow(_export_row(hazard))

    filename = f"隐患台账_{len(hazards)}条_{date.today().isoformat()}.csv"
    quoted_filename = quote(filename)
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}"
        },
    )


@router.post(
    "",
    response_model=HazardDetail,
    status_code=status.HTTP_201_CREATED,
    summary="登记隐患",
)
def create_hazard(payload: HazardCreate, db: DbSession) -> HazardDetail:
    return hazard_service.create_hazard(db, payload)


@router.get("/{hazard_id}", response_model=HazardDetail, summary="隐患详情（含整改流水）")
def get_hazard(hazard_id: int, db: DbSession) -> HazardDetail:
    return hazard_service.get_hazard(db, hazard_id)


@router.put("/{hazard_id}", response_model=HazardDetail, summary="更新隐患信息")
def update_hazard(hazard_id: int, payload: HazardUpdate, db: DbSession) -> HazardDetail:
    return hazard_service.update_hazard(db, hazard_id, payload)


@router.post(
    "/{hazard_id}/rectifications",
    response_model=HazardDetail,
    status_code=status.HTTP_201_CREATED,
    summary="追加整改跟踪记录",
)
def add_rectification(
    hazard_id: int, payload: HazardRectificationCreate, db: DbSession
) -> HazardDetail:
    return hazard_service.add_rectification(db, hazard_id, payload)


@router.post(
    "/{hazard_id}/transition",
    response_model=HazardDetail,
    summary="整改状态流转（开始整改 / 提交验收 / 销号 / 退回整改）",
    responses={409: {"description": "当前状态不允许该流转"}},
)
def transition_hazard(
    hazard_id: int, payload: HazardTransitionRequest, db: DbSession
) -> HazardDetail:
    return hazard_service.transition_hazard(db, hazard_id, payload)


@router.delete("/{hazard_id}", response_model=Message, summary="删除隐患及其整改流水")
def delete_hazard(hazard_id: int, db: DbSession) -> Message:
    hazard_service.delete_hazard(db, hazard_id)
    return Message(detail="隐患已删除", code="deleted")
