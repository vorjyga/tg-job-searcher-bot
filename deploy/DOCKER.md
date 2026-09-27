# Запуск бота в Docker рядом с существующим PostgreSQL

Этот вариант запускает **только бот** в контейнере. Существующий контейнер
PostgreSQL остаётся под своим текущим управлением. Боту не нужен входящий порт:
Telethon получает новые сообщения через исходящее соединение с Telegram, а
aiogram получает команды через polling.

## Подготовка сервера

Проверьте, что PostgreSQL использует постоянный volume, и узнайте имя его
контейнера и сети:

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}'
docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' POSTGRES_CONTAINER
```

`POSTGRES_CONTAINER` замените настоящим именем контейнера. Сеть должна быть
обычной Docker bridge-сетью, в которой контейнеры могут общаться по именам.
Новый `compose.yaml` подключит бот к **существующей** сети, не создавая и не
перезапуская PostgreSQL. Если Postgres работает на Compose, используйте имя
его сервисa как имя хоста в `DATABASE_URL`. Для другого контейнера используйте
его сетевой alias или имя контейнера. Внутри сети нужен порт PostgreSQL `5432`,
даже если на хосте он опубликован под другим номером.

В PostgreSQL создайте отдельные роль и базу `tg_jobs`, если их ещё нет:

```bash
docker exec -it POSTGRES_CONTAINER psql -U postgres -d postgres
```

```sql
CREATE ROLE tg_jobs LOGIN PASSWORD 'your-strong-password';
CREATE DATABASE tg_jobs OWNER tg_jobs;
\q
```

Скопируйте **текущие** исходники проекта на сервер. Не переносите `.env`,
`.venv`, `.git`, `.idea` и локальный каталог `secrets` вместе с исходниками.
Пример с Mac, где `SERVER` — ваш SSH-адрес:

```bash
rsync -az --exclude=.env --exclude=.venv --exclude=.git \
  --exclude=.idea --exclude=secrets --exclude=.pytest_cache \
  /Users/pas/projects/tg-jobs-searcher/ SERVER:~/tg-jobs-searcher/
```

На сервере перейдите в `~/tg-jobs-searcher`, создайте каталог для Telegram-сессии
и закрытый файл настроек:

```bash
cd ~/tg-jobs-searcher
sudo install -d -o 10001 -g 10001 -m 700 /var/lib/tg-jobs-searcher
cp .env.example .env
chmod 600 .env
```

В `.env` укажите `TG_JOBS_DOCKER_NETWORK` с именем найденной сети и остальные
настройки. Для подключения к контейнеру PostgreSQL используйте адрес вида:

```text
DATABASE_URL=postgresql+asyncpg://tg_jobs:<пароль>@POSTGRES_CONTAINER:5432/tg_jobs
TELEGRAM_SESSION_PATH=/var/lib/tg-jobs-searcher/telethon.session
TG_JOBS_DOCKER_NETWORK=existing_postgres_network
```

Замените плейсхолдеры. Если пароль содержит специальные символы URL, закодируйте
их. `.env` не добавляйте в Git и не передавайте вместе с логами. Настоящий
`BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`
заполните в этом же файле.

## Авторизация и первый запуск

Соберите образ и авторизуйте Telegram-аккаунт интерактивно **внутри контейнера**:

```bash
docker compose build bot
docker compose run --rm bot tg-jobs-searcher auth
```

Файл сессии появится в `/var/lib/tg-jobs-searcher` на сервере с правами `0600`.
Перед запуском сервера остановите локальный экземпляр бота на Mac. Если ранее
использовалась Supabase и нужно сохранить группы, ключевые слова и историю
уведомлений, перенесите данные в новую базу через `pg_dump`/`pg_restore` **до
первого запуска контейнера**. Иначе новая база будет пустой: миграции создадут
схему, а группы нужно будет добавить заново.

```bash
docker compose up -d bot
docker compose ps
docker compose logs --tail=100 bot
```

При запуске сначала применяются миграции Alembic, затем стартует бот. Проверьте
из своего Telegram `/start`, `/groups` и `/status`. Контейнер работает от
непривилегированного пользователя, файловая система образа доступна только для
чтения; запись разрешена для сессии и временного каталога. Входящих портов нет.

По умолчанию любой пользователь может отправить `/start` и начать работу без
приглашения. Администратор может переключить режим командой `/access_mode`.
Администратор, заданный через `OWNER_TELEGRAM_ID`, получит уведомление о первом
запуске и ежедневный отчёт после 09:00 по времени Тбилиси. Настройки каждого
пользователя отделены от остальных.
Группы всё равно читает общий Telethon-аккаунт, поэтому он должен состоять в
каждой группе.

## Обновление и диагностика

После копирования новой версии исходников:

```bash
docker compose up -d --build bot
docker compose logs --tail=100 bot
```

Бот перезапустится автоматически после сбоя или перезагрузки Docker. Для ручной
остановки используйте `docker compose stop bot`. Логи контейнера ограничены
30 МБ (`3 × 10 МБ`), но образы Docker и данные PostgreSQL всё равно занимают
место на сервере. Следите за свободным местом и храните резервные копии базы
вне этой виртуалки.
