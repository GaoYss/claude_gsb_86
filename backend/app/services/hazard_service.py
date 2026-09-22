"""隐患登记与整改跟踪业务逻辑（含状态流转规则）。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ConflictError, InvalidOperationError, NotFoundError
from app.db.base import now_local
from app.models import Hazard, HazardRectification, Inspection
from app.models.enums import HazardStatus, RectificationAction
from app.schemas.hazard import (
    HazardCreate,
    HazardRectificationCreate,
    HazardTransitionOption,
    HazardTransitionRequest,
    HazardUpdate,
)
from app.services import reservoir_service
from app.services.helpers import enum_to_value, next_code


@dataclass(frozen=True)
class TransitionRule:
    """一条允许的状态流转。"""

    target: HazardStatus
    label: str
    action: RectificationAction
    require_content: bool = False


def _rule(
    target: HazardStatus,
    label: str,
    action: RectificationAction,
    require_content: bool = False,
) -> TransitionRule:
    return TransitionRule(target=target, label=label, action=action, require_content=require_content)


# 隐患整改状态机：待整改 -> 整改中 -> 待验收 -> 已销号（已销号为终态）
TRANSITION_RULES: dict[str, list[TransitionRule]] = {
    HazardStatus.REGISTERED.value: [
        _rule(HazardStatus.RECTIFYING, "开始整改", RectificationAction.MEASURE),
        _rule(
            HazardStatus.CLOSED,
            "直接销号（立行立改）",
            RectificationAction.CLOSE,
            require_content=True,
        ),
    ],
    HazardStatus.RECTIFYING.value: [
        _rule(
            HazardStatus.PENDING_ACCEPTANCE,
            "提交验收",
            RectificationAction.PROGRESS,
            require_content=True,
        ),
        _rule(HazardStatus.CLOSED, "直接销号", RectificationAction.CLOSE, require_content=True),
    ],
    HazardStatus.PENDING_ACCEPTANCE.value: [
        _rule(HazardStatus.CLOSED, "验收通过并销号", RectificationAction.VERIFY),
        _rule(
            HazardStatus.RECTIFYING,
            "验收不通过，退回整改",
            RectificationAction.VERIFY,
            require_content=True,
        ),
    ],
    HazardStatus.CLOSED.value: [],
}


def available_transitions(status: str) -> list[HazardTransitionOption]:
    return [
        HazardTransitionOption(
            target_status=rule.target,
            label=rule.label,
            require_content=rule.require_content,
        )
        for rule in TRANSITION_RULES.get(status, [])
    ]


def _find_rule(current: str, target: str) -> TransitionRule | None:
    for rule in TRANSITION_RULES.get(current, []):
        if rule.target.value == target:
            return rule
    return None


def get_hazard(db: Session, hazard_id: int) -> Hazard:
    stmt = (
        select(Hazard)
        .options(
            selectinload(Hazard.reservoir),
            selectinload(Hazard.rectifications),
        )
        .where(Hazard.id == hazard_id)
    )
    hazard = db.scalar(stmt)
    if hazard is None:
        raise NotFoundError(f"隐患不存在：id={hazard_id}")
    return hazard


@dataclass(frozen=True)
class HazardFilter:
    """隐患列表 / 导出 / 汇总共用的筛选条件，保证各处口径一致。"""

    reservoir_id: int | None = None
    inspection_id: int | None = None
    category: str | None = None
    severity: str | None = None
    status: str | None = None
    source: str | None = None
    keyword: str | None = None
    overdue_only: bool = False
    open_only: bool = False
    discovered_from: date | None = None
    discovered_to: date | None = None


def validate_filter(filters: HazardFilter) -> None:
    """检查筛选条件之间是否互相冲突；冲突直接抛业务错误而不是返回空表。"""
    conflicts: list[str] = []

    if (
        filters.discovered_from
        and filters.discovered_to
        and filters.discovered_from > filters.discovered_to
    ):
        conflicts.append(
            f"发现日期范围不合法：起始日期（{filters.discovered_from.isoformat()}）"
            f"晚于截止日期（{filters.discovered_to.isoformat()}）"
        )

    if filters.status == HazardStatus.CLOSED.value:
        if filters.open_only:
            conflicts.append("整改状态选择了「已销号」，同时又勾选了「仅看未销号」，二者互相矛盾")
        if filters.overdue_only:
            conflicts.append("已销号隐患不存在逾期，「状态=已销号」与「仅看逾期」无法同时生效")

    if conflicts:
        raise InvalidOperationError("筛选条件存在冲突：" + "；".join(conflicts))


def _apply_conditions(stmt, filters: HazardFilter):
    """把筛选条件追加到查询语句上；count、分页列表、导出共用这一个入口。"""
    conditions = []
    if filters.reservoir_id:
        conditions.append(Hazard.reservoir_id == filters.reservoir_id)
    if filters.inspection_id:
        conditions.append(Hazard.inspection_id == filters.inspection_id)
    if filters.category:
        conditions.append(Hazard.category == filters.category)
    if filters.severity:
        conditions.append(Hazard.severity == filters.severity)
    if filters.status:
        conditions.append(Hazard.status == filters.status)
    if filters.source:
        conditions.append(Hazard.source == filters.source)
    if filters.open_only:
        conditions.append(Hazard.status != HazardStatus.CLOSED.value)
    if filters.overdue_only:
        conditions.append(
            and_(
                Hazard.status != HazardStatus.CLOSED.value,
                Hazard.deadline.is_not(None),
                Hazard.deadline < date.today(),
            )
        )
    if filters.discovered_from:
        conditions.append(Hazard.discovered_on >= filters.discovered_from)
    if filters.discovered_to:
        conditions.append(Hazard.discovered_on <= filters.discovered_to)
    if filters.keyword:
        like = f"%{filters.keyword.strip()}%"
        conditions.append(
            or_(
                Hazard.title.like(like),
                Hazard.code.like(like),
                Hazard.description.like(like),
                Hazard.assignee.like(like),
            )
        )
    return stmt.where(*conditions)


# 列表与导出统一的排序口径：未销号在前、期限近的在前、同序按登记时间倒序
_HAZARD_ORDER = (
    Hazard.status.desc(),
    Hazard.deadline.is_(None),
    Hazard.deadline.asc(),
    Hazard.id.desc(),
)


def count_hazards(db: Session, filters: HazardFilter) -> int:
    """按筛选条件统计总数；顶部计数、分页 total、导出行数全部以这里为准。"""
    stmt = _apply_conditions(select(func.count()).select_from(Hazard), filters)
    return db.scalar(stmt) or 0


def list_hazards(
    db: Session,
    filters: HazardFilter,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Hazard], int]:
    """分页查询隐患；total 与当前页数据使用同一组 WHERE 条件计算。"""
    validate_filter(filters)
    total = count_hazards(db, filters)
    rows = db.scalars(
        _apply_conditions(
            select(Hazard).options(selectinload(Hazard.reservoir)),
            filters,
        )
        .order_by(*_HAZARD_ORDER)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def iterate_hazards(db: Session, filters: HazardFilter, *, limit: int) -> list[Hazard]:
    """导出用：按同一筛选条件与排序取全量（调用方负责限制规模）。"""
    validate_filter(filters)
    rows = db.scalars(
        _apply_conditions(
            select(Hazard).options(selectinload(Hazard.reservoir)),
            filters,
        )
        .order_by(*_HAZARD_ORDER)
        .limit(limit)
    ).all()
    return list(rows)


def create_hazard(db: Session, payload: HazardCreate) -> Hazard:
    reservoir_service.get_reservoir(db, payload.reservoir_id)
    data = enum_to_value(payload.model_dump())

    inspection_id = data.get("inspection_id")
    if inspection_id:
        inspection = db.get(Inspection, inspection_id)
        if inspection is None:
            raise NotFoundError(f"来源巡查记录不存在：id={inspection_id}")
        if inspection.reservoir_id != data["reservoir_id"]:
            raise InvalidOperationError("来源巡查记录与所选水库不一致，请重新选择")

    discovered_on = data.get("discovered_on") or date.today()
    hazard = Hazard(
        code=next_code(db, Hazard, Hazard.code, prefix="YH", on=discovered_on),
        **{**data, "discovered_on": discovered_on},
    )
    # 登记即写一条流水，保证整改跟踪时间轴从发现开始可追溯
    hazard.rectifications.append(
        HazardRectification(
            action=RectificationAction.REGISTER.value,
            content=f"隐患登记：{hazard.title}",
            operator=data.get("discoverer"),
            status_from=None,
            status_to=HazardStatus.REGISTERED.value,
        )
    )
    db.add(hazard)
    db.commit()
    return get_hazard(db, hazard.id)


def update_hazard(db: Session, hazard_id: int, payload: HazardUpdate) -> Hazard:
    hazard = get_hazard(db, hazard_id)

    if payload.status is not None and payload.status.value != hazard.status:
        raise InvalidOperationError(
            "状态变更请使用 POST /api/v1/hazards/{id}/transition 接口，以便记录整改流水"
        )

    data = enum_to_value(payload.model_dump(exclude_unset=True, exclude={"status"}))
    if "inspection_id" in data and data["inspection_id"]:
        inspection = db.get(Inspection, data["inspection_id"])
        if inspection is None:
            raise NotFoundError(f"来源巡查记录不存在：id={data['inspection_id']}")
        if inspection.reservoir_id != hazard.reservoir_id:
            raise InvalidOperationError("来源巡查记录与隐患所属水库不一致")

    for key, value in data.items():
        setattr(hazard, key, value)
    db.commit()
    return get_hazard(db, hazard_id)


def transition_hazard(db: Session, hazard_id: int, payload: HazardTransitionRequest) -> Hazard:
    """按状态机流转隐患状态，并自动写入整改跟踪流水。"""
    hazard = get_hazard(db, hazard_id)
    target = payload.target_status.value
    rule = _find_rule(hazard.status, target)
    if rule is None:
        raise ConflictError(
            f"不允许从「{HazardStatus.label_of(hazard.status)}」变更为"
            f"「{HazardStatus.label_of(target)}」"
        )

    content = (payload.content or "").strip()
    if rule.require_content and not content:
        raise InvalidOperationError(f"变更为「{rule.label}」需要填写处理说明")

    status_from = hazard.status
    hazard.status = target
    hazard.closed_on = date.today() if target == HazardStatus.CLOSED.value else None
    hazard.rectifications.append(
        HazardRectification(
            action=rule.action.value,
            content=content or rule.label,
            operator=payload.operator,
            status_from=status_from,
            status_to=target,
        )
    )
    db.commit()
    return get_hazard(db, hazard_id)


def add_rectification(
    db: Session, hazard_id: int, payload: HazardRectificationCreate
) -> Hazard:
    """追加整改跟踪记录；记录整改措施时自动从「待整改」进入「整改中」。"""
    hazard = get_hazard(db, hazard_id)
    if hazard.status == HazardStatus.CLOSED.value:
        raise ConflictError("隐患已销号，不能再追加整改记录")

    record = HazardRectification(
        action=payload.action.value,
        content=payload.content,
        operator=payload.operator,
        recorded_at=payload.recorded_at or now_local(),
    )
    if (
        payload.action == RectificationAction.MEASURE
        and hazard.status == HazardStatus.REGISTERED.value
    ):
        record.status_from = HazardStatus.REGISTERED.value
        record.status_to = HazardStatus.RECTIFYING.value
        hazard.status = HazardStatus.RECTIFYING.value

    hazard.rectifications.append(record)
    db.commit()
    return get_hazard(db, hazard_id)


def delete_hazard(db: Session, hazard_id: int) -> None:
    hazard = get_hazard(db, hazard_id)
    db.delete(hazard)
    db.commit()


def overdue_hazard_count(db: Session) -> int:
    stmt = (
        select(func.count())
        .select_from(Hazard)
        .where(
            Hazard.status != HazardStatus.CLOSED.value,
            Hazard.deadline.is_not(None),
            Hazard.deadline < date.today(),
        )
    )
    return db.scalar(stmt) or 0
