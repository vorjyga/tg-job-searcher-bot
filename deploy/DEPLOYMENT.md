# Развёртывание на сервере

Инструкция рассчитана на Linux с systemd и уже запущенным PostgreSQL — на хосте
или в Docker. Сервис использует отдельные Unix-пользователя, PostgreSQL-роль и
базу. Он не меняет настройки других приложений на сервере.

Перед переносом остановите локальный экземпляр `tg-jobs-searcher`. Один и тот же
бот не должен одновременно получать обновления через polling на ноутбуке и
сервере. Сохраните копию текущей базы перед переключением.

## Один раз

Если PostgreSQL установлен на хосте, создайте роль и базу под учётной записью
его администратора:

```bash
sudo -u postgres createuser --pwprompt tg_jobs
sudo -u postgres createdb --owner=tg_jobs tg_jobs
```

Если PostgreSQL уже работает в Docker, сначала узнайте имя контейнера и
опубликованный порт:

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}'
```

Откройте `psql` в нужном контейнере (`POSTGRES_CONTAINER` замените его именем)
и создайте отдельные роль и базу. Пароль задайте свой; не используйте пример из
файла окружения:

```bash
docker exec -it POSTGRES_CONTAINER psql -U postgres -d postgres
```

```sql
CREATE ROLE tg_jobs LOGIN PASSWORD 'your-strong-password';
CREATE DATABASE tg_jobs OWNER tg_jobs;
\q
```

Для приложения, установленного на хосте, контейнер должен публиковать порт
PostgreSQL только на `127.0.0.1`, например `127.0.0.1:5432:5432` в Compose.
Если порт хоста уже занят, используйте другой, например `5433`, и укажите его в
`DATABASE_URL`. Изменение публикации порта обычно требует пересоздания
контейнера; убедитесь, что его данные сохранены в постоянном volume. Не
публикуйте PostgreSQL на всех сетевых интерфейсах ради этого бота.

Создайте системного пользователя и каталоги для программы, состояния и
конфигурации:

```bash
sudo useradd --system --user-group --home /var/lib/tg-jobs-searcher --shell /usr/sbin/nologin tg-jobs-searcher
sudo install -d -o tg-jobs-searcher -g tg-jobs-searcher -m 700 /var/lib/tg-jobs-searcher
sudo install -d -o root -g tg-jobs-searcher -m 750 /etc/tg-jobs-searcher
sudo install -d -o tg-jobs-searcher -g tg-jobs-searcher -m 755 /opt/tg-jobs-searcher
```

Скопируйте **текущий рабочий каталог проекта** в `/opt/tg-jobs-searcher`, не
включая `.venv`, `.env`, `secrets`, `.git` и `.idea`. В проекте могут быть
локальные изменения, которых ещё нет в удалённом Git-репозитории. Передайте
каталог сервисному пользователю, затем установите зависимости:

```bash
sudo chown -R tg-jobs-searcher:tg-jobs-searcher /opt/tg-jobs-searcher
sudo -u tg-jobs-searcher python3 -m venv /opt/tg-jobs-searcher/.venv
sudo -u tg-jobs-searcher /opt/tg-jobs-searcher/.venv/bin/python -m pip install --upgrade pip
sudo -u tg-jobs-searcher /opt/tg-jobs-searcher/.venv/bin/python -m pip install -e /opt/tg-jobs-searcher
```

Подготовьте закрытый файл переменных. Не добавляйте его в Git и не передавайте
в сообщениях:

```bash
sudo install -o root -g tg-jobs-searcher -m 640 \
  /opt/tg-jobs-searcher/deploy/tg-jobs-searcher.env.example \
  /etc/tg-jobs-searcher/tg-jobs-searcher.env
sudoedit /etc/tg-jobs-searcher/tg-jobs-searcher.env
```

В `DATABASE_URL` укажите пользователя и базу, созданные выше. Задайте токен
BotFather, Telegram user ID владельца, API ID/API hash и путь
`/var/lib/tg-jobs-searcher/telethon.session`.

Для PostgreSQL в Docker с опубликованным локальным портом строка подключения
имеет вид `postgresql+asyncpg://tg_jobs:<пароль>@127.0.0.1:5432/tg_jobs`.
Подставьте фактический порт хоста. Специальные символы пароля в URL требуется
закодировать; проще использовать пароль из букв и цифр.

