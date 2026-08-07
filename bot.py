from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import aiohttp


LOG = logging.getLogger("consilium_tg_bot")
START_RE = re.compile(r"^/start(?:@\w+)?(?:\s+([A-Za-z0-9_-]{1,128}))?\s*$")
REFRESH_LINK_CALLBACK = "refresh_auth_link"
AUTH_LINK_TEXT = "Ссылка готова. Нажмите кнопку, чтобы войти в «Консилиум»."
REFRESH_PROGRESS_TEXT = "Создаю новую ссылку…"
REFRESH_SUCCESS_TEXT = "Ссылка изменена, попробуйте открыть Консилиум"
REFRESH_ERROR_TEXT = "Не удалось создать новую ссылку. Попробуйте ещё раз."
REFRESH_DELAY_SECONDS = 2.0


class ConfigurationError(RuntimeError):
    pass


class RemoteAPIError(RuntimeError):
    pass


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load a small, dependency-free subset of the dotenv format."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    consilium_api_url: str
    bot_integration_secret: str
    polling_timeout: int = 30
    request_timeout: int = 15
    healthcheck_file: Path = Path("/tmp/consilium-tg-bot.heartbeat")
    healthcheck_max_age: int = 120

    @classmethod
    def from_env(cls) -> "Settings":
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        consilium_api_url = os.getenv("CONSILIUM_API_URL", "").strip().rstrip("/")
        bot_integration_secret = os.getenv("BOT_INTEGRATION_SECRET", "").strip()
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", telegram_bot_token),
                ("CONSILIUM_API_URL", consilium_api_url),
                ("BOT_INTEGRATION_SECRET", bot_integration_secret),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                f"Не заполнены обязательные переменные: {', '.join(missing)}"
            )
        if not consilium_api_url.startswith(("http://", "https://")):
            raise ConfigurationError(
                "CONSILIUM_API_URL должен начинаться с http:// или https://"
            )
        polling_timeout = int(os.getenv("POLLING_TIMEOUT", "30"))
        request_timeout = int(os.getenv("REQUEST_TIMEOUT", "15"))
        healthcheck_max_age = int(os.getenv("HEALTHCHECK_MAX_AGE", "120"))
        if not 1 <= polling_timeout <= 50:
            raise ConfigurationError("POLLING_TIMEOUT должен быть от 1 до 50 секунд")
        if not 1 <= request_timeout <= 60:
            raise ConfigurationError("REQUEST_TIMEOUT должен быть от 1 до 60 секунд")
        if healthcheck_max_age < polling_timeout + 30:
            raise ConfigurationError(
                "HEALTHCHECK_MAX_AGE должен быть минимум на 30 секунд больше POLLING_TIMEOUT"
            )
        return cls(
            telegram_bot_token=telegram_bot_token,
            consilium_api_url=consilium_api_url,
            bot_integration_secret=bot_integration_secret,
            polling_timeout=polling_timeout,
            request_timeout=request_timeout,
            healthcheck_file=Path(
                os.getenv("HEALTHCHECK_FILE", "/tmp/consilium-tg-bot.heartbeat")
            ),
            healthcheck_max_age=healthcheck_max_age,
        )


def mark_healthy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def healthcheck(settings: Settings) -> bool:
    try:
        age = time.time() - settings.healthcheck_file.stat().st_mtime
    except OSError:
        return False
    # Filesystems may round mtimes slightly into the future; tolerate small skew.
    return -5 <= age <= settings.healthcheck_max_age


