# Telegram-бот для входа в «Консилиум»

Минимальный бот в режиме long polling. Он выполняет одну задачу: по команде
`/start` запрашивает у API «Консилиума» одноразовую ссылку и отправляет её
пользователю кнопкой. Если ссылка не открывается, кнопка **Не работает ссылка**
сразу меняет сообщение на «Создаю новую ссылку…», запрашивает новую ссылку и
через две секунды показывает «Ссылка изменена, попробуйте открыть Консилиум» с
обновлённой кнопкой входа.

Поддерживаются оба сценария проекта:

- обычный `/start` — вход существующего Telegram-пользователя или создание
  нового профиля;
- `/start <token>` — привязка Telegram к профилю, с которого пользователь
  перешёл в бот через кнопку на сайте «Консилиума».

Telegram ID берётся только из подписанного Telegram-обновления (`from.id`).
Секрет интеграции пользователю не передаётся.

## Production-схема в Docker

Бот работает через long polling, поэтому ему не нужны открытый порт, Nginx,
домен или TLS-сертификат. Контейнер делает только исходящие подключения:

- к `api.telegram.org` по HTTPS;
- к контейнеру `consilium` через общую внутреннюю сеть Docker.

Готовый `docker-compose.yml` подключает бота к существующей внешней сети
`consilium-internal`. Именно такое имя сети уже задано в Compose-файле
«Консилиума». Внутренний адрес API:

```text
http://consilium:8000
```

Контейнер запускается непривилегированным пользователем, имеет read-only
файловую систему, сброшенные Linux capabilities, ограниченные логи,
автоперезапуск и healthcheck активности long polling. При старте бот
автоматически отключает старый webhook без удаления ожидающих сообщений.

## Публикация на сервере

Ниже предполагается, что проект «Консилиум» уже находится на сервере и
запускается через Docker Compose.

### 1. Остановить тестовую копию

Одновременно может работать только один long polling-процесс с одним токеном
Telegram. Перед серверным запуском закройте локальный `start.bat`. Иначе в
логах появится ошибка Telegram `409 Conflict`.

### 2. Проверить сеть «Консилиума»

На сервере выполните:

```bash
docker network inspect consilium-internal >/dev/null
```

Если сеть ещё не создана, сначала запустите «Консилиум»:

```bash
cd /root/anamnez_v2
docker-compose up -d consilium
```

Не создавайте отдельную сеть с другим именем: контейнеры должны видеть друг
друга по DNS-имени `consilium`.

### 3. Настроить «Консилиум»

В `/root/anamnez_v2/.env` должны быть заполнены:

```dotenv
PUBLIC_BASE_URL=https://consilium.chelovecbitmax.ru
BOT_INTEGRATION_SECRET=ДЛИННЫЙ_СЛУЧАЙНЫЙ_СЕКРЕТ
TELEGRAM_BOT_AUTH_URL=https://t.me/ИМЯ_БОТА?start={token}
```

Имя бота указывается без символа `@`. Сгенерировать секрет можно командой:

```bash
openssl rand -hex 32
```

После изменения перезапустите контейнер:

```bash
cd /root/anamnez_v2
docker-compose up -d --build consilium
docker-compose ps
```

### 4. Загрузить проект бота

Разместите эту папку, например, в `/root/tg_to_consillium`, затем:

```bash
cd /root/tg_to_consillium
cp .env.production.example .env
nano .env
```

Заполните `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=ТОКЕН_ОТ_BOTFATHER
CONSILIUM_API_URL=http://consilium:8000
BOT_INTEGRATION_SECRET=ТОТ_ЖЕ_СЕКРЕТ_ЧТО_У_КОНСИЛИУМА

POLLING_TIMEOUT=30
REQUEST_TIMEOUT=15
LOG_LEVEL=INFO
HEALTHCHECK_FILE=/tmp/consilium-tg-bot.heartbeat
HEALTHCHECK_MAX_AGE=120
```

Значение `BOT_INTEGRATION_SECRET` должно посимвольно совпадать в обоих
проектах. Ограничьте доступ к файлу:

```bash
chmod 600 .env
```

### 5. Собрать и запустить

На сервере, где используется Docker Compose v1:

```bash
cd /root/tg_to_consillium
docker-compose build --pull
docker-compose up -d
docker-compose ps
docker-compose logs --tail=100 consilium-telegram-bot
```

