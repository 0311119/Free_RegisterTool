from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import AccountModel, get_session
from platforms.chatgpt.token_refresh import TokenRefreshManager
from services.chatgpt_account_state import (
    INVALID_ACCOUNT_STATUS,
    classify_local_probe_state,
    classify_remote_sync_state,
)
from typing import Optional
from datetime import datetime, timezone
import io, csv, json, logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchRecoverRequest(BaseModel):
    ids: list[int]


def _build_chatgpt_refresh_account(acc: AccountModel):
    extra = acc.get_extra()

    class _RefreshAccount:
        pass

    obj = _RefreshAccount()
    obj.email = acc.email
    obj.access_token = extra.get("access_token") or acc.token
    obj.refresh_token = extra.get("refresh_token", "")
    obj.session_token = extra.get("session_token", "")
    obj.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
    return obj


def _effective_status(acc: AccountModel) -> tuple[str, str]:
    """Return (effective_status, invalid_reason).

    effective_status mirrors acc.status except it becomes 'invalid' when
    local-probe or remote-sync evidence shows the account is truly dead.
    invalid_reason is a short machine-readable tag (e.g. 'auth_401'),
    empty string when not invalid.
    """
    raw_status = str(acc.status or "").strip().lower()
    if raw_status == INVALID_ACCOUNT_STATUS:
        return INVALID_ACCOUNT_STATUS, "db_status"

    extra = acc.get_extra()
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else None
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    remote_sync = None
    if isinstance(sync_statuses.get("sub2api"), dict):
        remote_sync = sync_statuses.get("sub2api")
    elif isinstance(sync_statuses.get("cliproxyapi"), dict):
        remote_sync = sync_statuses.get("cliproxyapi")

    reason = classify_local_probe_state(local_probe) or classify_remote_sync_state(remote_sync)
    if reason:
        return INVALID_ACCOUNT_STATUS, reason
    return raw_status or "registered", ""


def _is_effectively_invalid(acc: AccountModel) -> bool:
    es, _ = _effective_status(acc)
    return es == INVALID_ACCOUNT_STATUS


def _account_response(acc: AccountModel) -> dict:
    data = acc.model_dump() if hasattr(acc, "model_dump") else acc.__dict__.copy()
    data.pop("_sa_instance_state", None)
    es, reason = _effective_status(acc)
    data["effective_status"] = es
    data["invalid_reason"] = reason
    return data


