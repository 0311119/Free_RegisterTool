import json
import unittest
from email.message import EmailMessage
from unittest.mock import patch, Mock

from core.base_mailbox import DuckDuckGoMailbox, create_mailbox


class DuckDuckGoMailboxTests(unittest.TestCase):
    def _build_mailbox(self):
        mailbox = create_mailbox(
            "duckduckgo",
            extra={
                "duckduckgo_email": "wjwago@duck.com",
                "duckduckgo_gmail_address": "ago950523@gmail.com",
                "duckduckgo_gmail_app_password": "app password",
            },
        )
        mailbox_logs = []
        mailbox._log_fn = mailbox_logs.append
        mailbox._test_logs = mailbox_logs
        return mailbox

    def test_factory_builds_duckduckgo_mailbox(self):
        mailbox = self._build_mailbox()

        self.assertIsInstance(mailbox, DuckDuckGoMailbox)
        self.assertEqual(mailbox.duck_email, "wjwago@duck.com")
        self.assertEqual(mailbox.gmail_address, "ago950523@gmail.com")
        self.assertEqual(mailbox.gmail_app_password, "apppassword")

    def test_get_email_returns_fixed_duck_address(self):
        mailbox = self._build_mailbox()

        account = mailbox.get_email()

        self.assertEqual(account.email, "wjwago@duck.com")
        self.assertEqual(account.account_id, "ago950523@gmail.com")
        self.assertEqual(account.extra["provider"], "duckduckgo")

    @patch("time.sleep", return_value=None)
    def test_wait_for_code_reads_forwarded_gmail_message(self, _sleep):
        mailbox = self._build_mailbox()
        message = EmailMessage()
        message["Subject"] = "Your temporary ChatGPT verification code"
        message["From"] = "OpenAI <noreply@tm.openai.com>"
        message["To"] = "ago950523@gmail.com"
        message["Date"] = "Fri, 15 May 2026 10:00:00 +0000"
        message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 654321"
        )
        fake_imap = _FakeIMAP(
            search_ids=[b"100 101"],
            messages={"101": message.as_bytes()},
        )
        mailbox._open_mailbox = lambda: fake_imap

        code = mailbox.wait_for_code(
            mailbox.get_email(),
            timeout=5,
            otp_sent_at=1,
        )

        self.assertEqual(code, "654321")
        self.assertTrue(fake_imap.logged_out)

    @patch("time.sleep", return_value=None)
    def test_wait_for_code_skips_seen_old_and_excluded_codes(self, _sleep):
        mailbox = self._build_mailbox()
        old_message = EmailMessage()
        old_message["Subject"] = "Your temporary ChatGPT verification code"
        old_message["From"] = "OpenAI <noreply@tm.openai.com>"
        old_message["To"] = "ago950523@gmail.com"
        old_message["Date"] = "Fri, 15 May 2026 09:58:00 +0000"
        old_message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 111111"
        )
        excluded_message = EmailMessage()
        excluded_message["Subject"] = "Your temporary ChatGPT verification code"
        excluded_message["From"] = "OpenAI <noreply@tm.openai.com>"
        excluded_message["To"] = "ago950523@gmail.com"
        excluded_message["Date"] = "Fri, 15 May 2026 10:01:00 +0000"
        excluded_message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 222222"
        )
        good_message = EmailMessage()
        good_message["Subject"] = "Your temporary ChatGPT verification code"
        good_message["From"] = "OpenAI <noreply@tm.openai.com>"
        good_message["To"] = "ago950523@gmail.com"
        good_message["Date"] = "Fri, 15 May 2026 10:02:00 +0000"
        good_message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 333333"
        )
        fake_imap = _FakeIMAP(
            search_ids=[b"200 201 202 203"],
            messages={
                "201": old_message.as_bytes(),
                "202": excluded_message.as_bytes(),
                "203": good_message.as_bytes(),
            },
        )
        mailbox._open_mailbox = lambda: fake_imap

        code = mailbox.wait_for_code(
            mailbox.get_email(),
            timeout=5,
            before_ids={"200"},
            exclude_codes={"222222"},
            otp_sent_at=1747303200,
        )

        self.assertEqual(code, "333333")

    @patch("time.sleep", return_value=None)
    def test_wait_for_code_reuses_imap_connection_across_polls(self, _sleep):
        mailbox = self._build_mailbox()
        message = EmailMessage()
        message["Subject"] = "Your temporary ChatGPT verification code"
        message["From"] = "OpenAI <noreply@tm.openai.com>"
        message["To"] = "ago950523@gmail.com"
        message["Date"] = "Fri, 15 May 2026 10:02:00 +0000"
        message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 333333"
        )
        fake_imap = _FakeIMAP(
            search_ids=[b"", b"300"],
            messages={"300": message.as_bytes()},
        )
        open_calls = []

        def _open_mailbox():
            open_calls.append(True)
            return fake_imap

        mailbox._open_mailbox = _open_mailbox

        code = mailbox.wait_for_code(
            mailbox.get_email(),
            timeout=6,
            otp_sent_at=1,
        )

        self.assertEqual(code, "333333")
        self.assertEqual(len(open_calls), 1)
        self.assertGreaterEqual(len(fake_imap.uid_calls), 2)

    @patch("time.sleep", return_value=None)
    def test_wait_for_code_reconnects_after_transient_timeout(self, _sleep):
        mailbox = self._build_mailbox()
        message = EmailMessage()
        message["Subject"] = "Your temporary ChatGPT verification code"
        message["From"] = "OpenAI <noreply@tm.openai.com>"
        message["To"] = "ago950523@gmail.com"
        message["Date"] = "Fri, 15 May 2026 10:03:00 +0000"
        message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 444444"
        )
        first_imap = _FakeIMAP(search_ids=[TimeoutError("timed out"), TimeoutError("timed out")], raise_on_search=True)
        second_imap = _FakeIMAP(search_ids=[b"400"], messages={"400": message.as_bytes()})
        clients = [first_imap, second_imap]

        def _open_mailbox():
            if not clients:
                raise AssertionError("unexpected mailbox open")
            return clients.pop(0)

        mailbox._open_mailbox = _open_mailbox

        code = mailbox.wait_for_code(
            mailbox.get_email(),
            timeout=6,
            otp_sent_at=1,
        )

        self.assertEqual(code, "444444")
        combined_logs = "\n".join(mailbox._test_logs)
        self.assertIn("transient Gmail IMAP failure, reconnecting", combined_logs)
        self.assertTrue(first_imap.logged_out)
        self.assertTrue(second_imap.logged_out)

    def test_get_current_ids_prefers_scoped_imap_search(self):
        mailbox = self._build_mailbox()
        fake_imap = _FakeIMAP(search_ids=[b"500 501"])
        mailbox._open_mailbox = lambda: fake_imap

        ids = mailbox.get_current_ids(mailbox.get_email())

        self.assertEqual(ids, {"500", "501"})
        command, args = fake_imap.uid_calls[0]
        self.assertEqual(command, "search")
        self.assertEqual(args[0], None)
        self.assertIn("X-GM-RAW", args)
        self.assertTrue(any("wjwago@duck.com" in str(item) for item in args))


    @patch("time.sleep", return_value=None)
    def test_wait_for_code_reads_forwarded_gmail_message_via_gmail_api(self, _sleep):
        mailbox = create_mailbox(
            "duckduckgo",
            extra={
                "duckduckgo_email": "wjwago@duck.com",
                "duckduckgo_gmail_address": "ago950523@gmail.com",
                "duckduckgo_gmail_api_mode": "gmail_api",
                "duckduckgo_gmail_api_credentials": json.dumps({
                    "installed": {
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }),
                "duckduckgo_gmail_api_token": json.dumps({
                    "access_token": "token",
                    "refresh_token": "refresh-token",
                    "expires_at": 9999999999,
                }),
            },
        )
        mailbox_logs = []
        mailbox._log_fn = mailbox_logs.append

        message = EmailMessage()
        message["Subject"] = "Your temporary ChatGPT verification code"
        message["From"] = "OpenAI <noreply@tm.openai.com>"
        message["To"] = "ago950523@gmail.com"
        message["Date"] = "Fri, 15 May 2026 10:05:00 +0000"
        message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 555666"
        )
        raw = message.as_bytes()

        def fake_request(method, url, params=None):
            if url.endswith('/messages'):
                return {"messages": [{"id": "api-101"}]}
            if url.endswith('/messages/api-101'):
                return {
                    "raw": __import__('base64').urlsafe_b64encode(raw).decode().rstrip('='),
                    "internalDate": str(1747303500000),
                }
            raise AssertionError(f'unexpected url: {url}')

        mailbox._gmail_api_request = fake_request

        code = mailbox.wait_for_code(
            mailbox.get_email(),
            timeout=5,
            otp_sent_at=1,
        )

        self.assertEqual(code, "555666")
        self.assertTrue(any("Gmail API" in line for line in mailbox_logs))

    @patch("time.sleep", return_value=None)
    def test_wait_for_code_accepts_same_code_when_it_arrives_in_new_gmail_api_message(self, _sleep):
        mailbox = create_mailbox(
            "duckduckgo",
            extra={
                "duckduckgo_email": "wjwago@duck.com",
                "duckduckgo_gmail_address": "ago950523@gmail.com",
                "duckduckgo_gmail_api_mode": "gmail_api",
                "duckduckgo_gmail_api_credentials": json.dumps({
                    "installed": {
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }),
                "duckduckgo_gmail_api_token": json.dumps({
                    "access_token": "token",
                    "refresh_token": "refresh-token",
                    "expires_at": 9999999999,
                }),
            },
        )

        new_message = EmailMessage()
        new_message["Subject"] = "Your temporary ChatGPT login code"
        new_message["From"] = "ChatGPT <noreply@tm.openai.com>"
        new_message["To"] = "ago950523@gmail.com"
        new_message["Date"] = "Fri, 15 May 2026 10:06:00 +0000"
        new_message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 833723"
        )
        old_message = EmailMessage()
        old_message["Subject"] = "Your OpenAI code is 833723"
        old_message["From"] = "OpenAI <noreply@tm.openai.com>"
        old_message["To"] = "ago950523@gmail.com"
        old_message["Date"] = "Fri, 15 May 2026 10:05:00 +0000"
        old_message.set_content(
            "Forwarded message for wjwago@duck.com\n\nYour verification code is 833723"
        )

        raw_new = new_message.as_bytes()
        raw_old = old_message.as_bytes()

        def fake_request(method, url, params=None):
            if url.endswith('/messages'):
                return {"messages": [{"id": "api-new"}, {"id": "api-old"}]}
            if url.endswith('/messages/api-new'):
                return {
                    "raw": __import__('base64').urlsafe_b64encode(raw_new).decode().rstrip('='),
                    "internalDate": str(1747303560000),
                }
            if url.endswith('/messages/api-old'):
                return {
                    "raw": __import__('base64').urlsafe_b64encode(raw_old).decode().rstrip('='),
                    "internalDate": str(1747303500000),
                }
            raise AssertionError(f'unexpected url: {url}')

        mailbox._gmail_api_request = fake_request

        code = mailbox.wait_for_code(
            mailbox.get_email(),
            timeout=5,
            before_ids={"api-old"},
            exclude_codes={"833723"},
            otp_sent_at=1,
        )

        self.assertEqual(code, "833723")


    def test_get_email_can_rotate_private_address_pool(self):
        mailbox = create_mailbox(
            "duckduckgo",
            extra={
                "duckduckgo_email": "wjwago@duck.com",
                "duckduckgo_gmail_address": "ago950523@gmail.com",
                "duckduckgo_gmail_app_password": "app password",
                "duckduckgo_alias_mode": "pool",
                "duckduckgo_alias_rotation": "round_robin",
                "duckduckgo_private_addresses": "alpha@duck.com\nbeta@duck.com",
            },
        )

        first = mailbox.get_email()
        second = mailbox.get_email()

        self.assertEqual(first.email, "alpha@duck.com")
        self.assertEqual(second.email, "beta@duck.com")
        self.assertEqual(first.extra["alias_mode"], "pool")
        self.assertEqual(second.extra["alias_rotation"], "round_robin")

    def test_get_email_pool_falls_back_to_primary_duck_address(self):
        mailbox = create_mailbox(
            "duckduckgo",
            extra={
                "duckduckgo_email": "wjwago@duck.com",
                "duckduckgo_gmail_address": "ago950523@gmail.com",
                "duckduckgo_gmail_app_password": "app password",
                "duckduckgo_alias_mode": "pool",
                "duckduckgo_private_addresses": "",
            },
        )
        mailbox_logs = []
        mailbox._log_fn = mailbox_logs.append

        account = mailbox.get_email()

        self.assertEqual(account.email, "wjwago@duck.com")
        self.assertTrue(any('fallback to primary Duck address' in line for line in mailbox_logs))

    @patch("requests.post")
    def test_get_email_can_auto_generate_real_private_duck_address(self, mock_post):
        mailbox = create_mailbox(
            "duckduckgo",
            extra={
                "duckduckgo_email": "wjwago@duck.com",
                "duckduckgo_gmail_address": "ago950523@gmail.com",
                "duckduckgo_gmail_app_password": "app password",
                "duckduckgo_alias_mode": "auto_generate",
                "duckduckgo_api_token": "duck-token",
            },
        )
        mailbox_logs = []
        mailbox._log_fn = mailbox_logs.append

        response = Mock()
        response.status_code = 200
        response.json.return_value = {"address": "alpha-beta"}
        mock_post.return_value = response

        account = mailbox.get_email()

        self.assertEqual(account.email, "alpha-beta@duck.com")
        self.assertEqual(account.extra["alias_mode"], "auto_generate")
        self.assertTrue(any("generated private address" in line for line in mailbox_logs))
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer duck-token")

    @patch("requests.post")
    def test_get_email_auto_generate_falls_back_to_pool(self, mock_post):
        mailbox = create_mailbox(
            "duckduckgo",
            extra={
                "duckduckgo_email": "wjwago@duck.com",
                "duckduckgo_gmail_address": "ago950523@gmail.com",
                "duckduckgo_gmail_app_password": "app password",
                "duckduckgo_alias_mode": "auto_generate",
                "duckduckgo_api_token": "duck-token",
                "duckduckgo_private_addresses": "alpha@duck.com\nbeta@duck.com",
                "duckduckgo_alias_rotation": "round_robin",
            },
        )
        mailbox_logs = []
        mailbox._log_fn = mailbox_logs.append
        mock_post.side_effect = RuntimeError("network boom")

        account = mailbox.get_email()

        self.assertEqual(account.email, "alpha@duck.com")
        self.assertTrue(any("fallback from auto_generate to configured private-address pool" in line for line in mailbox_logs))


class _FakeIMAP:
    def __init__(self, search_ids=None, messages=None, raise_on_search=False):
        self.search_ids = list(search_ids or [b""])
        self.messages = messages or {}
        self.raise_on_search = raise_on_search
        self.uid_calls = []
        self.logged_out = False
        self.select_calls = 0

    def select(self, mailbox_name):
        self.select_calls += 1
        return "OK", [b""]

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command == "search":
            value = self.search_ids.pop(0) if self.search_ids else b""
            if self.raise_on_search and isinstance(value, Exception):
                raise value
            return "OK", [value]
        if command == "fetch":
            uid = str(args[0])
            raw = self.messages.get(uid, b"")
            if not raw:
                return "NO", []
            return "OK", [(b"RFC822", raw)]
        return "NO", []

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


if __name__ == "__main__":
    unittest.main()