Нормальный лог запуска содержит:

```text
Бот запущен в режиме long polling
```

Статус контейнера после стартового периода должен стать `healthy`. Проверить
его отдельно:

```bash
docker inspect --format='{{.State.Health.Status}}' consilium-telegram-bot
```

После этого откройте бота в Telegram и нажмите **Start**. В ответ должна
появиться кнопка входа с публичным HTTPS-адресом «Консилиума».

### 6. Обновление

После загрузки новой версии файлов:

```bash
cd /root/tg_to_consillium
docker-compose up -d --build
docker-compose logs --tail=100 consilium-telegram-bot
```

### Диагностика

Показать последние логи:

```bash
docker-compose logs --tail=200 consilium-telegram-bot
```

Проверить доступность «Консилиума» из сети бота:

```bash
docker run --rm --network consilium-internal \
  python:3.12-slim python -c \
  "import urllib.request; print(urllib.request.urlopen('http://consilium:8000/api/health').read().decode())"
```

Частые причины ошибок:

- `409 Conflict` — локально или на другом сервере уже запущена вторая копия
  бота с тем же Telegram-токеном;
- `Интеграция с мессенджерами не настроена` — в контейнере «Консилиума» нет
  `BOT_INTEGRATION_SECRET` или контейнер не перезапущен;
- `Неверные данные интеграции` — секреты в двух `.env` различаются;
- `Name or service not known: consilium` — контейнеры подключены к разным
  Docker-сетям;
- кнопка содержит локальный адрес — в «Консилиуме» неверно задан
  `PUBLIC_BASE_URL`.

У бота нет базы и постоянного volume: профиль и авторизационные токены хранит
сам «Консилиум». Удаление или пересборка контейнера бота данные пользователей
не удаляет.

## Отдельный сценарий менеджера

Обычный `/start` по-прежнему создаёт пользовательскую ссылку входа в
Консилиум. Служебная ссылка из админ-панели содержит параметр `mgr_...`: бот
распознаёт его, привязывает подтверждённые Telegram `user_id` и `chat_id` к
учётной записи менеджера и не создаёт для него новый пользовательский профиль.

После привязки бот получает из внутреннего API уведомления о новых обращениях и
о сообщениях в диалогах с выключенным ИИ. Каждое уведомление подтверждается
серверу; при временной ошибке доставка повторяется позже.

## Требования для локального запуска

- Python 3.11 или новее;
- запущенный и доступный API «Консилиума»;
- Telegram-бот, созданный через `@BotFather`.

HTTP-запросы и обработка пользователей выполняются асинхронно через `aiohttp`.
Каждое обновление запускается отдельной ограниченной задачей, поэтому ожидание
ответа API или двухсекундная анимация одного пользователя не блокирует других.

## Локальный запуск без Docker

1. Скопируйте `.env.example` в `.env`.
2. Заполните:

   ```dotenv
   TELEGRAM_BOT_TOKEN=токен_от_BotFather
   CONSILIUM_API_URL=http://127.0.0.1:8000
   BOT_INTEGRATION_SECRET=тот_же_секрет_что_в_Консилиуме
   ```

3. Запустите:

   ```powershell
   python -m pip install -r requirements.txt
   python bot.py
   ```

   На текущем Windows-компьютере также можно запустить `start.bat`: он
   автоматически найдёт Python из соседнего проекта «Консилиум».

4. Откройте бота в Telegram и нажмите **Start**.

Для проверки без Telegram:

```powershell
python -m unittest discover -s tests -v
```

## Настройка самого «Консилиума» для локального запуска

Значение `BOT_INTEGRATION_SECRET` в обоих `.env` должно совпадать. После того
как имя Telegram-бота известно, в `.env` проекта «Консилиум» нужно указать:

```dotenv
TELEGRAM_BOT_AUTH_URL=https://t.me/ИМЯ_БОТА?start={token}
```

и перезапустить «Консилиум». Тогда кнопка Telegram на его экране входа будет
открывать бот с одноразовым токеном привязки.

Для серверного запуска задайте:

```dotenv
CONSILIUM_API_URL=https://consilium.chelovecbitmax.ru
```

Не публикуйте `.env`, токен Telegram и `BOT_INTEGRATION_SECRET`.
