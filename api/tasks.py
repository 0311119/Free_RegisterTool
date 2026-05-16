from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from typing import Optional
from datetime import datetime, timezone
import hashlib
import random
import sys
from core.db import TaskLog, engine
from core.task_runtime import SkipCurrentAttemptRequested, StopTaskRequested
import time, json, asyncio, threading, logging

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

_tasks: dict = {}
_tasks_lock = threading.Lock()

MAX_FINISHED_TASKS = 200
CLEANUP_THRESHOLD = 250
TASK_LOG_LIMIT = 2000


def _cleanup_old_tasks():
    """Remove oldest finished tasks when the dict grows too large."""
    with _tasks_lock:
        finished = [
            (tid, t) for tid, t in _tasks.items()
            if t.get("status") in ("done", "failed")
        ]
        if len(finished) <= MAX_FINISHED_TASKS:
            return
        finished.sort(key=lambda x: x[0])
        to_remove = finished[: len(finished) - MAX_FINISHED_TASKS]
        for tid, _ in to_remove:
            del _tasks[tid]


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = Field(default=1, ge=1, le=1000)  # 最大支持 1000 个
    concurrency: int = Field(default=1, ge=1, le=50)  # 最大并发 50
    register_delay_seconds: float = Field(default=0, ge=0)
    random_delay_min: Optional[float] = Field(default=None, ge=0)  # 随机延迟最小值 (秒)
    random_delay_max: Optional[float] = Field(default=None, ge=0)  # 随机延迟最大值 (秒)
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)
    # 定时任务配置
    task_id: Optional[str] = None  # 定时任务 ID（更新时使用）
    interval_type: Optional[str] = None  # minutes | hours
    interval_value: Optional[int] = None  # 间隔值
    # 并发控制配置
    email_retry_count: int = Field(default=3, ge=1, le=10)  # 邮箱创建重试次数
    stagger_seconds: float = Field(default=6.0, ge=0, le=60)  # Worker 错峰启动间隔(秒)
    email_max_concurrency: Optional[int] = Field(default=None, ge=1, le=20)


class TaskLogBatchDeleteRequest(BaseModel):
    ids: list[int]


def _log(task_id: str, msg: str):
    """Append a log entry to the task and print to console."""
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    with _tasks_lock:
        if task_id in _tasks:
            task = _tasks[task_id]
            logs = task.setdefault("logs", [])
            task["log_total"] = int(task.get("log_total", task.get("log_offset", 0) + len(logs))) + 1
            logs.append(entry)
            limit = max(1, int(TASK_LOG_LIMIT))
            if len(logs) > limit:
                drop_count = len(logs) - limit
                del logs[:drop_count]
                task["log_offset"] = int(task.get("log_offset", 0)) + drop_count
    _emit_console_line(entry)


def _emit_console_line(text: str) -> None:
    """Best-effort console output that never aborts the task on encoding errors."""
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass

    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")

    try:
        if hasattr(stream, "buffer"):
            stream.buffer.write((safe_text + "\n").encode(encoding, errors="replace"))
            stream.buffer.flush()
        else:
            stream.write(safe_text + "\n")
            stream.flush()
    except Exception:
        pass


def _save_task_log(platform: str, email: str, status: str,
                   error: str = "", detail: dict = None):
    """Write a TaskLog record to the database."""
    with Session(engine) as s:
        log = TaskLog(
            platform=platform,
            email=email,
            status=status,
            error=error,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
        )
        s.add(log)
        s.commit()


def _set_worker_state(task_id: str, worker_index: int, **patch) -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return
        states = task.setdefault("worker_states", [])
        while len(states) < worker_index:
            states.append(
                {
                    "index": len(states) + 1,
                    "state": "pending",
                    "stage": "pending",
                    "email": "",
                    "message": "",
                    "provider": "",
                    "proxy": "",
                    "updated_at": "",
                }
            )
        state = states[worker_index - 1]
        state.update(patch)
        state["index"] = worker_index
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        # O(1) 累加变更号，供 SSE progress 流签名比较用（不影响业务逻辑）
        task["worker_state_change_id"] = int(task.get("worker_state_change_id") or 0) + 1


def _resolve_mail_provider_sequence(value) -> list[str]:
    raw = str(value or "").strip()
    aliases = {
        "luckmail_cfworker": "luckmail,cfworker",
        "luckmail+cfworker": "luckmail,cfworker",
        "luckmail_cf": "luckmail,cfworker",
        "cf_luckmail": "cfworker,luckmail",
        "cfworker_luckmail": "cfworker,luckmail",
        "cfworker+luckmail": "cfworker,luckmail",
    }
    normalized = aliases.get(raw.lower(), raw)
    providers = [
        item.strip().lower()
        for item in normalized.replace("+", ",").replace("|", ",").replace(";", ",").split(",")
        if item.strip()
    ]
    return providers or ["laoudo"]


def _resolve_parallel_mail_mix(extra: dict | None) -> list[str]:
    raw = (extra or {}).get("mail_provider_mix")
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = str(raw or "").replace("+", ",").replace("|", ",").replace(";", ",").split(",")
    normalized = []
    seen = set()
    for item in values:
        provider = str(item or "").strip().lower()
        if not provider or provider in seen:
            continue
        seen.add(provider)
        normalized.append(provider)
    return normalized


