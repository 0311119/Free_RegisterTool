import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform


class _BlankMailbox:
    def get_email(self):
        return MailboxAccount(email="", account_id="blank-mailbox")

    def wait_for_code(self, *args, **kwargs):
        return "123456"


class _TrackingMailbox:
    def __init__(self):
        self.account = MailboxAccount(email="demo@example.com", account_id="tracked-mailbox")
        self.wait_call = None
        self.current_ids_calls = []
        self.status_updates = []

    def get_email(self):
        return self.account

    def get_current_ids(self, account):
        self.current_ids_calls.append(account)
        return {"mid-1"}

    def wait_for_code(self, *args, **kwargs):
        self.wait_call = (args, kwargs)
        return "123456"

    def update_status(self, account, success, error=None):
        self.status_updates.append((account, success, error))


class _FakeAdapter:
    def run(self, context):
        context.email_service.create_email()
        raise AssertionError("create_email 应该先报错")


class _VerificationAdapter:
    def __init__(self):
        self.run_called = False

    def run(self, context):
        self.run_called = True
        context.email_service.create_email()
        code = context.email_service.get_verification_code(
            timeout=30,
            otp_sent_at=123.0,
            exclude_codes={"654321"},
        )
        self.last_code = code
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return {"success": True, "password": fallback_password}


class _ContextCaptureAdapter:
    def __init__(self):
        self.context = None

    def run(self, context):
        self.context = context
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return {"success": True}


class _FailingAdapter:
    def run(self, context):
        context.email_service.create_email()
        return mock.Mock(success=False, error_message="registration failed")

    def build_account(self, result, fallback_password):
        raise AssertionError("build_account should not be called for failures")


class _OAuthBlockedAdapter:
    def run(self, context):
        context.email_service.create_email()
        return mock.Mock(
            success=False,
            error_message="OAuth blocked at add-phone step after basic account creation",
            metadata={"oauth_state": "blocked_by_phone"},
        )

    def build_account(self, result, fallback_password):
        raise AssertionError("build_account should not be called when OAuth is blocked")


class ChatGPTPluginTests(unittest.TestCase):
    def test_custom_provider_rejects_blank_email(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=_BlankMailbox(),
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FakeAdapter(),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                platform.register()

        self.assertIn("custom_provider 返回空邮箱地址", str(ctx.exception))

    def test_custom_provider_uses_mailbox_baseline_for_verification_code(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register()

        self.assertTrue(adapter.run_called)
        self.assertEqual(adapter.last_code, "123456")
        self.assertEqual(result["success"], True)
        self.assertEqual(mailbox.current_ids_calls, [mailbox.account])
        self.assertIsNotNone(mailbox.wait_call)
        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("before_ids"), {"mid-1"})
        self.assertEqual(kwargs.get("otp_sent_at"), 123.0)
        self.assertEqual(kwargs.get("exclude_codes"), {"654321"})

    def test_platform_passes_task_control_to_registration_context(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=_TrackingMailbox(),
        )
        task_control = object()
        platform.bind_task_control(task_control)
        adapter = _ContextCaptureAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        self.assertIs(adapter.context.task_control, task_control)

    def test_custom_provider_reports_success_status_back_to_mailbox(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        self.assertEqual(
            mailbox.status_updates,
            [(mailbox.account, True, None)],
        )

    def test_custom_provider_reports_failure_status_back_to_mailbox(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FailingAdapter(),
        ):
            with self.assertRaises(RuntimeError):
                platform.register()

        self.assertEqual(len(mailbox.status_updates), 1)
        account, success, error = mailbox.status_updates[0]
        self.assertEqual(account, mailbox.account)
        self.assertFalse(success)
        self.assertEqual(error, "registration failed")

    def test_oauth_blocked_result_is_treated_as_registration_failure(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_OAuthBlockedAdapter(),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                platform.register()

        self.assertIn("OAuth blocked", str(ctx.exception))
        self.assertEqual(len(mailbox.status_updates), 1)
        account, success, error = mailbox.status_updates[0]
        self.assertEqual(account, mailbox.account)
        self.assertFalse(success)
        self.assertIn("OAuth blocked", error)

    def test_payment_link_action_returns_cashier_url_and_description(self):
        platform = ChatGPTPlatform(config=RegisterConfig())
        account = mock.Mock(
            email="demo@example.com",
            token="access-token",
            user_id="user-1",
            extra={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "session_token": "session-token",
                "cookies": "oai-did=device-1",
            },
        )

        with mock.patch(
            "platforms.chatgpt.payment.generate_plus_link",
            return_value="https://chatgpt.com/checkout/openai_llc/cs_test_123",
        ):
            result = platform.execute_action(
                "payment_link",
                account,
                {"plan": "plus", "country": "DE"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["data"]["cashier_url"],
            "https://chatgpt.com/checkout/openai_llc/cs_test_123",
        )
        self.assertIn("Plus", result["data"]["description"])
        self.assertEqual(
            result["account_extra_patch"]["payment_link"]["plan"],
            "plus",
        )
        self.assertEqual(
            result["account_extra_patch"]["payment_link"]["country"],
            "DE",
        )

    def test_sync_sub2api_status_action_returns_sync_payload(self):
        platform = ChatGPTPlatform(config=RegisterConfig())
        account = mock.Mock(
            email="demo@example.com",
            token="access-token",
            user_id="user-1",
            extra={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "session_token": "session-token",
                "cookies": "oai-did=device-1",
            },
        )

        with mock.patch(
            "platforms.chatgpt.sub2api_upload.query_sub2api_account",
            return_value={
                "ok": True,
                "found": True,
                "uploaded": True,
                "remote_state": "exists",
                "message": "remote exists",
                "remote_account_id": 42,
            },
        ):
            result = platform.execute_action("sync_sub2api_status", account, {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["sync"]["remote_state"], "exists")
        self.assertEqual(
            result["account_extra_patch"]["sync_statuses"]["sub2api"]["remote_account_id"],
            42,
        )


if __name__ == "__main__":
    unittest.main()