async def post_json(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    try:
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            raw_body = await response.text()
            try:
                result = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise RemoteAPIError("сервис вернул некорректный ответ") from exc
            if response.status >= 400:
                detail = ""
                if isinstance(result, dict):
                    detail = str(
                        result.get("detail") or result.get("description") or ""
                    )
                raise RemoteAPIError(detail or f"HTTP {response.status}")
    except RemoteAPIError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RemoteAPIError("сервис временно недоступен") from exc
    except UnicodeDecodeError as exc:
        raise RemoteAPIError("сервис вернул некорректный ответ") from exc
    if not isinstance(result, dict):
        raise RemoteAPIError("сервис вернул некорректный ответ")
    return result


class ConsiliumClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession):
        self.settings = settings
        self.session = session

    async def create_auth_link(self, telegram_user_id: int, intent_token: str = "") -> str:
        result = await post_json(
            self.session,
            f"{self.settings.consilium_api_url}/api/auth/messenger/link",
            {
                "provider": "telegram",
                "provider_user_id": str(telegram_user_id),
                "intent_token": intent_token,
            },
            headers={
                "Authorization": f"Bearer {self.settings.bot_integration_secret}"
            },
            timeout=self.settings.request_timeout,
        )
        auth_url = str(result.get("auth_url", "")).strip()
        if not auth_url.startswith(("http://", "https://")):
            raise RemoteAPIError("сервис не вернул ссылку для входа")
        return auth_url

    async def bind_manager(self, telegram_user_id: int, chat_id: int, token: str) -> dict[str, Any]:
        return await post_json(
            self.session,
            f"{self.settings.consilium_api_url}/api/bot/manager-bind",
            {
                "provider": "telegram", "provider_user_id": str(telegram_user_id),
                "chat_id": str(chat_id), "token": token,
            },
            headers={"Authorization": f"Bearer {self.settings.bot_integration_secret}"},
            timeout=self.settings.request_timeout,
        )

    async def pull_manager_notifications(self) -> list[dict[str, Any]]:
        try:
            async with self.session.get(
                f"{self.settings.consilium_api_url}/api/bot/manager-notifications?provider=telegram&limit=20",
                headers={"Authorization": f"Bearer {self.settings.bot_integration_secret}"},
                timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout),
            ) as response:
                result = json.loads(await response.text())
                if response.status >= 400:
                    raise RemoteAPIError(str(result.get("detail") or f"HTTP {response.status}"))
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise RemoteAPIError("сервис уведомлений временно недоступен") from exc
        return result.get("notifications", []) if isinstance(result, dict) else []

    async def acknowledge_notification(
        self, notification_id: int, lease_token: str, success: bool, error: str = "",
    ) -> None:
        await post_json(
            self.session,
            f"{self.settings.consilium_api_url}/api/bot/manager-notifications/{notification_id}/ack",
            {"lease_token": lease_token, "success": success, "error": error},
            headers={"Authorization": f"Bearer {self.settings.bot_integration_secret}"},
            timeout=self.settings.request_timeout,
        )


class TelegramClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession):
        self.settings = settings
        self.session = session
        self.api_url = (
            f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        )

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        result = await post_json(
            self.session,
            f"{self.api_url}/{method}",
            payload,
            timeout=self.settings.polling_timeout + 10,
        )
        if not result.get("ok"):
            raise RemoteAPIError(str(result.get("description") or "ошибка Telegram"))
        return result.get("result")

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self.settings.polling_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call("getUpdates", payload)
        return result if isinstance(result, list) else []

    @staticmethod
    def auth_keyboard(auth_url: str) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Войти в Консилиум", "url": auth_url}],
                [
                    {
                        "text": "Не работает ссылка",
                        "callback_data": REFRESH_LINK_CALLBACK,
                    }
                ],
            ]
        }

    async def send_auth_link(self, chat_id: int, auth_url: str) -> None:
        await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": AUTH_LINK_TEXT,
                "reply_markup": self.auth_keyboard(auth_url),
            },
        )

    async def replace_auth_link(
        self,
        chat_id: int,
        message_id: int,
        auth_url: str,
        text: str = AUTH_LINK_TEXT,
    ) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": self.auth_keyboard(auth_url),
            },
        )

    async def show_refresh_progress(self, chat_id: int, message_id: int) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": REFRESH_PROGRESS_TEXT,
                "reply_markup": {"inline_keyboard": []},
            },
        )

    async def show_refresh_error(self, chat_id: int, message_id: int) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": REFRESH_ERROR_TEXT,
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Не работает ссылка",
                                "callback_data": REFRESH_LINK_CALLBACK,
                            }
                        ]
                    ]
                },
            },
        )

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        await self.call("answerCallbackQuery", payload)

    async def send_error(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def send_manager_bound(self, chat_id: int, name: str, manager_url: str) -> None:
        await self.call("sendMessage", {
            "chat_id": chat_id,
            "text": f"Готово! Telegram привязан к учётной записи менеджера «{name}». Теперь сюда будут приходить рабочие уведомления.",
            "reply_markup": {"inline_keyboard": [[{"text": "Открыть панель менеджера", "url": manager_url}]]},
        })

    async def send_manager_notification(self, chat_id: int, payload: dict[str, Any]) -> None:
        title = str(payload.get("title") or "Уведомление Консилиума")
        body = str(payload.get("body") or "Откройте панель менеджера.")
        manager_url = str(payload.get("manager_url") or "")
        message: dict[str, Any] = {"chat_id": chat_id, "text": f"{title}\n\n{body}"}
        if manager_url.startswith(("http://", "https://")):
            message["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть диалог", "url": manager_url}]]}
        await self.call("sendMessage", message)