class _TaskStore:
    """任务控制中心：支持停止和跳过当前条目。"""

    def __init__(self):
        self._stop: set = set()
        self._skip: set = set()
        self._pause: set = set()

    def request_stop(self, task_id: str) -> dict:
        self._stop.add(task_id)
        return self.snapshot(task_id)

    def request_skip_current(self, task_id: str) -> dict:
        self._skip.add(task_id)
        return self.snapshot(task_id)

    def request_pause(self, task_id: str) -> dict:
        self._pause.add(task_id)
        return self.snapshot(task_id)

    def request_resume(self, task_id: str) -> dict:
        self._pause.discard(task_id)
        return self.snapshot(task_id)

    def should_stop(self, task_id: str) -> bool:
        return task_id in self._stop

    def is_paused(self, task_id: str) -> bool:
        return task_id in self._pause

    def pop_skip(self, task_id: str) -> bool:
        """消费一次 skip 信号，返回是否命中。"""
        if task_id in self._skip:
            self._skip.discard(task_id)
            return True
        return False

    def clear(self, task_id: str) -> None:
        self._stop.discard(task_id)
        self._skip.discard(task_id)
        self._pause.discard(task_id)

    def snapshot(self, task_id: str) -> dict:
        with _tasks_lock:
            data = dict(_tasks.get(task_id, {}))
            if data:
                logs = list(data.get("logs", []))
                data["logs"] = logs
                data["log_offset"] = int(data.get("log_offset", 0))
                data["log_total"] = int(data.get("log_total", data["log_offset"] + len(logs)))
        if data:
            data["control"] = {
                "stop_requested": task_id in self._stop,
                "paused": task_id in self._pause,
                "pending_skip_requests": 1 if task_id in self._skip else 0,
                "targeted_skip_attempts": 0,
            }
        return data


_task_store = _TaskStore()


def _create_task_record(
    task_id: str,
    req: "RegisterTaskRequest",
    source: str,
    extra,
) -> None:
    """在 _tasks 里初始化一条任务记录（供内部和测试使用）。"""
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "status": "pending",
            "progress": f"0/{req.count}",
            "total": req.count,
            "platform": req.platform,
            "started": 0,
            "completed": 0,
            "logs": [],
            "log_offset": 0,
            "log_total": 0,
            "success": 0,
            "skipped": 0,
            "errors": [],
            "source": source,
            "worker_states": [],
        }


