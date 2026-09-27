# Развёртывание одной кнопкой через GitHub Actions

Workflow [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) запускается
вручную: **GitHub → Actions → Deploy bot → Run workflow**. Он передаёт на сервер
выбранный коммит `main`, обновляет серверный Git-клон без потери локальных
изменений, запускает `docker compose up -d --build bot` и проверяет контейнер.
Миграции Alembic уже входят в команду запуска контейнера в `compose.yaml`.

Ниже — одноразовая настройка. Пароли PostgreSQL, токен бота, Telegram-сессия
и `.env` остаются только на сервере. **Не присылайте SSH-ключи в чат и не
добавляйте их в Git.**

## 1. Создать ключ для GitHub Actions

На своём компьютере создайте отдельный ключ без парольной фразы:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tg-jobs-actions-deploy -C tg-jobs-actions-deploy -N ''
scp -P 2224 ~/.ssh/tg-jobs-actions-deploy.pub root@45.63.117.94:/tmp/tg-jobs-actions-deploy.pub
```

Если вход на сервер под root отключён, используйте действующего SSH-пользователя
и затем выполните команды следующего раздела через `sudo`.

## 2. Подготовить отдельного пользователя на сервере

Текущий клон находится в `/root/tg-job-searcher-bot`, куда другой пользователь
не сможет войти. Выполните на сервере под root **один раз**:

```bash
useradd --create-home --shell /bin/bash tg-jobs-deploy
usermod --append --groups docker tg-jobs-deploy
install -d -m 755 /opt
mv /root/tg-job-searcher-bot /opt/tg-job-searcher-bot
chown -R tg-jobs-deploy:tg-jobs-deploy /opt/tg-job-searcher-bot
install -d -o tg-jobs-deploy -g tg-jobs-deploy -m 700 /home/tg-jobs-deploy/.ssh
install -o tg-jobs-deploy -g tg-jobs-deploy -m 600 \
  /tmp/tg-jobs-actions-deploy.pub /home/tg-jobs-deploy/.ssh/authorized_keys
rm /tmp/tg-jobs-actions-deploy.pub
```

Имя каталога не меняется, поэтому имя Compose-проекта остаётся прежним.
Файл `.env` переезжает вместе с клоном; каталог Telegram-сессии
`/var/lib/tg-jobs-searcher` остаётся на месте. Пользователь должен иметь доступ
к Docker; членство в группе `docker` даёт фактически права root на сервере,
поэтому этот ключ должен использоваться только для развёртывания.

Проверьте доступ с компьютера:

```bash
ssh -i ~/.ssh/tg-jobs-actions-deploy -p 2224 tg-jobs-deploy@45.63.117.94 \
  'cd /opt/tg-job-searcher-bot && git status --short && docker compose version'
```

Если `git status --short` показывает изменения исходников, сохраните и
разберите их до первого запуска workflow. Развёртывание специально не
перезаписывает такие изменения.

## 3. Сохранить доступ в GitHub Secrets

На компьютере получите ключ SSH-хоста и сравните его отпечаток с отпечатком,
показанным **на самом сервере**:

```bash
ssh-keyscan -p 2224 -t ed25519 45.63.117.94 > /tmp/tg-jobs-known-hosts
ssh-keygen -lf /tmp/tg-jobs-known-hosts
```

На сервере:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Откройте в GitHub **Settings → Secrets and variables → Actions → New repository
secret** и добавьте:

| Секрет | Значение |
| --- | --- |
| `DEPLOY_HOST` | `45.63.117.94` |
| `DEPLOY_SSH_KEY` | Полное содержимое `~/.ssh/tg-jobs-actions-deploy` (закрытый ключ) |
| `DEPLOY_KNOWN_HOSTS` | Полное содержимое `/tmp/tg-jobs-known-hosts` после проверки отпечатка |

В workflow включена строгая проверка ключа сервера; он не отключает
`StrictHostKeyChecking`.

## 4. Запуск

После добавления workflow в ветку `main` откройте **Actions → Deploy bot →
Run workflow** и выберите `main`. Workflow передаёт на сервер именно выбранный
коммит, поэтому серверному пользователю не нужен отдельный ключ для чтения GitHub.
Если сборка или запуск не удались, откройте журнал запуска в Actions: там будут
состояние контейнера и последние строки его логов. Повторные одновременные
развёртывания ставятся в очередь.
