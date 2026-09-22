"""隐患登记与整改跟踪业务逻辑（含状态流转规则）。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select
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
class HazardFilter:
    """隐患列表 / 导出 / 汇总共用的同一份筛选条件。

    列表条数、顶部计数、导出内容都基于本条件生成，保证三者同源、不会对不上。
    """

    reservoir_id: int | None = None
    inspection_id: int | None = None
    category: str | None = None
    severity: str | None = None
    status: str | None = None
    source: str | None = None
    keyword: str | None = None
    open_only: bool = False
    overdue_only: bool = False
    discovered_from: date | None = None
    discovered_to: date | None = None

    def apply(self, stmt):
        """把筛选条件应用到任意以 Hazard 为源的语句（列表 / 计数 / 导出 / 分布）。"""
        if self.reservoir_id:
            stmt = stmt.where(Hazard.reservoir_id == self.reservoir_id)
        if self.inspection_id:
            stmt = stmt.where(Hazard.inspection_id == self.inspection_id)
        if self.category:
            stmt = stmt.where(Hazard.category == self.category)
        if self.severity:
            stmt = stmt.where(Hazard.severity == self.severity)
        if self.status:
            stmt = stmt.where(Hazard.status == self.status)
        if self.source:
            stmt = stmt.where(Hazard.source == self.source)
        if self.open_only:
            stmt = stmt.where(Hazard.status != HazardStatus.CLOSED.value)
        if self.overdue_only:
            stmt = stmt.where(
                Hazard.status != HazardStatus.CLOSED.value,
                Hazard.deadline.is_not(None),
                Hazard.deadline < date.today(),
            )
        if self.discovered_from:
            stmt = stmt.where(Hazard.discovered_on >= self.discovered_from)
        if self.discovered_to:
            stmt = stmt.where(Hazard.discovered_on <= self.discovered_to)
        if self.keyword:
            like = f"%{self.keyword.strip()}%"
            stmt = stmt.where(
                or_(
                    Hazard.title.like(like),
                    Hazard.code.like(like),
                    Hazard.description.like(like),
                    Hazard.assignee.like(like),
                )
            )
        return stmt

    @property
    def conflicts(self) -> list[str]:
        """条件之间互相冲突时返回逐条中文说明；为空表示没有冲突。"""
        messages: list[str] = []
        if self.status == HazardStatus.CLOSED.value and self.open_only:
            messages.append("「整改状态 = 已销号」与「仅看未销号」互相冲突，不可能同时满足")
        if self.status == HazardStatus.CLOSED.value and self.overdue_only:
            messages.append("「整改状态 = 已销号」与「仅看逾期」互相冲突，已销号隐患不存在逾期")
        if self.discovered_from and self.discovered_to and self.discovered_from > self.discovered_to:
            messages.append("发现日期范围无效：开始日期晚于结束日期")
        return messages


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


def validate_filter(filters: HazardFilter) -> None:
    """筛选条件互相冲突时直接报错（422 + 中文说明），不返回看似正常的空表。"""
    conflicts = filters.conflicts
    if conflicts:
        raise InvalidOperationError("；".join(conflicts) + "，请调整筛选条件")


def list_hazards(
    db: Session,
    filters: HazardFilter,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Hazard], int]:
    """分页查询隐患。条数与下方 summarize / export_hazards 使用完全相同的条件。"""
    validate_filter(filters)

    total = db.scalar(filters.apply(select(func.count()).select_from(Hazard))) or 0
    rows = db.scalars(
        filters.apply(select(Hazard).options(selectinload(Hazard.reservoir)))
        .order_by(
            Hazard.status.desc(),
            Hazard.deadline.is_(None),
            Hazard.deadline.asc(),
            Hazard.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def summarize_hazards(db: Session, filters: HazardFilter) -> dict[str, int]:
    """同一筛选条件下的顶部计数：匹配总数、其中未销号数、其中逾期数。"""
    validate_filter(filters)

    total = db.scalar(filters.apply(select(func.count()).select_from(Hazard))) or 0
    open_count = db.scalar(
        filters.apply(select(func.count()).select_from(Hazard)).where(
            Hazard.status != HazardStatus.CLOSED.value
        )
    ) or 0
    overdue_count = db.scalar(
        filters.apply(select(func.count()).select_from(Hazard)).where(
            Hazard.status != HazardStatus.CLOSED.value,
            Hazard.deadline.is_not(None),
            Hazard.deadline < date.today(),
        )
    ) or 0
    return {"total": total, "open": open_count, "overdue": overdue_count}


def export_hazards(db: Session, filters: HazardFilter, *, limit: int = 10000) -> list[Hazard]:
    """按筛选条件导出隐患（不分页，带上限保护）。与列表同源，导出即所见。"""
    validate_filter(filters)
    rows = db.scalars(
        filters.apply(select(Hazard).options(selectinload(Hazard.reservoir)))
        .order_by(
            Hazard.status.desc(),
            Hazard.deadline.is_(None),
            Hazard.deadline.asc(),
            Hazard.id.desc(),
        )
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
