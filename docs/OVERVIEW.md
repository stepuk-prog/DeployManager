# DeployManager — обзор проекта

Инструмент **деплоя и управления** программами на нодах. Переиспользует БД и инфраструктуру
диспетчера **ProgramManager2.0**. Развёртывает проекты вроде **BinoOptions**/**BinoStock** в
`/home/vova/Binodex/<Project>` и регистрирует их в БД диспетчера.

## Запуск

- **GUI** (Flet): `.venv/bin/python gui_main.py`
- **CLI** (интерактивный): `.venv/bin/python main.py`
- **CLI** (автоматизация):
  `main.py --project PATH --action {new,add,check,create,state,manage,uninstall,sync} \
   --command {start,stop,restart} --nodes all|… --dry-run --yes`
- **Тесты** (чистая логика, без БД/SSH): `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`

Конфигурация — в `.env` (gitignored): креды БД (`PG_*`), SSH (`SSH_USER`, `SSH_KEY`, `PRIV_USER`,
опц. `PRIV_KEY`/`*_PASSPHRASE`, `SSH_PORT`), `PROJECTS_DIR`, `RSYNC_DELETE`, `PLAYWRIGHT_BROWSER`,
`PROVISION`, `RSYNC_TIMEOUT`. venv проекта — `.venv`; на серверах у разворачиваемых проектов — `venv`.

## Архитектура (ядро UI-агностично)

| Модуль | Назначение |
|---|---|
| `settings/config.py` | Конфиг из `.env`. |
| `database/db.py` | `Database` (asyncpg через PgBouncer, `statement_cache_size=0`). Все SQL тут. |
| `classes/ssh_client.py` | `SshClient` (asyncssh, кэш соединений по `(user,host)`, keepalive, реконнект): `run`, `run_priv`, `ping`, `path_exists`, `read_file`. |
| `classes/deployer.py` | `rsync_project`/`sync_env`/`sync_units` (rsync с таймаутом), `provision` (venv/pip/playwright), `install_services` (юниты в `/etc` под root), `write_version`. |
| `classes/manifest.py` | git-версия проекта + парс `VERSION` с ноды. |
| `core/ui.py` | Единый интерактив: `ask`/`confirm`/`select`/`combobox`/`checkbox` (CLI → questionary/input; GUI → Flet-бэкенд). |
| `core/validate.py` | Сверка service-файлов ↔ `programdata` (путь/`service_name`/`Restart`/`venv`/абсолютность путей) + интерактивное разрешение. |
| `core/verify.py` | sha256-сверка файлов на ноде = локальным (git-tracked + untracked, минус rsync-исключения, + `.env`). |
| `core/deploy.py` | Оркестрация деплоя на ноды (`asyncio.gather`). |
| `core/update.py` | Синхронизация версии отставших нод (только код, без рестарта). |
| `core/sync_config.py` | Обновить `.env`/юниты без передеплоя (хэш-сверка). |
| `core/status.py`, `core/dashboard.py` | Версии/обзор по нодам (read-only). |
| `core/state.py` | `systemctl show` → `service_status.running` (без sudo). |
| `core/watchdog.py` | start/stop/restart через `dispatcher.watchdog_instruction` (`source='dm'`). |
| `core/uninstall.py` | Деинсталляция (только свои `service_name`, без glob). |
| `core/provision.py` | Детект пост-установок (playwright). |
| `core/audit.py` | Файловый audit-лог. |
| `cli.py` | `run(args)`, ветки, `_deploy_flow`, `_preflight`, `_install_units_light`. |
| `gui/` | Flet: `app.py` (окно), `backend.py` (диалоги через `asyncio.Future`), `log_sink.py` (stdout→лог с цветом). |

## БД диспетчера (ключевые объекты)

- `vocabulary.nodes` — серверы (`id`, `hostname`, `server_name`, `ip_address`, `is_online`).
- `program.programdata` — программы (PK `program_id` **без автоинкремента**, `service_name`, `folder`,
  `status`, `dispatcher`, `author`…). **Одна папка `folder` на НАБОР юнитов** (несколько binodex-юнитов
  делят одну папку проекта). Создание: `program_id = MAX+1`, `status=false`, `dispatcher=false`.
- `dispatcher.service_status` — привязка сервиса к ноде (`leader`/`standby`/`unavailable`, `running`).
- `dispatcher.watchdog_instruction` — очередь команд агенту (`start`/`stop`/`restart`; `source`
  `'gd'`/`'cron'`/`'manual'`/`'dm'`; `log_id` → `service_error_log`).
- `dispatcher.service_error_log` / `error_handling_log` — события и их обработка (агент пишет сюда
  после исполнения; DeployManager создаёт служебное событие `DM_MANUAL` под ручные команды).
- `dispatcher.deploy_journal` — журнал деплоя (`program_id`): флаги `folder_deployed`/
  `service_installed`/`db_updated` + `action`/`result`/`commit`/`operator`/`ts`.

## Ветки / действия

1. **Деплой с нуля** (`new`) — на чистые ноды.
2. **Добавить сервер** (`add`) — карта развёртывания + деплой на новые.
   - **Лёгкая доустановка юнитов** (в preflight 1/2): на ноде, где код уже совпадает (sha256, без
     `systemd/*.service` и `requirements.txt`), вместо передеплоя доставляются недостающие юниты +
     связи в БД; при изменившемся `requirements.txt` — `pip install -r`. Иначе (расходится КОД) —
     ветка обновления.
3. **Проверить версии** (`check`) — state-check + дашборд (версия/отставание/leader/running) + опц.
   синхронизация при рассинхроне + опц. управление.
- **Управление** (`manage`) — start/stop/restart через watchdog (leader — с предупреждением).
- **Обновление** (`update`) — синхронизация версии отставших нод (**только код, без рестарта**).
- **Sync** (`sync`) — обновить `.env`/юниты без передеплоя.
- **Деинсталляция** (`uninstall`) — снять сервисы (только свои `service_name`); гейт `status=true`.
- `create` — создать запись `programdata`; `state` — обновить `running`.

## SSH / деплой

- Вход под **vova** (rsync кода/venv — правильный владелец). Привилегии (юниты в `/etc`, systemctl)
  под **root** (`PRIV_USER=root`, тот же или отдельный `PRIV_KEY`). `ssh.run_priv`.
- **Пути в юнитах должны быть абсолютными** (`/home/vova/…`) — относительный `WorkingDirectory`/
  `ExecStart` ломает rsync/cp; валидация это ловит.
- rsync **исключает рантайм**: `*.log`, `*.session`, `files/*` (+ `RSYNC_INCLUDES=files/.gitkeep`),
  `__pycache__`, `*.pyc`, `.idea`, `*.md`, `.env.example`, `.git`, `.venv`, `venv`. **`.env` деплоится.**
  Исключать `logs/*` нельзя — срежет исходники пакета `logs`.
- `VERSION` — JSON-манифест (git SHA) на ноде; по нему «up-to-date/stale».

## Гайдлайны

- **Прод.** Деструктивное в БД/на нодах — только с подтверждением; деплой/uninstall/update —
  в журнал.
- Управление сервисами — **только через диспетчер** (`watchdog_instruction`), не `systemctl`
  напрямую. `Restart=no` в юнитах (иначе systemd конфликтует с failover диспетчера).
- НЕ удалять чужие юниты (только свои `service_name`, без glob).
- GUI: выбор — кнопками (`ui.select`)/чек-боксами, не текстовым полем; цвет лога/кнопок — по смыслу
  (зелёный/янтарный/красный).

См. также [CHANGELOG.md](CHANGELOG.md).