def _auto_upload_integrations(task_id: str, account):
    """注册成功后自动导入外部系统。"""
    try:
        from services.external_sync import sync_account
        for result in sync_account(account):
            name = result.get("name", "Auto Upload")
            ok = bool(result.get("ok"))
            msg = result.get("msg", "")
            _log(task_id, f"  [{name}] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
    except Exception as e:
        _log(task_id, f"  [Auto Upload] 自动导入异常: {e}")


def _run_register(task_id: str, req: RegisterTaskRequest):
    from core.registry import get
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from core.base_mailbox import create_mailbox

    with _tasks_lock:
        _tasks[task_id]["status"] = "running"
    success = 0
    skipped = 0
    errors = []

    # === 并发控制参数 ===
    max_workers = min(req.concurrency, req.count)
    email_sem = threading.Semaphore(min(req.email_max_concurrency or max_workers, max_workers))
    email_min_interval = 2.0  # LuckMail API 最小调用间隔(秒)
    email_retries = req.email_retry_count
    stagger_seconds = req.stagger_seconds
    last_email_call = [0.0]  # 可变闭包记录上次邮箱创建时间

    # === 线程安全数据结构 ===
    import queue
    result_queue = queue.Queue()
    stop_event = threading.Event()

    def _wait_while_paused() -> bool:
        logged_pause = False
        while _task_store.is_paused(task_id):
            if stop_event.is_set() or _task_store.should_stop(task_id):
                stop_event.set()
                return False
            if not logged_pause:
                _log(task_id, "Task paused, waiting for resume...")
                logged_pause = True
            time.sleep(0.25)
        if logged_pause:
            _log(task_id, "Task resumed, continuing")
        return True

    class _TaskControlBridge:
        """Bridge task stop/skip requests into cooperative platform checkpoints."""

        def __init__(self, task_id: str):
            self.task_id = task_id
            self._lock = threading.Lock()
            self._next_attempt_id = 1

        def start_attempt(self) -> int:
            with self._lock:
                attempt_id = self._next_attempt_id
                self._next_attempt_id += 1
                return attempt_id

        def finish_attempt(self, attempt_id: int | None) -> None:
            return None

        def checkpoint(self, *, consume_skip: bool = True, attempt_id: int | None = None) -> None:
            if stop_event.is_set() or _task_store.should_stop(self.task_id):
                stop_event.set()
                raise StopTaskRequested()
            if not _wait_while_paused():
                raise StopTaskRequested()
            if stop_event.is_set() or _task_store.should_stop(self.task_id):
                stop_event.set()
                raise StopTaskRequested()
            if consume_skip and _task_store.pop_skip(self.task_id):
                raise SkipCurrentAttemptRequested()

        def is_stop_requested(self) -> bool:
            return stop_event.is_set() or _task_store.should_stop(self.task_id)

    task_control = _TaskControlBridge(task_id)

    try:
        PlatformCls = get(req.platform)
        parallel_mail_mix = _resolve_parallel_mail_mix(req.extra)
        if parallel_mail_mix:
            shuffled_parallel_mail_mix = list(parallel_mail_mix)
            random.shuffle(shuffled_parallel_mail_mix)
        else:
            shuffled_parallel_mail_mix = []

        def _build_mailbox(proxy: Optional[str], index: int, attempt: int = 0):
            from core.config_store import config_store
            merged_extra = config_store.get_all().copy()
            merged_extra.update({k: v for k, v in req.extra.items() if v is not None})
            if shuffled_parallel_mail_mix:
                providers = shuffled_parallel_mail_mix
            else:
                providers = _resolve_mail_provider_sequence(merged_extra.get("mail_provider", "laoudo"))
            provider = providers[(index + attempt) % len(providers)]
            merged_extra["mail_provider"] = provider
            return create_mailbox(
                provider=provider,
                extra=merged_extra,
                proxy=proxy,
            )

        # === 消费者：注册 Worker（接收预创建的 mailbox） ===
        def _do_one(i: int, _mailbox, _proxy: str):
            # 停止/跳过信号
            if stop_event.is_set() or _task_store.should_stop(task_id):
                result_queue.put(("__stop_or_skip__", i, _task_store.pop_skip(task_id)))
                return
            if not _wait_while_paused():
                result_queue.put(("__stop_or_skip__", i, False))
                return

            # 错峰启动：分段 sleep 以响应 stop 信号
            if i > 0 and stagger_seconds > 0:
                delay = i * stagger_seconds
                _log(task_id, f"Worker-{i} staggered start, waiting {delay:.1f}s")
                slept = 0.0
                while slept < delay:
                    if stop_event.is_set() or _task_store.should_stop(task_id):
                        result_queue.put(("__stop_or_skip__", i, _task_store.pop_skip(task_id)))
                        return
                    if not _wait_while_paused():
                        result_queue.put(("__stop_or_skip__", i, False))
                        return
                    time.sleep(0.25)
                    slept += 0.25

            # 每轮循环检查停止/跳过
            if stop_event.is_set() or _task_store.should_stop(task_id):
                result_queue.put(("__stop_or_skip__", i, _task_store.pop_skip(task_id)))
                return
            if not _wait_while_paused():
                result_queue.put(("__stop_or_skip__", i, False))
                return
            if _task_store.pop_skip(task_id):
                result_queue.put(("__skip__", i, False))
                return

            attempt_token = None
            try:
                from core.proxy_pool import proxy_pool
                from core.config_store import config_store

                merged_extra = config_store.get_all().copy()
                merged_extra.update({k: v for k, v in req.extra.items() if v is not None})
                _config = RegisterConfig(
                    executor_type=req.executor_type,
                    captcha_solver=req.captcha_solver,
                    proxy=_proxy,
                    extra=merged_extra,
                )
                _platform = PlatformCls(config=_config, mailbox=_mailbox)
                attempt_token = task_control.start_attempt()
                try:
                    _platform.bind_task_control(task_control)
                    _platform._task_attempt_token = attempt_token
                    if getattr(_platform, "mailbox", None) is not None:
                        _platform.mailbox._task_attempt_token = attempt_token
                except Exception:
                    pass
                _platform._log_fn = lambda msg: _log(task_id, msg)
                if getattr(_platform, "mailbox", None) is not None:
                    _platform.mailbox._log_fn = _platform._log_fn
                with _tasks_lock:
                    _tasks[task_id]["started"] = max(int(_tasks[task_id].get("started") or 0), i + 1)
                _set_worker_state(
                    task_id,
                    i + 1,
                    state="running",
                    stage="registering",
                    proxy=str(_proxy or ""),
                    message="registering",
                )
                _log(task_id, f"Registering account {i+1}/{req.count}")
                if _proxy:
                    _log(task_id, f"Using proxy: {_proxy}")
                account = _platform.register(
                    email=req.email or None,
                    password=req.password,
                )
                if isinstance(account.extra, dict):
                    mail_provider = merged_extra.get("mail_provider", "")
                    if mail_provider:
                        account.extra.setdefault("mail_provider", mail_provider)
                    if mail_provider == "luckmail" and req.platform == "chatgpt":
                        mailbox_token = getattr(_mailbox, "_token", "") or ""
                        if mailbox_token:
                            account.extra.setdefault("mailbox_token", mailbox_token)
                        if merged_extra.get("luckmail_project_code"):
                            account.extra.setdefault("luckmail_project_code", merged_extra.get("luckmail_project_code"))
                        if merged_extra.get("luckmail_email_type"):
                            account.extra.setdefault("luckmail_email_type", merged_extra.get("luckmail_email_type"))
                        if merged_extra.get("luckmail_domain"):
                            account.extra.setdefault("luckmail_domain", merged_extra.get("luckmail_domain"))
                        if merged_extra.get("luckmail_base_url"):
                            account.extra.setdefault("luckmail_base_url", merged_extra.get("luckmail_base_url"))
                saved_account = save_account(account)
                _set_worker_state(
                    task_id,
                    i + 1,
                    state="success",
                    stage="done",
                    email=str(account.email or ""),
                    message="registered",
                )
                if _proxy:
                    # 使用智能选择器报告成功
                    use_smart_selector = _cfg_store.get("use_smart_proxy_selector", "1").strip() in {"1", "true", "yes"}
                    if use_smart_selector:
                        from core.smart_proxy_selector import smart_selector
                        smart_selector.report_proxy_result(_proxy, success=True)
                    else:
                        proxy_pool.report_success(_proxy)
                _log(task_id, f"[OK] Registered: {account.email}")
                _save_task_log(req.platform, account.email, "success")
                
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _log(task_id, f"  [升级链接] {cashier_url}")
                    with _tasks_lock:
                        _tasks[task_id].setdefault("cashier_urls", []).append(cashier_url)
                result_queue.put(("__success__", i, True))

                # 2. 外部同步不阻塞注册完成计数，避免 Free 注册被 CPA/Sub2API 网络拖慢。
                def _upload_later():
                    _auto_upload_integrations(task_id, saved_account or account)

                threading.Thread(target=_upload_later, daemon=True).start()
            except StopTaskRequested as e:
                stop_event.set()
                _set_worker_state(
                    task_id,
                    i + 1,
                    state="stopped",
                    stage="stopped",
                    message=str(e),
                )
                _log(task_id, str(e))
                result_queue.put(("__stop_or_skip__", i, False))
            except SkipCurrentAttemptRequested as e:
                _set_worker_state(
                    task_id,
                    i + 1,
                    state="skipped",
                    stage="skipped",
                    message=str(e),
                )
                _log(task_id, str(e))
                result_queue.put(("__skip__", i, False))
            except Exception as e:
                if _proxy:
                    try:
                        # 使用智能选择器报告失败
                        use_smart_selector = _cfg_store.get("use_smart_proxy_selector", "1").strip() in {"1", "true", "yes"}
                        if use_smart_selector:
                            from core.smart_proxy_selector import smart_selector
                            smart_selector.report_proxy_result(_proxy, success=False, auto_blacklist=True)
                        else:
                            from core.proxy_pool import proxy_pool
                            proxy_pool.report_fail(_proxy)
                    except Exception:
                        pass
                _log(task_id, f"[FAIL] Registration failed: {e}")
                _set_worker_state(
                    task_id,
                    i + 1,
                    state="error",
                    stage="failed",
                    message=str(e),
                )
                _save_task_log(req.platform, req.email or "", "failed", error=str(e))
                result_queue.put(("__error__", i, str(e)))
            finally:
                task_control.finish_attempt(attempt_token)

        # === 生产者-消费者流水线 ===
        from concurrent.futures import ThreadPoolExecutor

        inflight_registration_slots = threading.Semaphore(max_workers)

        def _acquire_registration_slot() -> bool:
            while not stop_event.is_set() and not _task_store.should_stop(task_id):
                if not _wait_while_paused():
                    return False
                if inflight_registration_slots.acquire(timeout=0.25):
                    return True
            return False

        _do_one_without_slot_release = _do_one

        def _do_one_and_release_slot(i: int, _mailbox, _proxy: str):
            try:
                _do_one_without_slot_release(i, _mailbox, _proxy)
            finally:
                inflight_registration_slots.release()

        _do_one = _do_one_and_release_slot

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            submitted_count = 0  # 已提交到线程池的数量
            futures = []

            for i in range(req.count):
                if not _acquire_registration_slot():
                    stop_event.set()
                    break
                if not _wait_while_paused():
                    stop_event.set()
                    break
                if stop_event.is_set() or _task_store.should_stop(task_id):
                    stop_event.set()
                    break

                # --- 邮箱创建（带 Semaphore 限流 + 重试） ---
                mailbox = None
                for attempt in range(email_retries):
                    if not _wait_while_paused():
                        stop_event.set()
                        break
                    if stop_event.is_set() or _task_store.should_stop(task_id):
                        stop_event.set()
                        break

                    try:
                        with email_sem:
                            # 确保最小调用间隔
                            now = time.time()
                            elapsed = now - last_email_call[0]
                            _log(task_id, f"Creating mailbox {i+1}/{req.count}...")
                            if elapsed < email_min_interval:
                                time.sleep(email_min_interval - elapsed)
                            mailbox = _build_mailbox(req.proxy or None, i, attempt)
                            last_email_call[0] = time.time()
                            provider_name = getattr(getattr(mailbox, "service_type", None), "value", None) or getattr(mailbox, "__class__", type(mailbox)).__name__
                            _set_worker_state(
                                task_id,
                                i + 1,
                                state="ready",
                                stage="mailbox_ready",
                                provider=str(provider_name or ""),
                                message="mailbox ready",
                            )
                            _log(task_id, f"Mailbox {i+1}/{req.count} created, provider={provider_name}")
                            break
                    except Exception as e:
                        error_str = str(e)
                        if attempt < email_retries - 1:
                            backoff = (2 ** attempt) * max(email_min_interval, 2.0)
                            if "rate" in error_str.lower() or "频率" in error_str:
                                _log(task_id, f"Mailbox creation rate limited, retry in {backoff:.0f}s ({attempt + 1}/{email_retries})")
                            else:
                                _log(task_id, f"Mailbox creation failed, retry in {backoff:.0f}s ({attempt + 1}/{email_retries}): {e}")
                            time.sleep(backoff)
                        else:
                            _log(task_id, f"[FAIL] Mailbox creation exhausted ({email_retries} attempts): {e}")

                if mailbox is None:
                    errors.append(f"Account #{i + 1} mailbox creation failed")
                    result_queue.put(("__error__", i, "邮箱创建失败"))
                    submitted_count += 1
                    inflight_registration_slots.release()
                    continue

                if stop_event.is_set() or _task_store.should_stop(task_id):
                    stop_event.set()
                    inflight_registration_slots.release()
                    break
                if not _wait_while_paused():
                    stop_event.set()
                    inflight_registration_slots.release()
                    break

                # 解析代理
                from core.proxy_pool import proxy_pool
                from core.config_store import config_store as _cfg_store
                
                _proxy = req.proxy
                if not _proxy:
                    # 检查是否启用智能代理选择
                    use_smart_selector = _cfg_store.get("use_smart_proxy_selector", "1").strip() in {"1", "true", "yes"}
                    
                    if use_smart_selector:
                        from core.smart_proxy_selector import smart_selector
                        _proxy = smart_selector.get_smart_proxy(
                            region="",
                            avoid_adjacent_ports=True,
                            randomize=True,
                            min_success_rate=0.3  # 优先选择成功率 > 30% 的代理
                        )
                    else:
                        _proxy = proxy_pool.get_next()

                # 立即提交 Worker（流水线：不等待其他邮箱）
                futures.append(pool.submit(_do_one, i, mailbox, _proxy))
                submitted_count += 1

            # --- 结果收集 ---
            processed = 0
            while processed < submitted_count:
                try:
                    result = result_queue.get(timeout=5)
                except queue.Empty:
                    if futures and not all(f.done() for f in futures):
                        continue
                    missing = submitted_count - processed
                    if missing > 0:
                        errors.append(f"{missing} 个账号流程结束但未返回结果")
                    break

                tag = result[0]

                if tag == "__success__":
                    success += 1
                elif tag == "__error__":
                    errors.append(result[2])
                elif tag == "__skip__":
                    skipped += 1
                elif tag == "__stop_or_skip__":
                    _, idx, was_skip = result
                    if was_skip:
                        skipped += 1
                    else:
                        with _tasks_lock:
                            _tasks[task_id]["status"] = "stopped"
                            _tasks[task_id]["completed"] = processed
                            _tasks[task_id]["progress"] = f"{processed}/{req.count}"
                            _tasks[task_id]["success"] = success
                            _tasks[task_id]["skipped"] = skipped
                            _tasks[task_id]["errors"] = errors
                        _task_store.clear(task_id)
                        return
                processed += 1
                with _tasks_lock:
                    _tasks[task_id]["completed"] = processed
                    _tasks[task_id]["progress"] = f"{processed}/{req.count}"
                    _tasks[task_id]["success"] = success
                    _tasks[task_id]["skipped"] = skipped
                    _tasks[task_id]["errors"] = list(errors)

    except Exception as e:
        _log(task_id, f"Fatal error: {e}")
        with _tasks_lock:
            _tasks[task_id]["status"] = "failed"
            _tasks[task_id]["error"] = str(e)
        return

    final_status = "stopped" if (stop_event.is_set() or _task_store.should_stop(task_id)) else "done"
    with _tasks_lock:
        _tasks[task_id]["status"] = final_status
        _tasks[task_id]["success"] = success
        _tasks[task_id]["skipped"] = skipped
        _tasks[task_id]["errors"] = errors
    if final_status == "stopped":
        _log(task_id, f"Task stopped: {success} succeeded, {skipped} skipped, {len(errors)} failed")
    else:
        _log(task_id, f"Done: {success} succeeded, {len(errors)} failed")
    _task_store.clear(task_id)
    _cleanup_old_tasks()

_PAYMENT_GEO_HINTS = {
    "US": ("en-US", "America/New_York"),
    "CA": ("en-CA", "America/Toronto"),
    "GB": ("en-GB", "Europe/London"),
    "DE": ("de-DE", "Europe/Berlin"),
    "FR": ("fr-FR", "Europe/Paris"),
    "ES": ("es-ES", "Europe/Madrid"),
    "IT": ("it-IT", "Europe/Rome"),
    "NL": ("nl-NL", "Europe/Amsterdam"),
    "JP": ("ja-JP", "Asia/Tokyo"),
    "KR": ("ko-KR", "Asia/Seoul"),
    "HK": ("zh-HK", "Asia/Hong_Kong"),
    "TW": ("zh-TW", "Asia/Taipei"),
    "SG": ("en-SG", "Asia/Singapore"),
    "AU": ("en-AU", "Australia/Sydney"),
    "NZ": ("en-NZ", "Pacific/Auckland"),
    "IN": ("en-IN", "Asia/Kolkata"),
    "ID": ("id-ID", "Asia/Jakarta"),
    "MY": ("ms-MY", "Asia/Kuala_Lumpur"),
    "PH": ("en-PH", "Asia/Manila"),
    "TH": ("th-TH", "Asia/Bangkok"),
    "VN": ("vi-VN", "Asia/Ho_Chi_Minh"),
    "TR": ("tr-TR", "Europe/Istanbul"),
    "BR": ("pt-BR", "America/Sao_Paulo"),
    "MX": ("es-MX", "America/Mexico_City"),
    "AR": ("es-AR", "America/Argentina/Buenos_Aires"),
    "CL": ("es-CL", "America/Santiago"),
    "CO": ("es-CO", "America/Bogota"),
    "AE": ("ar-AE", "Asia/Dubai"),
    "IL": ("he-IL", "Asia/Jerusalem"),
    "ZA": ("en-ZA", "Africa/Johannesburg"),
}


def _normalize_payment_component(value) -> str:
    return str(value or "").strip()


def _build_payment_idempotency_key(*, account_email: str, plan: str, billing_country: str, billing_currency: str) -> str:
    payload = "|".join(
        [
            _normalize_payment_component(account_email).lower(),
            _normalize_payment_component(plan).lower(),
            _normalize_payment_component(billing_country).upper(),
            _normalize_payment_component(billing_currency).upper(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_payment_geo_diagnostics(
    *,
    billing_country: str,
    billing_currency: str,
    proxy_url: str = "",
    proxy_geo_country: str = "",
) -> dict:
    country = _normalize_payment_component(billing_country).upper() or "US"
    currency = _normalize_payment_component(billing_currency).upper() or "USD"
    proxy_geo = _normalize_payment_component(proxy_geo_country).upper()
    locale_hint, timezone_hint = _PAYMENT_GEO_HINTS.get(country, ("en-US", "America/New_York"))
    if proxy_geo:
        proxy_geo_matches = proxy_geo == country
        proxy_geo_consistency = "match" if proxy_geo_matches else "mismatch"
        proxy_geo_consistency_reason = (
            "proxy_geo_matches_billing_country"
            if proxy_geo_matches
            else "proxy_geo_differs_from_billing_country"
        )
    else:
        proxy_geo_matches = None
        proxy_geo_consistency = "unknown"
        proxy_geo_consistency_reason = "proxy_geo_country_unavailable"
    return {
        "billing_country": country,
        "billing_currency": currency,
        "browser_locale_hint": locale_hint,
        "browser_timezone_hint": timezone_hint,
        "proxy_url": _normalize_payment_component(proxy_url),
        "proxy_geo_country": proxy_geo,
        "proxy_geo_matches_billing_country": proxy_geo_matches,
        "proxy_geo_consistency": proxy_geo_consistency,
        "proxy_geo_consistency_reason": proxy_geo_consistency_reason,
    }


def _load_account_extra(account) -> dict:
    try:
        import json as _json
        raw_extra = getattr(account, "extra_json", None) or getattr(account, "extra", None)
        if isinstance(raw_extra, str):
            return _json.loads(raw_extra) or {}
        if isinstance(raw_extra, dict):
            return raw_extra
    except Exception:
        pass
    return {}


def _persist_account_extra(task_id: str, account, update: dict, *, status: str | None = None) -> None:
    import json as _json

    merged = _load_account_extra(account)
    merged.update(update or {})
    account.extra_json = _json.dumps(merged, ensure_ascii=False)
    if status:
        account.status = status

    try:
        from core.db import Session as _DbSess, engine as _db_eng
        from core.db import AccountModel as _DbAcct
        from sqlmodel import select as _db_sel

        with _DbSess(_db_eng) as s:
            acct = s.exec(_db_sel(_DbAcct).where(_DbAcct.id == account.id)).first()
            if acct:
                raw = _load_account_extra(acct)
                raw.update(update or {})
                acct.extra_json = _json.dumps(raw, ensure_ascii=False)
                if status:
                    acct.status = status
                s.add(acct)
                s.commit()
                account.extra_json = acct.extra_json
                if status:
                    account.status = acct.status
    except Exception as db_err:
        _log(task_id, f"  [AutoPay] 写入 DB 失败（不影响支付结果）: {db_err}")


@router.post("/register")
def create_register_task(
    req: RegisterTaskRequest,
    background_tasks: BackgroundTasks,
):
    mail_provider = req.extra.get("mail_provider")
    if mail_provider == "luckmail":
        platform = req.platform
        if platform in ("tavily", "openblocklabs"):
            raise HTTPException(400, f"LuckMail 渠道暂时不支持 {platform} 项目注册")
        
        mapping = {
            "trae": "trae",
            "cursor": "cursor",
            "grok": "grok",
            "kiro": "kiro",
            "chatgpt": "openai"
        }
        req.extra["luckmail_project_code"] = mapping.get(platform, platform)

    task_id = f"task_{int(time.time()*1000)}"
    _create_task_record(task_id, req, "manual", req.extra)
    background_tasks.add_task(_run_register, task_id, req)
    return {"task_id": task_id}


@router.post("/{task_id}/stop")
def stop_task(task_id: str):
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")
    control = _task_store.request_stop(task_id)
    _log(task_id, "Stop request received")
    return {"ok": True, "control": control.get("control", {})}


@router.post("/{task_id}/pause")
def pause_task(task_id: str):
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")
    control = _task_store.request_pause(task_id)
    _log(task_id, "Pause request received")
    return {"ok": True, "control": control.get("control", {})}


@router.post("/{task_id}/resume")
def resume_task(task_id: str):
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")
    control = _task_store.request_resume(task_id)
    _log(task_id, "Resume request received")
    return {"ok": True, "control": control.get("control", {})}


@router.post("/{task_id}/skip-current")
def skip_current_task(task_id: str):
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")
    control = _task_store.request_skip_current(task_id)
    _log(task_id, "Skip current account request received")
    return {"ok": True, "control": control.get("control", {})}


@router.get("/logs")
def get_logs(platform: str = None, page: int = 1, page_size: int = 50):
    with Session(engine) as s:
        q = select(TaskLog)
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.order_by(TaskLog.id.desc())
        total = len(s.exec(q).all())
        items = s.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": items}


@router.post("/logs/batch-delete")
def batch_delete_logs(body: TaskLogBatchDeleteRequest):
    if not body.ids:
        raise HTTPException(400, "任务历史 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条任务历史")

    with Session(engine) as s:
        try:
            logs = s.exec(select(TaskLog).where(TaskLog.id.in_(unique_ids))).all()
            found_ids = {log.id for log in logs if log.id is not None}

            for log in logs:
                s.delete(log)

            s.commit()
            deleted_count = len(found_ids)
            not_found_ids = [log_id for log_id in unique_ids if log_id not in found_ids]
            logger.info("批量删除任务历史成功: %s 条", deleted_count)

            return {
                "deleted": deleted_count,
                "not_found": not_found_ids,
                "total_requested": len(unique_ids),
            }
        except Exception as e:
            s.rollback()
            logger.exception("批量删除任务历史失败")
            raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")


def _read_progress_snapshot(task_id: str) -> tuple[dict, str] | tuple[None, str]:
    """读取任务摘要 + 轻量签名。锁内仅做引用拷贝以避免与注册线程长时间抢锁；
    JSON 序列化和昂贵的字段比较都放到锁外。
    返回 (snapshot_dict_or_None, signature)。
    """
    # 锁内：仅快速 get + 浅拷贝 list 引用
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return None, ""
        status = task.get("status", "")
        progress = task.get("progress", "")
        total = task.get("total", 0)
        started = task.get("started", 0)
        completed = task.get("completed", 0)
        success = task.get("success", 0)
        skipped = task.get("skipped", 0)
        errors = task.get("errors") or []
        errors_n = len(errors)
        # errors 为字符串 list，浅拷贝一次即可（避免锁外被并发追加导致越界）
        errors_copy = list(errors)
        error_msg = task.get("error", "")
        # worker_states：只取个数 + 最后一项的状态作为签名指纹（避免 200 次属性扫描）
        worker_states = task.get("worker_states") or []
        workers_n = len(worker_states)
        # 仅复制对外暴露用的引用列表（不深拷贝单个 dict；只读路径）
        worker_states_view = list(worker_states)
        # 用累计变更号作为 signature 一部分（如有则用，否则按首尾 stage 比对）
        change_id = int(task.get("worker_state_change_id") or 0)
        control = task.get("control") or {}
        control_view = dict(control)
        cashier_urls = task.get("cashier_urls") or []
        cashier_view = list(cashier_urls)
        platform = task.get("platform", "")

    snapshot = {
        "id": task_id,
        "status": status,
        "progress": progress,
        "total": total,
        "started": started,
        "completed": completed,
        "success": success,
        "skipped": skipped,
        "errors": errors_copy,
        "error": error_msg,
        "worker_states": worker_states_view,
        "control": control_view,
        "cashier_urls": cashier_view,
        "platform": platform,
    }
    # 轻量 signature：O(1) 字段拼接，不再扫描 worker_states 内部
    signature = (
        f"{status}|{progress}|{started}|{completed}|{success}|{skipped}"
        f"|{errors_n}|{workers_n}|{change_id}"
    )
    return snapshot, signature


@router.get("/{task_id}/progress/stream")
async def stream_progress(task_id: str):
    """SSE 任务摘要流：仅在状态变化时推送，事件驱动取代 2s 轮询。

    性能策略：
    - tick 间隔 1.5s（用户视觉无感，锁竞争降到原来 1/3）
    - 锁内仅复制引用，不做 JSON 序列化；签名 O(1) 计算
    - 仅在 signature 变化时才 dump JSON 并推送
    """
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")

    async def event_generator():
        last_signature: str = ""
        idle_ticks = 0
        first = True
        while True:
            if not first:
                await asyncio.sleep(1.5)
            first = False
            snap, signature = _read_progress_snapshot(task_id)
            if snap is None:
                yield f"data: {json.dumps({'gone': True}, ensure_ascii=False)}\n\n"
                break
            if signature != last_signature:
                last_signature = signature
                idle_ticks = 0
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
            else:
                idle_ticks += 1
                # 每 ~15s 一次 keep-alive，防止反代超时
                if idle_ticks >= 10:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"
            if snap.get("status") in ("done", "failed", "stopped"):
                yield f"data: {json.dumps({'final': True, **snap}, ensure_ascii=False)}\n\n"
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    """SSE 实时日志流"""
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")

    async def event_generator():
        sent = since
        while True:
            with _tasks_lock:
                task = _tasks.get(task_id, {})
                logs = list(task.get("logs", []))
                offset = int(task.get("log_offset", 0))
                total = int(task.get("log_total", offset + len(logs)))
                status = task.get("status", "")
            if sent < offset:
                yield f"data: {json.dumps({'reset': True, 'log_offset': offset, 'log_total': total})}\n\n"
                sent = offset
            while sent < total:
                local_index = sent - offset
                if local_index < 0 or local_index >= len(logs):
                    break
                yield f"data: {json.dumps({'line': logs[local_index], 'index': sent})}\n\n"
                sent += 1
            if status in ("done", "failed", "stopped"):
                yield f"data: {json.dumps({'done': True, 'status': status, 'log_offset': offset, 'log_total': total})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )



# 定时任务管理 API
@router.post("/schedule/{task_id}/run")
def run_scheduled_task_now(task_id: str, background_tasks: BackgroundTasks):
    """立即执行定时任务"""
    from core.db import ScheduledTaskModel, Session, engine
    from core.scheduler import update_task_run_status
    from api.tasks import _run_register, _log
    import logging
    
    logger = logging.getLogger(__name__)
    
    with Session(engine) as s:
        task = s.get(ScheduledTaskModel, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        task_config = {
            "task_id": task.task_id,
            "platform": task.platform,
            "count": task.count,
            "executor_type": task.executor_type,
            "captcha_solver": task.captcha_solver,
            "extra": task.get_extra(),
            "interval_type": task.interval_type,
            "interval_value": task.interval_value,
        }
    run_task_id = f"manual_{task_id}_{int(time.time())}"
    
    # 创建 RegisterTaskRequest
    req = RegisterTaskRequest(**task_config)
    logger.info(f"准备手动运行任务 {run_task_id}, 配置：{task_config}")
    
    def run_with_status():
        try:
            # 先初始化 _tasks 记录
            _create_task_record(run_task_id, req, "scheduled_manual", req.extra)
            # 先记录开始
            _log(run_task_id, f"开始手动运行定时任务 {task_id}")
            _run_register(run_task_id, req)
            with _tasks_lock:
                task_state = dict(_tasks.get(run_task_id, {}))
            success = task_state.get("status") == "done" and not task_state.get("errors")
            error_msg = None if success else task_state.get("error") or "; ".join(task_state.get("errors") or [])
            update_task_run_status(task_id, success, error_msg)
            logger.info(f"任务 {run_task_id} 运行完成")
        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            update_task_run_status(task_id, False, error_msg)
            logger.error(f"任务 {run_task_id} 运行失败：{error_msg}")
    
    background_tasks.add_task(run_with_status)
    
    return {"task_id": run_task_id, "status": "running"}

@router.post("/schedule")
def create_scheduled_task(body: RegisterTaskRequest):
    """创建定时注册任务"""
    import uuid
    from core.db import ScheduledTaskModel, Session, engine
    from sqlmodel import select
    
    task_id = f"sched_{uuid.uuid4().hex[:8]}"
    
    # 保存到数据库
    db_task = ScheduledTaskModel(
        task_id=task_id,
        platform=body.platform,
        count=body.count,
        executor_type=body.executor_type,
        captcha_solver=body.captcha_solver,
        extra_json=json.dumps(body.extra, ensure_ascii=False),
        interval_type=body.interval_type or "minutes",
        interval_value=body.interval_value or 30,
        paused=False,
    )
    with Session(engine) as s:
        s.add(db_task)
        s.commit()
        s.refresh(db_task)
    
    # 添加到内存
    from core.scheduler import add_scheduled_register_task
    config = body.dict()
    config['task_id'] = task_id
    config['interval_type'] = db_task.interval_type
    config['interval_value'] = db_task.interval_value
    config['paused'] = db_task.paused
    add_scheduled_register_task(task_id, config)
    
    # 创建后立即在线程中执行一次
    def run_now():
        run_task_id = f"scheduled_{task_id}_{int(time.time())}"
        success = False
        error_msg = None
        try:
            # 先初始化 _tasks 记录
            req = RegisterTaskRequest(**config)
            _create_task_record(run_task_id, req, "scheduled", req.extra)
            _run_register(run_task_id, req)
            with _tasks_lock:
                task_state = dict(_tasks.get(run_task_id, {}))
            success = task_state.get("status") == "done" and not task_state.get("errors")
            if not success:
                error_msg = task_state.get("error") or "; ".join(task_state.get("errors") or [])
            print(f"[Scheduler] task {task_id} executed", flush=True)
        except Exception as e:
            error_msg = str(e)
            print(f"[Scheduler] task {task_id} failed: {e}", flush=True)
        finally:
            # 更新任务运行状态
            from core.scheduler import update_task_run_status
            update_task_run_status(task_id, success, error_msg)
    
    threading.Thread(target=run_now, daemon=True).start()
    print(f"[Scheduler] task {task_id} created and started", flush=True)
    
    return {"task_id": task_id, "status": "scheduled", "config": config}


@router.get("/schedule")
def list_scheduled_tasks():
    """获取所有定时任务"""
    from core.db import ScheduledTaskModel, Session, engine
    from core.scheduler import get_all_task_run_status
    run_status = get_all_task_run_status()

    result = []
    with Session(engine) as s:
        tasks = s.exec(select(ScheduledTaskModel)).all()
        for task in tasks:
            task_data = {
                "task_id": task.task_id,
                "platform": task.platform,
                "count": task.count,
                "executor_type": task.executor_type,
                "captcha_solver": task.captcha_solver,
                "extra": task.get_extra(),
                "interval_type": task.interval_type,
                "interval_value": task.interval_value,
                "paused": task.paused,
            }
            if task.task_id in run_status:
                task_data.update(run_status[task.task_id])
            else:
                task_data.setdefault("last_run_at", None)
                task_data.setdefault("last_run_success", None)
                task_data.setdefault("last_error", None)
            result.append(task_data)
    
    return {"tasks": result}


@router.get("/{task_id}")
def get_task(task_id: str, include_logs: bool = True):
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")
    item = _task_store.snapshot(task_id)
    if not include_logs:
        item.pop("logs", None)
    return item


@router.get("")
def list_tasks():
    with _tasks_lock:
        task_ids = list(_tasks.keys())
    summaries = []
    for task_id in task_ids:
        item = _task_store.snapshot(task_id)
        item.pop("logs", None)
        summaries.append(item)
    return summaries



# 定时任务管理 API - 更新任务
@router.put("/schedule")
def update_scheduled_task(body: RegisterTaskRequest):
    """更新定时任务配置"""
    from core.db import ScheduledTaskModel, Session, engine
    from core.scheduler import add_scheduled_register_task, remove_scheduled_register_task
    
    # 从根级别或 extra 中获取 task_id
    task_id = getattr(body, 'task_id', None) or (body.extra and body.extra.get('task_id'))
    if not task_id:
        raise HTTPException(400, "缺少任务 ID")
    
    config = body.dict()
    config['task_id'] = task_id
    with Session(engine) as s:
        task = s.get(ScheduledTaskModel, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        task.platform = body.platform
        task.count = body.count
        task.executor_type = body.executor_type
        task.captcha_solver = body.captcha_solver
        task.extra_json = json.dumps(body.extra, ensure_ascii=False)
        task.interval_type = body.interval_type or task.interval_type or "minutes"
        task.interval_value = body.interval_value or task.interval_value or 30
        task.updated_at = datetime.now(timezone.utc)
        s.add(task)
        s.commit()
        s.refresh(task)
        config['paused'] = task.paused
        config['interval_type'] = task.interval_type
        config['interval_value'] = task.interval_value
    if config.get('paused'):
        remove_scheduled_register_task(task_id)
    else:
        add_scheduled_register_task(task_id, config)
    
    return {"task_id": task_id, "status": "updated", "config": config}


# 手动运行定时任务



# 暂停/恢复定时任务



@router.delete("/schedule/{task_id}")
def delete_scheduled_task(task_id: str):
    """删除定时任务"""
    from core.db import ScheduledTaskModel, Session, engine
    from core.scheduler import remove_scheduled_register_task
    
    with Session(engine) as s:
        task = s.get(ScheduledTaskModel, task_id)
        if task:
            s.delete(task)
            s.commit()
    
    remove_scheduled_register_task(task_id)
    return {"ok": True}


@router.post("/schedule/{task_id}/toggle")
def toggle_scheduled_task(task_id: str):
    """暂停或恢复定时任务"""
    from core.db import ScheduledTaskModel, Session, engine
    from core.scheduler import add_scheduled_register_task, remove_scheduled_register_task
    
    with Session(engine) as s:
        task = s.get(ScheduledTaskModel, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        task.paused = not task.paused
        task.updated_at = datetime.now(timezone.utc)
        s.add(task)
        s.commit()
        s.refresh(task)
        paused = task.paused
        
        if paused:
            remove_scheduled_register_task(task_id)
        else:
            add_scheduled_register_task(task_id, {
                "task_id": task.task_id,
                "platform": task.platform,
                "count": task.count,
                "executor_type": task.executor_type,
                "captcha_solver": task.captcha_solver,
                "extra": task.get_extra(),
                "interval_type": task.interval_type,
                "interval_value": task.interval_value,
                "paused": task.paused,
            })
    
    return {"task_id": task_id, "paused": paused}
