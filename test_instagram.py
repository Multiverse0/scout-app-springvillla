import datetime
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

import instagram


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"message_id":"sent-1"}'


class InstagramTests(unittest.TestCase):
    def test_verify_signature(self):
        body = b'{"object":"instagram"}'
        digest = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(instagram.verify_signature(body, "sha256=" + digest, "app-secret"))
        self.assertFalse(instagram.verify_signature(body, "sha256=bad", "app-secret"))
        self.assertFalse(instagram.verify_signature(body, "", "app-secret"))

    def test_verify_challenge(self):
        self.assertTrue(instagram.verify_challenge("subscribe", "verify-me", "verify-me"))
        self.assertFalse(instagram.verify_challenge("subscribe", "wrong", "verify-me"))
        self.assertFalse(instagram.verify_challenge("unsubscribe", "verify-me", "verify-me"))

    def test_status_names_missing_environment_variables(self):
        names = ("INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_APP_SECRET", "INSTAGRAM_VERIFY_TOKEN")
        with patch.dict(os.environ, {}, clear=True):
            result = instagram.status()
        self.assertEqual(result["missing"], list(names))
        self.assertFalse(result["configured"])

    def test_parse_events_filters_echo_and_other_accounts(self):
        payload = {"entry": [{"id": "123", "messaging": [
            {"sender": {"id": "456", "username": "maker"}, "recipient": {"id": "123"},
             "timestamp": 1760000000000, "message": {"mid": "m1", "text": "Hi"}},
            {"sender": {"id": "123"}, "recipient": {"id": "456"},
             "message": {"mid": "m2", "text": "echo", "is_echo": True}},
            {"sender": {"id": "789"}, "recipient": {"id": "999"},
             "message": {"mid": "m3", "text": "wrong account"}},
        ]}]}
        messages = instagram.parse_events(payload, "123")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_id"], "456")
        self.assertEqual(messages[0]["username"], "maker")
        self.assertEqual(messages[0]["text"], "Hi")
        self.assertEqual(messages[0]["id"], "m1")

    def test_parse_attachment_and_ignore_empty_events(self):
        payload = {"entry": [{"messaging": [
            {"sender": {"id": "456"}, "recipient": {"id": "123"},
             "timestamp": 1760000000000, "message": {"attachments": [{"type": "image"}]}},
            {"sender": {"id": "457"}, "recipient": {"id": "123"},
             "timestamp": "invalid", "message": {"text": "Missing a valid timestamp"}},
        ]}]}
        messages = instagram.parse_events(payload, "123")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Received Instagram attachment: image.")

    def test_reply_window_expires_after_24_hours(self):
        now = datetime.datetime(2026, 11, 11, 12, tzinfo=datetime.timezone.utc)
        recent = {"last_inbound_at": (now - datetime.timedelta(hours=23)).isoformat()}
        expired = {"last_inbound_at": (now - datetime.timedelta(hours=24)).isoformat()}
        self.assertTrue(instagram.can_reply(recent, now))
        self.assertFalse(instagram.can_reply(expired, now))
        self.assertFalse(instagram.can_reply({}, now))

    def test_send_message_uses_configured_account_and_token(self):
        env = {
            "INSTAGRAM_ACCOUNT_ID": "123",
            "INSTAGRAM_ACCESS_TOKEN": "test-token",
            "INSTAGRAM_APP_SECRET": "app-secret",
            "INSTAGRAM_VERIFY_TOKEN": "verify-token",
            "INSTAGRAM_GRAPH_VERSION": "v22.0",
        }
        with patch.dict(os.environ, env), patch("instagram.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            result = instagram.send_message("456", "Hello")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://graph.facebook.com/v22.0/123/messages")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(request.data), {"recipient": {"id": "456"}, "message": {"text": "Hello"}})
        self.assertEqual(result["message_id"], "sent-1")


if __name__ == "__main__":
    unittest.main()
