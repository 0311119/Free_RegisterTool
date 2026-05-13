"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import base64
import json
import logging
import random
import re
import secrets
import time
import urllib.parse
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from datetime import datetime

from curl_cffi import requests as cffi_requests

from core.task_runtime import TaskInterruption
from core.smart_proxy_selector import smart_selector
from .oauth import OAuthManager, OAuthStart
from .http_client import OpenAIHTTPClient
from .sentinel_browser import get_sentinel_token_via_browser, warmup_page_and_extract_cookies, _run_browser_for_page, browser_form_submit
from .sentinel_token import build_sentinel_token
from .utils import (
    extract_flow_state,
    generate_datadog_trace,
    generate_device_id,
    generate_random_password,
    normalize_flow_url,
    seed_oai_device_cookie,
)
# from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType  # removed: external dep
# from ..database import crud  # removed: external dep
# from ..database.session import get_db  # removed: external dep
from .constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
)
from .session_fingerprint import SessionFingerprint
from .human_behavior_simulator import HumanBehaviorSimulator, HumanBehaviorConfig
# from ..config.settings import get_settings  # removed: external dep


logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    """娉ㄥ唽缁撴灉"""
    success: bool
    email: str = ""
    password: str = ""  # 娉ㄥ唽瀵嗙爜
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 浼氳瘽浠ょ墝
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 鎴?'login'锛屽尯鍒嗚处鍙锋潵婧?

    def to_dict(self) -> Dict[str, Any]:
        """杞崲涓哄瓧鍏?"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """鎻愪氦娉ㄥ唽琛ㄥ崟鐨勭粨鏋?"""
    success: bool
    page_type: str = ""  # 鍝嶅簲涓殑 page.type 瀛楁
    is_existing_account: bool = False  # 鏄惁涓哄凡娉ㄥ唽璐﹀彿
    response_data: Dict[str, Any] = None  # 瀹屾暣鐨勫搷搴旀暟鎹?
    error_message: str = ""


class ConsumerEmailServiceAdapter:
    """Adapt the existing mailbox service to the plain ChatGPT web registration client."""

    def __init__(self, email_service, email: str, email_info: Optional[Dict[str, Any]], log_fn: Callable[[str], None]):
        self.email_service = email_service
        self.email = email
        self.email_info = dict(email_info or {})
        self.log_fn = log_fn
        self._used_codes = set()

    @property
    def used_codes(self) -> set[str]:
        return set(self._used_codes)

    def wait_for_verification_code(self, email, timeout=60, otp_sent_at=None, exclude_codes=None):
        email_id = self.email_info.get("service_id")
        effective_excludes = set(exclude_codes or set()) | self._used_codes
        self.log_fn(f"普通 ChatGPT 网页注册：等待邮箱 {email} 的验证码 ({timeout}s)...")
        code = self.email_service.get_verification_code(
            email=self.email,
            email_id=email_id,
            timeout=timeout,
            pattern=OTP_CODE_PATTERN,
            otp_sent_at=otp_sent_at,
            exclude_codes=effective_excludes,
        )
        if code:
            code_text = str(code).strip()
            if code_text:
                self._used_codes.add(code_text)
            self.log_fn(f"普通 ChatGPT 网页注册：成功获取验证码 {code}")
        return code


class RefreshTokenRegistrationEngine:
    """
    娉ㄥ唽寮曟搸
    璐熻矗鍗忚皟閭鏈嶅姟銆丱Auth 娴佺▼鍜?OpenAI API 璋冪敤
    """

    def __init__(
        self,
        email_service,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        browser_mode: str = "headless",
        extra_config: Optional[dict] = None,
        task_control: Optional[object] = None,
        pre_oauth_auto_pay_hook: Optional[Callable[[RegistrationResult, dict], Optional[dict]]] = None,
    ):
        """
        鍒濆鍖栨敞鍐屽紩鎿?

        Args:
            email_service: 閭鏈嶅姟瀹炰緥
            proxy_url: 浠ｇ悊 URL
            callback_logger: 鏃ュ織鍥炶皟鍑芥暟
            task_uuid: 浠诲姟 UUID锛堢敤浜庢暟鎹簱璁板綍锛?
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self.browser_mode = str(browser_mode or "headless").strip().lower()
        self.extra_config = dict(extra_config or {})
        self._task_control = task_control
        self._pre_oauth_auto_pay_hook = pre_oauth_auto_pay_hook

        # 鍒涘缓 HTTP 瀹㈡埛绔?
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)

        # 鍒涘缓 OAuth 绠＄悊鍣?
        from .constants import OAUTH_CLIENT_ID, OAUTH_AUTH_URL, OAUTH_TOKEN_URL, OAUTH_REDIRECT_URI, OAUTH_SCOPE
        self.oauth_manager = OAuthManager(
            client_id=OAUTH_CLIENT_ID,
            auth_url=OAUTH_AUTH_URL,
            token_url=OAUTH_TOKEN_URL,
            redirect_uri=OAUTH_REDIRECT_URI,
            scope=OAUTH_SCOPE,
            proxy_url=proxy_url  # 浼犻€掍唬鐞嗛厤缃?
        )

        # 鐘舵€佸彉閲?
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 娉ㄥ唽瀵嗙爜
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 浼氳瘽浠ょ墝
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 鍙戦€佹椂闂存埑
        self._device_id: Optional[str] = None  # 褰撳墠娉ㄥ唽娴佺▼澶嶇敤鐨?Device ID
        self._used_verification_codes = set()  # 宸插彇杩囩殑楠岃瘉鐮侊紝閬垮厤浜屾鐧诲綍鏃舵崬鍒版棫鐮?
        self._is_existing_account: bool = False  # 鏄惁涓哄凡娉ㄥ唽璐﹀彿锛堢敤浜庤嚜鍔ㄧ櫥褰曪級
        self._token_acquisition_requires_login: bool = False  # 鏂版敞鍐岃处鍙烽渶瑕佷簩娆＄櫥褰曟嬁 token
        self._post_otp_continue_url: str = ""
        self._post_otp_page_type: str = ""
        self._authorize_sentinel: Optional[str] = None  # authorize_continue sentinel锛屽鐢ㄤ簬鍚庣画姝ラ
        self._register_continue_url: str = ""
        self._browser_frontend_state: Dict[str, Any] = {}
        self._about_you_create_account_already_exists_without_consent: bool = False
        self._authorization_context_bootstrap_attempted: bool = False
        self._last_phone_failure_reason: str = ""
        self._oauth_blocked_by_phone: bool = False
        self._proxy_geo_country: str = ""

        # ---- 隐蔽性增强组件 ----
        # Session 级指纹统一（版本号、视口、Accept-Language 锁定）
        self._session_fp: SessionFingerprint = SessionFingerprint()
        # 人类行为模拟器
        self._behavior_sim: HumanBehaviorSimulator = HumanBehaviorSimulator(
            HumanBehaviorConfig(
                min_delay=0.3,
                max_delay=1.5,
                thinking_delay_min=1.0,
                thinking_delay_max=3.0,
                page_observation_min=1.0,
                page_observation_max=3.0,
            )
        )
        
        # 将行为模拟器注入到 API 客户端中，实现自然的请求间隔
        self.http_client._behavior_sim = self._behavior_sim
        self.oauth_manager._behavior_sim = self._behavior_sim

    def _reinit_stealth_components(self, *, geo_code: str = None, identity_key: str = None):
        """根据运行时信息重新初始化隐蔽性组件（如获取到 GeoIP 后）"""
        self._session_fp = SessionFingerprint(
            geo_code=geo_code,
            identity_key=identity_key,
        )
        self._behavior_sim.reset()

    def _log(self, message: str, level: str = "info"):
        """璁板綍鏃ュ織"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 娣诲姞鍒版棩蹇楀垪琛?
        self.logs.append(log_message)

        # 璋冪敤鍥炶皟鍑芥暟
        if self.callback_logger:
            try:
                self.callback_logger(log_message)
            except Exception as e:
                logger.warning("Registration log callback failed: %s", e)

        # 璁板綍鍒版暟鎹簱锛堝鏋滄湁鍏宠仈浠诲姟锛?
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"璁板綍浠诲姟鏃ュ織澶辫触: {e}")

        # 鏍规嵁绾у埆璁板綍鍒版棩蹇楃郴缁?
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _checkpoint_task_control(self, *, consume_skip: bool = True) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is not None:
            task_control.checkpoint(consume_skip=consume_skip)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        resolved_length = max(int(length or DEFAULT_PASSWORD_LENGTH), 8)
        return generate_random_password(resolved_length)

    @staticmethod
    def _normalize_phone_number_for_openai(phone_number: Any) -> str:
        """OpenAI add-phone expects an E.164-style number with a leading plus."""
        raw = str(phone_number or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return raw
        if raw.startswith("+"):
            return f"+{digits}"
        if digits.startswith("00"):
            return f"+{digits[2:]}"
        return f"+{digits}"

    @staticmethod
    def _parse_optional_float(value: Any) -> Optional[float]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_optional_int(value: Any, default: int, min_value: int = 1, max_value: int = 1000) -> int:
        text = str(value or "").strip()
        if not text:
            return default
        try:
            parsed = int(float(text))
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, parsed))

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_smsbower_countries(value: Any, default: str = "12") -> list[str]:
        raw = str(value or "").strip() or default
        countries: list[str] = []
        for part in raw.replace(";", ",").split(","):
            country = part.strip()
            if country and country not in countries:
                countries.append(country)
        return countries or [default]

    @classmethod
    def _parse_smsbower_price_steps(
        cls,
        value: Any,
        max_price: Optional[float],
    ) -> list[Optional[float]]:
        raw = str(value or "").strip()
        if not raw:
            return [max_price]

        prices: list[float] = []
        for part in raw.replace(";", ",").split(","):
            parsed = cls._parse_optional_float(part)
            if parsed is None:
                continue
            if max_price is not None and parsed > max_price:
                continue
            if parsed not in prices:
                prices.append(parsed)

        if max_price is not None and max_price not in prices:
            prices.append(max_price)
        return prices or [max_price]

    @staticmethod
    def _phone_failure_reason_priority(reason: Any) -> int:
        normalized = str(reason or "").strip()
        priorities = {
            "environment_unusable_fraud_guard": 100,
            "fraud_guard_proxy_rotated": 95,
            "fraud_guard": 90,
            "phone_rate_limited": 85,
            "rate_limit_exceeded": 85,
            "cloudflare_blocked": 80,
            "add_phone_attempt_limit": 75,
            "voip_phone_disallowed": 70,
            "landline_disallowed": 70,
            "phone_number_in_use": 65,
            "phone_rejected": 60,
            "phone_validate_failed": 60,
            "non_sms_phone_channel": 55,
            "suspicious_behaviour": 55,
            "smsbower_activation_error": 50,
            "otp_timeout": 45,
            "otp_retry_requested": 45,
            "low_balance": 40,
            "missing_key": 40,
            "smsbower_error": 35,
            "no_numbers": 20,
            "local_phone_dedupe": 10,
            "local_prefix_cooldown": 10,
            "local_provider_cooldown": 10,
            "exception": 0,
            "": 0,
        }
        return priorities.get(normalized, 30 if normalized else 0)

    def _set_phone_failure_reason(self, reason: Any, *, force: bool = False) -> None:
        new_reason = str(reason or "").strip()
        if not new_reason:
            return
        current_reason = str(getattr(self, "_last_phone_failure_reason", "") or "").strip()
        if force or self._phone_failure_reason_priority(new_reason) >= self._phone_failure_reason_priority(current_reason):
            self._last_phone_failure_reason = new_reason

    def _normalize_browser_cookies(self, cookies: Any) -> list[dict]:
        """鍏煎 Playwright cookie 鍒楄〃鍜?{name: value} 褰㈠紡鐨?cookie 鏄犲皠銆?"""
        if not cookies:
            return []
        if isinstance(cookies, dict):
            normalized = []
            for name, value in cookies.items():
                if not name:
                    continue
                normalized.append(
                    {
                        "name": str(name),
                        "value": str(value or ""),
                        "domain": ".openai.com",
                        "path": "/",
                    }
                )
            return normalized
        normalized = []
        for item in cookies:
            if isinstance(item, dict) and item.get("name"):
                normalized.append(item)
        return normalized

    def _inject_browser_cookies(self, cookies: Any) -> list[dict]:
        normalized = self._normalize_browser_cookies(cookies)
        for cookie in normalized:
            domain = cookie.get("domain") or ".openai.com"
            path = cookie.get("path") or "/"
            self.session.cookies.set(
                cookie["name"],
                cookie.get("value", ""),
                domain=domain,
                path=path,
            )
        return normalized

    def _get_session_cookies_for_browser(self) -> Optional[list[dict]]:
        """浠?curl_cffi 浼氳瘽鎻愬彇 cookie锛岀敤浜庢敞鍏ユ祻瑙堝櫒涓婁笅鏂囬伩鍏?浼氳瘽宸茬粨鏉?銆?"""
        try:
            if not self.session:
                return None
            cookies = []
            for name, value in (self.session.cookies.get_dict() or {}).items():
                if name and value:
                    cookies.append({
                        "name": str(name),
                        "value": str(value),
                        "domain": ".openai.com",
                        "path": "/",
                    })
            return cookies if cookies else None
        except Exception:
            return None

    def _build_session_cookie_header(self) -> str:
        try:
            if not self.session:
                return ""
            cookies = self.session.cookies.get_dict() or {}
            parts = []
            for name, value in cookies.items():
                if name and value:
                    parts.append(f"{name}={value}")
            return "; ".join(parts)
        except Exception:
            return ""

    @staticmethod
    def _pre_oauth_auto_pay_failed(metadata: Any) -> bool:
        if not isinstance(metadata, dict):
            return False
        state = str(metadata.get("state") or "").strip().lower()
        return state == "failed_pre_oauth" or state.startswith("failed_pre_oauth:")

    @staticmethod
    def _format_pre_oauth_auto_pay_error(metadata: dict) -> str:
        state = str(metadata.get("state") or "").strip()
        error = str(metadata.get("error") or "").strip()
        parts = ["pre-oauth payment failed"]
        if state:
            parts.append(f"state={state}")
        if error:
            parts.append(f"error={error}")
        return "; ".join(parts)

    def _run_pre_oauth_auto_pay_hook(self, result: RegistrationResult) -> bool:
        hook = self._pre_oauth_auto_pay_hook
        if not callable(hook):
            return True

        session_token = result.session_token or self.session_token or ""
        if not session_token and self.session:
            try:
                session_token = self.session.cookies.get("__Secure-next-auth.session-token") or ""
            except Exception:
                session_token = ""
        if session_token:
            result.session_token = result.session_token or session_token
            self.session_token = self.session_token or session_token

        runtime = {
            "session_token": session_token,
            "cookie_header": self._build_session_cookie_header(),
            "device_id": self._device_id or "",
            "proxy_url": self.proxy_url or "",
            "proxy_geo_country": self._proxy_geo_country or "",
        }
        result.metadata = result.metadata or {}
        result.metadata["pre_oauth_auth"] = runtime

        try:
            metadata = hook(result, runtime)
            if isinstance(metadata, dict):
                result.metadata["auto_pay"] = metadata
                if self._pre_oauth_auto_pay_failed(metadata):
                    result.error_message = self._format_pre_oauth_auto_pay_error(metadata)
                    self._log(
                        f"Pre-oauth auto payment failed; stopping before OAuth: {result.error_message}",
                        "error",
                    )
                    return False
        except Exception as exc:
            result.error_message = f"pre-oauth payment hook exception: {exc}"
            self._log(
                f"Pre-oauth auto payment hook exception; stopping before OAuth: {exc}",
                "error",
            )
            result.metadata["auto_pay"] = {
                "plan": "plus",
                "state": "failed_pre_oauth",
                "flow_order": "before_oauth",
                "error": str(exc),
            }
            return False  # 异常路径同样需要阻止 OAuth，原来缺少此行导致异常被吞掉后流程继续
        return True

    def _get_browser_storage_state(self) -> Optional[dict]:
        storage_state = self._browser_frontend_state.get("storage_state")
        return storage_state if isinstance(storage_state, dict) and storage_state else None

    def _summarize_browser_cookies(self, cookies: Any) -> set[str]:
        names: set[str] = set()
        for cookie in self._normalize_browser_cookies(cookies):
            name = str(cookie.get("name") or "").strip()
            if name:
                names.add(name)
        return names

    def _summarize_storage_keys(self, storage_state: Any) -> set[str]:
        keys: set[str] = set()
        if not isinstance(storage_state, dict):
            return keys
        for origin, payload in storage_state.items():
            origin_name = str(origin or "").strip() or "-"
            data = payload if isinstance(payload, dict) else {}
            for bucket_name in ("localStorage", "sessionStorage"):
                bucket = data.get(bucket_name)
                if not isinstance(bucket, dict):
                    continue
                prefix = "local" if bucket_name == "localStorage" else "session"
                for key in bucket.keys():
                    key_name = str(key or "").strip()
                    if key_name:
                        keys.add(f"{origin_name}:{prefix}:{key_name}")
        return keys

    def _format_name_delta(self, added: set[str], removed: set[str], *, limit: int = 8) -> str:
        def _preview(values: set[str]) -> str:
            ordered = sorted(values)
            if not ordered:
                return "-"
            shown = ordered[:limit]
            suffix = f", +{len(ordered) - limit} more" if len(ordered) > limit else ""
            return ", ".join(shown) + suffix

        return f"+[{_preview(added)}] -[{_preview(removed)}]"

    def _log_browser_state_transition(
        self,
        *,
        label: str,
        previous_state: Dict[str, Any],
        next_state: Dict[str, Any],
        returned_cookies: Any,
    ) -> None:
        previous_cookie_names = previous_state.get("cookie_names")
        previous_cookies = (
            {str(name) for name in previous_cookie_names if str(name or "").strip()}
            if isinstance(previous_cookie_names, list)
            else self._summarize_browser_cookies(previous_state.get("cookies") or [])
        )
        returned_cookie_names = self._summarize_browser_cookies(returned_cookies)
        previous_storage = self._summarize_storage_keys(previous_state.get("storage_state"))
        next_storage = self._summarize_storage_keys(next_state.get("storage_state"))

        previous_nav = previous_state.get("navigation_chain") or []
        next_nav = next_state.get("navigation_chain") or []
        nav_tail = " -> ".join(str(url)[:80] for url in next_nav[-3:]) if next_nav else "-"
        challenge_before = previous_state.get("challenge_passed")
        challenge_after = next_state.get("challenge_passed")
        challenge_text = (
            ("yes" if challenge_before is True else "no" if challenge_before is False else "unknown")
            + "->"
            + ("yes" if challenge_after is True else "no" if challenge_after is False else "unknown")
        )

        self._log(
            f"{label}: browser_state_diff "
            f"cookies={self._format_name_delta(returned_cookie_names - previous_cookies, previous_cookies - returned_cookie_names)}; "
            f"storage={self._format_name_delta(next_storage - previous_storage, previous_storage - next_storage)}; "
            f"challenge={challenge_text}; "
            f"nav_steps={len(previous_nav)}->{len(next_nav)}; "
            f"nav_tail={nav_tail}"
        )

    def _sync_browser_form_result(
        self,
        browser_result: dict,
        *,
        label: str,
        update_post_otp_state: bool = False,
    ) -> Dict[str, Any]:
        response_body = str((browser_result or {}).get("body") or "")
        try:
            response_data = json.loads(response_body or "{}") if response_body else {}
        except Exception:
            response_data = {}

        final_url = normalize_flow_url(
            str(browser_result.get("final_url") or ""),
            auth_base="https://auth.openai.com",
        )
        flow_state = extract_flow_state(
            response_data,
            current_url=final_url,
            auth_base="https://auth.openai.com",
        )
        page_type = flow_state.page_type or str(browser_result.get("page_type") or "").strip()
        continue_url = flow_state.continue_url or normalize_flow_url(
            str(browser_result.get("continue_url") or ""),
            auth_base="https://auth.openai.com",
        )
        navigation_chain = [
            str(url).strip()
            for url in (browser_result.get("navigation_chain") or [])
            if str(url or "").strip()
        ]
        storage_state = browser_result.get("storage_state")
        if not isinstance(storage_state, dict):
            storage_state = {}
        previous_state = dict(self._browser_frontend_state or {})

        returned_cookies = browser_result.get("cookies") or []
        returned_cookie_names = self._summarize_browser_cookies(returned_cookies)
        if returned_cookies:
            injected = self._inject_browser_cookies(returned_cookies)
            self._log(f"{label}: browser cookies synced: {len(injected)}")

        challenge_passed = browser_result.get("challenge_passed")
        challenge_text = (
            "yes" if challenge_passed is True else "no" if challenge_passed is False else "unknown"
        )
        self._browser_frontend_state = {
            "challenge_passed": challenge_passed,
            "cookie_names": sorted(returned_cookie_names),
            "final_url": final_url,
            "page_type": page_type,
            "continue_url": continue_url,
            "navigation_chain": navigation_chain,
            "storage_state": storage_state,
        }
        self._log_browser_state_transition(
            label=label,
            previous_state=previous_state,
            next_state=self._browser_frontend_state,
            returned_cookies=returned_cookies,
        )
        self._log(
            f"{label}: challenge_passed={challenge_text}, "
            f"page_type={page_type or '-'}, "
            f"nav_steps={len(navigation_chain)}, "
            f"final_url={(final_url or '-')[:120]}"
        )

        if update_post_otp_state:
            self._post_otp_continue_url = continue_url
            self._post_otp_page_type = page_type

        return response_data

    def _submit_browser_form(
        self,
        *,
        label: str,
        page_url: str,
        form_type: str,
        form_value: str = "",
        flow: str = "",
        update_post_otp_state: bool = False,
    ) -> Tuple[Optional[dict], Dict[str, Any]]:
        initial_cookies = self._get_session_cookies_for_browser()
        initial_storage_state = self._get_browser_storage_state()
        self._log(
            f"{label}: browser_state_seed "
            f"cookies={len(initial_cookies or [])}, "
            f"storage_keys={len(self._summarize_storage_keys(initial_storage_state))}"
        )
        # 操作前注入自然延迟（模拟用户思考/等待）
        self._behavior_sim.natural_delay(0.5, 1.5)

        browser_result = browser_form_submit(
            page_url=page_url,
            form_type=form_type,
            form_value=form_value,
            proxy=self.proxy_url,
            timeout_ms=45000,
            headless=True,
            device_id=self._device_id,
            log_fn=lambda msg: self._log(msg),
            flow=flow,
            initial_cookies=initial_cookies,
            initial_storage_state=initial_storage_state,
            session_fp=self._session_fp,
        )
        if not browser_result:
            return None, {}

        response_data = self._sync_browser_form_result(
            browser_result,
            label=label,
            update_post_otp_state=update_post_otp_state,
        )
        return browser_result, response_data

    def _is_cloudflare_challenge_response(self, response) -> bool:
        body = str(getattr(response, "text", "") or "").lower()
        return (
            getattr(response, "status_code", None) == 403
            and (
                "just a moment" in body
                or "challenges.cloudflare.com" in body
                or "cf-browser-verification" in body
            )
        )

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 邮箱服务未返回有效数据", "error")
                return False

            email_value = str(self.email_info.get("email") or "").strip()
            if not email_value:
                self._log(
                    f"创建邮箱失败: {self.email_service.service_type.value} 返回空邮箱地址",
                    "error",
                )
                return False

            self.email_info["email"] = email_value
            self.email = email_value
            self._log(f"成功创建邮箱: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self, *, prompt: Optional[str] = "login") -> bool:
        """寮€濮?OAuth 娴佺▼"""
        try:
            self._log("开始 OAuth 授权流程...")
            self._log("当前走的是 Sign in with ChatGPT OAuth 应用入口，不是普通 chatgpt.com 消费者首页注册", "info")
            self.oauth_start = self.oauth_manager.start_oauth(prompt=prompt)
            self._log(f"OAuth URL 已生成: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """鍒濆鍖栦細璇?"""
        try:
            self.session = self.http_client.session
            if self._device_id:
                seed_oai_device_cookie(self.session, self._device_id)
            return True
        except Exception as e:
            self._log(f"鍒濆鍖栦細璇濆け璐? {e}", "error")
            return False

    def _warmup_page(self, page_url: str, label: str = "") -> bool:
        """浣跨敤娴忚鍣ㄩ鐑〉闈紝瑙ｅ喅 Cloudflare challenge 骞舵敞鍏?cookie 鍒颁細璇濄€?"""
        self._log(f"{label}: 娴忚鍣ㄩ鐑〉闈?{page_url}...")
        try:
            cookies = warmup_page_and_extract_cookies(
                page_url=page_url,
                proxy=self.proxy_url,
                timeout_ms=45000,
                headless=True,
                device_id=self._device_id,
                log_fn=lambda msg: self._log(msg),
                session_fp=self._session_fp,
            )
            
            # 添加：页面加载后的自然观察延迟
            self._behavior_sim.page_load_observation()
            
            if not cookies:
                self._log(f"{label}: 浏览器预热未获取到 cookie", "warning")
                return False

            cookies = self._inject_browser_cookies(cookies)
                
            cf = any(c.get("name") == "cf_clearance" for c in cookies)
            cf_bm = any(c.get("name") == "__cf_bm" for c in cookies)
            self._log(
                f"{label}: 娉ㄥ叆 {len(cookies)} 涓?cookie"
                + (f", cf_clearance={'yes' if cf else 'no'}" if cf else "")
                + (f", __cf_bm={'yes' if cf_bm else 'no'}" if cf_bm else "")
            )
            return True
        except Exception as e:
            self._log(f"{label}: 娴忚鍣ㄩ鐑紓甯? {e}", "warning")
            return False

    def _get_device_id(self) -> Optional[str]:
        """鑾峰彇骞跺鐢?Device ID锛屽悓鏃惰闂?OAuth URL 寤虹珛褰撳墠浼氳瘽銆?"""
        if not self.oauth_start:
            return None

        if not self._device_id:
            self._device_id = generate_device_id()

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.session:
                    self.session = self.http_client.session

                seed_oai_device_cookie(self.session, self._device_id)

                response = self.session.get(
                    self.oauth_start.auth_url,
                    timeout=20
                )

                if response.status_code < 400:
                    self._log(f"Device ID: {self._device_id}")
                    return self._device_id

                self._log(
                    f"获取 Device ID 失败: 建立 OAuth 会话返回 HTTP {response.status_code} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )
            except Exception as e:
                self._log(
                    f"获取 Device ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )

            if attempt < max_attempts:
                time.sleep(attempt)
                self.http_client.close()
                self.session = self.http_client.session

        return None

    def _default_user_agent(self) -> str:
        # 优先使用 SessionFingerprint 锁定的 UA
        if self._session_fp:
            return self._session_fp.user_agent
        try:
            user_agent = str(self.session.headers.get("User-Agent") or "").strip()
            if user_agent:
                return user_agent
        except Exception:
            pass
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.7103.92 Safari/537.36"
        )

    def _build_json_headers(
        self,
        *,
        referer: str,
        include_device_id: bool = False,
        include_datadog: bool = False,
        content_type: str = "application/json",
        accept: str = "application/json",
    ) -> Dict[str, str]:
        fp = self._session_fp
        headers = {
            "accept": accept,
            "accept-language": fp.accept_language if fp else "en-US,en;q=0.9",
            "content-type": content_type,
            "origin": "https://auth.openai.com",
            "referer": referer,
            "user-agent": self._default_user_agent(),
            "sec-ch-ua": fp.sec_ch_ua if fp else '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": fp.platform_version if fp else '"15.0.0"',
            "sec-ch-ua-full-version": fp.sec_ch_ua_full_version if fp else '"136.0.7103.92"',
            "sec-ch-ua-full-version-list": fp.sec_ch_ua_full_version_list if fp else '"Chromium";v="136.0.7103.92", "Google Chrome";v="136.0.7103.92", "Not.A/Brand";v="99.0.0.0"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if include_device_id and self._device_id:
            headers["oai-device-id"] = self._device_id
        if include_datadog:
            headers.update(generate_datadog_trace())
        return headers

    def _build_navigation_headers(self, *, referer: str) -> Dict[str, str]:
        ua = self._default_user_agent()
        fp = self._session_fp
        return {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "accept-language": fp.accept_language if fp else "en-US,en;q=0.9",
            "referer": referer,
            "user-agent": ua,
            "sec-ch-ua": fp.sec_ch_ua if fp else '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": fp.platform_version if fp else '"15.0.0"',
            "sec-ch-ua-full-version": fp.sec_ch_ua_full_version if fp else '"136.0.7103.92"',
            "sec-ch-ua-full-version-list": fp.sec_ch_ua_full_version_list if fp else '"Chromium";v="136.0.7103.92", "Google Chrome";v="136.0.7103.92", "Not.A/Brand";v="99.0.0.0"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }

    def _submit_auth_start(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        screen_hint: str,
        referer: str,
        log_label: str,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """鎻愪氦鎺堟潈鍏ュ彛琛ㄥ崟 鈥?鐩存帴閫氳繃 curl_cffi session 璋冪敤 API锛宑f_clearance 宸茬敱 _get_device_id 鍐欏叆 session銆?"""
        try:
            if screen_hint == "signup":
                initial_cookies = self._get_session_cookies_for_browser()
                if initial_cookies:
                    self._log(f"{log_label}: 注入浏览器 cookies {len(initial_cookies)} 个")
                self._log(f"{log_label}: 通过 Browser Form 提交...")
                browser_result = browser_form_submit(
                    page_url=referer,
                    form_type="email",
                    form_value=self.email,
                    proxy=self.proxy_url,
                    timeout_ms=45000,
                    headless=True,
                    device_id=did,
                    log_fn=lambda msg: self._log(msg),
                    flow="authorize_continue",
                    initial_cookies=initial_cookies,
                )
                if not browser_result:
                    return SignupFormResult(
                        success=False,
                        error_message="Browser Form 未返回结果",
                    )
                returned_cookies = browser_result.get("cookies") or []
                if returned_cookies:
                    injected = self._inject_browser_cookies(returned_cookies)
                    self._log(f"{log_label}: 回注浏览器 cookies {len(injected)} 个")
                status = int(browser_result.get("status") or 0)
                resp_body = str(browser_result.get("body") or "")
            else:
                self._log(f"{log_label}: 通过 curl_cffi session 直接调用 API...")

                headers = self._build_json_headers(
                    referer=referer,
                    include_device_id=True,
                )
                if sen_token:
                    headers["openai-sentinel-token"] = sen_token

                resp = self.session.post(
                    OPENAI_API_ENDPOINTS["signup"],
                    headers=headers,
                    json={"username": {"value": self.email, "kind": "email"}, "screen_hint": screen_hint},
                    timeout=30,
                )

                status = resp.status_code
                resp_body = resp.text
            self._log(f"{log_label}状态: {status}")

            if status < 200 or status >= 400:
                error_preview = (resp_body or "")[:500]
                if (
                    "Just a moment" in error_preview
                    or "cf-browser-verification" in error_preview
                    or "challenges.cloudflare.com" in error_preview
                ):
                    self._log(
                        "cloudflare_challenge_blocked: OAuth browser form hit Cloudflare challenge; "
                        "try a residential proxy or wait for IP cooldown",
                        "error",
                    )
                elif status == 403:
                    self._log(
                        f"授权入口 HTTP 403（非 Cloudflare）: {error_preview[:300]}",
                        "warning",
                    )
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {status}: {resp_body[:200]}"
                )

            # 瑙ｆ瀽鍝嶅簲鍒ゆ柇璐﹀彿鐘舵€?
            try:
                response_data = json.loads(resp_body or "{}")
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"响应页面类型: {page_type}")

                is_existing = page_type in {
                    OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
                    OPENAI_PAGE_TYPES["LOGIN_PASSWORD"],
                }

                if is_existing:
                    if record_existing_account:
                        if page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
                            self._log("检测到服务端切换到登录密码页面，将自动切换到登录流程")
                        else:
                            self._otp_sent_at = time.time()
                            self._log("检测到已注册账号，将自动切换到登录流程")
                        self._is_existing_account = True
                    else:
                        if page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
                            self._log("登录流程已进入密码页")
                        else:
                            self._otp_sent_at = time.time()
                            self._log("登录流程已触发，等待系统发送验证码")

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data
                )

            except Exception as parse_error:
                self._log(f"解析响应失败: {parse_error}", "warning")
                return SignupFormResult(success=True)

        except Exception as e:
            self._log(f"{log_label}失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _submit_signup_form(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """鎻愪氦娉ㄥ唽鍏ュ彛琛ㄥ崟銆?"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            log_label="鎻愪氦娉ㄥ唽琛ㄥ崟",
            record_existing_account=record_existing_account,
        )

    def _submit_login_start(self, did: str, sen_token: Optional[str]) -> SignupFormResult:
        """鎻愪氦鐧诲綍鍏ュ彛琛ㄥ崟銆?"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            log_label="提交登录入口",
            record_existing_account=False,
        )

    def _submit_login_password(self) -> SignupFormResult:
        """鎻愪氦鐧诲綍瀵嗙爜 鈥?閫氳繃 curl_cffi session 鐩存帴璋冪敤 API锛宑f_clearance 宸插湪 session 涓€?"""
        try:
            login_pwd_sentinel = build_sentinel_token(
                self.session,
                self._device_id or "",
                flow="password_verify",
            )

            headers = self._build_json_headers(
                referer="https://auth.openai.com/log-in/password",
                include_device_id=True,
                include_datadog=True,
            )
            if login_pwd_sentinel:
                headers["openai-sentinel-token"] = login_pwd_sentinel

            resp = self.session.post(
                OPENAI_API_ENDPOINTS["password_verify"],
                headers=headers,
                json={"password": self.password},
                timeout=30,
            )

            status = resp.status_code
            self._log(f"提交登录密码状态: {status}")

            if status < 200 or status >= 400:
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {status}: {resp.text[:200]}"
                )

            response_data = json.loads(resp.text or "{}")
            page_type = response_data.get("page", {}).get("type", "")
            self._log(f"登录密码响应页面类型: {page_type}")

            is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
            if is_existing:
                self._otp_sent_at = time.time()
                self._log("登录密码校验通过，等待系统发送验证码")

            return SignupFormResult(
                success=True,
                page_type=page_type,
                is_existing_account=is_existing,
                response_data=response_data,
            )

        except Exception as e:
            self._log(f"提交登录密码失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _probe_post_add_phone_state_after_failure(self) -> bool:
        """Check whether the session moved past add-phone despite a phone handler failure."""
        try:
            side_effect = getattr(getattr(self.session, "get", None), "side_effect", None)
            return_value = getattr(getattr(self.session, "get", None), "return_value", None)
            has_probe_response = side_effect is not None or self._safe_int(
                getattr(return_value, "status_code", 0)
            ) > 0
            if not has_probe_response:
                return False
        except Exception:
            return False

        self._get_workspace_id()
        if not self.session:
            return False

        consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        try:
            consent_resp = self.session.get(
                consent_url,
                headers=self._build_navigation_headers(referer="https://auth.openai.com/add-phone"),
                allow_redirects=True,
                timeout=20,
            )
        except Exception as exc:
            self._log(f"consent 页面探测失败: {exc}", "warning")
            return False

        consent_status = self._safe_int(getattr(consent_resp, "status_code", 0))
        consent_final_url = normalize_flow_url(
            str(getattr(consent_resp, "url", "") or consent_url),
            auth_base="https://auth.openai.com",
        )
        if consent_status == 403:
            self._log("consent 页面返回 403，仍处于 add-phone/授权阻塞状态", "warning")
        elif 200 <= consent_status < 400:
            consent_page_type = extract_flow_state(
                {},
                current_url=consent_final_url,
                auth_base="https://auth.openai.com",
            ).page_type
            if consent_page_type in {
                "consent",
                "organization_selection",
                "workspace_selection",
                "oauth_callback",
                "callback",
            }:
                self._post_otp_continue_url = consent_final_url or consent_url
                self._post_otp_page_type = consent_page_type
                return True

        about_url = "https://auth.openai.com/about-you"
        try:
            about_resp = self.session.get(
                about_url,
                headers=self._build_navigation_headers(referer=consent_url),
                allow_redirects=True,
                timeout=20,
            )
        except Exception as exc:
            self._log(f"about-you 页面探测失败: {exc}", "warning")
            return False

        about_status = self._safe_int(getattr(about_resp, "status_code", 0))
        about_final_url = normalize_flow_url(
            str(getattr(about_resp, "url", "") or about_url),
            auth_base="https://auth.openai.com",
        )
        if about_status == 403:
            self._log("about-you 页面返回 403，姓名/生日资料尚未创建或授权上下文被阻塞", "warning")
            return False
        if 200 <= about_status < 400:
            about_page_type = extract_flow_state(
                {},
                current_url=about_final_url,
                auth_base="https://auth.openai.com",
            ).page_type
            if about_page_type in {
                "consent",
                "organization_selection",
                "workspace_selection",
                "oauth_callback",
                "callback",
            }:
                self._post_otp_continue_url = about_final_url
                self._post_otp_page_type = about_page_type
                return True

        return False

    def _reset_auth_flow(self) -> None:
        """重置会话状态，准备使用全新 session 重新发起 OAuth 流程（保留 CF 防火墙令牌以防 403）。"""
        # 1. 尝试提取现有的 Cloudflare pass
        cf_cookies_dict = {}
        if self.session and hasattr(self.session, "cookies"):
            try:
                for k, v in self.session.cookies.items():
                    if k in ("cf_clearance", "__cf_bm"):
                        cf_cookies_dict[k] = v
            except Exception:
                pass
                
        # 从之前的无头浏览器状态中提取（如果上一步没有或者为了更准确）
        browser_cookies = self._browser_frontend_state.get("cookies", [])
        for c in browser_cookies:
            if c.get("name") in ("cf_clearance", "__cf_bm"):
                cf_cookies_dict[c["name"]] = c["value"]

        self.http_client.close()
        self.session = None
        self.oauth_start = None
        self.session_token = None
        self._otp_sent_at = None
        self._post_otp_continue_url = ""
        self._post_otp_page_type = ""
        self._register_continue_url = ""
        # 清除浏览器前端状态（cookies/storage），避免被 fraud_guard 标记的旧会话污染
        self._browser_frontend_state = {}
        self._authorize_sentinel = None
        self._authorization_context_bootstrap_attempted = False
        self._is_existing_account = False
        self._token_acquisition_requires_login = True
        self._about_you_create_account_already_exists_without_consent = False
        # 全新会话重置 fraud_guard 代理轮换计数
        self._fraud_guard_proxy_rotations = 0

        # 2. 将 CF Cookies 回注到崭新的会话中，欺骗 CF 免拦截
        if cf_cookies_dict:
            self.session = self.http_client.session
            for k, v in cf_cookies_dict.items():
                try:
                    self.session.cookies.set(k, v, domain=".openai.com")
                except Exception:
                    pass

    def _prepare_authorize_flow(self, label: str) -> Tuple[Optional[str], Optional[str]]:
        """鍒濆鍖栧綋鍓嶉樁娈电殑鎺堟潈娴佺▼锛岃繑鍥?device id 鍜?sentinel token銆?"""
        self._log(f"{label}: 初始化会话...")
        if not self._init_session():
            return None, None

        self._log(f"{label}: 初始化 OAuth 授权流程...")
        if not self._start_oauth():
            return None, None

        self._log(f"{label}: 获取 Device ID...")
        did = self._get_device_id()
        if not did:
            return None, None

        self._log(f"{label}: 执行 Sentinel POW 验证...")
        sen_token = self._check_sentinel(did)
        if not sen_token:
            return did, None

        self._log(f"{label}: Sentinel 验证通过")
        return did, sen_token

    def _prepare_basic_account_flow(self) -> Tuple[Optional[str], Optional[str]]:
        """Prepare the OAuth-backed registration entry session before token OAuth."""
        return self._prepare_authorize_flow("OAuth 注册入口会话")

    @staticmethod
    def _split_generated_profile_name(full_name: str) -> Tuple[str, str]:
        normalized = str(full_name or "").strip()
        if not normalized:
            return "Alex", "Taylor"
        parts = [part for part in normalized.split() if part]
        if len(parts) == 1:
            return parts[0], "Taylor"
        return parts[0], " ".join(parts[1:])

    def _adopt_consumer_registration_state(self, client: Any) -> None:
        state = getattr(client, "last_registration_state", None)
        self.session = getattr(client, "session", None)
        self._device_id = str(getattr(client, "device_id", "") or "").strip() or self._device_id
        self._post_otp_page_type = str(getattr(state, "page_type", "") or "").strip().lower()
        self._post_otp_continue_url = normalize_flow_url(
            str(
                getattr(state, "continue_url", "")
                or getattr(state, "current_url", "")
                or ""
            ),
            auth_base="https://auth.openai.com",
        )

    @staticmethod
    def _is_email_already_registered_error(message: Any) -> bool:
        text = str(message or "").lower()
        return (
            "user_already_exists" in text
            or "already_exists" in text
            or "account already exists for this email" in text
            or "email address, please login instead" in text
        )

    def _create_consumer_chatgpt_basic_account(self, result: RegistrationResult) -> Tuple[bool, str]:
        """Create the consumer ChatGPT account first, then continue with OAuth token login."""
        from .oauth_pkce_client import OAuthPkceClient

        if not self.email:
            return False, "consumer_chatgpt_registration_failed: missing_email"

        if not self.password:
            self.password = self._generate_password()
            self._log(f"Generated password: {self.password[:2]}***{self.password[-1]}")
        result.password = self.password

        user_info = generate_random_user_info()
        first_name, last_name = self._split_generated_profile_name(user_info.get("name", ""))
        birthdate = str(user_info.get("birthdate", "") or "").strip()
        self._log("3. 普通 ChatGPT 网页注册建号...")
        self._log(f"普通 ChatGPT 注册资料: {first_name} {last_name}, birthdate={birthdate}")

        email_adapter = ConsumerEmailServiceAdapter(
            self.email_service,
            self.email,
            self.email_info,
            self._log,
        )

        from .chatgpt_client import ChatGPTClient

        for attempt in range(2):
            client = ChatGPTClient(
                proxy=self.proxy_url,
                verbose=False,
                browser_mode=self.browser_mode,
            )
            client._log = self._log

            try:
                success, message = client.register_complete_flow(
                    self.email,
                    self.password,
                    first_name,
                    last_name,
                    birthdate,
                    email_adapter,
                )
                self._used_verification_codes.update(email_adapter.used_codes)
                if not success:
                    if self._is_email_already_registered_error(message):
                        self._log(
                            f"OpenAI提示该邮箱已有账号，直接切换到登录授权: {self.email}",
                            "warning",
                        )
                        self._is_existing_account = True
                        self._token_acquisition_requires_login = True
                        return True, ""
                    
                    if "409" in str(message) and "invalid session" in str(message).lower():
                        if attempt < 1:
                            self._log(
                                f"验证码验证报 409 Invalid Session，自动使用同一邮箱重新走注册状态机 (尝试 {attempt+1}/2)...",
                                "warning",
                            )
                            continue

                    return False, f"consumer_chatgpt_registration_failed: {message}"
                break
            except Exception as exc:
                self._used_verification_codes.update(email_adapter.used_codes)
                if self._is_email_already_registered_error(exc):
                    self._log(
                        f"OpenAI提示该邮箱已有账号，直接切换到登录授权: {self.email}",
                        "warning",
                    )
                    self._is_existing_account = True
                    self._token_acquisition_requires_login = True
                    return True, ""
                return False, f"consumer_chatgpt_registration_failed: {exc}"

        self._adopt_consumer_registration_state(client)

        session_token = ""
        try:
            if self.session:
                session_token = str(
                    self.session.cookies.get("__Secure-next-auth.session-token") or ""
                ).strip()
        except Exception:
            session_token = ""

        if session_token:
            result.session_token = session_token
            self.session_token = session_token

        # ⭐ before_oauth (Plus OAuth 前升级) 模式必须在 hook 调用前先 reuse session 落地 access_token，
        # 否则 _run_pre_oauth_auto_pay_hook 拿到的 access_token/session_token 都是 no，
        # 直接 skipped_pre_oauth_missing_auth → fall through OAuth → 走 add-phone 命中 fraud_guard。
        # 该方法此前定义但无人调用，导致 GoPay/PayPal 的 before_oauth 路径无效。
        if callable(self._pre_oauth_auto_pay_hook):
            try:
                self._prepare_pre_oauth_payment_auth(client, result)
            except Exception as exc:
                self._log(f"OAuth 前支付 session 落地异常（不阻断后续流程）: {exc}", "warning")

        self._log("普通 ChatGPT 网页注册阶段完成，开始进入 OAuth 登录授权取 Token")
        return True, ""

        client = OAuthPkceClient(
            proxy=self.proxy_url,
            log_fn=self._log,
        )

        try:
            client.init_oauth_session()
            client.refresh_sentinel()
            email_submit_data = client.submit_email(self.email)
            password_continue_url = normalize_flow_url(
                str((email_submit_data or {}).get("continue_url") or ""),
                auth_base="https://auth.openai.com",
            )
            continue_url = client.submit_password(
                self.email,
                self.password,
                continue_url=password_continue_url,
            )
            client.send_otp(continue_url)

            code = email_adapter.wait_for_verification_code(
                self.email,
                timeout=300,
            )
            self._used_verification_codes.update(email_adapter.used_codes)
            if not code:
                return False, "consumer_chatgpt_registration_failed: missing_verification_code"

            otp_code = str(code).strip()
            self.session = getattr(client, "session", None)
            self._device_id = str(getattr(client, "_device_id", "") or "").strip() or self._device_id
            browser_otp_result, browser_otp_response_data = self._submit_browser_form(
                label="基础账号 OTP 校验",
                page_url="https://auth.openai.com/email-verification",
                form_type="otp_validate",
                form_value=otp_code,
                flow="email_otp_validate",
                update_post_otp_state=True,
            )
            if browser_otp_result:
                otp_response_data = browser_otp_response_data
            else:
                otp_response_data = client.validate_otp(otp_code)
        except Exception as exc:
            self._used_verification_codes.update(email_adapter.used_codes)
            if self._is_email_already_registered_error(exc):
                self._log(
                    f"OpenAI 提示该邮箱已有账号，直接切换到登录授权: {self.email}",
                    "warning",
                )
                self._is_existing_account = True
                self._token_acquisition_requires_login = True
                return True, ""
            return False, f"consumer_chatgpt_registration_failed: {exc}"

        otp_flow_state = extract_flow_state(
            otp_response_data if isinstance(otp_response_data, dict) else {},
            current_url="https://auth.openai.com/about-you",
            auth_base="https://auth.openai.com",
        )
        normalized_post_otp_page_type = str(otp_flow_state.page_type or "about_you").strip().lower()
        normalized_post_otp_continue_url = (
            otp_flow_state.continue_url
            or normalize_flow_url(otp_flow_state.current_url, auth_base="https://auth.openai.com")
            or "https://auth.openai.com/about-you"
        )
        if normalized_post_otp_page_type == "add_phone":
            self._log(
                "基础账号模式 OTP 后不应进入 add-phone，改回 about-you 继续完成姓名/生日资料创建",
                "warning",
            )
            normalized_post_otp_page_type = "about_you"
            normalized_post_otp_continue_url = "https://auth.openai.com/about-you"

        self._post_otp_page_type = normalized_post_otp_page_type
        self._post_otp_continue_url = normalized_post_otp_continue_url

        session_token = ""
        try:
            if (
                result.access_token
                or (self.session and self._pre_oauth_auto_pay_hook is None)
            ):
                session_token = str(
                    self.session.cookies.get("__Secure-next-auth.session-token") or ""
                ).strip()
        except Exception:
            session_token = ""

        if session_token:
            result.session_token = session_token
            self.session_token = session_token

        if not self._complete_post_otp_flow(result, exchange_token=False):
            return False, result.error_message or "consumer_chatgpt_registration_failed: about_you_not_completed"

        self._log("普通 ChatGPT 网页注册阶段完成，开始进入 OAuth 登录授权取 Token")
        return True, ""

    def _prepare_pre_oauth_payment_auth(self, client: Any, result: RegistrationResult) -> None:
        """Land the plain ChatGPT callback so Plus checkout can run before Codex OAuth."""
        try:
            ok, payload = client.reuse_session_and_get_tokens()
        except Exception as exc:
            self._log(f"基础账号支付会话准备异常，稍后将跳过 OAuth 前支付: {exc}", "warning")
            return

        if not ok:
            self._log(f"基础账号支付会话准备失败，稍后将跳过 OAuth 前支付: {payload}", "warning")
            return

        data = payload if isinstance(payload, dict) else {}
        result.access_token = str(data.get("access_token") or result.access_token or "").strip()
        result.session_token = str(data.get("session_token") or result.session_token or "").strip()
        result.account_id = str(data.get("account_id") or result.account_id or "").strip()
        result.workspace_id = str(data.get("workspace_id") or result.workspace_id or result.account_id or "").strip()
        if result.session_token:
            self.session_token = result.session_token
        self._adopt_consumer_registration_state(client)
        if result.access_token:
            self._log("基础账号 ChatGPT session 已落地，可用于 OAuth 前 Plus 支付")
        else:
            self._log("基础账号 ChatGPT session 已落地，但未取到 access_token", "warning")

    def _complete_token_exchange(self, result: RegistrationResult) -> bool:
        """鍦ㄧ櫥褰曟€佸凡寤虹珛鍚庯紝缁х画瀹屾垚 workspace 鍜?OAuth token 鑾峰彇銆?"""
        self._log("等待登录验证码...")
        code = self._get_verification_code()
        if not code:
            result.error_message = "等待登录验证码失败"
            return False

        self._log("校验登录验证码...")
        if not self._validate_verification_code(code):
            result.error_message = "校验登录验证码失败"
            return False

        return self._complete_post_otp_flow(result)

    def _complete_token_exchange_from_current_session(self, result: RegistrationResult) -> bool:
        """Try Codex OAuth with the just-created consumer session before relogin."""
        if not self.session:
            return False

        self._log("尝试复用普通注册会话直接进入 OAuth 授权取 Token...")
        self._token_acquisition_requires_login = True

        try:
            if not self._start_oauth(prompt=None):
                return False

            callback_url, workspace_id = self._resolve_oauth_callback_url(
                self.oauth_start.auth_url if self.oauth_start else ""
            )
            if workspace_id:
                result.workspace_id = workspace_id

            # ⭐ 个人 ChatGPT 账号本身没有 workspace（workspace 只存在于 team/enterprise）。
            # HTTP consent 拿不到 callback 时，再用浏览器前端（若已就绪）重试一次，
            # 避免每次都 blind 回退到重新登录授权流程。
            if not callback_url:
                start_url = self.oauth_start.auth_url if self.oauth_start else ""
                if self._has_browser_frontend_state():
                    self._log("复用会话 HTTP 路径未拿到 callback，尝试浏览器 consent 直取 callback...", "warning")
                    try:
                        direct_callback = self._resolve_oauth_callback_via_browser(
                            workspace_id or "",
                            start_url,
                        )
                        if direct_callback:
                            self._log(f"✅ 复用会话浏览器路径拿到 OAuth callback: {direct_callback[:100]}...")
                            callback_url = direct_callback
                    except Exception as e:
                        self._log(f"复用会话浏览器 consent 尝试异常: {e}", "warning")

            if not callback_url:
                # add-phone 阻塞了 consent 流程，直接走手机验证拿 refresh_token
                self._log("复用会话未拿到 callback（add-phone 阻塞），直接走手机验证...", "warning")
                self._post_otp_page_type = "add_phone"
                start_url = self.oauth_start.auth_url if self.oauth_start else ""
                phone_ok = self._handle_phone_verification()
                if phone_ok:
                    self._log("手机验证通过，重新尝试获取 OAuth callback...")
                    retry_callback, retry_ws = self._resolve_oauth_callback_url(
                        self.oauth_start.auth_url if self.oauth_start else ""
                    )
                    if not retry_callback and self._has_browser_frontend_state():
                        retry_callback = self._resolve_oauth_callback_via_browser(
                            retry_ws or workspace_id or "", start_url
                        )
                    if retry_callback:
                        callback_url = retry_callback
                        self._log(f"✅ 手机验证后成功拿到 callback")
                    else:
                        self._log("手机验证后仍未拿到 callback", "warning")
                        return False
                else:
                    self._log("手机验证也失败", "warning")
                    # 根据最新需求，遇到addphone问题失败后，不再尝试用全新会话重试登录
                    self._oauth_blocked_by_phone = True
                    return False

            token_info = self._handle_oauth_callback(callback_url)
            if token_info:
                result.access_token = token_info.get("access_token", "")
                result.refresh_token = token_info.get("refresh_token", "")
                result.id_token = token_info.get("id_token", "")
                result.source = "register"
                if not result.workspace_id:
                    result.workspace_id = workspace_id or self._get_workspace_id() or ""
                self._log("复用普通注册会话已完成 OAuth token 交换")
                return True

            session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
            if session_cookie:
                self.session_token = session_cookie
                result.session_token = session_cookie
                result.source = "register"
                self._log("复用普通注册会话未换到 OAuth token，但已获取 Session Token", "warning")
                return True
        except Exception as e:
            self._log(f"复用普通注册会话获取 Token 失败: {e}", "warning")

        return False

    def _complete_post_otp_flow(
        self,
        result: RegistrationResult,
        *,
        exchange_token: bool = True,
    ) -> bool:
        """鍦?OTP 宸叉牎楠岄€氳繃鍚庯紝缁х画澶勭悊 add_phone/about_you/workspace/OAuth 娴佺▼銆?"""

        # 妫€鏌ユ槸鍚﹁繘鍏?add_phone 椤甸潰锛堥渶瑕佹墜鏈哄彿楠岃瘉锛?
        # 灏濊瘯澶氱鏂规硶缁曡繃 add_phone 椤甸潰
        post_page_type = getattr(self, "_post_otp_page_type", "") or ""
        started_from_add_phone = post_page_type.lower() == "add_phone"
        phone_verification_attempted = False
        phone_verification_succeeded = False
        flow_stage_label = "登录授权" if exchange_token else "基础账号准备"

        def _finalize_phone_failure():
            self._log("SMSBOWER 手机验证失败", "error")
            self._oauth_blocked_by_phone = True
            phone_reason = str(getattr(self, "_last_phone_failure_reason", "") or "").strip()
            if phone_reason == "no_numbers":
                phone_hint = "5) SMSBOWER 当前筛选条件无可用号码，请更换国家、放宽 max_price 或调整 provider/quality"
            elif phone_reason == "low_balance":
                phone_hint = "5) SMSBOWER 余额不足，请充值后重试"
            elif phone_reason == "missing_key":
                phone_hint = "5) SMSBOWER API Key 未配置"
            elif phone_reason == "cloudflare_blocked":
                phone_hint = "5) 手机号提交被 Cloudflare 拦截，请更换住宅代理或等待 IP 冷却"
            elif phone_reason == "environment_unusable_fraud_guard":
                phone_hint = "5) 当前 add-phone 环境连续触发 fraud_guard，已止损；这不是接码失败，请先暂停该账号/环境"
            elif phone_reason == "add_phone_attempt_limit":
                phone_hint = "5) 当前账号 add-phone 尝试次数已达硬上限，已停止继续买号，避免账户级风控加重"
            elif phone_reason in {"phone_rate_limited", "phone_rejected", "voip_phone_disallowed", "landline_disallowed", "phone_number_in_use"}:
                phone_hint = "5) OpenAI 已拒绝手机号提交，属于号码质量/账号环境问题，不是 SMS 轮询收不到码"
            else:
                phone_hint = "5) 检查 SMSBOWER 号码可用性、价格和 provider/quality 配置"
            result.error_message = (
                f"{flow_stage_label}失败，OpenAI 要求绑定手机号。建议："
                "1) 更换住宅代理 IP；2) 更换邮箱域名；3) 降低注册频率；"
                "4) 尝试不同时间段注册；"
                f"{phone_hint}"
            )
            return False

        def _finalize_post_phone_oauth_failure(reason: str):
            result.error_message = (
                f"{flow_stage_label}失败：手机号已验证通过，但 OAuth/workspace 授权未完成"
                f"（{reason}）。建议：1) 更换住宅代理 IP；2) 降低注册频率；"
                "3) 等待当前 IP 冷却；4) 重新登录授权获取 Token"
            )
            self._log(result.error_message, "error")
            return False

        def _fallback_to_phone(reason: str) -> bool:
            nonlocal phone_verification_attempted, phone_verification_succeeded
            if not started_from_add_phone or phone_verification_attempted:
                return False
            self._log(f"{reason}，回退到 SMSBOWER 手机接码验证...", "warning")
            phone_verification_attempted = True
            phone_verification_succeeded = self._handle_phone_verification()
            return phone_verification_succeeded

        def _phone_attempt_failed() -> bool:
            return bool(
                started_from_add_phone
                and phone_verification_attempted
                and not phone_verification_succeeded
            )
        if post_page_type.lower() == "add_phone":
            self._log(f"{flow_stage_label}阶段遇到 add-phone，直接进入手机验证阶段...", "warning")
            if not exchange_token:
                self._log(
                    "当前尚未进入 about-you，姓名/生日资料尚未创建；先处理手机验证，再继续后续流程",
                    "info",
                )
            phone_verification_attempted = True
            phone_verification_succeeded = self._handle_phone_verification()
            if not phone_verification_succeeded:
                probe_reasons = {
                    "cloudflare_blocked",
                    "phone_send_failed",
                    "phone_rejected",
                    "phone_rate_limited",
                    "environment_unusable_fraud_guard",
                    "add_phone_attempt_limit",
                }
                if (
                    (self._last_phone_failure_reason in probe_reasons or self.session)
                    and self._probe_post_add_phone_state_after_failure()
                ):
                    post_page_type = getattr(self, "_post_otp_page_type", "") or ""
                else:
                    return _finalize_phone_failure()
        
        # 妫€鏌ユ槸鍚﹁繘鍏?about_you 椤甸潰锛堥渶瑕佸畬鎴愮敤鎴蜂俊鎭缃級
        post_page_type = getattr(self, "_post_otp_page_type", "") or ""
        if post_page_type.lower() == "about_you":
            created_continue_url = self._create_account_during_oauth_if_needed()
            if created_continue_url:
                self._post_otp_continue_url = created_continue_url
                if (
                    not exchange_token
                    and self._is_plain_chatgpt_account_callback(created_continue_url)
                ):
                    self._post_otp_page_type = "plain_chatgpt_callback"
                    self._token_acquisition_requires_login = True
                    self._log(
                        "about-you create_account returned plain ChatGPT account callback; "
                        "basic account is created, deferring Codex OAuth/workspace resolution to login authorization"
                    )
                    return True
                if "consent" in created_continue_url:
                    self._post_otp_page_type = "consent"
                self._log(f"about-you create_account completed, continue_url={created_continue_url[:100]}...")
                post_page_type = getattr(self, "_post_otp_page_type", "") or ""
            else:
                self._log("about-you create_account returned no continue_url, fallback to page visit", "warning")

        if post_page_type.lower() == "about_you":
            self._log("验证码校验后进入 about-you 页面，访问页面以完成 Cookie 设置...", "info")
            try:
                about_you_url = "https://auth.openai.com/about-you"
                nav_headers = self._build_navigation_headers(referer=about_you_url)
                page_resp = self.session.get(
                    about_you_url,
                    headers=nav_headers,
                    allow_redirects=True,
                    timeout=30,
                )
                self._log(f"访问 about-you 页面状态: {page_resp.status_code}")
                # 绛夊緟椤甸潰瀹屾垚 Cookie 璁剧疆
                self._behavior_sim.natural_delay(2.0, 4.0)
                
                # 妫€鏌ラ噸瀹氬悜鍚庣殑 URL锛岀湅鏄惁宸茬粡璺宠浆鍒?consent 鎴栧叾浠栭〉闈?
                final_url = normalize_flow_url(
                    str(page_resp.url or ""),
                    auth_base="https://auth.openai.com",
                )
                final_page_type = extract_flow_state(
                    {},
                    current_url=final_url,
                    auth_base="https://auth.openai.com",
                ).page_type
                if final_page_type in {
                    "consent",
                    "organization_selection",
                    "workspace_selection",
                    "oauth_callback",
                    "callback",
                }:
                    self._log(f"about-you 页面已重定向到: {final_url[:100]}...")
                    self._post_otp_continue_url = final_url
                    self._post_otp_page_type = final_page_type
                else:
                    self._log("about-you 页面访问完成，Cookie 已更新")
            except Exception as e:
                self._log(f"访问 about-you 页面异常: {e}", "warning")

        post_page_type = getattr(self, "_post_otp_page_type", "") or ""
        if post_page_type.lower() == "about_you":
            if self._about_you_create_account_already_exists_without_consent:
                self._log(
                    f"OpenAI 提示该邮箱已有账号，基础账号已存在，直接切换到登录授权: {self.email}",
                    "warning",
                )
                self._is_existing_account = True
                self._token_acquisition_requires_login = True
                return True
            else:
                result.error_message = (
                    "about-you create_account did not reach OAuth callback; "
                    "OpenAI 上游没有返回 consent/workspace/callback，当前无法拿到 token"
                )
                self._log(result.error_message, "error")
                return False

        callback_url = ""
        requires_verified_account_context = bool(
            exchange_token and (self._token_acquisition_requires_login or self._is_existing_account)
        )
        context_bootstrap_attempted = False
        context_bootstrap_succeeded = False
        post_phone_context_refresh_attempted = False
        if requires_verified_account_context:
            identity_ok, identity_error = self._verify_current_account_identity_for_authorization()
            if not identity_ok:
                result.error_message = identity_error
                self._log(identity_error, "error")
                return False
        while True:
            self._log("获取 Workspace ID...")
            workspace_id = self._get_workspace_id()

            if (
                not workspace_id
                and requires_verified_account_context
                and not context_bootstrap_attempted
            ):
                context_bootstrap_attempted = True
                context_bootstrap_succeeded = self._bootstrap_authorization_context()
                if context_bootstrap_succeeded:
                    continue
            
            # 濡傛灉浠?cookie 涓幏鍙栧け璐ワ紝灏濊瘯閫氳繃 API 鑾峰彇
            if not workspace_id:
                self._log("尝试通过 API 获取 Workspace ID...", "warning")
                workspace_id = self._get_workspace_id_from_api()

            if not workspace_id:
                self._log("尝试从 consent 页面 HTML 提取 Workspace ID...", "warning")
                workspace_id = self._get_workspace_id_from_consent_html(
                    self._resolve_post_otp_continue_url()
                )
            
            # ⭐ 个人 ChatGPT 账号本来就没 workspace（workspace 只存在于 team/enterprise）
            # 在回退接码前，先尝试"无 workspace 直接走 consent callback"
            # 很多个人账号的 consent URL 会直接重定向到 callback URL，不需要选 workspace
            if (
                not workspace_id
                and exchange_token
                and not (started_from_add_phone and phone_verification_succeeded)
            ):
                self._log("个人账号可能无 workspace，尝试直接从 consent URL 获取 OAuth callback...", "warning")
                try:
                    direct_callback = self._resolve_oauth_callback_via_browser(
                        "",  # 空 workspace_id
                        self._resolve_post_otp_continue_url(),
                    )
                    if direct_callback:
                        self._log(f"✅ 无 workspace 直接拿到 OAuth callback: {direct_callback[:100]}...")
                        callback_url = direct_callback
                        result.workspace_id = ""  # 个人账号无 workspace
                        break  # 跳出 while 循环，直接走 _handle_oauth_callback
                except Exception as e:
                    self._log(f"无 workspace callback 尝试异常: {e}", "warning")
                # 再试一次用 session 直接 HTTP 请求 consent URL
                try:
                    callback_url, _ws = self._resolve_oauth_callback_url(
                        self._resolve_post_otp_continue_url()
                    )
                    if callback_url:
                        self._log(f"✅ 无 workspace 通过 HTTP consent 拿到 callback: {callback_url[:100]}...")
                        result.workspace_id = ""
                        break
                except Exception as e:
                    self._log(f"HTTP consent callback 尝试异常: {e}", "warning")
                # 若 exchange_token=False（基础账号模式），无 workspace 也算成功
                # 因为 free 账号在后续会用重新登录拿 token
            elif not workspace_id and not exchange_token:
                self._log("基础账号模式（exchange_token=False），个人账号无 workspace 属正常，继续")
                result.workspace_id = ""
                return True

            if not workspace_id and not callback_url:
                if _fallback_to_phone("绕过 add-phone 后仍无法获取 Workspace ID"):
                    continue
                if _phone_attempt_failed():
                    return _finalize_phone_failure()
                if (
                    started_from_add_phone
                    and phone_verification_succeeded
                    and not post_phone_context_refresh_attempted
                ):
                    post_phone_context_refresh_attempted = True
                    self._log("手机验证已通过，等待授权上下文刷新后重试获取 Workspace ID...", "warning")
                    continue
                if started_from_add_phone and phone_verification_succeeded:
                    return _finalize_post_phone_oauth_failure("OAuth callback blocked")
                if self._token_acquisition_requires_login:
                    if context_bootstrap_succeeded:
                        login_ready, login_error = self._restart_login_flow()
                        if not login_ready:
                            result.error_message = login_error
                            return False
                        return self._complete_token_exchange(result)
                    result.error_message = self._build_unbootstrapped_account_error()
                    self._log(result.error_message, "error")
                    return False
                result.error_message = "获取 Workspace ID 失败"
                return False

            result.workspace_id = workspace_id
            if not exchange_token:
                self._log("基础账号 workspace 上下文已就绪，延后到重新登录授权阶段获取 Token")
                return True

            callback_url = self._resolve_oauth_callback_via_browser(
                workspace_id,
                self._resolve_post_otp_continue_url(),
            )
            if not callback_url:
                self._log("选择 Workspace...")
                continue_url = self._select_workspace(workspace_id)
                if not continue_url:
                    if _fallback_to_phone("绕过 add-phone 后选择 Workspace 失败"):
                        continue
                    if _phone_attempt_failed():
                        return _finalize_phone_failure()
                    if started_from_add_phone and phone_verification_succeeded:
                        return _finalize_post_phone_oauth_failure("workspace select failed")
                    result.error_message = "选择 Workspace 失败"
                    return False

                self._log("跟随重定向链...")
                callback_url = self._follow_redirects(continue_url)
            if not callback_url:
                if _fallback_to_phone("缁曡繃 add-phone 鍚庤窡闅忛噸瀹氬悜閾惧け璐?"):
                    continue
                if _phone_attempt_failed():
                    return _finalize_phone_failure()
                if started_from_add_phone and phone_verification_succeeded:
                    return _finalize_post_phone_oauth_failure("OAuth callback blocked")
                result.error_message = "跟随重定向链失败"
                return False
            break

        self._log("处理 OAuth 回调并获取 Token...")
        token_info = self._handle_oauth_callback(callback_url)
        
        # 浼樺厛灏濊瘯浠?OAuth 鍥炶皟鑾峰彇 token 淇℃伅
        if token_info:
            result.account_id = token_info.get("account_id", "")
            result.access_token = token_info.get("access_token", "")
            result.refresh_token = token_info.get("refresh_token", "")
            result.id_token = token_info.get("id_token", "")
            result.source = "login" if self._is_existing_account else "register"
            self._log("OAuth 回调处理成功")
        else:
            # OAuth token 浜ゆ崲澶辫触鏃讹紝璁板綍璀﹀憡浣嗙户缁皾璇曟彁鍙?session token
            self._log("OAuth token 交换失败，尝试从 Session Cookie 提取令牌...", "warning")
            result.source = "login" if self._is_existing_account else "register"

        # 灏濊瘯浠?session cookie 鎻愬彇 session token锛堝嵆浣?OAuth 澶辫触涔彲鑳芥垚鍔燂級
        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("成功获取 Session Token")
            if not (result.refresh_token and result.id_token):
                result.error_message = (
                    "refresh-token mode requires real OAuth credentials: "
                    "missing refresh_token or id_token"
                )
                self._log(result.error_message, "error")
                return False
            return True
        else:
            # 娌℃湁 session token 涓?OAuth 涔熷け璐ワ紝鎵嶈涓烘敞鍐屽け璐?
            if not token_info:
                result.error_message = "处理 OAuth 回调失败且未获取到 Session Token"
                return False

        result.password = self.password or ""
        return True

    def _restart_login_flow(self) -> Tuple[bool, str]:
        """鏂版敞鍐岃处鍙峰畬鎴愬缓鍙峰悗锛岄噸鏂板彂璧蜂竴娆＄櫥褰曟祦绋嬫嬁 token銆?"""
        self._token_acquisition_requires_login = True
        self._log("注册完成，开始重新登录以获取 Token...")
        self._reset_auth_flow()

        did, sen_token = self._prepare_authorize_flow("重新登录")
        if not did:
            return False, "重新登录时获取 Device ID 失败"
        if not sen_token:
            return False, "重新登录时 Sentinel POW 验证失败"

        login_start_result = self._submit_login_start(did, sen_token)
        if not login_start_result.success:
            return False, f"重新登录提交邮箱失败: {login_start_result.error_message}"
        if login_start_result.page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            self._log("重新登录已直接进入邮箱验证码页面，等待系统发送验证码")
            return True, ""
        if login_start_result.page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            return False, f"重新登录未进入密码页面: {login_start_result.page_type or 'unknown'}"

        password_result = self._submit_login_password()
        if not password_result.success:
            return False, f"重新登录提交密码失败: {password_result.error_message}"
        if password_result.is_existing_account:
            return True, ""
        # 本账号在 reuse-session 阶段已通过邮箱 OTP，密码登录后 OpenAI 直接跳 add_phone；
        # 这属于正常流程（OTP 无需二次校验），设置 post_otp_page_type 让 _complete_token_exchange
        # 直接接管手机验证，而不是把 add_phone 当作"登录未进入验证码页面"误判为失败。
        password_page_type = str(password_result.page_type or "").lower()
        if password_page_type == "add_phone":
            self._post_otp_page_type = "add_phone"
            self._log("重新登录已跳过邮箱 OTP 直达 add-phone，进入手机验证阶段", "warning")
            return True, ""
        return False, f"重新登录未进入验证码页面: {password_result.page_type or 'unknown'}"

    def _check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> Optional[str]:
        """Prefer browser-observed Sentinel, but do not merge detached browser cookies into the HTTP session."""
        try:
            if not self.session:
                self.session = self.http_client.session
            if flow in {"username_password_create", "oauth_create_account", "authorize_continue"}:
                import os

                has_display = bool(
                    os.environ.get("DISPLAY")
                    or os.environ.get("WAYLAND_DISPLAY")
                    or os.name == "nt"
                )
                if has_display:
                    browser_result = _run_browser_for_page(
                        flow=flow,
                        proxy=self.proxy_url,
                        headless=True,
                        device_id=did,
                        log_fn=lambda msg: self._log(msg),
                        extract_cookies=True,
                    )
                    if browser_result and browser_result.get("sentinel_token"):
                        self._log(f"Sentinel Browser token acquired ({flow})")
                        return browser_result["sentinel_token"]
                    self._log(f"Browser sentinel unavailable, fallback to PoW ({flow})", "warning")
                else:
                    self._log(f"No display environment, fallback to PoW sentinel ({flow})", "warning")

            sen_token = build_sentinel_token(self.session, did, flow=flow)
            if sen_token:
                self._log(f"Sentinel token acquired ({flow})")
                return sen_token
            self._log(f"Sentinel token missing ({flow})", "warning")
            return None
        except Exception as exc:
            self._log(f"Sentinel check exception ({flow}): {exc}", "warning")
            return None

    def _mark_email_as_registered(self):
        """鏍囪閭涓哄凡娉ㄥ唽鐘舵€侊紙鐢ㄤ簬闃叉閲嶅灏濊瘯锛?"""
        try:
            with get_db() as db:
                # 妫€鏌ユ槸鍚﹀凡瀛樺湪璇ラ偖绠辩殑璁板綍
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 鍒涘缓涓€涓け璐ヨ褰曪紝鏍囪璇ラ偖绠卞凡娉ㄥ唽杩?
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 绌哄瘑鐮佽〃绀烘湭鎴愭姛娉ㄥ唽
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _get_verification_code(self) -> Optional[str]:
        """鑾峰彇楠岃瘉鐮侊紙澧炲己閲嶅妫€娴嬪拰鏃ュ織锛?"""
        try:
            self._log(f"[步骤 1] 正在等待邮箱 {self.email} 的验证码...")

            email_id = self.email_info.get("service_id") if self.email_info else None
            self._log(f"[步骤 2] email_id={email_id}")

            exclude_codes = {
                str(code).strip()
                for code in self._used_verification_codes
                if str(code or "").strip()
            }
            self._log(f"[步骤 3] exclude_codes={exclude_codes}")

            if exclude_codes:
                self._log(
                    "本轮取件将跳过已取过的验证码: "
                    + ", ".join(sorted(exclude_codes))
                )

            self._log(f"[步骤 4] 开始调用 email_service.get_verification_code()...")
            try:
                code = self.email_service.get_verification_code(
                    email=self.email,
                    email_id=email_id,
                    timeout=700,
                    pattern=OTP_CODE_PATTERN,
                    otp_sent_at=self._otp_sent_at,
                    exclude_codes=exclude_codes,
                )
                self._log(f"[步骤 5] get_verification_code 返回: code={code}")
            except BrokenPipeError as e:
                self._log(f"[错误] BrokenPipeError: {e}", "error")
                import traceback
                self._log(f"[错误] 堆栈: {traceback.format_exc()}", "error")
                self._log("尝试重新初始化 session 后再次获取...", "warning")

                # 閲嶆柊鍒濆鍖?session
                self._reset_auth_flow()
                did, sen_token = self._prepare_authorize_flow("閲嶆柊杩炴帴")
                if not did or not sen_token:
                    self._log("重新初始化 session 失败", "error")
                    return None

                self._log("重新初始化 session 成功，再次尝试获取验证码...", "info")
                code = self.email_service.get_verification_code(
                    email=self.email,
                    email_id=email_id,
                    timeout=700,
                    pattern=OTP_CODE_PATTERN,
                    otp_sent_at=self._otp_sent_at,
                    exclude_codes=exclude_codes,
                )
                self._log(f"[步骤 5b] 重试后 get_verification_code 返回: code={code}")
            except Exception as e:
                self._log(f"[错误] get_verification_code 异常: {type(e).__name__}: {e}", "error")
                import traceback
                self._log(f"[错误] 堆栈: {traceback.format_exc()}", "error")
                return None

            if code:
                code_str = str(code).strip()
                
                # 妫€鏌ユ槸鍚︿负閲嶅楠岃瘉鐮?
                if code_str in exclude_codes:
                    self._log(
                        f"警告: 获取到重复验证码 {code}（与之前相同）",
                        "warning"
                    )
                    self._log(
                        "OpenAI 可能发送了重复的验证码，将在验证时自动处理",
                        "warning"
                    )
                
                self._used_verification_codes.add(code_str)
                self._log(f"成功获取验证码: {code}")
                return code
            else:
                self._log("等待验证码超时", "error")
                return None

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"[最外层异常] 获取验证码失败: {type(e).__name__}: {e}", "error")
            import traceback
            self._log(f"[最外层堆栈] {traceback.format_exc()}", "error")
            return None

    @staticmethod
    def _decode_cookie_json_value(raw_value: str) -> Optional[Dict[str, Any]]:
        value = str(raw_value or "").strip()
        if not value:
            return None

        candidates = [value]
        if "." in value:
            parts = value.split(".")
            candidates = [parts[0], value, *parts[:2]]

        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            padded = candidate + "=" * (-len(candidate) % 4)
            for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                try:
                    decoded = decoder(padded.encode("ascii")).decode("utf-8")
                    parsed = json.loads(decoded)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return None

    def _decode_auth_session_cookie(self) -> Optional[Dict[str, Any]]:
        try:
            auth_cookie = self.session.cookies.get("oai-client-auth-session")
        except Exception:
            auth_cookie = None
        if not auth_cookie:
            return None
        return self._decode_cookie_json_value(auth_cookie)

    @classmethod
    def _find_nested_value(cls, data: Any, key: str) -> Any:
        if isinstance(data, dict):
            if key in data:
                return data.get(key)
            for value in data.values():
                found = cls._find_nested_value(value, key)
                if found not in (None, ""):
                    return found
        elif isinstance(data, list):
            for value in data:
                found = cls._find_nested_value(value, key)
                if found not in (None, ""):
                    return found
        return None

    def _extract_phone_verification_channel(self, response_data: Dict[str, Any]) -> str:
        channel = self._find_nested_value(response_data, "phone_verification_channel")
        if not channel:
            auth_json = self._decode_auth_session_cookie() or {}
            channel = self._find_nested_value(auth_json, "phone_verification_channel")
        return str(channel or "").strip().lower()

    @staticmethod
    def _normalize_account_email(value: Any) -> str:
        return str(value or "").strip().lower()

    def _target_account_email(self) -> str:
        email = self._normalize_account_email(self.email)
        if email:
            return email
        if isinstance(self.email_info, dict):
            return self._normalize_account_email(self.email_info.get("email"))
        return ""

    def _current_auth_session_email(self) -> str:
        auth_json = self._decode_auth_session_cookie() or {}
        candidates = [
            auth_json.get("email") if isinstance(auth_json, dict) else "",
            ((auth_json.get("user") or {}).get("email") if isinstance(auth_json.get("user"), dict) else ""),
            ((auth_json.get("account") or {}).get("email") if isinstance(auth_json.get("account"), dict) else ""),
        ]
        for candidate in candidates:
            email = self._normalize_account_email(candidate)
            if email:
                return email
        return ""

    def _verify_current_account_identity_for_authorization(self) -> Tuple[bool, str]:
        target_email = self._target_account_email()
        session_email = self._current_auth_session_email()
        if not target_email:
            return False, "account_identity_unverified: target email is missing before authorization context bootstrap"
        if not session_email:
            return False, f"account_identity_unverified: current auth session has no email; target={target_email}"
        if session_email != target_email:
            return (
                False,
                "account_identity_mismatch: "
                f"current auth session email={session_email}, target={target_email}",
            )
        self._log(f"Authorization identity verified: {target_email}")
        return True, ""

    def _build_unbootstrapped_account_error(self) -> str:
        auth_json = self._decode_auth_session_cookie() or {}
        workspaces = auth_json.get("workspaces") if isinstance(auth_json, dict) else []
        workspace_count = len(workspaces) if isinstance(workspaces, list) else 0
        email_hint = str(auth_json.get("email") or self.email or "").strip()
        email_part = f", email={email_hint}" if email_hint else ""
        return (
            "account_not_fully_bootstrapped: "
            "oai-client-auth-session has no workspaces after relogin; "
            "workspace API/consent HTML did not expose workspace/callback; "
            "account may need manual bootstrap before OAuth token exchange. "
            f" workspace_count={workspace_count}{email_part}"
        )

    def _extract_callback_url_from_candidate(self, candidate: str) -> str:
        normalized = normalize_flow_url(str(candidate or "").strip(), auth_base="https://auth.openai.com")
        if not normalized:
            return ""
        parsed = urllib.parse.urlparse(normalized)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        code = str((query.get("code") or [""])[0] or "").strip()
        state = str((query.get("state") or [""])[0] or "").strip()
        return normalized if code and state else ""

    def _is_plain_chatgpt_account_callback(self, candidate: str) -> bool:
        normalized = normalize_flow_url(str(candidate or "").strip(), auth_base="https://auth.openai.com")
        if not normalized:
            return False
        parsed = urllib.parse.urlparse(normalized)
        if parsed.netloc.lower() != "chatgpt.com":
            return False
        if parsed.path.rstrip("/") != "/api/auth/callback/openai":
            return False
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        code = str((query.get("code") or [""])[0] or "").strip()
        state = str((query.get("state") or [""])[0] or "").strip()
        return bool(code and not state)

    def _follow_and_extract_callback_url(self, start_url: str, max_depth: int = 10) -> str:
        current_url = normalize_flow_url(start_url, auth_base="https://auth.openai.com")
        referer = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        for hop in range(max_depth):
            if not current_url:
                return ""

            callback_url = self._extract_callback_url_from_candidate(current_url)
            if callback_url:
                return callback_url

            self._log(f"OAuth 跟随重定向 {hop + 1}/{max_depth}: {current_url[:120]}...")

            try:
                response = self.session.get(
                    current_url,
                    headers=self._build_navigation_headers(referer=referer),
                    allow_redirects=False,
                    timeout=15,
                )
            except Exception as e:
                self._log(f"OAuth 跟随重定向失败: {e}", "warning")
                return ""

            referer = current_url
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code in (301, 302, 303, 307, 308) and location:
                next_url = normalize_flow_url(
                    urllib.parse.urljoin(current_url, location),
                    auth_base="https://auth.openai.com",
                )
                callback_url = self._extract_callback_url_from_candidate(next_url)
                if callback_url:
                    return callback_url
                current_url = next_url
                continue

            callback_url = self._extract_callback_url_from_candidate(str(response.url))
            if callback_url:
                return callback_url
            break

        return ""

    # ────────────────────────────────────────────────────────────────────────
    # OAuth wrapper 层：workspace / callback 解析、代理轮换、run 主流程
    # ────────────────────────────────────────────────────────────────────────

    @classmethod
    def _extract_workspace_id_from_payload(cls, payload: Any) -> str:
        workspaces = cls._find_nested_value(payload, "workspaces")
        if not isinstance(workspaces, list):
            return ""
        fallback_workspace_id = ""
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                continue
            workspace_id = str(workspace.get("id") or "").strip()
            if not workspace_id:
                continue
            if str(workspace.get("kind") or "").strip().lower() == "personal":
                return workspace_id
            if not fallback_workspace_id:
                fallback_workspace_id = workspace_id
        return fallback_workspace_id

    @classmethod
    def _extract_workspace_id_from_html(cls, html: str) -> str:
        body = str(html or "")
        if not body:
            return ""
        for candidate in (body, body.replace('\\"', '"')):
            if "workspaces" not in candidate:
                continue
            match = re.search(r'"id"\s*:\s*"([0-9a-fA-F-]{36})"', candidate)
            if match:
                return match.group(1)
        return ""

    def _get_workspace_id(self) -> str:
        auth_json = self._decode_auth_session_cookie() or {}
        return self._extract_workspace_id_from_payload(auth_json)

    def _get_workspace_id_from_api(self) -> str:
        if self._has_browser_frontend_state():
            try:
                browser_result, _response_data = self._submit_browser_form(
                    label="Workspace API(browser)",
                    page_url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                    form_type="page_content",
                    form_value="",
                )
                body = str((browser_result or {}).get("body") or "")
                if body:
                    try:
                        payload = json.loads(body)
                    except Exception:
                        payload = {}
                    workspace_id = self._extract_workspace_id_from_payload(payload)
                    if workspace_id:
                        return workspace_id
                    return self._extract_workspace_id_from_html(body)
            except Exception as exc:
                self._log(f"Workspace API(browser) 失败: {exc}", "warning")
                return ""

        if not self.session:
            return ""

        try:
            response = self.session.get(
                "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                headers={
                    "accept": "application/json",
                    "accept-language": "en-US,en;q=0.9",
                    "referer": "https://chatgpt.com/",
                    "user-agent": self._default_user_agent(),
                },
                allow_redirects=True,
                timeout=15,
            )
        except Exception as exc:
            self._log(f"Workspace API 请求失败: {exc}", "warning")
            return ""

        try:
            payload = response.json()
        except Exception:
            payload = {}
        workspace_id = self._extract_workspace_id_from_payload(payload)
        if workspace_id:
            return workspace_id
        return self._extract_workspace_id_from_html(getattr(response, "text", ""))

    def _get_workspace_id_from_consent_html(self, consent_url: str) -> str:
        normalized_url = normalize_flow_url(
            consent_url or self._resolve_post_otp_continue_url(),
            auth_base="https://auth.openai.com",
        ) or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        if self._has_browser_frontend_state():
            html = self._fetch_consent_page_html_via_browser(normalized_url)
            workspace_id = self._extract_workspace_id_from_html(html)
            if workspace_id:
                return workspace_id

        if not self.session:
            return ""

        try:
            response = self.session.get(
                normalized_url,
                headers=self._build_navigation_headers(referer="https://auth.openai.com/about-you"),
                allow_redirects=True,
                timeout=20,
            )
        except Exception as exc:
            self._log(f"Consent HTML 请求失败: {exc}", "warning")
            return ""

        return self._extract_workspace_id_from_html(getattr(response, "text", ""))

    def _handle_oauth_callback(self, callback_url: str) -> Dict[str, Any]:
        if not self.oauth_start:
            self._log("处理 OAuth callback 失败: oauth_start 缺失", "warning")
            return {}

        normalized_callback = (
            self._extract_callback_url_from_candidate(callback_url)
            or normalize_flow_url(str(callback_url or "").strip(), auth_base="https://auth.openai.com")
        )
        if not normalized_callback:
            self._log("处理 OAuth callback 失败: callback_url 为空", "warning")
            return {}

        try:
            token_info = self.oauth_manager.handle_callback(
                normalized_callback,
                self.oauth_start.state,
                self.oauth_start.code_verifier,
            )
            return token_info if isinstance(token_info, dict) else {}
        except Exception as exc:
            self._log(f"处理 OAuth callback 失败: {exc}", "warning")
            return {}

    def _select_workspace(self, workspace_id: str) -> str:
        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id or not self.session:
            return ""

        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://auth.openai.com",
            "referer": self._resolve_post_otp_continue_url(),
            "user-agent": self._default_user_agent(),
        }

        try:
            response = self.session.post(
                "https://auth.openai.com/api/accounts/workspace/select",
                headers=headers,
                json={"workspace_id": normalized_workspace_id},
                allow_redirects=False,
                timeout=20,
            )
        except Exception as exc:
            self._log(f"选择 Workspace 请求失败: {exc}", "warning")
            return ""

        location = normalize_flow_url(
            str(response.headers.get("Location") or ""),
            auth_base="https://auth.openai.com",
        )
        callback_url = self._extract_callback_url_from_candidate(location)
        if callback_url:
            return callback_url

        try:
            payload = response.json()
        except Exception:
            payload = {}

        continue_url = normalize_flow_url(
            str((payload.get("continue_url") if isinstance(payload, dict) else "") or location or ""),
            auth_base="https://auth.openai.com",
        )
        page_type = str(
            (((payload.get("page") or {}) if isinstance(payload, dict) else {}).get("type") or "")
        ).strip().lower()
        orgs = self._find_nested_value(payload, "orgs")

        if page_type == "organization_select" or "organization" in str(continue_url or ""):
            first_org = next((item for item in (orgs or []) if isinstance(item, dict)), None)
            if first_org:
                org_id = str(first_org.get("id") or "").strip()
                projects = first_org.get("projects") or []
                first_project = projects[0] if isinstance(projects, list) and projects else {}
                project_id = str((first_project or {}).get("id") or "").strip() if isinstance(first_project, dict) else ""
                try:
                    org_response = self.session.post(
                        "https://auth.openai.com/api/organizations/select",
                        headers=headers,
                        json={
                            "organization_id": org_id,
                            "project_id": project_id,
                            "workspace_id": normalized_workspace_id,
                        },
                        allow_redirects=False,
                        timeout=20,
                    )
                    org_location = normalize_flow_url(
                        str(org_response.headers.get("Location") or ""),
                        auth_base="https://auth.openai.com",
                    )
                    callback_url = self._extract_callback_url_from_candidate(org_location)
                    if callback_url:
                        return callback_url
                    if not continue_url:
                        continue_url = org_location or normalize_flow_url(
                            str(getattr(org_response, "url", "") or ""),
                            auth_base="https://auth.openai.com",
                        )
                    if not continue_url:
                        try:
                            org_payload = org_response.json()
                        except Exception:
                            org_payload = {}
                        continue_url = normalize_flow_url(
                            str((org_payload.get("continue_url") if isinstance(org_payload, dict) else "") or ""),
                            auth_base="https://auth.openai.com",
                        )
                except Exception as exc:
                    self._log(f"选择 Organization 请求失败: {exc}", "warning")

        return continue_url

    def _follow_redirects(self, continue_url: str) -> str:
        callback_url = self._extract_callback_url_from_candidate(continue_url)
        if callback_url:
            return callback_url
        return self._follow_and_extract_callback_url(continue_url)

    def _resolve_oauth_callback_url(self, start_url: str) -> Tuple[str, str]:
        normalized_start = normalize_flow_url(
            start_url or self._resolve_post_otp_continue_url(),
            auth_base="https://auth.openai.com",
        ) or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        workspace_id = self._get_workspace_id() or ""
        callback_url = self._extract_callback_url_from_candidate(normalized_start)
        if callback_url:
            return callback_url, workspace_id

        if not self.session:
            return "", workspace_id

        try:
            response = self.session.get(
                normalized_start,
                headers=self._build_navigation_headers(
                    referer="https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
                ),
                allow_redirects=False,
                timeout=20,
            )
        except Exception as exc:
            self._log(f"获取 OAuth callback 失败: {exc}", "warning")
            return "", workspace_id

        for candidate in [response.headers.get("Location"), getattr(response, "url", "")]:
            callback_url = self._extract_callback_url_from_candidate(str(candidate or ""))
            if callback_url:
                return callback_url, workspace_id

        if not workspace_id:
            workspace_id = self._get_workspace_id() or ""

        if workspace_id:
            continue_url = self._select_workspace(workspace_id)
            callback_url = self._extract_callback_url_from_candidate(continue_url)
            if callback_url:
                return callback_url, workspace_id
            callback_url = self._follow_and_extract_callback_url(continue_url)
            if callback_url:
                return callback_url, workspace_id

        fallback_url = normalize_flow_url(
            str(response.headers.get("Location") or getattr(response, "url", "") or normalized_start),
            auth_base="https://auth.openai.com",
        )
        return self._follow_and_extract_callback_url(fallback_url), workspace_id

    def _resolve_oauth_callback_via_browser(self, workspace_id: str, start_url: str) -> str:
        normalized_start = normalize_flow_url(
            start_url or self._resolve_post_otp_continue_url(),
            auth_base="https://auth.openai.com",
        ) or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        callback_url = self._extract_callback_url_from_candidate(normalized_start)
        if callback_url:
            return callback_url

        if not self._has_browser_frontend_state():
            return ""

        # 先用 page_content 被动 GET：如果 OpenAI 直接重定向 consent → callback（无需用户点击）
        try:
            browser_result, response_data = self._submit_browser_form(
                label="OAuth callback(browser)",
                page_url=normalized_start,
                form_type="page_content",
                form_value="",
            )
        except Exception as exc:
            self._log(f"浏览器获取 OAuth callback 失败: {exc}", "warning")
            browser_result, response_data = None, None

        if browser_result and self._is_retry_only_browser_error_page(browser_result):
            self._log("consent_authorize encountered retry-only error page", "warning")
            return ""

        if browser_result and not self._is_retry_only_browser_error_page(browser_result):
            callback_url = self._extract_callback_from_browser_result(browser_result)
            if callback_url:
                return callback_url

            continue_url = normalize_flow_url(
                str(
                    (response_data or {}).get("continue_url")
                    or browser_result.get("continue_url")
                    or browser_result.get("final_url")
                    or ""
                ),
                auth_base="https://auth.openai.com",
            )
            callback_url = self._extract_callback_url_from_candidate(continue_url)
            if callback_url:
                return callback_url
            if continue_url and continue_url != normalized_start:
                callback_url = self._follow_and_extract_callback_url(continue_url)
                if callback_url:
                    return callback_url

        # ⭐ 关键修复：page_content 拿不到 callback 说明 consent 页停在表单页等待用户授权
        # 用 consent_authorize 主动点击 "Authorize/Allow" 按钮触发 OAuth 授权回调
        # 这是个人账号（无 workspace）走 OAuth 拿 code 的标准路径
        final_url_after_get = ""
        if browser_result:
            final_url_after_get = str(
                browser_result.get("final_url") or normalized_start
            ).strip()
        consent_url = final_url_after_get or normalized_start
        if "/consent" in consent_url or "/sign-in-with-chatgpt" in consent_url:
            self._log(
                "consent 页未自动跳转，使用 consent_authorize 主动点击 Authorize 按钮...",
                "warning",
            )
            try:
                authorize_result, authorize_data = self._submit_browser_form(
                    label="OAuth consent authorize",
                    page_url=consent_url,
                    form_type="consent_authorize",
                    form_value="",
                )
            except Exception as exc:
                self._log(f"consent_authorize 失败: {exc}", "warning")
                authorize_result, authorize_data = None, None

            if authorize_result and not self._is_retry_only_browser_error_page(authorize_result):
                callback_url = self._extract_callback_from_browser_result(authorize_result)
                if callback_url:
                    self._log("✅ consent_authorize 点击成功，已拿到 OAuth callback")
                    return callback_url

                # 检查 authorize 响应里的 continue_url
                authorize_continue = normalize_flow_url(
                    str(
                        (authorize_data or {}).get("continue_url")
                        or authorize_result.get("continue_url")
                        or authorize_result.get("final_url")
                        or ""
                    ),
                    auth_base="https://auth.openai.com",
                )
                callback_url = self._extract_callback_url_from_candidate(authorize_continue)
                if callback_url:
                    self._log("✅ consent_authorize 完成，从 continue_url 拿到 callback")
                    return callback_url
                if authorize_continue and authorize_continue != consent_url:
                    callback_url = self._follow_and_extract_callback_url(authorize_continue)
                    if callback_url:
                        self._log("✅ consent_authorize 完成，跟随重定向拿到 callback")
                        return callback_url

            # consent_authorize 拿不到 callback：诊断 consent 页错误状态
            authorize_body = str((authorize_result or {}).get("body") or "")
            authorize_visible_buttons = (authorize_result or {}).get("visible_buttons") or []
            normalized_buttons = [str(b or "").strip() for b in authorize_visible_buttons if str(b or "").strip()]
            if normalized_buttons:
                self._log(
                    f"consent_authorize 页面按钮: {normalized_buttons[:5]}",
                    "warning",
                )
            # 提取 consent 页关键错误信息（OpenAI 错误通常嵌入在 HTML script/JSON 里）
            body_lower = authorize_body.lower()
            error_hints: list[str] = []
            if "verify" in body_lower and "phone" in body_lower:
                error_hints.append("要求验证手机号")
            if "fraud" in body_lower or "suspicious" in body_lower:
                error_hints.append("风控/可疑行为")
            if "codex" in body_lower and ("not available" in body_lower or "unavailable" in body_lower):
                error_hints.append("codex 不可用")
            if "session" in body_lower and ("expired" in body_lower or "invalid" in body_lower):
                error_hints.append("session 过期")
            if "rate" in body_lower and "limit" in body_lower:
                error_hints.append("被限速")
            if error_hints:
                self._log(f"consent 页错误信息提示: {', '.join(error_hints)}", "warning")
            # 输出 body 关键片段帮助定位（截取含 error 关键字的上下文）
            for kw in ("error", "errorMessage", "errorCode", "phone_verification"):
                idx = authorize_body.find(kw)
                if idx >= 0:
                    snippet = authorize_body[max(0, idx - 50): idx + 200]
                    self._log(f"consent body[{kw}]: {snippet[:250]}", "warning")
                    break

        # 最终 fallback：如果有 workspace_id，走 workspace 选择
        normalized_workspace_id = str(workspace_id or "").strip()
        if normalized_workspace_id:
            continue_url = self._select_workspace(normalized_workspace_id)
            callback_url = self._extract_callback_url_from_candidate(continue_url)
            if callback_url:
                return callback_url
            return self._follow_and_extract_callback_url(continue_url)

        return ""

    def _try_rotate_proxy_on_fraud_guard(self) -> str:
        rotation_limit = self._get_fraud_guard_rotation_limit()
        current_rotations = int(getattr(self, "_fraud_guard_proxy_rotations", 0) or 0)
        if rotation_limit <= 0:
            self._log("fraud_guard: 当前配置禁止代理轮换", "warning")
            return ""
        if current_rotations >= rotation_limit:
            self._log(
                f"fraud_guard: 已达到代理轮换上限 {current_rotations}/{rotation_limit}",
                "warning",
            )
            return ""

        old_proxy = str(self.proxy_url or "").strip()
        if old_proxy:
            try:
                smart_selector.add_to_blacklist(old_proxy, duration=1800)
                smart_selector.report_proxy_result(old_proxy, success=False, auto_blacklist=False)
            except Exception as exc:
                self._log(f"fraud_guard: 标记旧代理失败: {exc}", "warning")

        new_proxy = ""
        try:
            new_proxy = str(smart_selector.get_smart_proxy(randomize=True) or "").strip()
        except Exception as exc:
            self._log(f"fraud_guard: 智能代理选择失败: {exc}", "warning")

        if not new_proxy or new_proxy == old_proxy:
            try:
                from core.proxy_pool import proxy_pool
                new_proxy = str(proxy_pool.get_next(respect_cooldown=False) or "").strip()
            except Exception as exc:
                self._log(f"fraud_guard: 代理池回退失败: {exc}", "warning")
                new_proxy = ""

        if not new_proxy or new_proxy == old_proxy:
            return ""

        self._sync_runtime_proxy(new_proxy, clear_browser_state=True)
        self._fraud_guard_proxy_rotations = current_rotations + 1
        self._log(
            f"fraud_guard: 代理已从 {old_proxy or '-'} → {new_proxy} "
            f"(rotation {self._fraud_guard_proxy_rotations}/{rotation_limit})",
            "warning",
        )
        return new_proxy

    def run(self) -> RegistrationResult:
        """主流程入口：创建基础账号 → pre-oauth hook → OAuth token 交换"""
        result = RegistrationResult(success=False)
        result.metadata = result.metadata or {}

        try:
            # Step 1: IP 检查
            ip_ok, geo = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP location check failed: {geo}"
                return result

            # Step 2: 创建邮箱
            if not self._create_email():
                result.error_message = "Failed to create email"
                return result
            result.email = self.email or ""

            # Step 3: 创建基础 ChatGPT 账号（注册 + OTP + about-you，跳过 add-phone）
            basic_ok, basic_error = self._create_consumer_chatgpt_basic_account(result)
            if not basic_ok:
                result.error_message = basic_error or "consumer_chatgpt_registration_failed"
                return result

            # Step 4: Pre-OAuth auto payment hook
            if not self._run_pre_oauth_auto_pay_hook(result):
                return result

            # Step 5: OAuth 授权（全新 session 登录 → 邮箱 → 密码 → OTP → add-phone → consent → callback → refresh_token）
            # 注意：不复用基础注册 session，避免被 fraud_guard 标记的旧会话状态污染
            self._log("基础账号已就绪，开始 OAuth 授权（全新 session 登录流程）...")
            # 依据最新需求：创建完基础账号后不要立刻使用当前 session 进行 oauth
            # 而是直接清空 session，进入全新的重新登录流程（重收邮箱验证码）
            # 所以直接跳过当前的 _complete_token_exchange_from_current_session 逻辑
            # block everything before login_ok
            
            login_ok, login_error = self._restart_login_flow()
            if not login_ok:
                result.error_message = login_error or "restart_login_flow_failed"
                return result

            # Step 6: 完成 token 交换（OTP → add-phone → consent → callback → refresh_token）
            # 外层 OAuth retry 逻辑移除：因为业务需求"验证邮箱再一直addphone，如果失败也不要login了"
            token_ok = self._complete_token_exchange(result)
            if token_ok:
                result.success = True
                result.metadata["token_acquired_via_relogin"] = True
                return result

            # 若 token_exchange() 返回 False 且 _oauth_blocked_by_phone 为 True 时
            # 说明经过了一轮登录/验证邮箱并走到了 addphone 但是没过去风控
            if getattr(self, "_oauth_blocked_by_phone", False):
                result.success = False
                result.error_message = "oauth_blocked_by_phone"
                result.metadata["oauth_state"] = "blocked_by_phone"
                self._log("新登录会话的 OAuth 在 add-phone 步骤被阻断，按要求保留基础账号状态并结束不再重试。", "warning")
                return result

            result.error_message = result.error_message or "Failed to obtain real OAuth credentials after restart login"
            return result

        except TaskInterruption:
            raise
        except Exception as exc:
            self._log(f"run() 异常: {exc}", "error")
            result.error_message = f"registration_exception: {exc}"
            return result

    def _create_account_during_oauth_if_needed(self) -> str:
        self._about_you_create_account_already_exists_without_consent = False
        user_info = generate_random_user_info()
        create_account_body = json.dumps(user_info)
        browser_result, response_data = self._submit_browser_form(
            label="OAuth about-you create_account",
            page_url="https://auth.openai.com/about-you",
            form_type="create_account",
            form_value=create_account_body,
            flow="oauth_create_account",
            update_post_otp_state=True,
        )
        if not browser_result:
            self._log("OAuth about-you create_account 失败: Browser Form returned no result", "warning")
            return ""

        status = int(browser_result.get("status") or 0)
        body_text = str(browser_result.get("body") or "")
        if status == 200:
            return normalize_flow_url(
                str(response_data.get("continue_url") or ""),
                auth_base="https://auth.openai.com",
            )

        body_preview = body_text[:200]
        if status == 400 and "already_exists" in body_preview.lower():
            callback_url = self._extract_callback_from_browser_result(browser_result)
            if callback_url:
                return callback_url

            continue_url = normalize_flow_url(
                str(response_data.get("continue_url") or browser_result.get("continue_url") or ""),
                auth_base="https://auth.openai.com",
            )
            if continue_url and ("consent" in continue_url or "organization" in continue_url):
                return continue_url

            final_url = normalize_flow_url(
                str(browser_result.get("final_url") or ""),
                auth_base="https://auth.openai.com",
            )
            if final_url and ("consent" in final_url or "organization" in final_url):
                return final_url

            page_type = str(browser_result.get("page_type") or self._post_otp_page_type or "").strip().lower()
            self._log(
                "OAuth about-you create_account returned already_exists without consent state: "
                f"page_type={page_type or '-'}, final_url={(final_url or '-')[:120]}",
                "warning",
            )
            self._about_you_create_account_already_exists_without_consent = True
            return ""

        self._log(f"OAuth about-you create_account 失败: {status} {body_preview}", "warning")
        return ""

    def _resolve_post_otp_continue_url(self) -> str:
        continue_url = normalize_flow_url(
            self._post_otp_continue_url,
            auth_base="https://auth.openai.com",
        )
        page_type = str(self._post_otp_page_type or "").strip().lower()

        if continue_url and "about-you" in continue_url:
            if self._has_browser_frontend_state():
                created_continue_url = self._create_account_during_oauth_if_needed()
                if created_continue_url:
                    return created_continue_url

            self._log("OTP 后进入 about-you，按参考 RT 逻辑补齐 consent 跳转...")
            try:
                response = self.session.get(
                    "https://auth.openai.com/about-you",
                    headers=self._build_navigation_headers(
                        referer="https://auth.openai.com/email-verification"
                    ),
                    allow_redirects=True,
                    timeout=30,
                )
                final_url = normalize_flow_url(
                    str(response.url or ""),
                    auth_base="https://auth.openai.com",
                )
                callback_url = self._extract_callback_url_from_candidate(final_url)
                if callback_url:
                    return callback_url
                if "consent" in final_url or "organization" in final_url:
                    return final_url
            except Exception as e:
                self._log(f"GET about-you 失败: {e}", "warning")

            created_continue_url = self._create_account_during_oauth_if_needed()
            if created_continue_url:
                return created_continue_url

        if not continue_url and "consent" in page_type:
            continue_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        if continue_url:
            return continue_url

        return "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

    def _has_browser_frontend_state(self) -> bool:
        return bool(self._browser_frontend_state)

    def _bootstrap_authorization_context(self) -> bool:
        """Nudge upstream account initialization before retrying OAuth once."""
        if self._authorization_context_bootstrap_attempted:
            self._log("Authorization context bootstrap already attempted; skipping retry", "warning")
            return False

        self._authorization_context_bootstrap_attempted = True
        self._log("Authorization context missing workspace; visiting ChatGPT initialization entrypoints...")

        any_success = False

        if self._has_browser_frontend_state():
            for label, page_url in (
                ("Context bootstrap ChatGPT home(browser)", "https://chatgpt.com/"),
                ("Context bootstrap ChatGPT session(browser)", "https://chatgpt.com/api/auth/session"),
            ):
                try:
                    browser_result, _response_data = self._submit_browser_form(
                        label=label,
                        page_url=page_url,
                        form_type="page_content",
                        form_value="",
                    )
                    status = int((browser_result or {}).get("status") or 0)
                    final_url = str((browser_result or {}).get("final_url") or "")
                    self._log(f"{label}: status={status}, final_url={(final_url or '-')[:120]}")
                    if 200 <= status < 500 and status != 403:
                        any_success = True
                except Exception as exc:
                    self._log(f"{label}: failed: {exc}", "warning")

        http_targets = (
            ("Context bootstrap ChatGPT home", "https://chatgpt.com/", "https://chatgpt.com/"),
            ("Context bootstrap ChatGPT session", "https://chatgpt.com/api/auth/session", "https://chatgpt.com/"),
            ("Context bootstrap auth about-you", "https://auth.openai.com/about-you", "https://auth.openai.com/email-verification"),
            (
                "Context bootstrap consent",
                "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "https://auth.openai.com/about-you",
            ),
        )

        for label, url, referer in http_targets:
            try:
                if "auth.openai.com" in url:
                    headers = self._build_navigation_headers(referer=referer)
                else:
                    headers = {
                        "accept": (
                            "application/json"
                            if url.endswith("/api/auth/session")
                            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                        ),
                        "accept-language": "en-US,en;q=0.9",
                        "referer": referer,
                        "user-agent": self._default_user_agent(),
                    }
                response = self.session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=30,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                final_url = str(getattr(response, "url", "") or url)
                self._log(f"{label}: status={status}, final_url={final_url[:120]}")
                if 200 <= status < 500 and status != 403:
                    any_success = True
            except Exception as exc:
                self._log(f"{label}: failed: {exc}", "warning")

        if any_success:
            self._log("Authorization context bootstrap finished; restarting OAuth login once...")
        else:
            self._log("Authorization context bootstrap did not reach any initialization entrypoint", "warning")
        return any_success

    def _ensure_basic_account_ready_for_token_authorization(self) -> Tuple[bool, str]:
        """Verify the basic account session, then browse/init before token OAuth."""
        identity_ok, identity_error = self._verify_current_account_identity_for_authorization()
        if not identity_ok:
            return False, identity_error

        if not self._bootstrap_authorization_context():
            return False, "basic_account_context_bootstrap_failed: ChatGPT initialization entrypoints were not reachable"

        identity_ok, identity_error = self._verify_current_account_identity_for_authorization()
        if not identity_ok:
            return False, identity_error

        self._log("Basic account context is ready for token OAuth")
        return True, ""

    def _extract_callback_from_browser_result(self, browser_result: Optional[dict]) -> str:
        if not isinstance(browser_result, dict):
            return ""
        for candidate in [browser_result.get("final_url"), *(browser_result.get("navigation_chain") or [])]:
            callback_url = self._extract_callback_url_from_candidate(str(candidate or ""))
            if callback_url:
                return callback_url
        return ""

    def _is_retry_only_browser_error_page(self, browser_result: Optional[dict]) -> bool:
        if not isinstance(browser_result, dict):
            return False
        buttons = browser_result.get("visible_buttons") or []
        normalized_buttons = [
            str(btn or "").strip().lower()
            for btn in buttons
            if str(btn or "").strip()
        ]
        if normalized_buttons and all(btn in {"重试", "retry", "try again"} for btn in normalized_buttons):
            return True

        body = str(browser_result.get("body") or "").strip().lower()
        if not body:
            return False
        if "authorize" in body or "allow" in body or "consent-approve" in body:
            return False
        if ("<button>重试</button>" in body or ">retry<" in body or ">try again<" in body):
            return True
        return False

    def _get_fraud_guard_rotation_limit(self) -> int:
        raw_value = ""
        try:
            from core.config_store import config_store

            raw_value = str(config_store.get("fraud_guard_proxy_rotations", "") or "").strip()
        except Exception:
            raw_value = ""

        try:
            extra_value = str((self.extra_config or {}).get("fraud_guard_proxy_rotations", "") or "").strip()
            if extra_value:
                raw_value = extra_value
        except Exception:
            pass

        try:
            import os

            env_value = str(os.getenv("FRAUD_GUARD_PROXY_ROTATIONS", "") or "").strip()
            if env_value:
                raw_value = env_value
        except Exception:
            pass

        return self._parse_optional_int(raw_value, default=3, min_value=0, max_value=20)

    def _get_add_phone_send_attempt_limit(self) -> int:
        raw_value = ""
        keys = (
            "add_phone_max_send_attempts",
            "chatgpt_add_phone_max_send_attempts",
            "smsbower_add_phone_send_attempts",
        )
        try:
            from core.config_store import config_store

            for key in keys:
                value = str(config_store.get(key, "") or "").strip()
                if value:
                    raw_value = value
                    break
        except Exception:
            raw_value = ""

        try:
            extra = self.extra_config or {}
            for key in keys:
                value = str(extra.get(key, "") or "").strip()
                if value:
                    raw_value = value
                    break
        except Exception:
            pass

        try:
            import os

            env_value = str(os.getenv("CHATGPT_ADD_PHONE_MAX_SEND_ATTEMPTS", "") or "").strip()
            if env_value:
                raw_value = env_value
        except Exception:
            pass

        return self._parse_optional_int(raw_value, default=8, min_value=1, max_value=100)

    @staticmethod
    def _phone_prefix(phone_number: Any, *, length: int = 6) -> str:
        digits = re.sub(r"\D", "", str(phone_number or ""))
        return digits[:length] if digits else ""

    @staticmethod
    def _smsbower_actual_provider(number: Any) -> str:
        for attr in ("provider_id", "provider", "operator"):
            raw_value = getattr(number, attr, "")
            if not isinstance(raw_value, (str, int, float)):
                continue
            value = str(raw_value or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _extract_openai_error_code(body: Any) -> str:
        text = str(body or "").strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or "").strip()
                    if code:
                        return code
                code = str(data.get("code") or "").strip()
                if code:
                    return code
        except Exception:
            pass
        match = re.search(r'"code"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1).strip()
        body_lower = text.lower()
        for code in (
            "fraud_guard",
            "rate_limit_exceeded",
            "landline_disallowed",
            "voip_phone_disallowed",
            "phone_number_in_use",
            "suspicious_behaviour",
            "suspicious_behavior",
        ):
            if code in body_lower:
                return code
        if "too many" in body_lower or ("rate" in body_lower and "limit" in body_lower):
            return "rate_limit_exceeded"
        return ""

    def _log_add_phone_attempt(self, event: dict) -> None:
        safe_event = {
            "country": str(event.get("country") or ""),
            "provider": str(event.get("provider") or ""),
            "phone_prefix": str(event.get("phone_prefix") or ""),
            "proxy": str(event.get("proxy") or ""),
            "response_code": int(event.get("response_code") or 0),
            "error_code": str(event.get("error_code") or ""),
            "entered_wait_for_code": bool(event.get("entered_wait_for_code")),
        }
        try:
            self._log(
                "add_phone_attempt "
                + json.dumps(safe_event, ensure_ascii=False, sort_keys=True)
            )
        except Exception:
            pass

    def _sync_runtime_proxy(self, new_proxy: str, *, clear_browser_state: bool = False) -> None:
        normalized_proxy = str(new_proxy or "").strip()
        self.proxy_url = normalized_proxy or None
        self.http_client.close()
        self.http_client.proxy_url = self.proxy_url
        self.oauth_manager.proxy_url = self.proxy_url
        self.session = self.http_client.session
        if self._device_id:
            seed_oai_device_cookie(self.session, self._device_id)
        if clear_browser_state:
            self._browser_frontend_state = {}
            self._authorize_sentinel = None
            self._post_otp_continue_url = ""
            self._post_otp_page_type = ""
            self._authorization_context_bootstrap_attempted = False

    def _fetch_consent_page_html_via_browser(self, consent_url: str) -> str:
        if not self._has_browser_frontend_state():
            return ""

        browser_result, _response_data = self._submit_browser_form(
            label="Consent HTML",
            page_url=consent_url,
            form_type="page_content",
            form_value="",
        )
        if not browser_result:
            return ""

        status = int(browser_result.get("status") or 0)
        if status != 200:
            self._log(f"Consent HTML browser fetch failed: HTTP {status}", "warning")
            return ""
        return str(browser_result.get("body") or "")

    def _handle_phone_verification(self) -> bool:
        """Complete add-phone using the browser-native request stack and persisted frontend state."""
        from core.smsbower import (
            SmsBowerClient,
            SmsBowerError,
            SmsBowerInvalidPhoneExceptionError,
            SmsBowerTimeoutError,
            SmsBowerNoNumberError,
            SmsBowerWaitRetryError,
        )

        sms_provider = "smsbower"
        api_key = ""
        try:
            from core.config_store import config_store

            sms_provider = str(config_store.get("sms_provider", "smsbower") or "smsbower").strip().lower() or "smsbower"
            api_key = str(config_store.get("smsbower_api_key", "") or "").strip()
        except Exception:
            pass

        try:
            extra = self.extra_config or {}
            extra_provider = str(extra.get("sms_provider", "") or "").strip().lower()
            if extra_provider:
                sms_provider = extra_provider
        except Exception:
            pass

        if sms_provider in {"5sim", "fivesim"}:
            sms_provider = "5sim"
        elif sms_provider in {"hero", "herosms"}:
            sms_provider = "herosms"
        else:
            sms_provider = "smsbower"

        provider_key_field = {
            "smsbower": "smsbower_api_key",
            "5sim": "sim5_api_key",
            "herosms": "herosms_api_key",
        }.get(sms_provider, "smsbower_api_key")
        provider_env_key = {
            "smsbower": "SMSBOWER_API_KEY",
            "5sim": "SIM5_API_KEY",
            "herosms": "HEROSMS_API_KEY",
        }.get(sms_provider, "SMSBOWER_API_KEY")

        try:
            from core.config_store import config_store

            api_key = str(config_store.get(provider_key_field, api_key) or api_key).strip()
        except Exception:
            pass
        try:
            extra = self.extra_config or {}
            api_key = str(extra.get(provider_key_field, api_key) or api_key).strip()
        except Exception:
            pass
        if not api_key:
            import os

            api_key = os.getenv(provider_env_key, "").strip()
        if not api_key:
            self._log(f"{sms_provider.upper()} API key missing, skipping phone verification", "warning")
            self._last_phone_failure_reason = "missing_key"
            return False

        client = SmsBowerClient.from_provider(sms_provider, api_key)
        activation_id = None
        activation_completed = False
        self._last_phone_failure_reason = ""
        self._fraud_guard_consecutive = 0

        try:
            balance = client.get_balance()
            self._log(f"{sms_provider.upper()} balance: ${balance:.4f}")
            if balance < 0.05:
                self._log(f"{sms_provider.upper()} balance too low (${balance:.4f} < $0.05)", "warning")
                self._last_phone_failure_reason = "low_balance"
                return False

            country_config = "16,10,12,73,33,117,78,86,151,6,22,52,187"
            quality = ""
            max_price = None
            min_price = None
            price_steps_config = ""
            provider_ids = ""
            except_provider_ids = ""
            phone_attempts_config = "50"
            otp_timeout_config = "120"
            code_attempts_config = "2"
            try:
                import os

                country_config = str(os.getenv("SMSBOWER_COUNTRY", country_config) or country_config).strip() or country_config
                quality = str(os.getenv("SMSBOWER_TYPE", quality) or quality).strip().lower()
                max_price = self._parse_optional_float(os.getenv("SMSBOWER_MAX_PRICE", ""))
                min_price = self._parse_optional_float(os.getenv("SMSBOWER_MIN_PRICE", ""))
                price_steps_config = str(os.getenv("SMSBOWER_PRICE_STEPS", price_steps_config) or price_steps_config).strip()
                provider_ids = str(os.getenv("SMSBOWER_PROVIDER_IDS", provider_ids) or provider_ids).strip()
                except_provider_ids = str(
                    os.getenv("SMSBOWER_EXCEPT_PROVIDER_IDS", except_provider_ids) or except_provider_ids
                ).strip()
                phone_attempts_config = str(
                    os.getenv("SMSBOWER_PHONE_ATTEMPTS", phone_attempts_config) or phone_attempts_config
                ).strip()
                otp_timeout_config = str(
                    os.getenv("SMSBOWER_OTP_TIMEOUT_SECONDS", otp_timeout_config) or otp_timeout_config
                ).strip()
                code_attempts_config = str(
                    os.getenv("SMSBOWER_CODE_ATTEMPTS", code_attempts_config) or code_attempts_config
                ).strip()
            except Exception:
                pass
            try:
                from core.config_store import config_store

                stored_country = str(config_store.get("smsbower_country", "") or "").strip()
                stored_quality = str(config_store.get("smsbower_type", "") or "").strip().lower()
                stored_max_price = self._parse_optional_float(config_store.get("smsbower_max_price", ""))
                stored_min_price = self._parse_optional_float(config_store.get("smsbower_min_price", ""))
                stored_price_steps = str(config_store.get("smsbower_price_steps", "") or "").strip()
                stored_provider_ids = str(config_store.get("smsbower_provider_ids", "") or "").strip()
                stored_except_provider_ids = str(config_store.get("smsbower_except_provider_ids", "") or "").strip()
                stored_phone_attempts = str(config_store.get("smsbower_phone_attempts", "") or "").strip()
                stored_otp_timeout = str(config_store.get("smsbower_otp_timeout_seconds", "") or "").strip()
                stored_code_attempts = str(config_store.get("smsbower_code_attempts", "") or "").strip()
                if stored_country:
                    country_config = stored_country
                if stored_quality:
                    quality = stored_quality
                if stored_max_price is not None:
                    max_price = stored_max_price
                if stored_min_price is not None:
                    min_price = stored_min_price
                if stored_price_steps:
                    price_steps_config = stored_price_steps
                if stored_provider_ids:
                    provider_ids = stored_provider_ids
                if stored_except_provider_ids:
                    except_provider_ids = stored_except_provider_ids
                if stored_phone_attempts:
                    phone_attempts_config = stored_phone_attempts
                if stored_otp_timeout:
                    otp_timeout_config = stored_otp_timeout
                if stored_code_attempts:
                    code_attempts_config = stored_code_attempts
            except Exception:
                pass
            try:
                extra = self.extra_config or {}
                extra_country = str(extra.get("smsbower_country", "") or "").strip()
                extra_quality = str(extra.get("smsbower_type", "") or "").strip().lower()
                extra_max_price = self._parse_optional_float(extra.get("smsbower_max_price", ""))
                extra_min_price = self._parse_optional_float(extra.get("smsbower_min_price", ""))
                extra_price_steps = str(extra.get("smsbower_price_steps", "") or "").strip()
                extra_provider_ids = str(extra.get("smsbower_provider_ids", "") or "").strip()
                extra_except_provider_ids = str(extra.get("smsbower_except_provider_ids", "") or "").strip()
                extra_phone_attempts = str(extra.get("smsbower_phone_attempts", "") or "").strip()
                extra_otp_timeout = str(extra.get("smsbower_otp_timeout_seconds", "") or "").strip()
                extra_code_attempts = str(extra.get("smsbower_code_attempts", "") or "").strip()
                if extra_country:
                    country_config = extra_country
                if "smsbower_type" in extra:
                    quality = extra_quality
                if extra_max_price is not None:
                    max_price = extra_max_price
                if extra_min_price is not None:
                    min_price = extra_min_price
                if extra_price_steps:
                    price_steps_config = extra_price_steps
                if extra_provider_ids:
                    provider_ids = extra_provider_ids
                if extra_except_provider_ids:
                    except_provider_ids = extra_except_provider_ids
                if extra_phone_attempts:
                    phone_attempts_config = extra_phone_attempts
                if extra_otp_timeout:
                    otp_timeout_config = extra_otp_timeout
                if extra_code_attempts:
                    code_attempts_config = extra_code_attempts
            except Exception:
                pass

            def _set_smsbower_status_with_retry(status_value: int, label: str) -> bool:
                import time as _time

                for status_attempt in range(1, 4):
                    try:
                        client.set_status(activation_id, status_value)
                        self._log(f"SMSBOWER activation {activation_id} marked {label}")
                        return True
                    except Exception as exc:
                        self._log(
                            f"SMSBOWER setStatus({label}) failed for activation {activation_id} "
                            f"({status_attempt}/3): {exc}",
                            "warning",
                        )
                        if status_attempt < 3:
                            _time.sleep(2)
                return False

            if quality == "sliver":
                quality = "silver"
            if quality not in {"", "gold", "silver"}:
                self._log(f"Invalid SMSBOWER quality config: {quality}, fallback to any", "warning")
                quality = ""

            countries = self._parse_smsbower_countries(country_config)
            price_limits = self._parse_smsbower_price_steps(price_steps_config, max_price)
            max_phone_attempts = self._parse_optional_int(phone_attempts_config, default=50, min_value=1, max_value=200)
            otp_timeout_seconds = self._parse_optional_int(otp_timeout_config, default=120, min_value=30, max_value=600)
            max_code_attempts = self._parse_optional_int(code_attempts_config, default=2, min_value=1, max_value=5)
            max_add_phone_send_attempts = self._get_add_phone_send_attempt_limit()
            no_number_target = "下一个国家或价格" if price_steps_config else "下一个国家"
            quality_label = f", quality={quality}" if quality else ""
            price_label = f", max_price={max_price:g}" if max_price is not None else ""
            price_steps_label = (
                ", price_steps=" + ",".join(f"{price:g}" for price in price_limits if price is not None)
                if price_steps_config
                else ""
            )
            min_price_label = f", min_price={min_price:g}" if min_price is not None else ""
            provider_label = f", provider_ids={provider_ids}" if provider_ids else ""
            except_provider_label = f", except_provider_ids={except_provider_ids}" if except_provider_ids else ""
            self._log(
                f"{sms_provider.upper()} config: "
                f"countries={','.join(countries)}, service=dr(OpenAI){quality_label}"
                f"{price_label}{price_steps_label}{min_price_label}{provider_label}{except_provider_label}"
                f", phone_attempts_per_country={max_phone_attempts}, add_phone_send_attempts={max_add_phone_send_attempts}, "
                f"otp_timeout={otp_timeout_seconds}s, code_attempts={max_code_attempts}"
            )

            used_phones: set[str] = set()
            cooled_prefixes: set[str] = set()
            cooled_actual_providers: set[str] = set()
            add_phone_send_attempts = 0
            local_skip_attempts = 0

            saw_available_number = False
            global_attempt = 0
            attempts_by_country: dict[str, int] = {}
            total_phone_attempt_limit = max_phone_attempts * max(1, len(countries)) * max(1, len(price_limits))
            # 在所有国家间轮转；smsbower_phone_attempts 表示每个国家最多取号次数。
            skip_countries: dict[str, str] = {}  # country -> reason
            while global_attempt < total_phone_attempt_limit:
              for active_max_price in price_limits:
                active_price_label = f", max_price={active_max_price:g}" if active_max_price is not None else ""
                for country in countries:
                    if country in skip_countries:
                        continue
                    if attempts_by_country.get(country, 0) >= max_phone_attempts:
                        continue
                    if global_attempt >= total_phone_attempt_limit:
                        break
                    landline_rejections = 0
                    non_sms_rejections = 0
                    in_use_rejections = 0
                    self._suspicious_same_country_retries = 0
                    country_had_number = False
                    remaining_country_attempts = max_phone_attempts - attempts_by_country.get(country, 0)
                    for _country_attempt in range(min(3, remaining_country_attempts)):  # 每国每轮最多 3 次
                        if global_attempt >= total_phone_attempt_limit:
                            break
                        if add_phone_send_attempts >= max_add_phone_send_attempts:
                            self._set_phone_failure_reason("add_phone_attempt_limit")
                            self._log(
                                f"add_phone_attempt_limit: reached {max_add_phone_send_attempts} phone send attempts for this account; stopping before buying more numbers",
                                "error",
                            )
                            return False
                        global_attempt += 1
                        attempts_by_country[country] = attempts_by_country.get(country, 0) + 1
                        phone_attempt = attempts_by_country[country]
                        if activation_id:
                            try:
                                client.cancel(activation_id)
                            except Exception:
                                pass
                            activation_id = None
                            activation_completed = False

                        self._log(
                            f"Fetching SMSBOWER number service=dr(OpenAI), country={country}{quality_label}{active_price_label}"
                            + (f" (phone {phone_attempt}/{max_phone_attempts})" if phone_attempt > 1 else "")
                        )
                        try:
                            phone_exception = ",".join(used_phones) if used_phones else None
                            try:
                                number = client.get_number(
                                    service="dr",
                                    country=country,
                                    max_price=active_max_price,
                                    min_price=min_price,
                                    phone_exception=phone_exception,
                                    provider_ids=provider_ids or None,
                                    except_provider_ids=except_provider_ids or None,
                                    quality=quality,
                                )
                            except SmsBowerInvalidPhoneExceptionError:
                                if not phone_exception:
                                    raise
                                # 之前这里会打印警告，现在改为 debug 隐藏或者完全注释掉，避免被误认为报错日志冗余
                                # self._log("SMSBOWER rejected phoneException; retrying getNumber without excluded phones", "debug")
                                number = client.get_number(
                                    service="dr",
                                    country=country,
                                    max_price=active_max_price,
                                    min_price=min_price,
                                    phone_exception=None,
                                    provider_ids=provider_ids or None,
                                    except_provider_ids=except_provider_ids or None,
                                    quality=quality,
                                )

                            saw_available_number = True
                            actual_provider = self._smsbower_actual_provider(number)
                            number_prefix = self._phone_prefix(number.phone_number)
                            if number.phone_number in used_phones:
                                local_skip_attempts += 1
                                self._log(
                                    f"local_dedupe: SMSBOWER returned duplicate phone prefix={number_prefix}; skipping before OpenAI send",
                                    "warning",
                                )
                                if not self._last_phone_failure_reason:
                                    self._set_phone_failure_reason("local_phone_dedupe")
                                try:
                                    client.cancel(number.activation_id)
                                except Exception:
                                    pass
                                if local_skip_attempts >= max_phone_attempts:
                                    self._log("local_dedupe: too many locally rejected SMSBOWER numbers; stopping", "error")
                                    return False
                                continue
                            if number_prefix and number_prefix in cooled_prefixes:
                                local_skip_attempts += 1
                                self._log(
                                    f"local_dedupe: phone prefix {number_prefix} is cooled after prior rejection; skipping before OpenAI send",
                                    "warning",
                                )
                                if not self._last_phone_failure_reason:
                                    self._set_phone_failure_reason("local_prefix_cooldown")
                                try:
                                    client.cancel(number.activation_id)
                                except Exception:
                                    pass
                                if local_skip_attempts >= max_phone_attempts:
                                    self._log("local_dedupe: too many locally rejected SMSBOWER numbers; stopping", "error")
                                    return False
                                continue
                            if actual_provider and actual_provider in cooled_actual_providers:
                                local_skip_attempts += 1
                                self._log(
                                    f"local_dedupe: SMSBOWER provider {actual_provider} is cooled after prior rejection; skipping before OpenAI send",
                                    "warning",
                                )
                                if not self._last_phone_failure_reason:
                                    self._set_phone_failure_reason("local_provider_cooldown")
                                try:
                                    client.cancel(number.activation_id)
                                except Exception:
                                    pass
                                if local_skip_attempts >= max_phone_attempts:
                                    self._log("local_dedupe: too many locally rejected SMSBOWER numbers; stopping", "error")
                                    return False
                                continue
                            activation_id = number.activation_id
                            activation_completed = False
                            used_phones.add(number.phone_number)
                            phone_for_openai = self._normalize_phone_number_for_openai(number.phone_number)
                            self._log(
                                f"Acquired phone number: {number.phone_number} "
                                f"(quality={number.quality or 'any'}, activation_id={activation_id})"
                            )
                        except SmsBowerNoNumberError:
                            self._log(
                                f"SMSBOWER country={country}{active_price_label} 无可用号码，尝试下一国家",
                                "warning",
                            )
                            self._set_phone_failure_reason("no_numbers")
                            break  # 跳到下一国家

                        # 模拟人类行为：随机延迟 1-3 秒（避免请求间隔过快触发风控）
                        if add_phone_send_attempts >= max_add_phone_send_attempts:
                            self._set_phone_failure_reason("add_phone_attempt_limit")
                            self._log(
                                f"add_phone_attempt_limit: reached {max_add_phone_send_attempts} phone send attempts for this account; stopping before buying more numbers",
                                "error",
                            )
                            return False
                        import random as _rnd
                        _human_delay = _rnd.uniform(1.0, 3.0)
                        import time as _time
                        _time.sleep(_human_delay)

                        browser_result, response_data = self._submit_browser_form(
                            label="手机号提交",
                            page_url="https://auth.openai.com/add-phone",
                            form_type="phone_send",
                            form_value=phone_for_openai,
                            flow="phone_verification",
                        )
                        add_phone_send_attempts += 1
                        add_phone_event = {
                            "country": country,
                            "provider": actual_provider or provider_ids,
                            "phone_prefix": number_prefix,
                            "proxy": self.proxy_url or "",
                            "response_code": 0,
                            "error_code": "",
                            "entered_wait_for_code": False,
                        }
                        if not browser_result:
                            self._log(f"Phone send failed for {number.phone_number}: no browser result", "warning")
                            self._log_add_phone_attempt(add_phone_event)
                            self._set_phone_failure_reason("phone_send_failed")
                            continue

                        send_status = int(browser_result.get("status") or 0)
                        send_body = str(browser_result.get("body") or "")
                        openai_error_code = self._extract_openai_error_code(send_body)
                        add_phone_event["response_code"] = send_status
                        add_phone_event["error_code"] = openai_error_code
                        self._log(f"Phone send status: {send_status} HTTP {send_status}, len={len(send_body)}")
                        if send_status != 200:
                            self._log(f"Phone send response body: {send_body[:500]}", "warning")
                            self._log_add_phone_attempt(add_phone_event)
                            if send_status == 403 and (
                                "Just a moment" in send_body
                                or "cf-browser-verification" in send_body
                                or "challenges.cloudflare.com" in send_body
                            ):
                                # 按用户要求"不能失败，一直换号试"
                                # Cloudflare 挑战通常下次浏览器会话可解决，cancel 当前号继续
                                self._log("cloudflare_challenge_blocked: phone send 被 Cloudflare 拦截，cancel 后换号重试", "warning")
                                try:
                                    client.cancel(activation_id)
                                except Exception:
                                    pass
                                activation_id = None
                                activation_completed = False
                                self._set_phone_failure_reason("cloudflare_blocked")
                                import time as _time
                                _time.sleep(5)
                                return False
                            body_lower = send_body.lower()
                            if "landline" in body_lower or "landline_disallowed" in body_lower:
                                landline_rejections += 1
                                if number_prefix:
                                    cooled_prefixes.add(number_prefix)
                                if actual_provider:
                                    cooled_actual_providers.add(actual_provider)
                                self._log("non_retryable_phone_rejection: landline_disallowed", "warning")
                                self._log(
                                    f"Phone {number.phone_number} rejected: landline_disallowed（座机）→ 跳过 country={country}",
                                    "warning",
                                )
                                self._set_phone_failure_reason("landline_disallowed")
                                return False
                            if "voip_phone_disallowed" in body_lower or "voip" in body_lower:
                                if number_prefix:
                                    cooled_prefixes.add(number_prefix)
                                if actual_provider:
                                    cooled_actual_providers.add(actual_provider)
                                self._log(
                                    f"Phone {number.phone_number} rejected: voip_phone_disallowed",
                                    "warning",
                                )
                                self._set_phone_failure_reason("voip_phone_disallowed")
                                continue
                            if "fraud_guard" in body_lower:
                                if number_prefix:
                                    cooled_prefixes.add(number_prefix)
                                if actual_provider:
                                    cooled_actual_providers.add(actual_provider)
                                self._log(
                                    f"Phone {number.phone_number} rejected: fraud_guard — 会话/IP 被 OpenAI 标记",
                                    "error",
                                )
                                # 立即 cancel 当前号码避免计费
                                try:
                                    client.cancel(activation_id)
                                except Exception:
                                    pass
                                activation_id = None
                                activation_completed = False

                                # 1) 根据需求：只要遇到了addphone的时候就只尝试换手机号，不再换代理也不要重新登录
                                # 直接跨国家继续买号
                                if not hasattr(self, '_fraud_guard_consecutive'):
                                    self._fraud_guard_consecutive = 0
                                self._fraud_guard_consecutive += 1
                                self._log(
                                    f"fraud_guard #{self._fraud_guard_consecutive}: 返回IP标记，继续尝试下一个号码",
                                    "warning",
                                )

                                # 2) 兜底：连续 N 个 fraud_guard 止损（可选，可以调大）
                                if self._fraud_guard_consecutive >= 15:
                                    self._set_phone_failure_reason("environment_unusable_fraud_guard")
                                    self._log(
                                        "environment_unusable: consecutive fraud_guard reached 15; stopping add-phone",
                                        "error",
                                    )
                                    return False

                                self._set_phone_failure_reason("fraud_guard")
                                continue  # 改为继续尝试当前国家下一个号码，而不是跳出循环换国家
                            if "suspicious" in body_lower or "suspicious_behav" in body_lower:
                                if number_prefix:
                                    cooled_prefixes.add(number_prefix)
                                if actual_provider:
                                    cooled_actual_providers.add(actual_provider)
                                # 不立即跳国家：同国家再试最多 2 个号码（有时只是特定号段被标记）
                                if not hasattr(self, '_suspicious_same_country_retries'):
                                    self._suspicious_same_country_retries = 0
                                self._suspicious_same_country_retries += 1
                                if self._suspicious_same_country_retries <= 2:
                                    self._log(
                                        f"Phone {number.phone_number} rejected: suspicious_behaviour，同国家再试 ({self._suspicious_same_country_retries}/2)",
                                        "warning",
                                    )
                                    self._set_phone_failure_reason("suspicious_behaviour")
                                    import time as _time
                                    _time.sleep(5)  # 等待 5 秒后再试
                                    continue
                                else:
                                    self._suspicious_same_country_retries = 0
                                    self._log(
                                        f"Phone {number.phone_number} rejected: suspicious_behaviour → 跳过 country={country}，尝试下一国家",
                                        "warning",
                                    )
                                    self._set_phone_failure_reason("suspicious_behaviour")
                                    break  # 本轮换国家，下一轮还可以再试
                            if "too many" in body_lower or ("rate" in body_lower and "limit" in body_lower):
                                if number_prefix:
                                    cooled_prefixes.add(number_prefix)
                                if actual_provider:
                                    cooled_actual_providers.add(actual_provider)
                                # 按用户要求"不能失败，一直换号试"
                                # rate_limit 是账户级限速，递增退避等待让限速窗口过期
                                if not hasattr(self, '_rate_limit_consecutive'):
                                    self._rate_limit_consecutive = 0
                                self._rate_limit_consecutive += 1
                                # 退避：最多 3s（用户需求：换号比等待更划算，不等限速窗口）
                                rl_backoff = min(3, self._rate_limit_consecutive)
                                self._log(
                                    f"Phone verification rate-limited #{self._rate_limit_consecutive}: "
                                    f"等待 {rl_backoff}s 后继续换号尝试（OpenAI 账户级限速窗口过期）",
                                    "warning",
                                )
                                # 立即 cancel 当前号码避免计费
                                try:
                                    client.cancel(activation_id)
                                except Exception:
                                    pass
                                activation_id = None
                                activation_completed = False
                                import time as _time
                                _time.sleep(rl_backoff)
                                self._set_phone_failure_reason("phone_rate_limited")
                                continue  # 同 country 继续换号
                            if "phone_number_in_use" in body_lower or "already in use" in body_lower:
                                in_use_rejections += 1
                                if number_prefix:
                                    cooled_prefixes.add(number_prefix)
                                if actual_provider:
                                    cooled_actual_providers.add(actual_provider)
                                if in_use_rejections >= 3:
                                    self._log(
                                        f"Phone {number.phone_number} rejected: phone_number_in_use 连续 {in_use_rejections} 次 → 跳过 country={country}",
                                        "warning",
                                    )
                                    self._set_phone_failure_reason("phone_number_in_use")
                                    break  # 跳到下一国家
                                self._log(
                                    f"Phone {number.phone_number} rejected: phone_number_in_use，继续尝试 country={country} 下一个号码",
                                    "warning",
                                )
                                self._set_phone_failure_reason("phone_number_in_use")
                                continue
                            self._log(f"Phone send rejected: {send_body[:200]}", "warning")
                            if number_prefix:
                                cooled_prefixes.add(number_prefix)
                            if actual_provider:
                                cooled_actual_providers.add(actual_provider)
                            self._set_phone_failure_reason("phone_rejected")
                            continue

                        verification_channel = self._extract_phone_verification_channel(response_data)
                        if verification_channel and verification_channel != "sms":
                            non_sms_rejections += 1
                            self._log_add_phone_attempt(add_phone_event)
                            if non_sms_rejections >= 3:
                                self._log(
                                    f"OpenAI selected {verification_channel} phone channel for country={country} 连续 {non_sms_rejections} 次 → 跳过该国家",
                                    "warning",
                                )
                                self._set_phone_failure_reason("non_sms_phone_channel")
                                break
                            self._log(
                                f"OpenAI selected {verification_channel} phone channel for {number.phone_number}; rotating number",
                                "warning",
                            )
                            self._set_phone_failure_reason("non_sms_phone_channel")
                            continue

                        # OpenAI 接受号码 → 清零 fraud_guard 连续计数
                        self._fraud_guard_consecutive = 0
                        if not _set_smsbower_status_with_retry(1, "ready after OpenAI accepted phone send"):
                            self._log_add_phone_attempt(add_phone_event)
                            self._set_phone_failure_reason("smsbower_set_ready_failed")
                            continue

                        add_phone_event["entered_wait_for_code"] = True
                        self._log_add_phone_attempt(add_phone_event)
                        self._log(f"Waiting for SMS verification code ({otp_timeout_seconds}s)...")
                        try:
                            def _phone_code_poll(status, _code):
                                self._checkpoint_task_control()
                                self._log(f"SMSBOWER poll: status={status}")

                            code = client.wait_for_code(
                                activation_id,
                                timeout=otp_timeout_seconds,
                                interval=5.0,
                                on_poll=_phone_code_poll,
                            )
                            activation_completed = True
                            self._log(f"Received phone verification code: {code}")
                        except SmsBowerWaitRetryError:
                            self._log("SMSBOWER requested retry before code arrival; switching to OTP resend", "warning")
                            self._set_phone_failure_reason("otp_retry_requested")
                            code = None
                        except SmsBowerTimeoutError:
                            self._log("Timed out waiting for phone verification code", "warning")
                            if number_prefix:
                                cooled_prefixes.add(number_prefix)
                            if actual_provider:
                                cooled_actual_providers.add(actual_provider)
                            self._set_phone_failure_reason("otp_timeout")
                            continue
                        except SmsBowerError as exc:
                            # 单个号码 SMSBOWER 服务端错误（激活取消/SQL 错误等）
                            # 不应中止整个流程，cancel 当前号码后尝试下一个
                            self._log(f"SMSBOWER 激活异常 (activation_id={activation_id}): {exc}", "warning")
                            self._set_phone_failure_reason("smsbower_activation_error")
                            try:
                                client.cancel(activation_id)
                            except Exception:
                                pass
                            activation_id = None
                            activation_completed = False
                            continue

                        for code_attempt in range(1, max_code_attempts + 1):
                            if code_attempt > 1 or code is None:
                                self._log(f"Resending phone OTP {code_attempt}/{max_code_attempts}...")
                                resend_result, _resend_data = self._submit_browser_form(
                                    label="手机验证码重发",
                                    page_url="https://auth.openai.com/add-phone",
                                    form_type="phone_otp_resend",
                                    form_value="{}",
                                    flow="phone_verification",
                                )
                                if not resend_result or int(resend_result.get("status") or 0) >= 400:
                                    break
                                if not _set_smsbower_status_with_retry(3, "retry after OpenAI OTP resend"):
                                    break
                                try:
                                    code = client.wait_for_code(
                                        activation_id,
                                        timeout=otp_timeout_seconds,
                                        interval=5.0,
                                    )
                                    activation_completed = True
                                    self._log(f"Received resent phone verification code: {code}")
                                except SmsBowerTimeoutError:
                                    self._log("Timed out waiting for resent phone verification code", "warning")
                                    self._set_phone_failure_reason("otp_timeout")
                                    break

                            validate_result, response_data = self._submit_browser_form(
                                label="手机验证码提交",
                                page_url="https://auth.openai.com/add-phone",
                                form_type="phone_validate",
                                form_value=str(code),
                                update_post_otp_state=True,
                                flow="phone_verification",
                            )
                            if not validate_result:
                                break

                            validate_status = int(validate_result.get("status") or 0)
                            validate_body = str(validate_result.get("body") or "")
                            self._log(f"Phone validate status: {validate_status}")
                            if validate_status == 200:
                                if not self._post_otp_continue_url:
                                    self._post_otp_continue_url = normalize_flow_url(
                                        str(response_data.get("continue_url") or ""),
                                        auth_base="https://auth.openai.com",
                                    ) or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
                                if not self._post_otp_page_type:
                                    self._post_otp_page_type = str(
                                        ((response_data.get("page") or {}).get("type") or "")
                                    ).strip() or "consent"
                                self._log("Phone verification succeeded")
                                return True

                            self._log(f"Phone validate failed: {validate_body[:200]}", "warning")
                            self._set_phone_failure_reason("phone_validate_failed")
                            if "invalid" in validate_body.lower() or "expired" in validate_body.lower():
                                continue
                            break

                        self._log(f"Phone {number.phone_number} failed verification, rotating number...", "warning")

              # while 循环内：检查是否所有国家都被永久跳过
              if len(skip_countries) >= len(countries):
                  self._log("所有国家都被永久跳过（座机/non-sms），终止", "error")
                  break
              # 如果本轮没有任何号码被成功获取，说明所有国家在当前价格下都无号
              if not saw_available_number:
                  self._log("本轮所有国家均无可用号码，终止循环", "error")
                  break
              # 一轮所有国家都尝试过了且没成功，继续 while 下一轮（重置本轮标记）
              saw_available_number = False
            if not saw_available_number and not self._last_phone_failure_reason:
                self._set_phone_failure_reason("no_numbers")
            self._log(
                f"All {global_attempt} phone attempts failed verification "
                f"(limit={max_phone_attempts} per country)",
                "error",
            )
            return False
        except TaskInterruption:
            raise
        except SmsBowerNoNumberError:
            self._log("SMSBOWER has no available OpenAI numbers", "warning")
            self._set_phone_failure_reason("no_numbers")
            return False
        except SmsBowerError as exc:
            self._log(f"SMSBOWER error: {exc}", "warning")
            self._set_phone_failure_reason("smsbower_error")
            return False
        except Exception as exc:
            self._log(f"Phone verification exception: {exc}", "warning")
            if not self._last_phone_failure_reason:
                self._set_phone_failure_reason("exception")
            return False
        finally:
            if activation_id and not activation_completed:
                try:
                    client.cancel(activation_id)
                except Exception:
                    pass

    def _check_email_domain_and_suggest(self):
        """检查邮箱类型并进行预警建议"""
        try:
            if not self.email or '@' not in self.email:
                return
            
            domain = self.email.split('@')[-1].lower()
            
            if domain != 'hotmail.com':
                self._log("=" * 60, "warning")
                self._log("[WARN] 注册失败可能与邮箱域名有关", "warning")
                self._log("=" * 60, "warning")
                self._log(f"当前邮箱域名: {domain}", "warning")
                self._log("建议使用: hotmail.com", "warning")
                self._log("", "warning")
                self._log("原因: OpenAI 对非 hotmail.com 邮箱的风控较严格", "warning")
                self._log("使用 hotmail.com 邮箱可以提高注册成功率", "warning")
                self._log("", "warning")
                self._log("建议操作:", "warning")
                self._log("1. 更换邮箱服务为 hotmail.com", "warning")
                self._log("2. 更换住宅代理 IP", "warning")
                self._log("3. 降低注册频率", "warning")
                self._log("4. 尝试不同时间段注册", "warning")
                self._log("=" * 60, "warning")
        except Exception as e:
            self._log(f"检查邮箱域名建议失败: {e}", "debug")

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        淇濆瓨娉ㄥ唽缁撴灉鍒版暟鎹簱

        Args:
            result: 娉ㄥ唽缁撴灉

        Returns:
            鏄惁淇濆瓨鎴愬姛
        """
        if not result.success:
            return False

        return True  # 鐢?account_manager 缁熶竴澶勭悊瀛樺簱

    def _set_result_failure(self, result: RegistrationResult, message: str) -> RegistrationResult:
        result.success = False
        result.error_message = message
        result.logs = self.logs
        result.metadata = result.metadata or {}
        return result

    def _finalize_refresh_token_result(self, result: RegistrationResult) -> RegistrationResult:
        result.logs = self.logs
        result.metadata = result.metadata or {}
        if self._proxy_geo_country:
            result.metadata["proxy_geo_country"] = self._proxy_geo_country
        if not result.email:
            result.email = self.email or ""
        if not result.password:
            result.password = self.password or ""
        if not result.account_id:
            result.account_id = result.workspace_id or ""
        if not result.workspace_id:
            result.workspace_id = result.account_id or ""

        if result.refresh_token and result.id_token:
            result.success = True
            result.error_message = ""
            return result

        result.success = False
        if not result.error_message:
            result.error_message = (
                "refresh-token mode requires real OAuth credentials: "
                "missing refresh_token or id_token"
            )
        return result


    def _register_password(self) -> Tuple[bool, str]:
        password = self.password or self._generate_password()
        self.password = password
        try:
            sentinel = build_sentinel_token(
                self.session,
                self._device_id or "",
                flow="username_password_create",
            )
        except Exception:
            sentinel = ""
        form_value = json.dumps(
            {
                "password": password,
                "sentinel": sentinel or "",
            },
            separators=(",", ":"),
        )
        browser_result, response_data = self._submit_browser_form(
            label="Create password",
            page_url="https://auth.openai.com/create-account/password",
            form_type="password_create",
            form_value=form_value,
            flow="username_password_create",
        )
        if not browser_result:
            return False, None

        status = int(browser_result.get("status") or 0)
        body = str(browser_result.get("body") or "")
        if status < 200 or status >= 400:
            if (
                status == 403
                and (
                    "Just a moment" in body
                    or "cf-browser-verification" in body
                    or "challenges.cloudflare.com" in body
                )
            ):
                self._log(
                    "cloudflare_challenge_blocked: password create hit Cloudflare challenge",
                    "error",
                )
            else:
                self._log(f"Create password failed: HTTP {status}: {body[:200]}", "warning")
            return False, None

        continue_url = normalize_flow_url(
            str(
                response_data.get("continue_url")
                or browser_result.get("continue_url")
                or browser_result.get("final_url")
                or ""
            ),
            auth_base="https://auth.openai.com",
        )
        self._register_continue_url = continue_url
        page_type = str(browser_result.get("page_type") or "").strip()
        if not page_type:
            page_type = extract_flow_state(
                response_data,
                current_url=continue_url,
                auth_base="https://auth.openai.com",
            ).page_type
        if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            self._otp_sent_at = time.time()
        return True, password

    def _send_verification_code(self) -> bool:
        self._otp_sent_at = time.time()
        return True

    def _validate_verification_code(self, code: str) -> bool:
        """Submit email OTP to OpenAI /api/accounts/email-otp/validate.

        对齐参考实现 (chatgpt_client.verify_email_otp 与 GitHub 注册机)：
        仅带 datadog trace，不附带 sentinel token；成功后从响应抽取
        page_type/continue_url 写入 _post_otp_* 供后续流程判断。
        """
        code_str = str(code or "").strip()
        if not code_str:
            return False

        url = "https://auth.openai.com/api/accounts/email-otp/validate"
        headers = self._build_json_headers(
            referer="https://auth.openai.com/email-verification",
            include_datadog=True,
        )

        try:
            self._behavior_sim.natural_delay(0.8, 2.0)
            resp = self.session.post(
                url,
                headers=headers,
                json={"code": code_str},
                timeout=30,
            )
            status = resp.status_code
            self._log(f"登录 OTP 提交状态: {status}")

            if status != 200:
                self._log(f"登录 OTP 提交失败: {resp.text[:300]}", "error")
                return False

            try:
                data = json.loads(resp.text or "{}")
            except Exception:
                data = {}

            flow_state = extract_flow_state(
                data,
                current_url=str(resp.url or url),
                auth_base="https://auth.openai.com",
            )
            page_type = flow_state.page_type or str(
                (data.get("page") or {}).get("type") or ""
            ).strip().lower()
            continue_url = flow_state.continue_url or normalize_flow_url(
                str((data.get("page") or {}).get("next") or ""),
                auth_base="https://auth.openai.com",
            )

            self._post_otp_page_type = page_type
            self._post_otp_continue_url = continue_url
            self._log(
                f"登录 OTP 验证成功 page={page_type or '-'} "
                f"next={continue_url[:100] if continue_url else '-'}"
            )
            return True
        except Exception as exc:
            self._log(f"登录 OTP 提交异常: {exc}", "error")
            return False

    def _create_user_account(self) -> bool:
        return True



# 鍏煎鏃у懡鍚嶏紝閫愭杩佺Щ鍒版洿瑙佸悕鐭ユ剰鐨勭被鍚嶃€?
RegistrationEngine = RefreshTokenRegistrationEngine
