import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot import (
    ConfigurationError,
    ConsiliumClient,
    RemoteAPIError,
    Settings,
    TelegramClient,
    healthcheck,
    handle_update,
    mark_healthy,
)


def settings() -> Settings:
    return Settings(
        telegram_bot_token="123:test",
        consilium_api_url="http://consilium.test",
        bot_integration_secret="shared-secret",
    )


class FakeTelegram:
    def __init__(self):
        self.links = []
        self.errors = []
        self.replaced_links = []
        self.callback_answers = []
        self.refresh_progress = []
        self.refresh_errors = []
        self.manager_bindings = []

    async def send_auth_link(self, chat_id, auth_url):
        self.links.append((chat_id, auth_url))

    async def send_error(self, chat_id, text):
        self.errors.append((chat_id, text))

    async def replace_auth_link(self, chat_id, message_id, auth_url, text=None):
        self.replaced_links.append((chat_id, message_id, auth_url, text))

    async def answer_callback(self, callback_query_id, text=""):
        self.callback_answers.append((callback_query_id, text))

    async def show_refresh_progress(self, chat_id, message_id):
        self.refresh_progress.append((chat_id, message_id))

    async def show_refresh_error(self, chat_id, message_id):
        self.refresh_errors.append((chat_id, message_id))

    async def send_manager_bound(self, chat_id, name, manager_url):
        self.manager_bindings.append((chat_id, name, manager_url))


