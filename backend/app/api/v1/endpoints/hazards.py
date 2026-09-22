"""隐患登记与整改跟踪接口。"""

import csv
import io
from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response

from app.api.deps import DbSession, PageParams
from app.models.enums import (
    HazardSeverity,
    HazardSource,
    HazardStatus,
    StructurePart,
)
from app.schemas.common import Message
from app.schemas.hazard import (
    HazardCreate,
    HazardDetail,
    HazardListSummary,
    HazardPage,
    HazardRead,
    HazardRectificationCreate,
    HazardTransitionRequest,
    HazardUpdate,
)
from app.services import hazard_service
from app.services.hazard_service import HazardFilter

router = APIRouter(prefix="/hazards", tags=["隐患与整改"])

# 单次导出上限，避免隐患规模较大时一次性拖垮服务
EXPORT_LIMIT = 10000


def query_hazard_filter(
    reservoir_id: Annotated[int | None, Query(description="按水库过滤")] = None,
    inspection_id: Annotated[int | None, Query(description="按来源巡查记录过滤")] = None,
    category: Annotated[StructurePart | None, Query(description="隐患类别（部位）")] = None,
    severity: Annotated[HazardSeverity | None, Query(description="隐患等级")] = None,
    hazard_status: Annotated[
        HazardStatus | None, Query(alias="status", description="整改状态")
    ] = None,
    source: Annotated[HazardSource | None, Query(description="隐患来源")] = None,
    keyword: Annotated[
        str | None, Query(description="按标题 / 编号 / 描述 / 责任人搜索")
    ] = None,
    open_only: Annotated[bool, Query(description="只看未销号隐患")] = False,
    overdue_only: Annotated[bool, Query(description="只看逾期未整改隐患")] = False,
    discovered_from: Annotated[
        date | None, Query(description="发现日期起（含，YYYY-MM-DD）")
    ] = None,
    discovered_to: Annotated[
        date | None, Query(description="发现日期止（含，YYYY-MM-DD）")
    ] = None,
) -> HazardFilter:
    """列表、顶部汇总与导出共用的筛选依赖：三个入口拿到的是同一份条件。"""
    return HazardFilter(
        reservoir_id=reservoir_id,
        inspection_id=inspection_id,
        category=category.value if category else None,
        severity=severity.value if severity else None,
        status=hazard_status.value if hazard_status else None,
        source=source.value if source else None,
        keyword=(keyword.strip() or None) if keyword else None,
        open_only=open_only,
        overdue_only=overdue_only,
        discovered_from=discovered_from,
        discovered_to=discovered_to,
    )


HazardFilterDep = Annotated[HazardFilter, Depends(query_hazard_filter)]


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if total else 0


@router.get("", response_model=HazardPage, summary="分页查询隐患台账")
def list_hazards(
    db: DbSession,
    pagination: PageParams,
    filters: HazardFilterDep,
) -> HazardPage:
    items, total = hazard_service.list_hazards(
        db, filters, page=pagination.page, page_size=pagination.page_size
    )
    summary = hazard_service.summarize_hazards(db, filters)
    return HazardPage(
        items=[HazardRead.model_validate(item) for item in items],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        pages=_pages(total, pagination.page_size),
        summary=HazardListSummary(**summary),
    )


@router.get("/export", summary="按当前筛选条件导出隐患台账（CSV）")
def export_hazards(db: DbSession, filters: HazardFilterDep) -> Response:
    """导出与列表完全同源的 CSV：同样的条件、同样的中文口径（含 BOM，Excel 可直接打开）。"""
    rows = hazard_service.export_hazards(db, filters, limit=EXPORT_LIMIT)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "隐患编号",
            "所属水库",
            "隐患标题",
            "隐患类别",
            "隐患等级",
            "整改状态",
            "隐患来源",
            "发现日期",
            "发现人",
            "整改期限",
            "销号日期",
            "整改责任人",
            "是否逾期",
            "隐患描述",
            "整改方案",
        ]
    )
    today = date.today()
    for hazard in rows:
        is_overdue = bool(
            hazard.status != HazardStatus.CLOSED.value
            and hazard.deadline is not None
            and hazard.deadline < today
        )
        writer.writerow(
            [
                hazard.code,
                hazard.reservoir.name if hazard.reservoir else "",
                hazard.title,
                StructurePart.label_of(hazard.category),
                HazardSeverity.label_of(hazard.severity),
                HazardStatus.label_of(hazard.status),
                HazardSource.label_of(hazard.source),
                hazard.discovered_on.isoformat() if hazard.discovered_on else "",
                hazard.discoverer or "",
                hazard.deadline.isoformat() if hazard.deadline else "",
                hazard.closed_on.isoformat() if hazard.closed_on else "",
                hazard.assignee or "",
                "逾期" if is_overdue else "",
                (hazard.description or "").replace("\r\n", " ").replace("\n", " "),
                (hazard.plan or "").replace("\r\n", " ").replace("\n", " "),
            ]
        )

    filename = f"隐患台账_{today.isoformat()}.csv"
    # 开头写入 UTF-8 BOM，Excel 直接打开不会乱码
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
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
