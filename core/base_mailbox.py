"""邮箱池基类 - 抽象临时邮箱/收件服务"""
import base64
import json
import random
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Any, Callable
from .proxy_utils import build_requests_proxy_config
@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息
class BaseMailbox(ABC):
    def _log(self, message: str) -> None:
        log_fn = getattr(self, "_log_fn", None)
        if callable(log_fn):
            log_fn(message)
    def _checkpoint(self, *, consume_skip: bool = True) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint(
            consume_skip=consume_skip,
            attempt_id=getattr(self, "_task_attempt_token", None),
        )
    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk
    def _run_polling_wait(
        self,
        *,
        timeout: int,
        poll_interval: float,
        poll_once: Callable[[], Optional[str]],
        timeout_message: str | None = None,
    ) -> str:
        timeout_seconds = max(int(timeout or 0), 1)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._checkpoint()
            code = poll_once()
            if code:
                return code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep_with_checkpoint(min(float(poll_interval), remaining))
        self._checkpoint()
        raise TimeoutError(timeout_message or f"等待验证码超时 ({timeout_seconds}s)")
    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...
    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...
    def _safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)"""
        import re
        text = str(text or "")
        if not text:
            return None
        patterns = []
        if pattern:
            patterns.append(pattern)
        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                r"(?<!#)(?<!\d)(\d{6})(?!\d)",
            ]
        )
        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None
    def _decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re
        text = str(raw or "")
        if not text:
            return ""
        # 简单切分 Header 和 Body
        if "\r\n\r\n" in text:
            text = text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in text:
            text = text.split("\n\n", 1)[1]
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...
    def _yyds_safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)"""
        import re
        text = str(text or "")
        if not text:
            return None
        # [修复点 1]：优先过滤掉所有 URL 链接，直接从根源防止提取到追踪链接（如 SendGrid）里的随机数字
        text = re.sub(r"https?://\S+", "", text)
        patterns = []
        if pattern:
            # [修复点 2]：如果外部传入了纯 \d{6} 的粗糙正则，自动为其加上字母数字边界
            if pattern in (r"\d{6}", r"(\d{6})"):
                patterns.append(r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])")
            else:
                patterns.append(pattern)
        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                # [修复点 3]：修改兜底正则，严格要求 6 位数字前后不能有字母或数字（防止匹配 u20216706）
                r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])",
            ]
        )
        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None
    def _yyds_decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re
        text = str(raw or "")
        if not text:
            return ""
            
        # [修复点 4]：只有在明确包含常见邮件 Header 时，才进行 \r\n\r\n 切分。
        # 否则会误删 MaliAPI 等直接返回的已解析 JSON 正文内容（遇到普通的正文换行就错误截断了）
        if re.search(r"(?im)^(?:Return-Path|Received|Date|From|To|Subject|Content-Type):", text):
            if "\r\n\r\n" in text:
                text = text.split("\r\n\r\n", 1)[1]
            elif "\n\n" in text:
                text = text.split("\n\n", 1)[1]
                
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
def create_mailbox(
    provider: str, extra: dict = None, proxy: str = None
) -> "BaseMailbox":
    """工厂方法：根据 provider 创建对应的 mailbox 实例"""
    extra = extra or {}
    if provider == "tempmail_lol":
        return TempMailLolMailbox(proxy=proxy)
    elif provider == "skymail":
        return SkyMailMailbox(
            api_base=extra.get("skymail_api_base", "https://api.skymail.ink"),
            auth_token=extra.get("skymail_token", ""),
            domain=extra.get("skymail_domain", ""),
            proxy=proxy,
        )
    elif provider == "duckmail":
        return DuckMailMailbox(
            api_url=(extra.get("duckmail_api_url") or "https://www.duckmail.sbs"),
            provider_url=(
                extra.get("duckmail_provider_url") or "https://api.duckmail.sbs"
            ),
            bearer=(extra.get("duckmail_bearer") or "kevin273945"),
            domain=extra.get("duckmail_domain", ""),
            api_key=extra.get("duckmail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "duckduckgo":
        return DuckDuckGoMailbox(
            duck_email=extra.get("duckduckgo_email", ""),
            gmail_address=extra.get("duckduckgo_gmail_address", ""),
            gmail_app_password=extra.get("duckduckgo_gmail_app_password", ""),
            imap_host=extra.get("duckduckgo_imap_host", "imap.gmail.com"),
            imap_port=extra.get("duckduckgo_imap_port", 993),
            mailbox_name=extra.get("duckduckgo_mailbox", "INBOX"),
            all_mailbox_name=extra.get("duckduckgo_all_mailbox", "[Gmail]/All Mail"),
            gmail_api_mode=extra.get("duckduckgo_gmail_api_mode", "imap"),
            gmail_api_credentials=extra.get("duckduckgo_gmail_api_credentials", ""),
            gmail_api_token=extra.get("duckduckgo_gmail_api_token", ""),
            alias_mode=extra.get("duckduckgo_alias_mode", "fixed"),
            private_addresses=extra.get("duckduckgo_private_addresses", ""),
            alias_rotation=extra.get("duckduckgo_alias_rotation", "random"),
            duck_api_token=extra.get("duckduckgo_api_token", ""),
            proxy=proxy,
        )
    elif provider == "freemail":
        return FreemailMailbox(
            api_url=extra.get("freemail_api_url", ""),
            admin_token=extra.get("freemail_admin_token", ""),
            username=extra.get("freemail_username", ""),
            password=extra.get("freemail_password", ""),
            proxy=proxy,
        )
    elif provider == "mail2925":
        return Mail2925Mailbox(
            login_name=extra.get("mail2925_login_name", ""),
            password=extra.get("mail2925_password", ""),
            alias_mode=extra.get("mail2925_alias_mode", "main"),
            domain=extra.get("mail2925_domain", "2925.com"),
            proxy=proxy,
        )
    elif provider == "moemail":
        return MoeMailMailbox(
            api_url=extra.get("moemail_api_url", "https://sall.cc"),
            api_key=extra.get("moemail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "maliapi":
        return MaliAPIMailbox(
            api_url=extra.get("maliapi_base_url", "https://maliapi.215.im/v1"),
            api_key=extra.get("maliapi_api_key", ""),
            domain=extra.get("maliapi_domain", ""),
            auto_domain_strategy=extra.get("maliapi_auto_domain_strategy", ""),
            proxy=proxy,
        )
    elif provider == "gptmail":
        return GPTMailMailbox(
            api_url=extra.get("gptmail_base_url", "https://mail.chatgpt.org.uk"),
            api_key=extra.get("gptmail_api_key", ""),
            domain=extra.get("gptmail_domain", ""),
            proxy=proxy,
        )
    elif provider == "opentrashmail":
        return OpenTrashMailMailbox(
            api_url=extra.get("opentrashmail_api_url", ""),
            domain=extra.get("opentrashmail_domain", ""),
            password=extra.get("opentrashmail_password", ""),
            proxy=proxy,
        )
    elif provider == "cfworker":
        return CFWorkerMailbox(
            api_url=extra.get("cfworker_api_url", ""),
            admin_token=extra.get("cfworker_admin_token", ""),
            domain=extra.get("cfworker_domain", ""),
            domain_override=extra.get("cfworker_domain_override", ""),
            domains=extra.get("cfworker_domains", ""),
            enabled_domains=extra.get("cfworker_enabled_domains", ""),
            subdomain=extra.get("cfworker_subdomain", ""),
            random_subdomain=extra.get("cfworker_random_subdomain", False),
            fingerprint=extra.get("cfworker_fingerprint", ""),
            custom_auth=extra.get("cfworker_custom_auth", ""),
            proxy=proxy,
        )
    elif provider == "luckmail":
        return LuckMailMailbox(
            base_url=extra.get("luckmail_base_url") or "https://mails.luckyous.com/",
            api_key=extra.get("luckmail_api_key", ""),
            project_code=extra.get("luckmail_project_code", ""),
            email_type=extra.get("luckmail_email_type", ""),
            domain=extra.get("luckmail_domain", ""),
            proxy=proxy,
        )
    else:  # laoudo
        return LaoudoMailbox(
            auth_token=extra.get("laoudo_auth", ""),
            email=extra.get("laoudo_email", ""),
            account_id=extra.get("laoudo_account_id", ""),
        )
class LaoudoMailbox(BaseMailbox):
    """laoudo.com 邮箱服务"""
    def __init__(self, auth_token: str, email: str, account_id: str):
        self.auth = auth_token
        self._email = email
        self._account_id = account_id
        self.api = "https://laoudo.com/api/email"
        self._ua = "Mozilla/5.0"
    def get_email(self) -> MailboxAccount:
        if not self._email:
            raise RuntimeError(
                "Laoudo 邮箱未配置或已失效，请检查 laoudo_auth、laoudo_email、laoudo_account_id 配置，"
                "或切换到 tempmail_lol（无需配置）"
            )
        return MailboxAccount(email=self._email, account_id=self._account_id)
    def get_current_ids(self, account: MailboxAccount) -> set:
        from curl_cffi import requests as curl_requests
        try:
            r = curl_requests.get(
                f"{self.api}/list",
                params={
                    "accountId": account.account_id,
                    "allReceive": 0,
                    "emailId": 0,
                    "timeSort": 1,
                    "size": 50,
                    "type": 0,
                },
                headers={"authorization": self.auth, "user-agent": self._ua},
                timeout=15,
                impersonate="chrome131",
            )
            if r.status_code == 200:
                mails = r.json().get("data", {}).get("list", []) or []
                return {
                    m.get("id") or m.get("emailId")
                    for m in mails
                    if m.get("id") or m.get("emailId")
                }
        except Exception:
            pass
        return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "trae",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from curl_cffi import requests as curl_requests
        seen = set(before_ids) if before_ids else set()
        h = {"authorization": self.auth, "user-agent": self._ua}
        def poll_once() -> Optional[str]:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={
                        "accountId": account.account_id,
                        "allReceive": 0,
                        "emailId": 0,
                        "timeSort": 1,
                        "size": 50,
                        "type": 0,
                    },
                    headers=h,
                    timeout=15,
                    impersonate="chrome131",
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (
                            str(mail.get("subject", ""))
                            + " "
                            + str(mail.get("content") or mail.get("html") or "")
                        )
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=4,
            poll_once=poll_once,
        )
class AitreMailbox(BaseMailbox):
    """mail.aitre.cc 临时邮箱"""
    def __init__(self, email: str):
        self._email = email
        self.api = "https://mail.aitre.cc/api/tempmail"
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._email)
    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(
                f"{self.api}/emails", params={"email": account.email}, timeout=10
            )
            emails = r.json().get("emails", [])
            return {str(m["id"]) for m in emails if "id" in m}
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "trae",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests
        seen = set(before_ids) if before_ids else set()
        last_check = None
        def poll_once() -> Optional[str]:
            nonlocal last_check
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(
                        f"{self.api}/emails",
                        params={"email": account.email},
                        timeout=10,
                    )
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = mail.get("preview", "") + mail.get("content", "")
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol 免费临时邮箱（无需注册，自动生成）"""
    def __init__(self, proxy: str = None):
        self.api = "https://api.tempmail.lol/v2"
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._email = None
    def get_email(self) -> MailboxAccount:
        import requests
        r = requests.post(
            f"{self.api}/inbox/create", json={}, proxies=self.proxy, timeout=15
        )
        data = r.json()
        email = data.get("address") or data.get("email", "")
        if not email:
            raise RuntimeError(f"tempmail.lol API 返回空邮箱: {data}")
        self._email = email
        self._token = data.get("token", "")
        print(f"[TempMailLol] 生成邮箱: {self._email}")
        return MailboxAccount(email=self._email, account_id=self._token)
    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(
                f"{self.api}/inbox",
                params={"token": account.account_id},
                proxies=self.proxy,
                timeout=10,
            )
            return {str(m["id"]) for m in r.json().get("emails", [])}
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests
        seen = set(before_ids or [])
        otp_sent_at = kwargs.get("otp_sent_at")
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        self._log(f"[TempMailLol] 排除验证码: {exclude_codes}")
        def poll_once() -> Optional[str]:
            try:
                r = requests.get(
                    f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy,
                    timeout=10,
                )
                data = r.json()
                emails = data.get("emails", [])
                self._log(f"[TempMailLol] 获取到 {len(emails)} 封邮件")
                if not emails and len(data) > 1:
                    # 记录非正常情况，帮助调试
                    self._log(f"[TempMailLol] API 返回: {str(data)[:300]}")
                for mail in sorted(
                    emails,
                    key=lambda x: x.get("date", 0),
                    reverse=True,
                ):
                    mid = str(mail.get("id", "")).strip()
                    # 修复：如果 API 返回空 ID，避免 "" 被 seen 拦截导致死循环
                    if mid and mid in seen:
                        continue
                    
                    text = (
                        str(mail.get("subject", ""))
                        + " "
                        + str(mail.get("body", ""))
                        + " "
                        + str(mail.get("html", ""))
                    )
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        self._log(f"[TempMailLol] 邮件 {mid or '(空ID)'} 提取验证码={code}, 排除={code in exclude_codes}")
                        if code in exclude_codes:
                            continue
                        # 使用 ID 或验证码本身作为 seen 标识符
                        seen.add(mid or code)
                        return code
            except Exception as e:
                self._log(f"[TempMailLol] 轮询异常: {e}")
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class SkyMailMailbox(BaseMailbox):
    """SkyMail / CloudMail 自建邮箱服务"""
    def __init__(self, api_base: str, auth_token: str, domain: str, proxy: str = None):
        self.api = (api_base or "").rstrip("/")
        self.auth_token = auth_token or ""
        self.domain = domain or ""
        self.proxy = build_requests_proxy_config(proxy)
    def _headers(self) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": self.auth_token,
        }
    def _ensure_config(self) -> None:
        if not self.api or not self.auth_token or not self.domain:
            raise RuntimeError(
                "SkyMail 未配置完整：请设置 skymail_api_base、skymail_token、skymail_domain"
            )
    def _gen_prefix(self) -> str:
        import random
        import string
        length = random.randint(8, 13)
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))
    def get_email(self) -> MailboxAccount:
        import requests
        self._ensure_config()
        email = f"{self._gen_prefix()}@{self.domain}"
        payload = {"list": [{"email": email}]}
        r = requests.post(
            f"{self.api}/api/public/addUser",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {r.status_code} {r.text[:200]}")
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {data}")
        self._log(f"[SkyMail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)
    def _list_mails(self, email: str) -> list:
        import requests
        payload = {
            "toEmail": email,
            "num": 1,
            "size": 20,
        }
        r = requests.post(
            f"{self.api}/api/public/emailList",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if data.get("code") != 200:
            return []
        return data.get("data") or []
    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._list_mails(account.account_id or account.email)
            ids = set()
            for i, msg in enumerate(mails):
                mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                if mid:
                    ids.add(str(mid))
                else:
                    digest = (
                        str(msg.get("date") or msg.get("time") or "")
                        + "|"
                        + str(msg.get("subject") or "")
                    )
                    ids.add(f"idx-{i}-{digest}")
            return ids
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        target = account.account_id or account.email
        seen = set(before_ids or [])
        def poll_once() -> Optional[str]:
            try:
                mails = self._list_mails(target)
                for i, msg in enumerate(mails):
                    mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                    if not mid:
                        digest = (
                            str(msg.get("date") or msg.get("time") or "")
                            + "|"
                            + str(msg.get("subject") or "")
                        )
                        mid = f"idx-{i}-{digest}"
                    mid = str(mid)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    content = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(msg.get("content") or ""),
                            str(msg.get("text") or ""),
                            str(msg.get("html") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in content.lower():
                        continue
                    code = self._safe_extract(content, code_pattern)
                    if code:
                        self._log(f"[SkyMail] 命中验证码: {code}")
                        return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class DuckMailMailbox(BaseMailbox):
    """DuckMail 自动生成邮箱（随机创建账号）"""
    def __init__(
        self,
        api_url: str = "https://www.duckmail.sbs",
        provider_url: str = "https://api.duckmail.sbs",
        bearer: str = "kevin273945",
        domain: str = "",
        api_key: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://www.duckmail.sbs").rstrip("/")
        self.provider_url = (provider_url or "https://api.duckmail.sbs").rstrip("/")
        self.bearer = bearer or "kevin273945"
        self.domain = str(domain or "").strip()
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._address = None
        # 如果配置了 API Key，直接请求 DuckMail API；否则走前端代理
        self._direct = bool(self.api_key)
    def _proxy_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "content-type": "application/json",
            "x-api-provider-base-url": self.provider_url,
        }
    def _direct_headers(self, token: str = "") -> dict:
        auth = token or self.api_key
        return {
            "authorization": f"Bearer {auth}",
            "content-type": "application/json",
        }
    def _request(self, method: str, endpoint: str, token: str = "", **kwargs):
        """统一请求方法，根据模式选择直连或代理"""
        import requests
        if self._direct:
            url = f"{self.provider_url}{endpoint}"
            headers = self._direct_headers(token)
        else:
            from urllib.parse import quote
            url = f"{self.api}/api/mail?endpoint={quote(endpoint, safe='')}"
            headers = (
                self._proxy_headers()
                if not token
                else {
                    "authorization": f"Bearer {token}",
                    "x-api-provider-base-url": self.provider_url,
                }
            )
        r = requests.request(
            method, url, headers=headers, proxies=self.proxy, timeout=15, **kwargs
        )
        return r
    def get_email(self) -> MailboxAccount:
        import random, string
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        domain = self.domain or self.provider_url.replace("https://api.", "").replace(
            "https://", ""
        )
        address = f"{username}@{domain}"
        print(f"[DuckMail] 创建账号: {address} direct={self._direct}")
        # 创建账号
        r = self._request(
            "POST", "/accounts", json={"address": address, "password": password}
        )
        if r.status_code >= 400 or not r.text.strip().startswith("{"):
            raise RuntimeError(
                f"[DuckMail] 创建账号失败: HTTP {r.status_code} body={r.text[:300]}"
            )
        data = r.json()
        self._address = data.get("address", address)
        # 登录获取 token
        r2 = self._request(
            "POST", "/token", json={"address": self._address, "password": password}
        )
        if r2.status_code >= 400 or not r2.text.strip().startswith(("{", "[")):
            raise RuntimeError(
                f"[DuckMail] 登录失败: HTTP {r2.status_code} body={r2.text[:300]}"
            )
        self._token = r2.json().get("token", "")
        return MailboxAccount(email=self._address, account_id=self._token)
    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._request("GET", "/messages?page=1", token=account.account_id)
            return {str(m["id"]) for m in r.json().get("hydra:member", [])}
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from datetime import datetime
        import re
        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")
        def _parse_message_timestamp(*values) -> Optional[float]:
            for value in values:
                if value in (None, ""):
                    continue
                if isinstance(value, (int, float)):
                    numeric = float(value)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                text = str(value).strip()
                if not text:
                    continue
                try:
                    numeric = float(text)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                except (TypeError, ValueError):
                    pass
                try:
                    normalized = text.replace("Z", "+00:00")
                    return datetime.fromisoformat(normalized).timestamp()
                except ValueError:
                    continue
            return None
        def poll_once() -> Optional[str]:
            try:
                r = self._request("GET", "/messages?page=1", token=account.account_id)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    # 请求邮件详情获取完整 text
                    try:
                        r2 = self._request(
                            "GET", f"/messages/{mid}", token=account.account_id
                        )
                        detail = r2.json()
                        body = (
                            str(detail.get("text") or "")
                            + " "
                            + str(detail.get("subject") or "")
                        )
                    except Exception:
                        detail = {}
                        body = str(msg.get("subject") or "")
                    message_ts = _parse_message_timestamp(
                        detail.get("createdAt"),
                        detail.get("created_at"),
                        detail.get("receivedAt"),
                        detail.get("received_at"),
                        detail.get("date"),
                        detail.get("created"),
                        msg.get("createdAt"),
                        msg.get("created_at"),
                        msg.get("receivedAt"),
                        msg.get("received_at"),
                        msg.get("date"),
                        msg.get("created"),
                    )
                    if otp_sent_at and message_ts and message_ts < float(otp_sent_at):
                        continue
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class DuckDuckGoMailbox(BaseMailbox):
    """DuckDuckGo Email Protection mailbox via Gmail IMAP or Gmail API forwarding."""
    def __init__(
        self,
        duck_email: str,
        gmail_address: str,
        gmail_app_password: str,
        imap_host: str = "imap.gmail.com",
        imap_port: int = 993,
        mailbox_name: str = "INBOX",
        all_mailbox_name: str = "[Gmail]/All Mail",
        gmail_api_mode: str = "imap",
        gmail_api_credentials: str = "",
        gmail_api_token: str = "",
        alias_mode: str = "fixed",
        private_addresses: str = "",
        alias_rotation: str = "random",
        duck_api_token: str = "",
        proxy: str = None,
    ):
        self.duck_email = str(duck_email or "").strip()
        self.gmail_address = str(gmail_address or "").strip()
        self.gmail_app_password = str(gmail_app_password or "").replace(" ", "").strip()
        self.imap_host = str(imap_host or "imap.gmail.com").strip() or "imap.gmail.com"
        try:
            self.imap_port = int(imap_port or 993)
        except (TypeError, ValueError):
            self.imap_port = 993
        self.mailbox_name = str(mailbox_name or "INBOX").strip() or "INBOX"
        self.all_mailbox_name = str(all_mailbox_name or "[Gmail]/All Mail").strip() or "[Gmail]/All Mail"
        self.gmail_api_mode = str(gmail_api_mode or "imap").strip().lower() or "imap"
        self.gmail_api_credentials = str(gmail_api_credentials or "").strip()
        self.gmail_api_token = str(gmail_api_token or "").strip()
        self.alias_mode = str(alias_mode or "fixed").strip().lower() or "fixed"
        self.private_addresses = str(private_addresses or "").strip()
        self.alias_rotation = str(alias_rotation or "random").strip().lower() or "random"
        self.duck_api_token = str(duck_api_token or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._imap_connect_timeout = 15.0
        self._imap_socket_timeout = 20.0
        self._imap_search_window = 50
        self._imap_reconnect_attempts = 2
        self._gmail_api_access_token_skew = 60
        self._gmail_api_search_window = 20
        self._gmail_api_credentials_cache = None
        self._gmail_api_token_cache = None
        self._alias_index = 0
        self._generated_addresses: list[str] = []
    def _normalize_gmail_api_mode(self) -> str:
        mode = str(self.gmail_api_mode or "imap").strip().lower()
        if mode in {"gmailapi", "gmail-api", "api"}:
            return "gmail_api"
        return mode or "imap"
    def _require_config(self) -> None:
        missing = []
        if not self.duck_email:
            missing.append("duckduckgo_email")
        if self._normalize_gmail_api_mode() == "gmail_api":
            if not self.gmail_address:
                missing.append("duckduckgo_gmail_address")
            if not self.gmail_api_credentials:
                missing.append("duckduckgo_gmail_api_credentials")
            if not self.gmail_api_token:
                missing.append("duckduckgo_gmail_api_token")
        else:
            if not self.gmail_address:
                missing.append("duckduckgo_gmail_address")
            if not self.gmail_app_password:
                missing.append("duckduckgo_gmail_app_password")
        if missing:
            raise RuntimeError(
                "DuckDuckGo mailbox missing config: " + ", ".join(missing)
            )
    def _normalize_alias_mode(self) -> str:
        mode = str(self.alias_mode or "fixed").strip().lower()
        if mode in {"auto", "auto_generate", "auto-generated", "generate", "real_random"}:
            return "auto_generate"
        if mode in {"private", "private_pool", "pool", "random_pool"}:
            return "pool"
        return "fixed"
    def _normalize_alias_rotation(self) -> str:
        rotation = str(self.alias_rotation or "random").strip().lower()
        if rotation in {"round_robin", "round-robin", "sequential", "ordered"}:
            return "round_robin"
        return "random"
    def _parse_private_addresses(self) -> list[str]:
        import re
        values: list[str] = []
        seen: set[str] = set()
        for part in re.split(r"[\r\n,;]+", self.private_addresses):
            candidate = str(part or "").strip().lower()
            if not candidate or "@" not in candidate:
                continue
            if not candidate.endswith("@duck.com"):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            values.append(candidate)
        return values
    def _is_valid_generated_duck_address(self, value: str) -> bool:
        import re
        return bool(re.fullmatch(r"[a-z0-9-]+@duck\.com", str(value or "").strip().lower()))
    def _select_duck_address_from_pool(self) -> str:
        candidates = self._parse_private_addresses()
        if not candidates:
            self._log("[DuckDuckGo] alias_mode=pool but no private addresses configured; fallback to primary Duck address")
            return self.duck_email
        if self._normalize_alias_rotation() == "round_robin":
            selected = candidates[self._alias_index % len(candidates)]
            self._alias_index += 1
            return selected
        return random.choice(candidates)
    def _create_private_duck_address(self) -> str:
        import requests
        token = str(self.duck_api_token or "").strip()
        if not token:
            raise RuntimeError("DuckDuckGo private-address generation requires duckduckgo_api_token")
        response = requests.post(
            "https://quack.duckduckgo.com/api/email/addresses",
            headers={"Authorization": f"Bearer {token}"},
            proxies=self.proxy,
            timeout=20,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"DuckDuckGo private-address generation failed: HTTP {response.status_code}: {response.text[:200]}"
            )
        payload = response.json() or {}
        address = str(payload.get("address") or "").strip().lower()
        if address and "@" not in address:
            address = f"{address}@duck.com"
        if not self._is_valid_generated_duck_address(address):
            raise RuntimeError(f"DuckDuckGo private-address generation returned invalid address: {address!r}")
        if address not in self._generated_addresses:
            self._generated_addresses.append(address)
        return address
    def _select_duck_address(self) -> str:
        alias_mode = self._normalize_alias_mode()
        if alias_mode == "auto_generate":
            try:
                address = self._create_private_duck_address()
                self._log(f"[DuckDuckGo] generated private address: {address}")
                return address
            except Exception as exc:
                self._log(f"[DuckDuckGo] auto-generate private address failed: {exc}")
                if self._parse_private_addresses():
                    self._log("[DuckDuckGo] fallback from auto_generate to configured private-address pool")
                    return self._select_duck_address_from_pool()
                self._log("[DuckDuckGo] fallback from auto_generate to primary Duck address")
                return self.duck_email
        if alias_mode == "pool":
            return self._select_duck_address_from_pool()
        return self.duck_email
    def _decode_header_value(self, value: Any) -> str:
        from email.header import decode_header
        parts = []
        for chunk, encoding in decode_header(str(value or "")):
            if isinstance(chunk, bytes):
                try:
                    parts.append(chunk.decode(encoding or "utf-8", errors="ignore"))
                except Exception:
                    parts.append(chunk.decode("utf-8", errors="ignore"))
            else:
                parts.append(str(chunk))
        return "".join(parts).strip()
    def _extract_message_texts(self, message) -> tuple[str, str]:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_filename():
                    continue
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="ignore")
                except Exception:
                    text = payload.decode("utf-8", errors="ignore")
                content_type = (part.get_content_type() or "").lower()
                if content_type == "text/plain":
                    plain_parts.append(text)
                elif content_type == "text/html":
                    html_parts.append(text)
        else:
            payload = message.get_payload(decode=True) or b""
            charset = message.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="ignore")
            except Exception:
                text = payload.decode("utf-8", errors="ignore")
            if (message.get_content_type() or "").lower() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)
        return "\n".join(plain_parts), "\n".join(html_parts)
    def _parse_message_timestamp(self, message) -> Optional[float]:
        from email.utils import parsedate_to_datetime
        for header_name in ("Date",):
            raw_value = message.get(header_name)
            if not raw_value:
                continue
            try:
                parsed = parsedate_to_datetime(raw_value)
            except Exception:
                continue
            if parsed is None:
                continue
            try:
                return parsed.timestamp()
            except Exception:
                continue
        return None
    def _load_json_config(self, raw_value: Any, field_name: str) -> dict:
        if isinstance(raw_value, dict):
            return dict(raw_value)
        text = str(raw_value or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"DuckDuckGo {field_name} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"DuckDuckGo {field_name} must be a JSON object")
        return value
    def _gmail_api_credentials_data(self) -> dict:
        if self._gmail_api_credentials_cache is None:
            payload = self._load_json_config(
                self.gmail_api_credentials,
                "gmail_api_credentials",
            )
            installed = payload.get("installed") if isinstance(payload.get("installed"), dict) else payload
            self._gmail_api_credentials_cache = dict(installed or {})
        return dict(self._gmail_api_credentials_cache or {})
    def _gmail_api_token_data(self) -> dict:
        if self._gmail_api_token_cache is None:
            self._gmail_api_token_cache = self._load_json_config(
                self.gmail_api_token,
                "gmail_api_token",
            )
        return dict(self._gmail_api_token_cache or {})
    def _persist_gmail_api_token(self, token_data: dict) -> None:
        safe_token = dict(token_data or {})
        if not safe_token:
            return
        self._gmail_api_token_cache = safe_token
        self.gmail_api_token = json.dumps(safe_token, ensure_ascii=False)
        try:
            from core.config_store import config_store
            config_store.set("duckduckgo_gmail_api_token", self.gmail_api_token)
        except Exception:
            pass
    def _gmail_api_token_expired(self, token_data: dict) -> bool:
        access_token = str((token_data or {}).get("access_token") or "").strip()
        if not access_token:
            return True
        expires_at = (token_data or {}).get("expires_at")
        try:
            expiry_value = float(expires_at)
        except (TypeError, ValueError):
            expiry_value = 0.0
        if expiry_value <= 0:
            return False
        return time.time() >= max(expiry_value - self._gmail_api_access_token_skew, 0)
    def _refresh_gmail_api_access_token(self) -> dict:
        import requests
        credentials = self._gmail_api_credentials_data()
        token_data = self._gmail_api_token_data()
        client_id = str(credentials.get("client_id") or "").strip()
        client_secret = str(credentials.get("client_secret") or "").strip()
        token_uri = str(credentials.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
        refresh_token = str(token_data.get("refresh_token") or "").strip()
        if not client_id or not client_secret or not refresh_token:
            raise RuntimeError("DuckDuckGo Gmail API token is missing client_id/client_secret/refresh_token")
        response = requests.post(
            token_uri,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"DuckDuckGo Gmail API token refresh failed: HTTP {response.status_code}: {response.text[:300]}"
            )
        refreshed = response.json() or {}
        merged = dict(token_data)
        merged.update(refreshed)
        expires_in = refreshed.get("expires_in")
        try:
            merged["expires_at"] = time.time() + max(float(expires_in or 0), 0.0)
        except (TypeError, ValueError):
            merged["expires_at"] = time.time() + 300.0
        if refresh_token and not merged.get("refresh_token"):
            merged["refresh_token"] = refresh_token
        self._persist_gmail_api_token(merged)
        return merged
    def _ensure_gmail_api_access_token(self, *, force_refresh: bool = False) -> str:
        token_data = self._gmail_api_token_data()
        if force_refresh or self._gmail_api_token_expired(token_data):
            token_data = self._refresh_gmail_api_access_token()
        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("DuckDuckGo Gmail API token does not contain access_token")
        return access_token
    def _gmail_api_request(self, method: str, url: str, *, params: dict | None = None) -> dict:
        import requests
        access_token = self._ensure_gmail_api_access_token()
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            timeout=20,
        )
        if response.status_code == 401:
            access_token = self._ensure_gmail_api_access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {access_token}"
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                timeout=20,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"DuckDuckGo Gmail API request failed: HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            return response.json() or {}
        except Exception as exc:
            raise RuntimeError(f"DuckDuckGo Gmail API returned invalid JSON: {exc}") from exc
    def _gmail_api_build_queries(self, *, keyword: str, target_email: str) -> list[str]:
        queries: list[str] = []
        gmail_address = str(self.gmail_address or "").strip().lower()
        escaped_target = f'"{target_email}"' if target_email else ""
        escaped_keyword = f'"{keyword.strip()}"' if str(keyword or "").strip() else ""
        for query in (
            " ".join(
                item
                for item in (
                    f"to:{gmail_address}" if gmail_address else "",
                    escaped_target,
                    escaped_keyword,
                    '(OpenAI OR ChatGPT OR "verification code")',
                    'newer_than:7d',
                )
                if item
            ),
            " ".join(
                item
                for item in (
                    escaped_target,
                    escaped_keyword,
                    '(OpenAI OR ChatGPT OR "verification code")',
                    'newer_than:7d',
                )
                if item
            ),
            " ".join(
                item
                for item in (
                    f"to:{gmail_address}" if gmail_address else "",
                    '(OpenAI OR ChatGPT OR "verification code")',
                    'newer_than:7d',
                )
                if item
            ),
            'newer_than:7d',
        ):
            normalized = " ".join(str(query or "").split()).strip()
            if normalized and normalized not in queries:
                queries.append(normalized)
        return queries
    def _gmail_api_list_message_ids(self, *, keyword: str, target_email: str) -> list[str]:
        base_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        for query in self._gmail_api_build_queries(keyword=keyword, target_email=target_email):
            payload = self._gmail_api_request(
                "GET",
                base_url,
                params={"q": query, "maxResults": self._gmail_api_search_window},
            )
            messages = payload.get("messages") or []
            ids = [
                str(item.get("id") or "").strip()
                for item in messages
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ]
            if ids:
                return ids
        return []
    def _gmail_api_fetch_message(self, message_id: str):
        from email import message_from_bytes
        payload = self._gmail_api_request(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            params={"format": "raw"},
        )
        raw_encoded = str(payload.get("raw") or "").strip()
        if not raw_encoded:
            return None, b"", 0.0
        padding = "=" * (-len(raw_encoded) % 4)
        raw_bytes = base64.urlsafe_b64decode(raw_encoded + padding)
        internal_date = payload.get("internalDate")
        try:
            message_ts = float(internal_date) / 1000.0 if internal_date is not None else 0.0
        except (TypeError, ValueError):
            message_ts = 0.0
        return message_from_bytes(raw_bytes), raw_bytes, message_ts
    def _open_mailbox(self):
        import imaplib
        self._require_config()
        previous_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(self._imap_connect_timeout)
            client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        finally:
            socket.setdefaulttimeout(previous_timeout)
        try:
            if hasattr(client, "sock") and client.sock is not None:
                client.sock.settimeout(self._imap_socket_timeout)
        except Exception:
            pass
        client.login(self.gmail_address, self.gmail_app_password)
        self._select_mailbox(client, self.mailbox_name)
        return client
    def _select_mailbox(self, client, mailbox_name: str) -> None:
        status, _ = client.select(mailbox_name)
        if status != "OK":
            raise RuntimeError(
                f"DuckDuckGo mailbox failed to select IMAP folder: {mailbox_name}"
            )
    @staticmethod
    def _normalize_imap_search_args(*parts: str) -> tuple[str, ...]:
        return tuple(str(part).strip() for part in parts if str(part or "").strip())
    def _execute_search_plan(self, client, plan: tuple[str, ...]) -> set[str]:
        status, data = client.uid("search", None, *plan)
        if status != "OK":
            return set()
        raw = b" ".join(item for item in (data or []) if isinstance(item, bytes)).strip()
        if not raw:
            return set()
        return {item.decode("utf-8", errors="ignore") for item in raw.split() if item}
    def _search_message_ids(self, client, *, target_email: str = "") -> set[str]:
        from datetime import datetime, timezone
        since_value = datetime.now(timezone.utc).strftime("%d-%b-%Y")
        mailboxes = []
        for mailbox_name in (self.mailbox_name, self.all_mailbox_name):
            if mailbox_name and mailbox_name not in mailboxes:
                mailboxes.append(mailbox_name)
        for mailbox_name in mailboxes:
            self._select_mailbox(client, mailbox_name)
            search_plans: list[tuple[str, ...]] = []
            if target_email:
                search_plans.append(
                    self._normalize_imap_search_args(
                        "X-GM-RAW", f'"{target_email} newer_than:7d"'
                    )
                )
                search_plans.append(
                    self._normalize_imap_search_args(
                        "SINCE", since_value, "TEXT", f'"{target_email}"'
                    )
                )
                search_plans.append(
                    self._normalize_imap_search_args("TEXT", f'"{target_email}"')
                )
            search_plans.append(self._normalize_imap_search_args("SINCE", since_value))
            search_plans.append(self._normalize_imap_search_args("ALL"))
            for plan in search_plans:
                if not plan:
                    continue
                try:
                    ids = self._execute_search_plan(client, plan)
                except Exception as exc:
                    if self._is_transient_imap_error(exc):
                        raise
                    continue
                if ids:
                    return ids
        return set()
    def _fetch_message(self, client, uid: str):
        from email import message_from_bytes
        status, data = client.uid("fetch", uid, "(RFC822)")
        if status != "OK":
            return None, b""
        raw_bytes = b""
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw_bytes = item[1]
                break
        if not raw_bytes:
            return None, b""
        return message_from_bytes(raw_bytes), raw_bytes
    def _is_transient_imap_error(self, exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, socket.timeout):
            return True
        message = str(exc or "")
        transient_markers = (
            "timed out",
            "timeout",
            "WinError 10060",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "EOF",
            "socket error",
        )
        lowered = message.lower()
        return any(marker.lower() in lowered for marker in transient_markers)
    def _close_mailbox(self, client) -> None:
        if client is None:
            return
        try:
            client.logout()
        except Exception:
            pass
    def _wait_for_code_via_imap(
        self,
        account: MailboxAccount,
        *,
        keyword: str,
        timeout: int,
        before_ids: set,
        code_pattern: str | None,
        exclude_codes: set[str],
        otp_sent_at,
    ) -> str:
        seen = {str(item) for item in (before_ids or set())}
        keyword_lower = str(keyword or "").strip().lower()
        target_email = str(account.email or self.duck_email or "").strip().lower()
        previous_ids = set(seen)
        client = None
        reconnects_used = 0
        def poll_once() -> Optional[str]:
            nonlocal client
            nonlocal reconnects_used
            try:
                if client is None:
                    client = self._open_mailbox()
                    reconnects_used = 0
                current_ids = sorted(
                    self._search_message_ids(client, target_email=target_email),
                    key=lambda item: int(item) if str(item).isdigit() else str(item),
                )
                for uid in reversed(current_ids[-self._imap_search_window:]):
                    if uid in seen:
                        continue
                    seen.add(uid)
                    message, raw_bytes = self._fetch_message(client, uid)
                    if message is None:
                        continue
                    subject = self._decode_header_value(message.get("Subject"))
                    header_text = "\n".join(
                        [
                            self._decode_header_value(message.get("From")),
                            self._decode_header_value(message.get("To")),
                            self._decode_header_value(message.get("Delivered-To")),
                            self._decode_header_value(message.get("X-Forwarded-To")),
                            self._decode_header_value(message.get("X-Original-To")),
                        ]
                    )
                    plain_text, html_text = self._extract_message_texts(message)
                    decoded_raw = self._decode_raw_content(
                        raw_bytes.decode("utf-8", errors="ignore")
                    )
                    combined = "\n".join(
                        [subject, header_text, plain_text, html_text, decoded_raw]
                    ).strip()
                    if not combined:
                        continue
                    if keyword_lower and keyword_lower not in combined.lower():
                        continue
                    if target_email and target_email not in combined.lower():
                        loose_match = (
                            "openai" in combined.lower()
                            or "verification code" in combined.lower()
                            or "temporary chatgpt verification code" in combined.lower()
                        )
                        if not loose_match:
                            continue
                    message_ts = self._parse_message_timestamp(message)
                    if otp_sent_at and message_ts and message_ts < float(otp_sent_at):
                        continue
                    code = self._safe_extract(combined, code_pattern)
                    # A resent login challenge may legitimately reuse the same OTP digits
                    # while still arriving in a brand-new forwarded message. Only suppress
                    # excluded codes when they were already present before this wait began.
                    if code and code in exclude_codes and uid in previous_ids:
                        continue
                    if code:
                        self._log(f"[DuckDuckGo] received forwarded OTP from Gmail: {code}")
                        return code
            except Exception as exc:
                if self._is_transient_imap_error(exc) and reconnects_used < self._imap_reconnect_attempts:
                    reconnects_used += 1
                    self._log(
                        f"[DuckDuckGo] transient Gmail IMAP failure, reconnecting "
                        f"({reconnects_used}/{self._imap_reconnect_attempts}): {exc}"
                    )
                    self._close_mailbox(client)
                    client = None
                    return None
                self._log(f"[DuckDuckGo] Gmail IMAP poll failed: {exc}")
                self._close_mailbox(client)
                client = None
            return None
        try:
            return self._run_polling_wait(
                timeout=timeout,
                poll_interval=5,
                poll_once=poll_once,
            )
        finally:
            self._close_mailbox(client)
    def _wait_for_code_via_gmail_api(
        self,
        account: MailboxAccount,
        *,
        keyword: str,
        timeout: int,
        before_ids: set,
        code_pattern: str | None,
        exclude_codes: set[str],
        otp_sent_at,
    ) -> str:
        seen = {str(item) for item in (before_ids or set())}
        keyword_lower = str(keyword or "").strip().lower()
        target_email = str(account.email or self.duck_email or "").strip().lower()
        previous_ids = set(seen)
        def poll_once() -> Optional[str]:
            try:
                current_ids = self._gmail_api_list_message_ids(
                    keyword=keyword,
                    target_email=target_email,
                )
                for message_id in reversed(current_ids):
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    message, raw_bytes, internal_ts = self._gmail_api_fetch_message(message_id)
                    if message is None:
                        continue
                    subject = self._decode_header_value(message.get("Subject"))
                    header_text = "\n".join(
                        [
                            self._decode_header_value(message.get("From")),
                            self._decode_header_value(message.get("To")),
                            self._decode_header_value(message.get("Delivered-To")),
                            self._decode_header_value(message.get("X-Forwarded-To")),
                            self._decode_header_value(message.get("X-Original-To")),
                        ]
                    )
                    plain_text, html_text = self._extract_message_texts(message)
                    decoded_raw = self._decode_raw_content(
                        raw_bytes.decode("utf-8", errors="ignore")
                    )
                    combined = "\n".join(
                        [subject, header_text, plain_text, html_text, decoded_raw]
                    ).strip()
                    if not combined:
                        continue
                    if keyword_lower and keyword_lower not in combined.lower():
                        continue
                    if target_email and target_email not in combined.lower():
                        loose_match = (
                            "openai" in combined.lower()
                            or "verification code" in combined.lower()
                            or "temporary chatgpt verification code" in combined.lower()
                        )
                        if not loose_match:
                            continue
                    message_ts = internal_ts or self._parse_message_timestamp(message)
                    # DuckDuckGo/Gmail forwarding timestamps can lag the page-side send marker,
                    # especially during the login-challenge phase. Allow a grace window so we
                    # don't discard the valid second OTP just because its message date looks early.
                    if otp_sent_at and message_ts and message_ts < (float(otp_sent_at) - 300.0):
                        continue
                    code = self._safe_extract(combined, code_pattern)
                    # A resent login challenge may legitimately reuse the same OTP digits
                    # while still arriving in a brand-new forwarded message. Only suppress
                    # excluded codes when they were already present before this wait began.
                    if code and code in exclude_codes and message_id in previous_ids:
                        continue
                    if code:
                        self._log(f"[DuckDuckGo] received forwarded OTP from Gmail API: {code}")
                        return code
            except Exception as exc:
                self._log(f"[DuckDuckGo] Gmail API poll failed: {exc}")
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
        )
    def get_email(self) -> MailboxAccount:
        self._require_config()
        selected_email = self._select_duck_address()
        alias_mode = self._normalize_alias_mode()
        return MailboxAccount(
            email=selected_email,
            account_id=self.gmail_address or selected_email,
            extra={
                "provider": "duckduckgo",
                "forward_email": self.gmail_address,
                "imap_host": self.imap_host,
                "mailbox": self.mailbox_name,
                "all_mailbox": self.all_mailbox_name,
                "gmail_api_mode": self._normalize_gmail_api_mode(),
                "base_duck_email": self.duck_email,
                "alias_mode": alias_mode,
                "alias_rotation": self._normalize_alias_rotation(),
                "duck_auto_generate_enabled": alias_mode == "auto_generate",
            },
        )
    def get_current_ids(self, account: MailboxAccount) -> set:
        if self._normalize_gmail_api_mode() == "gmail_api":
            try:
                return set(
                    self._gmail_api_list_message_ids(
                        keyword="",
                        target_email=str(account.email or self.duck_email or "").strip().lower(),
                    )
                )
            except Exception:
                return set()
        client = None
        try:
            client = self._open_mailbox()
            return self._search_message_ids(
                client,
                target_email=str(account.email or self.duck_email or "").strip().lower(),
            )
        except Exception:
            return set()
        finally:
            self._close_mailbox(client)
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")
        active_mode = self._normalize_gmail_api_mode()
        self._log(f"[DuckDuckGo] mailbox mode={active_mode} email={getattr(account, 'email', '')}")
        if active_mode == "gmail_api":
            return self._wait_for_code_via_gmail_api(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                code_pattern=code_pattern,
                exclude_codes=exclude_codes,
                otp_sent_at=otp_sent_at,
            )
        return self._wait_for_code_via_imap(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
            exclude_codes=exclude_codes,
            otp_sent_at=otp_sent_at,
        )
class MaliAPIMailbox(BaseMailbox):
    """YYDS Mail / MaliAPI 临时邮箱服务"""
    def __init__(
        self,
        api_url: str = "https://maliapi.215.im/v1",
        api_key: str = "",
        domain: str = "",
        auto_domain_strategy: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = str(domain or "").strip()
        self.auto_domain_strategy = str(auto_domain_strategy or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None
        self._temp_token = None
    def _headers(self, bearer: str = "") -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict = None,
        params: dict = None,
        bearer: str = "",
    ) -> Any:
        import requests
        response = requests.request(
            method,
            f"{self.api}{path}",
            headers=self._headers(bearer),
            json=json_body,
            params=params,
            proxies=self.proxy,
            timeout=15,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status_code >= 400:
            error = response.text or f"HTTP {response.status_code}"
            error_code = ""
            if isinstance(payload, dict):
                error = str(payload.get("error") or error).strip()
                error_code = str(payload.get("errorCode") or "").strip()
            if error_code:
                raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
            raise RuntimeError(f"MaliAPI 请求失败: {str(error).strip()}")
        if isinstance(payload, dict):
            if payload.get("success") is False:
                error = str(payload.get("error") or "unknown error").strip()
                error_code = str(payload.get("errorCode") or "").strip()
                if error_code:
                    raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
                raise RuntimeError(f"MaliAPI 请求失败: {error}")
            if "data" in payload:
                return payload.get("data")
        return payload
    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError("MaliAPI 未配置：请在全局设置中填写 maliapi_api_key")
    def _list_messages(self, account: MailboxAccount) -> list[dict]:
        data = self._request("GET", "/messages", params={"address": account.email})
        if isinstance(data, dict):
            messages = data.get("messages", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]
    def _get_message_detail(self, message_id: str) -> dict:
        data = self._request("GET", f"/messages/{message_id}")
        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return data["message"]
        return data if isinstance(data, dict) else {}
    def get_email(self) -> MailboxAccount:
        self._ensure_api_key()
        body = {}
        if self.domain:
            body["domain"] = self.domain
        if self.auto_domain_strategy:
            body["autoDomainStrategy"] = self.auto_domain_strategy
        data = self._request("POST", "/accounts", json_body=body)
        if not isinstance(data, dict):
            raise RuntimeError(f"MaliAPI 返回异常: {data}")
        email = str(data.get("address") or data.get("email") or "").strip()
        temp_token = str(
            data.get("tempToken") or data.get("temp_token") or data.get("token") or ""
        ).strip()
        inbox_id = str(data.get("id") or "").strip()
        if not email:
            raise RuntimeError(f"MaliAPI 返回空邮箱: {data}")
        self._email = email
        self._temp_token = temp_token
        self._log(f"[MaliAPI] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=temp_token or inbox_id or email,
            extra={
                "provider": "maliapi",
                "temp_token": temp_token,
                "inbox_id": inbox_id,
            },
        )
    def get_current_ids(self, account: MailboxAccount) -> set:
        self._ensure_api_key()
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account)
                if message.get("id") is not None
            }
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        self._ensure_api_key()
        seen = {str(mid) for mid in (before_ids or set())}
        def poll_once() -> Optional[str]:
            try:
                for message in self._list_messages(account):
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = message
                    search_text = " ".join(
                        [
                            str(detail.get("subject") or message.get("subject") or ""),
                            str(detail.get("text") or ""),
                            str(detail.get("html") or ""),
                            str(message.get("subject") or ""),
                            str(message.get("snippet") or ""),
                        ]
                    ).strip()
                    search_text = self._yyds_decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue
                    code = self._yyds_safe_extract(search_text, code_pattern)
                    if code:
                        self._log(f"[MaliAPI] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class GPTMailMailbox(BaseMailbox):
    """GPTMail 临时邮箱服务"""
    def __init__(
        self,
        api_url: str = "https://mail.chatgpt.org.uk",
        api_key: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://mail.chatgpt.org.uk").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = self._normalize_domain(domain)
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None
    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain
    @staticmethod
    def _generate_local_part() -> str:
        import string
        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"
    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers
    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 15,
    ) -> Any:
        import requests
        response = requests.request(
            method,
            f"{self.api}{path}",
            params=params,
            json=json_body,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"GPTMail API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else ""
            message = str(error or response.text or f"HTTP {response.status_code}").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {message}")
        if isinstance(payload, dict) and payload.get("success") is False:
            error = str(payload.get("error") or "unknown error").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {error}")
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload
    def _list_messages(self, email: str) -> list[dict]:
        data = self._request_json("GET", "/api/emails", params={"email": email}, timeout=10)
        if isinstance(data, dict):
            messages = data.get("emails", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]
    def _get_message_detail(self, message_id: str) -> dict[str, Any]:
        data = self._request_json("GET", f"/api/email/{message_id}", timeout=10)
        return data if isinstance(data, dict) else {}
    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._email = email
            self._log(f"[GPTMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={"provider": "gptmail", "domain": self.domain, "local_address": True},
            )
        data = self._request_json("GET", "/api/generate-email")
        if not isinstance(data, dict):
            raise RuntimeError(f"GPTMail 返回异常: {data}")
        email = str(data.get("email") or "").strip()
        if not email:
            raise RuntimeError(f"GPTMail 返回空邮箱: {data}")
        self._email = email
        self._log(f"[GPTMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "gptmail"},
        )
    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }
        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = {}
                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from_address") or ""),
                            str(message.get("content") or ""),
                            str(message.get("html_content") or ""),
                            str(detail.get("subject") or ""),
                            str(detail.get("content") or ""),
                            str(detail.get("html_content") or ""),
                            str(detail.get("raw_headers") or ""),
                        ]
                    ).strip()
                    search_text = self._decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue
                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[GPTMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class OpenTrashMailMailbox(BaseMailbox):
    """OpenTrashMail 临时邮箱服务"""
    def __init__(
        self,
        api_url: str = "",
        domain: str = "",
        password: str = "",
        proxy: str = None,
    ):
        self.api = str(api_url or "").strip().rstrip("/")
        self.domain = self._normalize_domain(domain)
        self.password = str(password or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain
    @staticmethod
    def _generate_local_part() -> str:
        import string
        prefix = "".join(random.choices(string.ascii_lowercase, k=8))
        suffix = "".join(random.choices(string.digits, k=2))
        return f"{prefix}{suffix}"
    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json, text/plain, */*"}
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        timeout: int = 15,
    ):
        import requests
        request_params = dict(params or {})
        if self.password and "password" not in request_params:
            request_params["password"] = self.password
        return requests.request(
            method,
            f"{self.api}{path}",
            params=request_params or None,
            json=None,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
    def _require_api(self) -> None:
        if not self.api:
            raise RuntimeError(
                "OpenTrashMail 未配置 API URL，请检查 opentrashmail_api_url"
            )
    def _build_email_path(self, email: str) -> str:
        from urllib.parse import quote
        return quote(str(email or "").strip(), safe="@")
    def _parse_random_email(self, html_text: str) -> str:
        import re
        text = str(html_text or "")
        if not text:
            return ""
        match = re.search(r"/address/([^\"'<>\s]+@[^\"'<>\s]+)", text, re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()
        match = re.search(
            r"([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
            text,
            re.IGNORECASE,
        )
        if match:
            return str(match.group(1) or "").strip()
        return ""
    def _list_messages(self, email: str) -> list[dict[str, Any]]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}",
            timeout=10,
        )
        if response.status_code == 404:
            return []
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 收件箱返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc
        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 收件箱查询失败: {str(error).strip()}")
        if not payload:
            return []
        messages: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for message_id, item in payload.items():
                if not isinstance(item, dict):
                    continue
                message = dict(item)
                message.setdefault("id", str(message_id))
                messages.append(message)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    messages.append(item)
        return messages
    def _get_message_detail(self, email: str, message_id: str) -> dict[str, Any]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}/{message_id}",
            timeout=10,
        )
        if response.status_code == 404:
            return {}
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 邮件详情返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc
        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 邮件详情查询失败: {str(error).strip()}")
        return payload if isinstance(payload, dict) else {}
    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._log(f"[OpenTrashMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={
                    "provider": "opentrashmail",
                    "domain": self.domain,
                    "local_address": True,
                },
            )
        self._require_api()
        response = self._request("GET", "/api/random", timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenTrashMail 随机邮箱生成失败: HTTP {response.status_code}"
            )
        email = self._parse_random_email(response.text)
        if not email:
            preview = (response.text or "")[:200]
            raise RuntimeError(f"OpenTrashMail 未能解析随机邮箱: {preview}")
        self._log(f"[OpenTrashMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "opentrashmail"},
        )
    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }
        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    detail = self._get_message_detail(account.email, message_id)
                    parsed = detail.get("parsed") if isinstance(detail, dict) else {}
                    if not isinstance(parsed, dict):
                        parsed = {}
                    decoded_raw = self._decode_raw_content(detail.get("raw") or "")
                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from") or ""),
                            str(message.get("body") or ""),
                            str(detail.get("from") or ""),
                            str(parsed.get("subject") or ""),
                            str(parsed.get("body") or ""),
                            str(parsed.get("htmlbody") or ""),
                            decoded_raw,
                        ]
                    ).strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue
                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[OpenTrashMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class CFWorkerMailbox(BaseMailbox):
    """Cloudflare Worker 自建临时邮箱服务"""
    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        domain_override: str = "",
        domains: Any = None,
        enabled_domains: Any = None,
        subdomain: str = "",
        random_subdomain: Any = False,
        fingerprint: str = "",
        custom_auth: str = "",
        proxy: str = None,
    ):
        self.api = self._normalize_api_url(api_url)
        self.admin_token = admin_token
        self.domain = self._normalize_domain(domain)
        self.domain_supports_random_subdomain = self._has_wildcard_prefix(domain)
        self.domain_override = self._normalize_domain(domain_override)
        self.domain_override_supports_random_subdomain = self._has_wildcard_prefix(domain_override)
        domain_entries = self._parse_domain_entries(domains)
        self.domains = [domain for domain, _ in domain_entries]
        self.domains_supporting_random_subdomain = {
            domain for domain, supports_random in domain_entries if supports_random
        }
        raw_enabled_domains = self._parse_domains(enabled_domains)
        if self.domains:
            allowed = set(self.domains)
            self.enabled_domains = [d for d in raw_enabled_domains if d in allowed]
        else:
            self.enabled_domains = raw_enabled_domains
        self.enabled_domains_supporting_random_subdomain = {
            domain for domain in self.enabled_domains if domain in self.domains_supporting_random_subdomain
        }
        self.subdomain = self._normalize_subdomain(subdomain)
        self.random_subdomain = self._to_bool(random_subdomain)
        self.fingerprint = fingerprint
        self.custom_auth = custom_auth
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._mail_api_mode = "admin"
    @staticmethod
    def _is_transient_network_error(exc: Exception) -> bool:
        message = str(exc or "")
        transient_markers = (
            "SSLError",
            "UNEXPECTED_EOF_WHILE_READING",
            "EOF occurred in violation of protocol",
            "IncompleteRead",
            "Connection broken",
            "ProtocolError",
            "ProxyError",
            "Unable to connect to proxy",
            "RemoteDisconnected",
            "Connection aborted",
            "Connection reset",
            "Read timed out",
            "ConnectTimeout",
            "ReadTimeout",
            "Max retries exceeded",
        )
        return any(marker in message for marker in transient_markers)
    def _headers(
        self,
        *,
        include_admin_auth: bool = True,
        include_custom_auth: bool = True,
        bearer_token: str = "",
    ) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
        }
        if include_admin_auth and self.admin_token:
            h["x-admin-auth"] = self.admin_token
        if self.fingerprint:
            h["x-fingerprint"] = self.fingerprint
        if include_custom_auth and self.custom_auth:
            h["x-custom-auth"] = self.custom_auth
        if bearer_token:
            h["Authorization"] = f"Bearer {bearer_token}"
        return h
    @staticmethod
    def _normalize_api_url(api_url: Any) -> str:
        from urllib.parse import urlsplit, urlunsplit
        raw = str(api_url or "").strip()
        if not raw:
            return ""
        parts = urlsplit(raw)
        path = (parts.path or "").rstrip("/")
        if path.endswith("/admin"):
            path = path[: -len("/admin")]
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment)).rstrip("/")
    def _ensure_api_configured(self) -> None:
        if not self.api:
            raise RuntimeError("CF Worker API URL 未配置")
    def _read_json(self, response, action: str):
        try:
            return response.json()
        except Exception:
            body = (response.text or "").strip()
            snippet = body[:200] if body else "<empty>"
            raise RuntimeError(
                f"CF Worker {action} 返回非 JSON 响应: HTTP {response.status_code}, body={snippet}"
            )
    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        timeout: int = 15,
        headers: Optional[dict] = None,
    ):
        import requests
        import time as _time
        url = f"{self.api}{path}"
        last_response = None
        last_exc: Exception | None = None
        proxies_to_try = [self.proxy]
        if self.proxy:
            proxies_to_try.append(None)
        def _preview_text(response) -> str:
            try:
                body = (response.text or "").strip()
            except Exception as exc:
                return f"<body unavailable: {type(exc).__name__}: {exc}>"
            return body[:200] or "<empty>"
        for active_proxy in proxies_to_try:
            for attempt in range(1, 4):
                try:
                    response = requests.request(
                        method,
                        url,
                        params=params,
                        json=payload,
                        headers=headers or self._headers(),
                        proxies=active_proxy,
                        timeout=timeout,
                    )
                    last_response = response
                    preview = _preview_text(response)
                    if response.status_code >= 400:
                        if "private site password" in preview.lower():
                            raise RuntimeError(
                                "CFWorker API 需要私有站点密码，请配置 cfworker_custom_auth"
                            )
                        raise RuntimeError(
                            f"CFWorker API {path} 失败: HTTP {response.status_code} {preview}"
                        )
                    try:
                        return response.json()
                    except Exception as exc:
                        raise RuntimeError(
                            f"CFWorker API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
                        ) from exc
                except Exception as exc:
                    last_exc = exc
                    if self._is_transient_network_error(exc) and attempt < 3:
                        self._log(
                            f"[CFWorker] transient request failure {path} via "
                            f"{'proxy' if active_proxy else 'direct'} "
                            f"({attempt}/3): {type(exc).__name__}: {exc}"
                        )
                        _time.sleep(min(2, attempt))
                        continue
                    break
            if active_proxy:
                self._log("[CFWorker] proxy path unstable, retrying request without proxy")
        if last_exc is not None:
            raise RuntimeError(f"CFWorker API {path} failed: {last_exc}")
        if last_response is not None:
            preview = _preview_text(last_response)
            raise RuntimeError(
                f"CFWorker API {path} failed without usable JSON: HTTP {last_response.status_code} {preview}"
            )
        raise RuntimeError(f"CFWorker API {path} failed: unknown error")
    def _cfworker_plan(
        self,
        *,
        path: str,
        mode: str,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        timeout: int = 15,
        bearer_token: str = "",
    ) -> dict:
        return {
            "path": path,
            "mode": mode,
            "params": params,
            "payload": payload,
            "timeout": timeout,
            "headers": self._headers(
                include_admin_auth=(mode == "admin"),
                include_custom_auth=True,
                bearer_token=bearer_token if mode == "address" else "",
            ),
        }
    def _request_json_with_plans(self, method: str, plans: list[dict]):
        errors = []
        for index, plan in enumerate(plans, start=1):
            try:
                data = self._request_json(
                    method,
                    plan["path"],
                    params=plan.get("params"),
                    payload=plan.get("payload"),
                    timeout=int(plan.get("timeout") or 15),
                    headers=plan.get("headers"),
                )
                return data, str(plan.get("mode") or "admin"), str(plan.get("path") or "")
            except Exception as exc:
                label = f"{plan.get('mode') or 'unknown'}:{plan.get('path') or ''}"
                errors.append(f"{label} -> {type(exc).__name__}: {exc}")
                if index < len(plans):
                    self._log(f"[CFWorker] request fallback after {label}: {exc}")
        raise RuntimeError(" ; ".join(errors))
    @staticmethod
    def _extract_mailbox_token(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("token", "jwt", "credential", "address_credential", "access_token"):
                value = str(data.get(key, "") or "").strip()
                if value:
                    return value
            for key in ("data", "result"):
                nested = data.get(key)
                token = CFWorkerMailbox._extract_mailbox_token(nested)
                if token:
                    return token
        return ""
    def _prefer_admin_api(self) -> bool:
        return bool(str(self.admin_token or "").strip())
    def _build_get_email_plans(self, payload: dict) -> list[dict]:
        plans: list[dict] = []
        admin_plan = self._cfworker_plan(
            path="/admin/new_address",
            mode="admin",
            payload=payload,
            timeout=15,
        )
        api_payload = dict(payload)
        api_payload.setdefault("enableRandomSubdomain", self.random_subdomain)
        address_plan = self._cfworker_plan(
            path="/api/new_address",
            mode="address",
            payload=api_payload,
            timeout=15,
        )
        if self._prefer_admin_api():
            plans.extend([admin_plan, address_plan])
        else:
            plans.extend([address_plan, admin_plan])
        return plans
    def _build_get_mails_plans(self, email: str, *, bearer_token: str = "", limit: int = 20) -> list[dict]:
        plans: list[dict] = []
        admin_plan = self._cfworker_plan(
            path="/admin/mails",
            mode="admin",
            params={"limit": limit, "offset": 0, "address": email},
            timeout=10,
        )
        if bearer_token:
            address_plan = self._cfworker_plan(
                path="/api/mails",
                mode="address",
                params={"limit": limit, "offset": 0},
                timeout=10,
                bearer_token=bearer_token,
            )
            if self._prefer_admin_api():
                plans.extend([admin_plan, address_plan])
            else:
                plans.extend([address_plan, admin_plan])
        else:
            plans.append(admin_plan)
        return plans
    def _generate_local_part(self) -> str:
        import string
        # 避免纯数字开头，提高邮箱格式“像真人”的程度
        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"
    @staticmethod
    def _normalize_domain(domain: Any) -> str:
        value = str(domain or "").strip().lower()
        if value.startswith("@"):
            value = value[1:]
        if value.startswith("*."):
            value = value[2:]
        return value
    @staticmethod
    def _has_wildcard_prefix(domain: Any) -> bool:
        value = str(domain or "").strip().lower()
        if value.startswith("@"):
            value = value[1:]
        return value.startswith("*.")
    @staticmethod
    def _normalize_subdomain(value: Any) -> str:
        sub = str(value or "").strip().lower().strip(".")
        if sub.startswith("@"):
            sub = sub[1:]
        parts = [part for part in sub.split(".") if part]
        return ".".join(parts)
    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}
    @classmethod
    def _parse_domains(cls, value: Any) -> list[str]:
        return [domain for domain, _ in cls._parse_domain_entries(value)]
    @classmethod
    def _parse_domain_entries(cls, value: Any) -> list[tuple[str, bool]]:
        if not value:
            return []
        items: list[Any]
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [
                    part for chunk in text.splitlines() for part in chunk.split(",")
                ]
        else:
            items = [value]
        domains: list[tuple[str, bool]] = []
        seen = set()
        for item in items:
            supports_random_subdomain = cls._has_wildcard_prefix(item)
            domain = cls._normalize_domain(item)
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append((domain, supports_random_subdomain))
        return domains
    def _pick_domain(self) -> tuple[str, bool]:
        if self.domain_override:
            return self.domain_override, self.domain_override_supports_random_subdomain
        if self.enabled_domains:
            domain = random.choice(self.enabled_domains)
            return domain, domain in self.enabled_domains_supporting_random_subdomain
        return self.domain, self.domain_supports_random_subdomain
    def _generate_subdomain_label(self, length: int = 6) -> str:
        import string
        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.choices(alphabet, k=length))
    def _compose_domain(self, base_domain: str, *, allow_random_subdomain: bool = False) -> str:
        domain = self._normalize_domain(base_domain)
        if not domain:
            return ""
        sub_parts: list[str] = []
        if self.random_subdomain and allow_random_subdomain:
            sub_parts.append(self._generate_subdomain_label())
        if self.subdomain:
            sub_parts.append(self.subdomain)
        if not sub_parts:
            return domain
        return f"{'.'.join(sub_parts)}.{domain}"
    def get_email(self) -> MailboxAccount:
        self._ensure_api_configured()
        name = self._generate_local_part()
        payload = {"enablePrefix": True, "name": name}
        base_domain, allow_random_subdomain = self._pick_domain()
        if self.random_subdomain and not allow_random_subdomain:
            self._log(
                "[CFWorker] 随机子域名已开启，但当前域名未显式声明通配符支持，继续使用基础域名收件"
            )
        selected_domain = self._compose_domain(
            base_domain,
            allow_random_subdomain=allow_random_subdomain,
        )
        if selected_domain:
            payload["domain"] = selected_domain
            self._log(f"[CFWorker] 本次使用域名: {selected_domain}")
        data, mail_api_mode, used_path = self._request_json_with_plans(
            "POST",
            self._build_get_email_plans(payload),
        )
        email = data.get("email", data.get("address", ""))
        token = self._extract_mailbox_token(data)
        if not email or not token:
            raise RuntimeError(
                f"CFWorker API {used_path or '/admin/new_address'} 返回缺少 email/token: {data}"
            )
        self._token = token
        self._mail_api_mode = mail_api_mode
        print(
            f"[CFWorker] 生成邮箱: {email} token={token[:40] if token else 'NONE'}..."
        )
        return MailboxAccount(
            email=email,
            account_id=token,
            extra={
                key: value
                for key, value in {
                    "cfworker_domain": selected_domain,
                    "cfworker_mail_api_mode": mail_api_mode,
                }.items()
                if value
            }
            or None,
        )
    def _get_mails(self, email: str, *, bearer_token: str = "") -> list:
        self._ensure_api_configured()
        data, mail_api_mode, _used_path = self._request_json_with_plans(
            "GET",
            self._build_get_mails_plans(email, bearer_token=bearer_token, limit=20),
        )
        self._mail_api_mode = mail_api_mode
        return data.get("results", data) if isinstance(data, dict) else data
    def _get_mails_via_global_scan(self, email: str, limit: int = 25) -> list:
        self._ensure_api_configured()
        data = self._request_json(
            "GET",
            "/admin/mails",
            params={"limit": limit, "offset": 0},
            timeout=10,
        )
        results = data.get("results", data) if isinstance(data, dict) else data
        target_email = str(email or "").strip().lower()
        matched = []
        for mail in results or []:
            mail_address = str((mail or {}).get("address") or "").strip().lower()
            if mail_address == target_email:
                matched.append(mail)
        return matched
    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            bearer_token = str(account.account_id or self._token or "").strip()
            mails = self._get_mails(account.email, bearer_token=bearer_token)
            return {str(m.get("id", "")) for m in mails}
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        from datetime import datetime, timezone
        seen = set(before_ids or [])
        self._log(f"[CFWorker] 等验证码分支: 地址轮询收码 ({account.email})")
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")
        otp_cutoff = float(otp_sent_at) - 2 if otp_sent_at else None
        otp_skew_grace_seconds = 30
        last_poll_error: dict[str, str] = {"detail": ""}
        empty_address_polls = 0
        global_scan_interval = max(
            1, int(kwargs.get("cfworker_global_scan_interval") or 4)
        )
        global_scan_limit = max(
            5, min(50, int(kwargs.get("cfworker_global_scan_limit") or 25))
        )
        def sort_key(mail: dict) -> tuple[float, int]:
            created_at = str(mail.get("created_at", "") or "").strip()
            created_ts = float("-inf")
            if created_at:
                try:
                    created_ts = (
                        datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=timezone.utc)
                        .timestamp()
                    )
                except Exception:
                    created_ts = float("-inf")
            try:
                mail_id = int(mail.get("id", 0) or 0)
            except (TypeError, ValueError):
                mail_id = 0
            return (created_ts, mail_id)
        def poll_once() -> Optional[str]:
            nonlocal empty_address_polls
            try:
                bearer_token = str(account.account_id or self._token or "").strip()
                mails = self._get_mails(account.email, bearer_token=bearer_token)
                if mails:
                    empty_address_polls = 0
                else:
                    empty_address_polls += 1
                if not mails and (
                    empty_address_polls == 1
                    or empty_address_polls % global_scan_interval == 0
                ):
                    fallback_mails = self._get_mails_via_global_scan(
                        account.email,
                        limit=global_scan_limit,
                    )
                    if fallback_mails:
                        self._log(
                            f"[CFWorker] 地址查询为空，改用全局收件箱回退命中 {len(fallback_mails)} 封({account.email})"
                        )
                        mails = fallback_mails
                last_poll_error["detail"] = ""
                for mail in sorted(mails, key=sort_key, reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    created_at = str(mail.get("created_at", "") or "").strip()
                    if otp_cutoff and created_at:
                        try:
                            mail_ts = (
                                datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                                .replace(tzinfo=timezone.utc)
                                .timestamp()
                            )
                            if mail_ts < (otp_cutoff - otp_skew_grace_seconds):
                                self._log(
                                    f"[CFWorker] \u8df3\u8fc7\u65e7\u90ae\u4ef6 id={mid} created_at={created_at}"
                                )
                                continue
                        except Exception:
                            pass
                    # 仅在通过时间边界筛选后再标记为已处理，避免边界邮件被过早加入 seen。
                    seen.add(mid)
                    raw = str(mail.get("raw", ""))
                    subject = str(mail.get("subject", ""))
                    search_text = f"{subject} {self._decode_raw_content(raw)}".strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    search_text = re.sub(r"m=\+\d+\.\d+", "", search_text)
                    search_text = re.sub(r"\bt=\d+\b", "", search_text)
                    if keyword and keyword.lower() not in search_text.lower():
                        continue
                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        self._log(
                            f"[CFWorker] \u8df3\u8fc7\u5df2\u7528\u9a8c\u8bc1\u7801 id={mid} created_at={created_at} code={code}"
                        )
                        continue
                    if code:
                        self._log(
                            f"[CFWorker] \u547d\u4e2d\u65b0\u9a8c\u8bc1\u7801 id={mid} created_at={created_at} code={code}"
                        )
                        return code
            except Exception as exc:
                last_poll_error["detail"] = f"{type(exc).__name__}: {exc}"
                self._log(f"[CFWorker] 拉取邮件失败: {last_poll_error['detail']}")
            return None
        timeout_message = f"\u7b49\u5f85\u9a8c\u8bc1\u7801\u8d85\u65f6 ({timeout}s)"
        try:
            return self._run_polling_wait(
                timeout=timeout,
                poll_interval=3,
                poll_once=poll_once,
                timeout_message=timeout_message,
            )
        except TimeoutError as exc:
            detail = last_poll_error["detail"]
            if detail:
                raise TimeoutError(f"{timeout_message}; last_error={detail}") from exc
            raise
class MoeMailMailbox(BaseMailbox):
    """MoeMail (sall.cc) 邮箱服务 - 自动注册账号并生成临时邮箱"""
    def __init__(
        self, api_url: str = "https://sall.cc", api_key: str = "", proxy: str = None
    ):
        self.api = api_url.rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._session_token = None
        self._email = None
    def _api_headers(self) -> dict:
        if not self.api_key:
            return {}
        return {"X-API-Key": self.api_key}
    def _register_and_login(self) -> str:
        import requests, random, string
        s = requests.Session()
        s.proxies = self.proxy
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        s.headers.update(
            {"user-agent": ua, "origin": self.api, "referer": f"{self.api}/zh-CN/login"}
        )
        s.headers.update(self._api_headers())
        # 注册
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        print(f"[MoeMail] 注册账号: {username} / {password}")
        r_reg = s.post(
            f"{self.api}/api/auth/register",
            json={"username": username, "password": password, "turnstileToken": ""},
            timeout=15,
        )
        print(f"[MoeMail] 注册结果: {r_reg.status_code} {r_reg.text[:80]}")
        # 获取 CSRF
        csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        # 登录
        s.post(
            f"{self.api}/api/auth/callback/credentials",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=f"username={username}&password={password}&csrfToken={csrf}&redirect=false&callbackUrl={self.api}",
            allow_redirects=True,
            timeout=15,
        )
        self._session = s
        for cookie in s.cookies:
            if "session-token" in cookie.name:
                self._session_token = cookie.value
                print(f"[MoeMail] 登录成功")
                return cookie.value
        print(f"[MoeMail] 登录失败，cookies: {[c.name for c in s.cookies]}")
        return ""
    def get_email(self) -> MailboxAccount:
        # 每次调用都重新注册新账号，保证邮箱唯一
        self._session_token = None
        self._register_and_login()
        import random, string
        name = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        # 获取可用域名列表，随机选一个
        domain = "sall.cc"
        try:
            cfg_r = self._session.get(
                f"{self.api}/api/config", headers=self._api_headers(), timeout=10
            )
            domains = [
                d.strip()
                for d in cfg_r.json().get("emailDomains", "sall.cc").split(",")
                if d.strip()
            ]
            if domains:
                domain = random.choice(domains)
        except Exception:
            pass
        r = self._session.post(
            f"{self.api}/api/emails/generate",
            headers=self._api_headers(),
            json={"name": name, "domain": domain, "expiryTime": 86400000},
            timeout=15,
        )
        data = r.json()
        self._email = data.get("email", data.get("address", ""))
        email_id = data.get("id", "")
        print(
            f"[MoeMail] 生成邮箱: {self._email} id={email_id} domain={domain} status={r.status_code}"
        )
        if not email_id:
            print(f"[MoeMail] 生成失败: {data}")
        if email_id:
            self._email_count = getattr(self, "_email_count", 0) + 1
        return MailboxAccount(email=self._email, account_id=str(email_id))
    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails/{account.account_id}",
                headers=self._api_headers(),
                timeout=10,
            )
            return {str(m.get("id", "")) for m in r.json().get("messages", [])}
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        seen = set(before_ids or [])
        def poll_once() -> Optional[str]:
            try:
                r = self._session.get(
                    f"{self.api}/api/emails/{account.account_id}",
                    headers=self._api_headers(),
                    timeout=10,
                )
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    body = (
                        str(
                            msg.get("content")
                            or msg.get("text")
                            or msg.get("body")
                            or msg.get("html")
                            or ""
                        )
                        + " "
                        + str(msg.get("subject") or "")
                    )
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class LuckMailMailbox(BaseMailbox):
    """LuckMail 混合模式：ChatGPT 走购买邮箱，其他平台走订单接码"""
    def __init__(
        self,
        base_url: str,
        api_key: str,
        project_code: str = "",
        email_type: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        if not base_url or not api_key:
            raise RuntimeError(
                "LuckMail 未配置：请在全局设置中填写 luckmail_base_url 和 luckmail_api_key"
            )
        from .luckmail import LuckMailClient
        self._client = LuckMailClient(
            base_url=base_url,
            api_key=api_key,
            proxy_url=proxy,
        )
        self._project_code = project_code
        self._email_type = email_type or None
        self._domain = domain or None
        self._order_no = None
        self._token = None
        self._email = None
        self._purchase_id = None
    def _use_purchase_mode(self, account: MailboxAccount = None) -> bool:
        if (
            account
            and account.account_id
            and str(account.account_id).startswith("tok_")
        ):
            return True
        if self._token:
            return True
        return self._project_code == "openai"
    def _resolve_token(self, account: MailboxAccount = None) -> str:
        token = (account.account_id if account else "") or self._token
        if token:
            self._token = token
            return token
        email = (account.email if account else "") or self._email
        if not email:
            return ""
        try:
            purchases = self._client.user.get_purchases(
                page=1,
                page_size=100,
                keyword=email,
            )
        except Exception:
            return ""
        email_lower = str(email).strip().lower()
        for item in purchases.list:
            if str(item.email_address).strip().lower() == email_lower and item.token:
                self._token = item.token
                self._email = item.email_address
                return item.token
        return ""
    def _cancel_order_silently(self, order_no: str) -> None:
        if not order_no:
            return
        try:
            self._client.user.cancel_order(order_no)
            self._log(f"[LuckMail] 已取消订单: {order_no}")
        except Exception:
            pass
    def _extract_code_from_token_mails(
        self,
        token: str,
        code_pattern: str = None,
        before_ids: set = None,
        exclude_codes: set = None,
    ) -> Optional[str]:
        try:
            mail_list = self._client.user.get_token_mails(token)
        except Exception:
            return None
        seen = {str(mid) for mid in (before_ids or set())}
        excluded = {str(code) for code in (exclude_codes or set()) if code}
        for mail in mail_list.mails:
            message_id = str(mail.message_id or "")
            if message_id and message_id in seen:
                continue
            body = " ".join(
                [
                    str(mail.subject or ""),
                    str(mail.body or ""),
                    str(mail.html_body or ""),
                ]
            )
            code = self._safe_extract(body, code_pattern)
            if not code and message_id:
                try:
                    detail = self._client.user.get_token_mail_detail(token, message_id)
                    detail_body = " ".join(
                        [
                            str(getattr(detail, "verification_code", "") or ""),
                            str(getattr(detail, "subject", "") or ""),
                            str(getattr(detail, "body_text", "") or ""),
                            self._decode_raw_content(str(getattr(detail, "body_text", "") or "")),
                            str(getattr(detail, "body_html", "") or ""),
                            self._decode_raw_content(str(getattr(detail, "body_html", "") or "")),
                        ]
                    )
                    code = self._safe_extract(detail_body, code_pattern)
                except Exception:
                    pass
            if code and code in excluded:
                continue
            if code:
                return code
        return None
    def _is_openai_history_mail(self, mail) -> bool:
        text = " ".join(
            [
                str(getattr(mail, "from_addr", "") or ""),
                str(getattr(mail, "subject", "") or ""),
                str(getattr(mail, "body", "") or ""),
                str(getattr(mail, "html_body", "") or ""),
            ]
        ).lower()
        return "openai" in text or "chatgpt" in text
    def _check_purchased_mailbox_usable(self, email: str, token: str) -> tuple[bool, str]:
        try:
            alive = self._client.user.check_token_alive(token)
        except Exception as e:
            return False, f"token alive check failed: {e}"
        if not getattr(alive, "alive", False):
            status = getattr(alive, "status", "")
            message = getattr(alive, "message", "")
            return False, f"token not alive: {status} {message}".strip()
        try:
            mail_list = self._client.user.get_token_mails(token)
        except Exception as e:
            return False, f"mail list check failed: {e}"
        mails = list(getattr(mail_list, "mails", None) or [])
        history_count = sum(1 for mail in mails if self._is_openai_history_mail(mail))
        if history_count:
            return False, f"mailbox already has {history_count} OpenAI/ChatGPT mail(s)"
        mail_count = len(mails)
        if mail_count:
            self._log(
                f"[LuckMail] purchased mailbox has {mail_count} existing non-OpenAI mail(s); baseline will skip them"
            )
        return True, ""
    def _disable_unusable_purchase(self, item: dict, reason: str) -> None:
        purchase_id = item.get("id") if isinstance(item, dict) else None
        try:
            purchase_id = int(purchase_id)
        except (TypeError, ValueError):
            purchase_id = 0
        if purchase_id <= 0:
            return
        try:
            self._client.user.set_purchase_disabled(purchase_id, 1)
            self._log(
                f"[LuckMail] disabled unusable purchased mailbox purchase_id={purchase_id} ({reason})"
            )
        except Exception as e:
            self._log(
                f"[LuckMail] failed to disable unusable purchased mailbox "
                f"purchase_id={purchase_id}: {e}"
            )
    def _normalize_purchase_item(self, item) -> dict:
        if item is None:
            return {}
        if isinstance(item, dict):
            return dict(item)
        return {
            "id": getattr(item, "id", 0),
            "email_address": getattr(item, "email_address", ""),
            "token": getattr(item, "token", ""),
            "project_name": getattr(item, "project_name", ""),
            "price": getattr(item, "price", ""),
            "status": getattr(item, "status", 1),
            "tag_id": getattr(item, "tag_id", 0),
            "tag_name": getattr(item, "tag_name", ""),
            "user_disabled": getattr(item, "user_disabled", 0),
            "warranty_hours": getattr(item, "warranty_hours", 0),
            "warranty_until": getattr(item, "warranty_until", None),
            "created_at": getattr(item, "created_at", None),
        }
    def _build_purchase_account(self, item: dict, email: str, token: str) -> MailboxAccount:
        purchase_id = item.get("id") if isinstance(item, dict) else None
        try:
            purchase_id = int(purchase_id)
        except (TypeError, ValueError):
            purchase_id = 0
        self._email = email
        self._token = token
        self._purchase_id = purchase_id or None
        extra = {
            "provider": "luckmail",
            "token": token,
            "project_code": self._project_code,
        }
        if self._purchase_id:
            extra["purchase_id"] = self._purchase_id
        return MailboxAccount(
            email=email,
            account_id=token,
            extra=extra,
        )
    def get_email(self) -> MailboxAccount:
        if not self._project_code:
            raise RuntimeError("LuckMail project_code is required before creating a mailbox")
        if self._use_purchase_mode():
            self._token = None
            self._email = None
            self._purchase_id = None
            last_error = ""
            max_purchase_attempts = 3
            for attempt in range(1, max_purchase_attempts + 1):
                try:
                    result = self._client.user.purchase_emails(
                        project_code=self._project_code,
                        quantity=1,
                        email_type=self._email_type,
                        domain=self._domain,
                    )
                except Exception as e:
                    raise RuntimeError(f"LuckMail purchase failed: {e}") from e
                purchases = (result or {}).get("purchases") or []
                if not purchases:
                    raise RuntimeError(f"LuckMail purchase returned empty result: {result}")
                item = purchases[0]
                email = str(item.get("email_address") or "").strip()
                token = str(item.get("token") or "").strip()
                if not email or not token:
                    raise RuntimeError(f"LuckMail purchase returned missing email/token: {item}")
                usable, reason = self._check_purchased_mailbox_usable(email, token)
                if not usable:
                    last_error = reason
                    self._log(
                        f"[LuckMail] rejected purchased mailbox attempt {attempt}/{max_purchase_attempts}: "
                        f"{email} ({reason})"
                    )
                    self._disable_unusable_purchase(item, reason)
                    continue
                self._log(f"[LuckMail] purchased mailbox: {email}")
                if item.get("warranty_until"):
                    self._log(f"[LuckMail] warranty_until: {item.get('warranty_until')}")
                return self._build_purchase_account(item, email, token)
            raise RuntimeError(
                f"LuckMail failed to buy usable LuckMail mailbox after {max_purchase_attempts} attempts: {last_error}"
            )
        self._log(
            f"[LuckMail] 分支: 其他平台 + LuckMail -> 创建订单/订单接码 "
            f"(project_code={self._project_code}, email_type={self._email_type or '-'})"
        )
        try:
            body = {"project_code": self._project_code}
            if self._email_type:
                body["email_type"] = self._email_type
            order = self._client.user._sync_create_order(body)
        except Exception as e:
            raise RuntimeError(f"LuckMail 创建订单失败: {e}") from e
        self._order_no = order.order_no
        email = order.email_address
        self._email = email
        self._purchase_id = None
        self._log(f"[LuckMail] 订单 {order.order_no} 分配邮箱: {email}")
        self._log(f"[LuckMail] 超时时间: {order.expired_at}")
        return MailboxAccount(email=email, account_id=order.order_no)
    def get_current_ids(self, account: MailboxAccount) -> set:
        if not self._use_purchase_mode(account):
            return set()
        token = self._resolve_token(account)
        if not token:
            return set()
        try:
            mail_list = self._client.user.get_token_mails(token)
            return {str(m.message_id) for m in (mail_list.mails or []) if m.message_id}
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        if not self._use_purchase_mode(account):
            self._log("[LuckMail] 等验证码分支: 订单接码")
            order_no = account.account_id or self._order_no
            if not order_no:
                raise RuntimeError("LuckMail 未创建订单，无法等待验证码")
            def on_poll_order(result):
                self._log(f"[LuckMail] 轮询中... 状态: {result.status}")
            deadline = time.monotonic() + max(int(timeout or 0), 1)
            last_status = "pending"
            try:
                while time.monotonic() < deadline:
                    self._checkpoint()
                    remaining = max(1, int(deadline - time.monotonic()))
                    slice_timeout = min(remaining, 6)
                    try:
                        code_result = self._client.user._sync_wait_for_code(
                            order_no=order_no,
                            timeout=slice_timeout,
                            interval=3.0,
                            on_poll=on_poll_order,
                        )
                    except Exception as e:
                        raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e
                    last_status = str(code_result.status or "pending")
                    if code_result.status == "success" and code_result.verification_code:
                        code = code_result.verification_code
                        self._log(f"[LuckMail] 收到验证码: {code}")
                        return code
                    if code_result.status in {"cancelled", "timeout"}:
                        break
            except Exception:
                self._cancel_order_silently(order_no)
                raise
            self._cancel_order_silently(order_no)
            raise TimeoutError(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: {last_status}"
            )
        token = self._resolve_token(account)
        if not token:
            raise RuntimeError("LuckMail 未找到已购邮箱 Token，无法等待验证码")
        self._log("[LuckMail] 等验证码分支: 已购邮箱 Token 收码")
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }
        seen_message_ids = {str(mid) for mid in (before_ids or set()) if mid}
        pending_message_ids: set[str] = set()
        pending_message_first_seen: dict[str, float] = {}
        pending_parse_timeout_seconds = max(
            int(kwargs.get("pending_parse_timeout_seconds") or 20),
            1,
        )
        no_new_mail_timeout_seconds = max(
            int(kwargs.get("no_new_mail_timeout_seconds") or 20),
            1,
        )
        no_new_mail_started_at: Optional[float] = None
        if before_ids is None:
            seen_message_ids = self.get_current_ids(account)
            if seen_message_ids:
                self._log(
                    f"[LuckMail] 已建立旧邮件基线，先跳过 {len(seen_message_ids)} 封历史邮件"
                )
        saw_new_mail = False
        def extract_token_mail_code(mail, message_id: str) -> Optional[str]:
            body = " ".join(
                [
                    str(mail.subject or ""),
                    str(mail.body or ""),
                    str(mail.html_body or ""),
                ]
            )
            code = self._safe_extract(body, code_pattern)
            if code:
                return code
            if not message_id:
                return None
            try:
                detail = self._client.user.get_token_mail_detail(token, message_id)
            except Exception as e:
                self._log(f"[LuckMail] 拉取邮件详情失败 message_id={message_id}: {e}")
                return None
            detail_body = " ".join(
                [
                    str(getattr(detail, "verification_code", "") or ""),
                    str(getattr(detail, "subject", "") or ""),
                    str(getattr(detail, "body_text", "") or ""),
                    self._decode_raw_content(str(getattr(detail, "body_text", "") or "")),
                    str(getattr(detail, "body_html", "") or ""),
                    self._decode_raw_content(str(getattr(detail, "body_html", "") or "")),
                ]
            )
            return self._safe_extract(detail_body, code_pattern)
        def poll_once() -> Optional[str]:
            nonlocal saw_new_mail, no_new_mail_started_at
            now = time.monotonic()
            if no_new_mail_started_at is None:
                no_new_mail_started_at = now
            new_mail_count = 0
            try:
                mail_list = self._client.user.get_token_mails(token)
            except Exception as e:
                err_text = f"{e.__class__.__name__}: {e}"
                transient = e.__class__.__name__ in {"NetworkError", "TimeoutError"} or any(
                    marker in err_text.lower()
                    for marker in (
                        "remote end closed connection",
                        "connection aborted",
                        "connection reset",
                        "network",
                        "timeout",
                    )
                )
                if transient:
                    self._log(f"[LuckMail] 轮询网络异常，继续等待: {err_text[:200]}")
                    return None
                raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e
            mail_items = list(mail_list.mails or [])
            pending_lookup = {
                str(mail.message_id or "").strip(): mail
                for mail in mail_items
                if str(mail.message_id or "").strip()
            }
            for pending_id in list(pending_message_ids):
                first_seen = pending_message_first_seen.get(pending_id, now)
                if now - first_seen > pending_parse_timeout_seconds:
                    self._log(
                        f"[LuckMail] 邮件 {pending_id} 解析超过 {pending_parse_timeout_seconds}s，跳过该邮件"
                    )
                    pending_message_ids.discard(pending_id)
                    pending_message_first_seen.pop(pending_id, None)
                    seen_message_ids.add(pending_id)
                    continue
                mail = pending_lookup.get(pending_id)
                if not mail:
                    continue
                code = extract_token_mail_code(mail, pending_id)
                if code and code in exclude_codes:
                    self._log(
                        f"[LuckMail] 跳过已使用验证码 message_id={pending_id} code={code}"
                    )
                    pending_message_ids.discard(pending_id)
                    pending_message_first_seen.pop(pending_id, None)
                    seen_message_ids.add(pending_id)
                    continue
                if code:
                    self._log(f"[LuckMail] 收到验证码: {code}")
                    pending_message_ids.discard(pending_id)
                    pending_message_first_seen.pop(pending_id, None)
                    seen_message_ids.add(pending_id)
                    return code
            for mail in mail_items:
                message_id = str(mail.message_id or "").strip()
                if message_id and (message_id in seen_message_ids or message_id in pending_message_ids):
                    continue
                new_mail_count += 1
                saw_new_mail = True
                code = extract_token_mail_code(mail, message_id)
                if code and code in exclude_codes:
                    self._log(
                        f"[LuckMail] 跳过已使用验证码 message_id={message_id or '-'} code={code}"
                    )
                    if message_id:
                        seen_message_ids.add(message_id)
                    continue
                if code:
                    self._log(f"[LuckMail] 收到验证码: {code}")
                    if message_id:
                        seen_message_ids.add(message_id)
                    return code
                if message_id:
                    pending_message_ids.add(message_id)
                    pending_message_first_seen.setdefault(message_id, now)
            if not pending_message_ids and now - no_new_mail_started_at > no_new_mail_timeout_seconds:
                self._log(
                    f"[LuckMail] {no_new_mail_timeout_seconds}s 内未收到新邮件，停止等待本次验证码"
                )
                raise TimeoutError(f"LuckMail {no_new_mail_timeout_seconds}s 内未收到新邮件")
            self._log(
                f"[LuckMail] 轮询中: 新邮件={new_mail_count}, 待解析={len(pending_message_ids)}"
            )
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: "
                f"has_new_mail={saw_new_mail}"
            ),
        )
    def update_status(self, account: MailboxAccount, success: bool, error: str = None) -> None:
        if success:
            return
        purchase_id = None
        if account and isinstance(getattr(account, "extra", None), dict):
            purchase_id = account.extra.get("purchase_id")
        if not purchase_id:
            purchase_id = self._purchase_id
        try:
            purchase_id = int(purchase_id)
        except (TypeError, ValueError):
            purchase_id = 0
        if purchase_id <= 0:
            return
        reason = str(error or "registration failed").strip() or "registration failed"
        self._disable_unusable_purchase({"id": purchase_id}, reason)
class FreemailMailbox(BaseMailbox):
    """
    Freemail 自建邮箱服务（基于 Cloudflare Worker）
    项目: https://github.com/idinging/freemail
    支持管理员令牌或账号密码两种认证方式
    """
    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        username: str = "",
        password: str = "",
        proxy: str = None,
    ):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.username = username
        self.password = password
        self.proxy = build_requests_proxy_config(proxy)
        self._session = None
        self._email = None
    def _get_session(self):
        import requests
        s = requests.Session()
        s.proxies = self.proxy
        if self.admin_token:
            s.headers.update({"Authorization": f"Bearer {self.admin_token}"})
        elif self.username and self.password:
            s.post(
                f"{self.api}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=15,
            )
        self._session = s
        return s
    def get_email(self) -> MailboxAccount:
        if not self._session:
            self._get_session()
        import requests
        import traceback
        
        # 记录调用栈，追踪是谁在创建邮箱
        self._log(f"[Freemail] 正在调用 get_email() 创建新邮箱...")
        self._log(f"[Freemail] 调用栈:\n{''.join(traceback.format_stack()[-5:-1])}")
        
        r = self._session.get(f"{self.api}/api/generate", timeout=15)
        data = r.json()
        email = data.get("email", "")
        self._email = email
        print(f"[Freemail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)
    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails",
                params={"mailbox": account.email, "limit": 50},
                timeout=10,
            )
            return {str(m["id"]) for m in r.json() if "id" in m}
        except Exception:
            return set()
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        self._log(f"[Freemail] 排除验证码: {exclude_codes}")
        # 确保 session 已初始化
        if not self._session:
            self._log("[Freemail] Session 未初始化，正在初始化...")
            self._get_session()
        def poll_once() -> Optional[str]:
            try:
                api_url = f"{self.api}/api/emails"
                params = {"mailbox": account.email, "limit": 20}
                self._log(f"[Freemail] 请求: {api_url}, 参数: {params}")
                r = self._session.get(
                    api_url,
                    params=params,
                    timeout=10,
                )
                response_text = str(getattr(r, "text", "") or "")
                self._log(
                    f"[Freemail] 响应状态: {getattr(r, 'status_code', '-')}, "
                    f"响应前 200 字符: {response_text[:200]}"
                )
                try:
                    emails = r.json()
                except Exception as json_err:
                    self._log(f"[Freemail] JSON 解析失败: {json_err}")
                    emails = []
                self._log(f"[Freemail] 获取到 {len(emails)} 封邮件")
                for msg in emails:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    # 优先用 verification_code 字段
                    code = str(msg.get("verification_code") or "").strip()
                    if code and code != "None":
                        self._log(f"[Freemail] 邮件 {mid} verification_code={code}, 排除={code in exclude_codes}")
                        if code in exclude_codes:
                            continue
                        return code
                    # 兜底：从 preview、subject 和 body 提取
                    text = (
                        str(msg.get("preview", "")) + " " +
                        str(msg.get("subject", "")) + " " +
                        str(msg.get("body", "") or msg.get("html", "") or msg.get("text", "") or "")
                    )
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        self._log(f"[Freemail] 邮件 {mid} 提取验证码={code}, 排除={code in exclude_codes}")
                        if code in exclude_codes:
                            continue
                        return code
            except Exception as e:
                self._log(f"[Freemail] 轮询异常: {e}")
                import traceback
                self._log(f"[Freemail] 堆栈: {traceback.format_exc()}")
                pass
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
class Mail2925WebClient:
    """2925 webmail session client."""
    @staticmethod
    def _split_mailbox_login(login_name: str, domain: str) -> tuple[str, str]:
        raw_login = (login_name or "").strip()
        raw_domain = (domain or "2925.com").strip().lstrip("@")
        if "@" not in raw_login:
            return raw_login, raw_domain
        local_part, _, domain_part = raw_login.partition("@")
        return local_part.strip(), (domain_part or raw_domain).strip().lstrip("@")
    def __init__(
        self,
        login_name: str,
        password: str,
        domain: str = "2925.com",
        proxy: str = None,
        log_fn: Callable[[str], None] | None = None,
    ):
        self.login_name, self.domain = self._split_mailbox_login(login_name, domain)
        self.password = password or ""
        self.proxy = proxy
        self._log_fn = log_fn
        self.base_url = "https://mail.2925.com"
        self.api_base = f"{self.base_url}/mailv2"
        self._session = None
        self._authorization = ""
        self._device_uid = ""
        self._cookies = []
    def _log(self, message: str) -> None:
        if callable(self._log_fn):
            self._log_fn(message)
    def _mailbox_email(self) -> str:
        return f"{self.login_name}@{self.domain}"
    def _requests_session(self):
        import requests
        if self._session is None:
            session = requests.Session()
            session.proxies = build_requests_proxy_config(self.proxy)
            session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.0.0 Safari/537.36"
                    ),
                    "Referer": f"{self.base_url}/",
                    "Origin": self.base_url,
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
            self._session = session
        return self._session
    def _login(self) -> dict[str, Any]:
        import hashlib
        import uuid
        session = self._requests_session()
        device_uid = self._device_uid or str(uuid.uuid4())
        response = session.post(
            f"{self.base_url}/mailv2/auth/weblogin",
            params={"traceId": uuid.uuid4().hex},
            data={
                "uname": self._mailbox_email(),
                "rsapwd": hashlib.md5(self.password.encode("utf-8")).hexdigest(),
                "rememberLogin": "false",
                "deviceIds": "[]",
            },
            headers={"deviceUid": device_uid, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        result = ((data or {}).get("result") or {}) if isinstance(data, dict) else {}
        if data.get("code") != 200 or not result.get("success") or not result.get("token"):
            raise RuntimeError(
                f"2925 login failed: code={data.get('code')} message={data.get('message') or result.get('message')}"
            )
        jwt_response = session.post(
            f"{self.base_url}/mailv2/auth/token",
            params={"traceId": uuid.uuid4().hex},
            headers={"deviceUid": device_uid},
            timeout=20,
        )
        jwt_response.raise_for_status()
        jwt_data = jwt_response.json()
        jwt_token = ((jwt_data or {}).get("result") or "") if isinstance(jwt_data, dict) else ""
        if jwt_token:
            session.cookies.set("jwt_token", jwt_token, domain="mail.2925.com", path="/")
        return {
            "authorization": f"Bearer {result.get('token')}",
            "device_uid": device_uid,
            "cookies": response.cookies,
        }
    def _ensure_session(self):
        session = self._requests_session()
        if self._authorization and self._device_uid:
            return session
        auth = self._login()
        self._authorization = auth.get("authorization", "")
        self._device_uid = auth.get("device_uid", "")
        self._cookies = auth.get("cookies", []) or []
        session.headers.update(
            {
                "Authorization": self._authorization,
                "deviceUid": self._device_uid,
            }
        )
        try:
            session.cookies.update(self._cookies)
        except Exception:
            pass
        return session
    def _request(self, method: str, path: str, *, params: dict | None = None):
        import uuid
        session = self._ensure_session()
        query = dict(params or {})
        query.setdefault("traceId", uuid.uuid4().hex)
        response = session.request(
            method,
            f"{self.base_url}{path}",
            params=query,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code") not in (None, 0, 200):
            raise RuntimeError(
                f"2925 API request failed: {path} code={data.get('code')} message={data.get('message')}"
            )
        return data
    def list_messages(
        self,
        folder: str = "Inbox",
        filter_type: int = 0,
        page_index: int = 1,
        page_count: int = 25,
    ) -> dict:
        data = self._request(
            "GET",
            "/mailv2/maildata/MailList/mails",
            params={
                "Folder": folder,
                "MailBox": self._mailbox_email(),
                "FilterType": filter_type,
                "PageIndex": page_index,
                "PageCount": page_count,
            },
        )
        return data.get("result") or {}
    def get_message(
        self,
        message_id: str,
        folder_name: str = "Inbox",
        is_pre: bool = False,
    ) -> dict:
        data = self._request(
            "GET",
            "/mailv2/maildata/MailRead/mails/read",
            params={
                "MessageID": message_id,
                "FolderName": folder_name,
                "MailBox": self._mailbox_email(),
                "IsPre": str(bool(is_pre)).lower(),
            },
        )
        return data.get("result") or {}
class Mail2925Mailbox(BaseMailbox):
    """2925 mailbox provider over web session APIs."""
    MAIN_ALIAS_MODES = {"main", "fixed", "none", "non-plus", "nonplus"}
    RANDOM_ALIAS_MODES = {"random", "random_local"}
    def __init__(
        self,
        login_name: str,
        password: str,
        alias_mode: str = "plus",
        domain: str = "2925.com",
        proxy: str = None,
        web_client: Mail2925WebClient | None = None,
    ):
        self.login_name, self.domain = Mail2925WebClient._split_mailbox_login(
            login_name,
            domain,
        )
        self.password = password or ""
        self.alias_mode = self._normalize_alias_mode(alias_mode)
        self.proxy = proxy
        self.base_email = (
            f"{self.login_name}@{self.domain}" if self.login_name and self.domain else ""
        )
        self.web_client = web_client or Mail2925WebClient(
            login_name=self.login_name,
            password=self.password,
            domain=self.domain,
            proxy=proxy,
            log_fn=self._log,
        )
    @classmethod
    def _normalize_alias_mode(cls, alias_mode: str) -> str:
        mode = (alias_mode or "plus").strip().lower()
        if mode in cls.MAIN_ALIAS_MODES:
            return "main"
        if mode in cls.RANDOM_ALIAS_MODES:
            return mode
        return "plus"
    def _retry_without_proxy_if_needed(self, exc: Exception) -> bool:
        message = str(exc or "")
        active_proxy = getattr(self.web_client, "proxy", None)
        if not active_proxy:
            return False
        transient_markers = (
            "ProxyError",
            "Unable to connect to proxy",
            "SSLError",
            "UNEXPECTED_EOF_WHILE_READING",
            "EOF occurred in violation of protocol",
            "RemoteDisconnected",
            "Connection aborted",
            "Connection reset",
            "Read timed out",
            "ConnectTimeout",
            "ReadTimeout",
            "Max retries exceeded",
        )
        if not any(marker in message for marker in transient_markers):
            return False
        self._log("[2925] proxy failed, retrying mailbox without proxy")
        self.web_client.proxy = None
        if hasattr(self.web_client, "_session"):
            self.web_client._session = None
        if hasattr(self.web_client, "_authorization"):
            self.web_client._authorization = ""
        if hasattr(self.web_client, "_device_uid"):
            self.web_client._device_uid = ""
        if hasattr(self.web_client, "_cookies"):
            self.web_client._cookies = []
        return True
    def _list_inbox_messages(
        self,
        *,
        page_count: int = 25,
    ) -> dict:
        primary = self.web_client.list_messages(
            folder="Inbox",
            filter_type=0,
            page_index=1,
            page_count=page_count,
        )
        primary_messages = primary.get("list") or []
        if primary_messages:
            return primary
        fallback = self.web_client.list_messages(
            folder="INBOX",
            filter_type=0,
            page_index=1,
            page_count=page_count,
        )
        fallback_messages = fallback.get("list") or []
        if fallback_messages:
            self._log("[2925] inbox folder fallback matched INBOX")
            return fallback
        return primary
    def _validate_config(self) -> None:
        if not self.login_name or not self.password:
            raise RuntimeError(
                "2925 mailbox is not configured. Please set mail2925_login_name and mail2925_password."
            )
    def _random_suffix(self, length: int = 8) -> str:
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "".join(random.choice(alphabet) for _ in range(length))
    def _message_id(self, message: dict) -> str:
        return str(message.get("messageId") or message.get("mailId") or "").strip()
    def _build_message_text(self, message: dict, detail: dict | None = None) -> str:
        import html
        import re
        detail = detail or {}
        parts: list[str] = []
        for candidate in (
            message.get("subject"),
            message.get("bodyContent"),
            detail.get("mailSubject"),
            detail.get("bodyText"),
            detail.get("bodyHtmlText"),
        ):
            if candidate:
                parts.append(str(candidate))
        sender = message.get("sender") or {}
        if sender.get("sender"):
            parts.append(str(sender.get("sender")))
        if sender.get("senderDisplay"):
            parts.append(str(sender.get("senderDisplay")))
        for recipient in message.get("toAddress") or []:
            if recipient:
                parts.append(str(recipient))
        for recipient in detail.get("mailTo") or []:
            if isinstance(recipient, dict) and recipient.get("emailAddress"):
                parts.append(str(recipient.get("emailAddress")))
        combined = "\n".join(parts)
        combined = html.unescape(combined)
        combined = re.sub(r"<[^>]+>", " ", combined)
        return re.sub(r"\s+", " ", combined).strip()
    def _message_matches_account(
        self,
        account: MailboxAccount,
        message: dict,
        detail: dict | None = None,
    ) -> bool:
        target = (getattr(account, "email", "") or "").strip().lower()
        account_extra = getattr(account, "extra", {}) or {}
        alias_mode = self._normalize_alias_mode(
            str(account_extra.get("alias_mode") or self.alias_mode).strip().lower()
        )
        base_email = str(
            account_extra.get("base_email", "") or self.base_email
        ).strip().lower()
        candidates = {target}
        if base_email and alias_mode == "main":
            candidates.add(base_email)
        if alias_mode == "main" and "+" in target and "@" in target:
            local, domain = target.split("@", 1)
            candidates.add(f"{local.split('+', 1)[0]}@{domain}")
        candidates.discard("")
        if not candidates:
            return True
        haystack = self._build_message_text(message, detail).lower()
        return any(candidate in haystack for candidate in candidates)
    def _message_has_any_recipient_hint(self, message: dict, detail: dict | None = None) -> bool:
        detail = detail or {}
        for recipient in message.get("toAddress") or []:
            if str(recipient or "").strip():
                return True
        for recipient in detail.get("mailTo") or []:
            if isinstance(recipient, dict) and str(recipient.get("emailAddress") or "").strip():
                return True
        return False
    def get_email(self) -> MailboxAccount:
        self._validate_config()
        if self.alias_mode == "main":
            address = self.base_email
        elif self.alias_mode in self.RANDOM_ALIAS_MODES:
            address = f"{self._random_suffix(10)}@{self.domain}"
        else:
            address = f"{self.login_name}+{self._random_suffix()}@{self.domain}"
        return MailboxAccount(
            email=address,
            account_id=address,
            extra={
                "provider": "mail2925",
                "base_email": self.base_email,
                "login_name": self.login_name,
                "alias_mode": self.alias_mode,
            },
        )
    def get_current_ids(self, account: MailboxAccount) -> set:
        account_extra = getattr(account, "extra", {}) or {}
        alias_mode = self._normalize_alias_mode(
            str(account_extra.get("alias_mode") or self.alias_mode).lower()
        )
        if alias_mode != "main":
            self._log("[2925] skip pre-send inbox snapshot for generated alias")
            return set()
        try:
            result = self._list_inbox_messages(page_count=25)
        except Exception as exc:
            self._log(f"[2925] web list failed while reading ids: {exc}")
            return set()
        return {
            self._message_id(message)
            for message in (result.get("list") or [])
            if self._message_id(message)
        }
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = {str(uid) for uid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        pending_retry_ids: set[str] = set()
        def poll_once() -> Optional[str]:
            try:
                result = self._list_inbox_messages(page_count=25)
                messages = result.get("list") or []
                self._log(f"[2925] polling inbox: messages={len(messages)}, seen={len(seen)}")
                alias_mismatch_count = 0
                alias_mismatch_after_detail_count = 0
                for message in messages:
                    message_id = self._message_id(message)
                    if (
                        not message_id
                        or (message_id in seen and message_id not in pending_retry_ids)
                    ):
                        continue
                    preview_matches = self._message_matches_account(account, message)
                    preview_has_recipient_hint = self._message_has_any_recipient_hint(message)
                    if not preview_matches and preview_has_recipient_hint:
                        pending_retry_ids.discard(message_id)
                        seen.add(message_id)
                        alias_mismatch_count += 1
                        continue
                    folder_name = str(message.get("folder") or "Inbox")
                    try:
                        detail = self.web_client.get_message(
                            message_id,
                            folder_name=folder_name,
                            is_pre=False,
                        )
                    except Exception as detail_exc:
                        if self._retry_without_proxy_if_needed(detail_exc):
                            pending_retry_ids.add(message_id)
                            continue
                        pending_retry_ids.add(message_id)
                        self._log(
                            f"[2925] detail fetch failed for message id={message_id}: {detail_exc}"
                        )
                        continue
                    detail_matches = self._message_matches_account(account, message, detail)
                    if not preview_matches and not detail_matches:
                        pending_retry_ids.discard(message_id)
                        seen.add(message_id)
                        alias_mismatch_after_detail_count += 1
                        continue
                    text = self._build_message_text(message, detail)
                    if keyword and keyword.lower() not in text.lower():
                        pending_retry_ids.discard(message_id)
                        seen.add(message_id)
                        self._log(f"[2925] skip message id={message_id}: keyword mismatch")
                        continue
                    code = self._safe_extract(text, code_pattern)
                    if not code:
                        pending_retry_ids.add(message_id)
                        continue
                    pending_retry_ids.discard(message_id)
                    seen.add(message_id)
                    if code in exclude_codes:
                        self._log(f"[2925] skip excluded code id={message_id} code={code}")
                        continue
                    if alias_mismatch_count:
                        self._log(
                            f"[2925] skip alias mismatch messages: count={alias_mismatch_count}"
                        )
                    if alias_mismatch_after_detail_count:
                        self._log(
                            "[2925] skip alias mismatch after detail messages: "
                            f"count={alias_mismatch_after_detail_count}"
                        )
                    self._log(f"[2925] received verification code: {code}")
                    return code
                if alias_mismatch_count:
                    self._log(
                        f"[2925] skip alias mismatch messages: count={alias_mismatch_count}"
                    )
                if alias_mismatch_after_detail_count:
                    self._log(
                        "[2925] skip alias mismatch after detail messages: "
                        f"count={alias_mismatch_after_detail_count}"
                    )
            except Exception as exc:
                if self._retry_without_proxy_if_needed(exc):
                    return None
                self._log(f"[2925] web poll failed: {exc}")
            return None
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=6,
            poll_once=poll_once,
            timeout_message=f"2925 waiting for verification code timed out ({timeout}s)",
        )