class FakeConsilium:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def create_auth_link(self, telegram_user_id, intent_token=""):
        self.calls.append((telegram_user_id, intent_token))
        if self.error:
            raise self.error
        return "https://consilium.test/auth/messenger?t=one-time"

    async def bind_manager(self, telegram_user_id, chat_id, token):
        self.calls.append(("manager", telegram_user_id, chat_id, token))
        return {"display_name": "Ольга", "manager_url": "https://consilium.test/manager"}


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_notification_has_open_results_button(self):
        telegram = TelegramClient(settings(), object())
        telegram.call = AsyncMock()

        await telegram.send_manager_notification(42, {
            "title": "Результаты анализов готовы",
            "body": "Документы появились.",
            "action_url": "https://consilium.test/result",
            "action_label": "Открыть результаты",
        })

        telegram.call.assert_awaited_once()
        method, message = telegram.call.await_args.args
        self.assertEqual(method, "sendMessage")
        button = message["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Открыть результаты")
        self.assertEqual(button["url"], "https://consilium.test/result")

    async def test_start_creates_link_for_verified_telegram_sender(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium()
        update = {
            "message": {
                "text": "/start",
                "chat": {"id": 10},
                "from": {"id": 20},
            }
        }

        await handle_update(update, telegram, consilium)

        self.assertEqual(consilium.calls, [(20, "")])
        self.assertEqual(
            telegram.links,
            [(10, "https://consilium.test/auth/messenger?t=one-time")],
        )

    async def test_deep_link_token_is_forwarded(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium()
        update = {
            "message": {
                "text": "/start AbC_123-xYz",
                "chat": {"id": 10},
                "from": {"id": 20},
            }
        }

        await handle_update(update, telegram, consilium)

        self.assertEqual(consilium.calls, [(20, "AbC_123-xYz")])

    async def test_manager_deep_link_uses_separate_binding_scenario(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium()
        await handle_update({"message": {
            "text": "/start mgr_secret", "chat": {"id": 10}, "from": {"id": 20},
        }}, telegram, consilium)
        self.assertEqual(consilium.calls, [("manager", 20, 10, "mgr_secret")])
        self.assertEqual(telegram.links, [])
        self.assertEqual(
            telegram.manager_bindings,
            [(10, "Ольга", "https://consilium.test/manager")],
        )

    async def test_non_start_message_is_ignored(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium()

        await handle_update(
            {
                "message": {
                    "text": "Привет",
                    "chat": {"id": 10},
                    "from": {"id": 20},
                }
            },
            telegram,
            consilium,
        )

        self.assertEqual(consilium.calls, [])
        self.assertEqual(telegram.links, [])

    async def test_broken_link_button_replaces_link_in_same_message(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium()
        update = {
            "callback_query": {
                "id": "callback-1",
                "data": "refresh_auth_link",
                "from": {"id": 20},
                "message": {
                    "message_id": 30,
                    "chat": {"id": 10},
                },
            }
        }

        with patch("bot.asyncio.sleep") as sleep:
            await handle_update(update, telegram, consilium)

        self.assertEqual(consilium.calls, [(20, "")])
        self.assertEqual(telegram.refresh_progress, [(10, 30)])
        self.assertEqual(
            telegram.replaced_links,
            [
                (
                    10,
                    30,
                    "https://consilium.test/auth/messenger?t=one-time",
                    "Ссылка изменена, попробуйте открыть Консилиум",
                )
            ],
        )
        self.assertEqual(telegram.callback_answers[0][0], "callback-1")
        sleep.assert_awaited_once()

    async def test_refresh_failure_replaces_progress_with_retry(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium(RemoteAPIError("backend unavailable"))

        await handle_update(
            {
                "callback_query": {
                    "id": "callback-error",
                    "data": "refresh_auth_link",
                    "from": {"id": 20},
                    "message": {"message_id": 30, "chat": {"id": 10}},
                }
            },
            telegram,
            consilium,
        )

        self.assertEqual(telegram.refresh_progress, [(10, 30)])
        self.assertEqual(telegram.refresh_errors, [(10, 30)])
        self.assertEqual(telegram.replaced_links, [])

    async def test_auth_message_contains_refresh_button(self):
        telegram = TelegramClient(settings(), object())
        with patch.object(telegram, "call") as call:
            await telegram.send_auth_link(10, "https://consilium.test/auth?t=new")

        payload = call.await_args.args[1]
        buttons = payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["url"], "https://consilium.test/auth?t=new")
        self.assertEqual(buttons[1][0]["text"], "Не работает ссылка")
        self.assertEqual(buttons[1][0]["callback_data"], "refresh_auth_link")

    async def test_unrelated_callback_is_ignored(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium()

        await handle_update(
            {
                "callback_query": {
                    "id": "callback-2",
                    "data": "another_action",
                    "from": {"id": 20},
                }
            },
            telegram,
            consilium,
        )

        self.assertEqual(consilium.calls, [])
        self.assertEqual(telegram.callback_answers, [])

    async def test_two_users_are_processed_concurrently(self):
        class ConcurrentConsilium:
            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def create_auth_link(self, telegram_user_id, intent_token=""):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                return f"https://consilium.test/auth?t={telegram_user_id}"

        telegram = FakeTelegram()
        consilium = ConcurrentConsilium()

        def callback_update(number):
            return {
                "callback_query": {
                    "id": f"callback-{number}",
                    "data": "refresh_auth_link",
                    "from": {"id": number},
                    "message": {
                        "message_id": number,
                        "chat": {"id": number},
                    },
                }
            }

        with patch("bot.REFRESH_DELAY_SECONDS", 0):
            await asyncio.gather(
                handle_update(callback_update(20), telegram, consilium),
                handle_update(callback_update(21), telegram, consilium),
            )

        self.assertEqual(consilium.max_active, 2)

    async def test_api_failure_gets_safe_user_message(self):
        telegram = FakeTelegram()
        consilium = FakeConsilium(RemoteAPIError("secret backend detail"))

        await handle_update(
            {
                "message": {
                    "text": "/start",
                    "chat": {"id": 10},
                    "from": {"id": 20},
                }
            },
            telegram,
            consilium,
        )

        self.assertEqual(len(telegram.errors), 1)
        self.assertNotIn("secret backend detail", telegram.errors[0][1])

    @patch("bot.post_json")
    async def test_consilium_request_matches_project_contract(self, post_json_mock):
        post_json_mock.return_value = {
            "auth_url": "https://consilium.test/auth/messenger?t=token"
        }

        session = object()
        url = await ConsiliumClient(settings(), session).create_auth_link(
            42, "intent-token"
        )

        self.assertEqual(
            url, "https://consilium.test/auth/messenger?t=token"
        )
        args, kwargs = post_json_mock.await_args
        self.assertEqual(
            args,
            (
                session,
                "http://consilium.test/api/auth/messenger/link",
                {
                    "provider": "telegram",
                    "provider_user_id": "42",
                    "intent_token": "intent-token",
                },
            ),
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer shared-secret"
        )

    async def test_required_environment_is_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    async def test_healthcheck_uses_recent_polling_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat"
            current = settings()
            current = Settings(
                telegram_bot_token=current.telegram_bot_token,
                consilium_api_url=current.consilium_api_url,
                bot_integration_secret=current.bot_integration_secret,
                healthcheck_file=heartbeat,
                healthcheck_max_age=120,
            )

            self.assertFalse(healthcheck(current))
            mark_healthy(heartbeat)
            self.assertTrue(healthcheck(current))
            old_time = time.time() - 121
            os.utime(heartbeat, (old_time, old_time))
            self.assertFalse(healthcheck(current))

    def test_docker_deployment_is_isolated_and_polling_disables_webhook(self):
        project = Path(__file__).resolve().parents[1]
        source = (project / "bot.py").read_text(encoding="utf-8")
        compose = (project / "docker-compose.yml").read_text(encoding="utf-8")
        production_env = (project / ".env.production.example").read_text(encoding="utf-8")
        self.assertIn('telegram.call("deleteWebhook", {"drop_pending_updates": False})', source)
        self.assertIn("container_name: consilium-telegram-bot", compose)
        self.assertIn("external: true", compose)
        self.assertNotIn("ports:", compose)
        self.assertIn("CONSILIUM_API_URL=http://consilium:8000", production_env)


if __name__ == "__main__":
    unittest.main()