Авторизуйте пользовательский Telegram-клиент от имени сервисного пользователя:

```bash
sudo -u tg-jobs-searcher bash -c '
  set -a
  . /etc/tg-jobs-searcher/tg-jobs-searcher.env
  set +a
  /opt/tg-jobs-searcher/.venv/bin/tg-jobs-searcher auth
'
```

Команда интерактивно запросит номер телефона, код Telegram и пароль 2FA при его
наличии. Убедитесь, что созданный файл сессии имеет права `0600` и принадлежит
`tg-jobs-searcher`.

Если нужно сохранить добавленные группы, ключевые слова и уже отправленные
уведомления из Supabase, перенесите текущую базу через `pg_dump`/`pg_restore`
**до первого запуска сервиса**. Для пустой новой базы этого не требуется:
миграции создадут таблицы при запуске. Не запускайте одновременно локальный
экземпляр с Supabase и серверный экземпляр с новой базой: разные базы не
разделяют защиту от дубликатов.

Установите unit и запустите сервис:

```bash
sudo install -o root -g root -m 644 \
  /opt/tg-jobs-searcher/deploy/tg-jobs-searcher.service \
  /etc/systemd/system/tg-jobs-searcher.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-jobs-searcher
sudo systemctl status tg-jobs-searcher
```

`ExecStartPre` применяет миграции перед каждым запуском. Обычный процесс защищён
PostgreSQL advisory lock, поэтому второй экземпляр не начнёт читать группы.
`OWNER_TELEGRAM_ID` задаёт администратора. По умолчанию любой пользователь может
отправить боту `/start` и сразу начать работу; администратор может переключить
режим входа командой `/access_mode`. Администратор получит
уведомление о новом пользователе и ежедневный отчёт после 09:00 по времени
Тбилиси.

## Обновление

Остановите сервис, обновите исходный код и зависимости, затем снова запустите
его. Перед обновлением сохраните резервную копию базы.

```bash
sudo systemctl stop tg-jobs-searcher
# Скопируйте в /opt/tg-jobs-searcher новую версию проекта тем же способом, что и при установке.
sudo -u tg-jobs-searcher /opt/tg-jobs-searcher/.venv/bin/python -m pip install -e /opt/tg-jobs-searcher
sudo systemctl start tg-jobs-searcher
sudo systemctl status tg-jobs-searcher
```

## Резервное копирование и диагностика

Сохраняйте резервную копию базы **вне сервера**. Если PostgreSQL работает на
хосте, можно создать архив так:

```bash
sudo -u postgres pg_dump -Fc tg_jobs > /srv/backups/tg_jobs-$(date +%F).dump
```

Файл `/var/lib/tg-jobs-searcher/telethon.session` нужен для доступа к Telegram.
Храните его в резервной копии с правами `0600`; его компрометация равна доступу
к Telegram-аккаунту.

Полезные команды:

```bash
sudo systemctl status tg-jobs-searcher
sudo journalctl -u tg-jobs-searcher -f
sudo -u tg-jobs-searcher bash -c 'set -a; . /etc/tg-jobs-searcher/tg-jobs-searcher.env; set +a; /opt/tg-jobs-searcher/.venv/bin/tg-jobs-searcher --check'
```

Если Telegram-сессия перестала быть авторизованной, остановите сервис, удалите
только файл `telethon.session`, снова выполните команду `auth` выше и запустите
сервис. Не удаляйте базу: в ней хранятся группы, задания и защита от дублей.

## Сквозная проверка после запуска

1. Отправьте `/start`, затем `/check_group` для тестовой группы, куда уже входит
   подключённый аккаунт.
2. Добавьте эту группу через `/add`, ключ `stage6-check` и режим истории.
3. Убедитесь через `/status`, что задача завершилась, и отправьте в тестовую
   группу новое сообщение с `stage6-check`.
4. Проверьте одно уведомление с ссылкой на пост. Измените ключ, повторите тест и
   удалите группу через `/groups`.
5. Перезапустите сервис командой `sudo systemctl restart tg-jobs-searcher` и
   убедитесь, что `/status` и уведомления продолжают работать.