def _account_summary_response(acc: AccountModel) -> dict:
    extra = acc.get_extra()
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    sub2api_sync = sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {}
    es, reason = _effective_status(acc)
    return {
        "id": acc.id,
        "platform": acc.platform,
        "email": acc.email,
        "password": acc.password,
        "user_id": acc.user_id,
        "region": acc.region,
        "status": acc.status,
        "cashier_url": acc.cashier_url,
        "created_at": acc.created_at,
        "updated_at": acc.updated_at,
        "effective_status": es,
        "invalid_reason": reason,
        "has_refresh_token": bool(extra.get("refresh_token")),
        "chatgpt_local": chatgpt_local,
        "sub2api_sync": sub2api_sync,
        "auto_pay_state": extra.get("auto_pay_state", ""),
        "auto_pay_diagnostic_code": extra.get("auto_pay_diagnostic_code", ""),
        "auto_pay_plan": extra.get("auto_pay_plan", ""),
        "auto_pay_provider": extra.get("auto_pay_provider", ""),
    }


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    status_group: Optional[str] = None,
    email: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    summary: bool = False,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if email:
        q = q.where(AccountModel.email.contains(email))
    # 按照创建时间倒序排列
    q = q.order_by(AccountModel.created_at.desc())

    page = max(1, int(page or 1))
    page_size = min(1000, max(1, int(page_size or 20)))

    if not status_group and status != INVALID_ACCOUNT_STATUS:
        count_q = select(func.count(AccountModel.id))
        if platform:
            count_q = count_q.where(AccountModel.platform == platform)
        if email:
            count_q = count_q.where(AccountModel.email.contains(email))
        if status:
            q = q.where(AccountModel.status == status)
            count_q = count_q.where(AccountModel.status == status)

        total = int(session.exec(count_q).one() or 0)
        items = session.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
        serializer = _account_summary_response if summary else _account_response
        return {"total": total, "page": page, "items": [serializer(acc) for acc in items]}

    rows = session.exec(q).all()
    if status == INVALID_ACCOUNT_STATUS:
        rows = [row for row in rows if _is_effectively_invalid(row)]
    elif status:
        rows = [row for row in rows if str(row.status or "") == status]
    elif status_group == "success":
        rows = [row for row in rows if not _is_effectively_invalid(row)]
    elif status_group == "failed":
        rows = [row for row in rows if _is_effectively_invalid(row)]

    total = len(rows)
    items = rows[(page - 1) * page_size: page * page_size]

    serializer = _account_summary_response if summary else _account_response
    return {"total": total, "page": page, "items": [serializer(acc) for acc in items]}


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    effective_statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
        es, _ = _effective_status(acc)
        effective_statuses[es] = effective_statuses.get(es, 0) + 1
    return {
        "total": len(accounts),
        "by_platform": platforms,
        "by_status": statuses,
        "by_effective_status": effective_statuses,
    }


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created += 1
    session.commit()
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    not_found_ids = []
    
    try:
        for account_id in body.ids:
            acc = session.get(AccountModel, account_id)
            if acc:
                session.delete(acc)
                deleted_count += 1
            else:
                not_found_ids.append(account_id)
        
        session.commit()
        logger.info(f"批量删除成功: {deleted_count} 个账号")
        
        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(body.ids)
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/batch-recover")
def batch_recover_accounts(
    body: BatchRecoverRequest,
    session: Session = Depends(get_session),
):
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多恢复 1000 个账号")

    recovered = 0
    failed = 0
    not_found_ids = []
    items = []
    refresh_manager = TokenRefreshManager()

    try:
        for account_id in unique_ids:
            acc = session.get(AccountModel, account_id)
            if not acc:
                not_found_ids.append(account_id)
                failed += 1
                items.append(
                    {
                        "id": account_id,
                        "email": "",
                        "ok": False,
                        "message": "账号不存在",
                    }
                )
                continue

            if acc.platform != "chatgpt":
                failed += 1
                items.append(
                    {
                        "id": acc.id,
                        "email": acc.email,
                        "ok": False,
                        "message": "当前仅支持 ChatGPT 账号恢复",
                    }
                )
                continue

            refresh_result = refresh_manager.refresh_account(_build_chatgpt_refresh_account(acc))
            if not refresh_result.success:
                failed += 1
                items.append(
                    {
                        "id": acc.id,
                        "email": acc.email,
                        "ok": False,
                        "message": refresh_result.error_message or "恢复失败",
                    }
                )
                continue

            extra = acc.get_extra()
            extra["access_token"] = refresh_result.access_token
            if refresh_result.refresh_token:
                extra["refresh_token"] = refresh_result.refresh_token
            extra.pop("chatgpt_local", None)
            sync_statuses = extra.get("sync_statuses")
            if isinstance(sync_statuses, dict):
                if "sub2api" in sync_statuses:
                    sync_statuses["sub2api"] = {}
                if "cliproxyapi" in sync_statuses:
                    sync_statuses["cliproxyapi"] = {}
                extra["sync_statuses"] = sync_statuses
            acc.set_extra(extra)
            acc.token = refresh_result.access_token
            acc.status = "registered"
            acc.updated_at = datetime.now(timezone.utc)
            session.add(acc)
            recovered += 1
            items.append(
                {
                    "id": acc.id,
                    "email": acc.email,
                    "ok": True,
                    "message": "恢复成功",
                }
            )

        session.commit()
        return {
            "recovered": recovered,
            "failed": failed,
            "not_found": not_found_ids,
            "total_requested": len(unique_ids),
            "items": items,
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量恢复失败")
        raise HTTPException(500, f"批量恢复失败: {str(e)}")


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    data = acc.model_dump() if hasattr(acc, "model_dump") else acc.__dict__.copy()
    data.pop("_sa_instance_state", None)
    es, reason = _effective_status(acc)
    data["effective_status"] = es
    data["invalid_reason"] = reason
    return data


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        acc.status = body.status
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    session.delete(acc)
    session.commit()
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        from core.registry import get
        try:
            PlatformCls = get(acc.platform)
            plugin = PlatformCls(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