async def handle_update(
    update: dict[str, Any],
    telegram: TelegramClient,
    consilium: ConsiliumClient,
) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return await handle_refresh_callback(callback, telegram, consilium)

    message = update.get("message")
    if not isinstance(message, dict):
        return
    text = message.get("text")
    if not isinstance(text, str) or not text.startswith("/start"):
        return
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    telegram_user_id = sender.get("id")
    if not isinstance(chat_id, int) or not isinstance(telegram_user_id, int):
        return

    match = START_RE.fullmatch(text)
    if not match:
        await telegram.send_error(
            chat_id,
            "Некорректная команда запуска. Откройте бота заново по ссылке.",
        )
        return

    token = match.group(1) or ""
    try:
        if token.startswith("mgr_"):
            binding = await consilium.bind_manager(telegram_user_id, chat_id, token)
            await telegram.send_manager_bound(
                chat_id, str(binding.get("display_name") or "Менеджер"),
                str(binding.get("manager_url") or ""),
            )
            return
        auth_url = await consilium.create_auth_link(
            telegram_user_id=telegram_user_id,
            intent_token=token,
        )
        await telegram.send_auth_link(chat_id, auth_url)
    except RemoteAPIError as exc:
        LOG.warning(
            "Не удалось создать ссылку для Telegram user_id=%s: %s",
            telegram_user_id,
            exc,
        )
        await telegram.send_error(
            chat_id,
            "Не удалось создать ссылку для входа. Попробуйте нажать /start ещё раз.",
        )


async def handle_refresh_callback(
    callback: dict[str, Any],
    telegram: TelegramClient,
    consilium: ConsiliumClient,
) -> None:
    if callback.get("data") != REFRESH_LINK_CALLBACK:
        return
    callback_id = callback.get("id")
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    telegram_user_id = sender.get("id")
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if (
        not isinstance(callback_id, str)
        or not isinstance(telegram_user_id, int)
        or not isinstance(chat_id, int)
        or not isinstance(message_id, int)
    ):
        return

    try:
        await telegram.answer_callback(callback_id, "Создаю новую ссылку…")
        started_at = time.monotonic()
        await telegram.show_refresh_progress(chat_id, message_id)
        auth_url = await consilium.create_auth_link(telegram_user_id=telegram_user_id)
        remaining_delay = REFRESH_DELAY_SECONDS - (time.monotonic() - started_at)
        if remaining_delay > 0:
            await asyncio.sleep(remaining_delay)
        await telegram.replace_auth_link(
            chat_id,
            message_id,
            auth_url,
            text=REFRESH_SUCCESS_TEXT,
        )
    except RemoteAPIError as exc:
        LOG.warning(
            "Не удалось пересоздать ссылку для Telegram user_id=%s: %s",
            telegram_user_id,
            exc,
        )
        try:
            await telegram.show_refresh_error(chat_id, message_id)
        except RemoteAPIError:
            pass


async def run() -> None:
    load_dotenv()
    settings = Settings.from_env()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(
                signum,
                lambda _signum, _frame: loop.call_soon_threadsafe(stop_event.set),
            )

    tasks: set[asyncio.Task[None]] = set()
    semaphore = asyncio.Semaphore(100)

    async def process_update(update: dict[str, Any]) -> None:
        async with semaphore:
            try:
                await handle_update(update, telegram, consilium)
            except Exception:
                LOG.exception("Непредвиденная ошибка при обработке обновления")

    async with aiohttp.ClientSession() as session:
        telegram = TelegramClient(settings, session)
        consilium = ConsiliumClient(settings, session)
        await telegram.call("deleteWebhook", {"drop_pending_updates": False})
        LOG.info("Webhook Telegram отключён; long polling готов к приёму событий")
        offset: int | None = None
        async def notification_loop() -> None:
            while not stop_event.is_set():
                try:
                    for notification in await consilium.pull_manager_notifications():
                        success = False
                        error = ""
                        try:
                            await telegram.send_manager_notification(
                                int(notification["recipient_id"]), notification.get("payload") or {},
                            )
                            success = True
                        except Exception as exc:
                            error = str(exc)
                            LOG.warning("Не удалось отправить уведомление менеджеру: %s", exc)
                        await consilium.acknowledge_notification(
                            int(notification["id"]), str(notification["lease_token"]), success, error,
                        )
                except RemoteAPIError as exc:
                    LOG.warning("Ошибка получения уведомлений менеджеров: %s", exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

        notification_task = asyncio.create_task(notification_loop())
        LOG.info("Бот запущен в асинхронном режиме long polling")
        while not stop_event.is_set():
            try:
                for update in await telegram.get_updates(offset):
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    task = asyncio.create_task(process_update(update))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                mark_healthy(settings.healthcheck_file)
            except RemoteAPIError as exc:
                LOG.warning("Ошибка long polling: %s", exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
            except Exception:
                LOG.exception("Непредвиденная ошибка long polling")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        notification_task.cancel()
        await asyncio.gather(notification_task, return_exceptions=True)
    LOG.info("Бот остановлен")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--healthcheck":
            load_dotenv()
            raise SystemExit(0 if healthcheck(Settings.from_env()) else 1)
        if len(sys.argv) != 1:
            raise ConfigurationError("Неизвестные аргументы командной строки")
        asyncio.run(run())
    except (ConfigurationError, ValueError) as exc:
        LOG.error("%s", exc)
        raise SystemExit(2) from exc
