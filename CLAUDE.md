# DeployManager — контекст проекта (для Claude Code)

Инструмент **деплоя и управления** программами на нодах. Переиспользует БД и инфраструктуру
диспетчера **ProgramManager2.0** (`../ProgramManager2.0`). Развёртывает проекты вроде
**BinoOptions** (`/home/vlad/PythonProjects/BinoDex/Options/BinoOptions`).

GitHub: `git@github.com-stepuk:stepuk-prog/DeployManager.git` (alias `github.com-stepuk`).

## Запуск / тесты / окружение
- GUI: `.venv/bin/python gui_main.py` (Flet). CLI: `.venv/bin/python main.py [--action …]`.
- CLI-флаги: `--project PATH --action {new,add,check,create,state,manage,uninstall} --command {start,stop,restart} --nodes all|... --dry-run --yes`.
- Тесты: `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` (чистая логика, без БД/SSH).
- `.env` (gitignored) — креды БД Program + SSH. Ключи: `PG_*`, `SSH_USER=vova`, `SSH_KEY=/home/vlad/.ssh/id_rsa`, `PRIV_USER=root`, `PROJECTS_DIR`, `RSYNC_DELETE`, `PLAYWRIGHT_BROWSER`, `PROVISION`.
- venv в проекте — **`.venv`** (на серверах у проектов — `venv`, см. `config.VENV_DIR`).

## Архитектура (ядро UI-агностично)
- `settings/config.py` — конфиг из `.env`.
- `database/db.py` — `Database` (asyncpg, БД **Program** через PgBouncer). Все запросы тут.
- `classes/ssh_client.py` — `SshClient` (asyncssh, кэш соединений по (user,host)); `run`, `run_priv` (root/sudo), `ping`, `path_exists`, `read_file`.
- `classes/deployer.py` — `rsync_project` (mkdir -p + rsync, dry-run), `provision` (venv/pip/playwright), `install_services` (юниты в /etc под root), `write_version`.
- `classes/manifest.py` — git-версия проекта + парс `VERSION` с ноды.
- `core/ui.py` — **единый интерактив**: `ask`/`confirm`/`select`(один)/`checkbox`(много). Все async. CLI → questionary(TTY)/input; GUI → `set_backend(FletUi)`. Никогда не мешать `input()` с questionary (raw-режим).
- `core/validate.py` — сверка service-файлов ↔ `programdata` (путь/`service_name`/`Restart`(off под Dispatcher)/`venv` в ExecStart) + интерактивное разрешение.
- `core/deploy.py` — оркестрация `_deploy_one`/`deploy` (per-node, dry_run).
- `core/status.py`, `core/dashboard.py` — версии/обзор по нодам (read-only).
- `core/state.py` — `systemctl show` → `service_status.running/systemd_error` (без sudo).
- `core/watchdog.py` — start/stop/restart через `dispatcher.watchdog_instruction` (source='dm'); не дёргаем systemctl напрямую.
- `core/uninstall.py` — деинсталляция (см. ниже).
- `core/provision.py` — детект пакетов с пост-установкой (playwright → `playwright install <browser>`).
- `core/audit.py` — файловый audit (`logs/deploy_audit.log`).
- `cli.py` — `run(args)`, ветки, `_deploy_flow`, `_preflight`, `_leader_guard`, `_bind_and_report`, `_journal_deploy`.
- `gui/` — Flet: `app.py` (окно), `backend.py` (FletUi — диалоги через asyncio.Future, один event-loop), `log_sink.py` (stdout→лог-панель + цвет).

## БД Program (ключевые объекты)
- `vocabulary.nodes` — серверы (id, hostname, server_name, ip_address, is_online).
- `program.programdata` — программы (program_id PK **без автоинкремента**, service_name, folder, status, dispatcher, author, …). **Одна папка `folder` на НАБОР юнитов** (5 binodex делят `/home/vova/Binodex/BinoOptions`). Создание записи: `program_id = MAX+1`, `status=false`, `dispatcher=false`.
- `dispatcher.service_status` — привязка сервиса к ноде (PK service_id+node_id; status leader/standby/unavailable, running). FK status — vocabulary-значения.
- `dispatcher.watchdog_instruction` — очередь команд агенту (insert без instruction_id; команды start/stop/restart; source 'gd'/'dm').
- `dispatcher.deploy_journal` — **наш журнал** (ключ program_id): флаги `folder_deployed/service_installed/db_updated` + action/result/commit/operator/details/ts. DDL: `scripts/deploy_journal.sql`.
- **Каскадное удаление programdata уже настроено** (FK у screen_otc_meta/error_handling_log → CASCADE, watchdog_instruction.log_id → SET NULL). `DELETE FROM program.programdata` проходит насквозь.
- Авторы для создания записи: `Proger M1` (975218672), `Толстый` (6275724296).

## SSH / деплой
- Вход под **vova** (rsync кода/venv/playwright — правильный владелец). У vova **нет passwordless sudo** на части нод → привилегии (юниты в /etc, systemctl) под **root** (`PRIV_USER=root`, тот же ключ). `ssh.run_priv`.
- rsync **исключает**: `.git .venv venv logs/* files/* __pycache__ *.pyc .idea pictures/new *.md .env.example`. **`.env` ДЕПЛОИТСЯ** (не исключён).
- provision = venv → pip install -r → playwright install (если есть в requirements; предлагается).
- `VERSION` — манифест (git SHA) на ноде; по нему статус «up-to-date/stale».

## Ветки/действия
1. **Деплой с нуля** (`new`) — на чистые ноды.
2. **Добавить сервер** (`add`) — показывает карту развёртывания, деплой на новые; preflight пропускает уже развёрнутые (есть `VERSION`) — «обновление = отдельная ветка»; предупреждает о рассинхроне версий.
3. **Проверить версии** (`check`) — дашборд (версия/отставание/leader/running) + опц. state-check + опц. управление.
- **Управление** (`manage`) — start/stop/restart через watchdog (leader — с предупреждением).
- **Деинсталляция** (`uninstall`) — 2 режима поиска: `[1]` по service-файлам = **весь проект** (все юниты + папка), `[2]` из БД = одна программа (старые). **Гейт: status=true → запрет** (сперва stop через watchdog). Ноды ищутся SSH-пробой наличия папки/юнитов. Снять привязку → stop/disable/rm юнитов (root) → rm папки (опц) → журнал → опц. удаление записей из programdata (каскад).
- `create` — создать запись programdata; `state` — обновить running.
- **Обновление** (`update`/`sync`) — `core/update.py`: синхронизация отставших нод до локальной версии (rsync→provision→install→write_version→restart), предлагается всплывающим диалогом в «Проверить версии» при рассинхроне. `core/sync_config.py` (action `sync`): обновить `.env`/юниты без передеплоя (с хэш-сверкой, не трогает идентичные).

## Гайдлайны/гочи
- **Прод!** Деструктивное в БД/на нодах — только с подтверждением; деплой/uninstall/update логировать в журнал.
- Деплой: **первичный**, **добавление серверов** и **обновление** (`core/update.py` — синхронизация версии уже развёрнутых нод; при update playwright НЕ переустанавливаем).
- **rsync исключает рантайм, не папки целиком:** `*.log`, `*.session`, `files/*` (+ `RSYNC_INCLUDES=files/.gitkeep`). НЕ возвращать `logs/*` — срежет исходники `logs/*.py`, приложение упадёт на `import logs`.
- SSH: ключ грузится с понятной ошибкой; `PRIV_KEY` + `*_PASSPHRASE` опциональны; ключ для rsync должен быть `chmod 600`.
- Flet **0.85** (1.0-alpha): `ft.run`, `page.show_dialog/pop_dialog`, `page.run_task`, `ft.Border.all`, `ft.Colors`. Чек-боксы требуют TTY (в CLI), в GUI — нативные.
- НЕ удалять чужие юниты (только свои `service_name`, без glob). Чужой проект `option-*` (наш — `binodex-*`).
- Цвет лога — **пер-значение** (span'ы): leader/active/up-to-date зелёный; standby/inactive/stale/отстаёт янтарный; unavailable/⛔/❌/‼️/🛑/ошибка/⚠️⚠️/fail красный. Кнопки диалогов: Да/OK светло-зелёные, Нет/Отмена приглушённо-красные, danger «Да» красная + «Нет» серая.

## Состояние (на момент записи)
- Всё на ветке **`master`** (запушено; `feature/gui`/`feature/management` влиты и удалены). См. `CHANGELOG.md`.
- Готово: первичный деплой, добавление серверов, дашборд/«Проверить версии» (state-check по умолчанию), watchdog-управление, деинсталляция (3 режима, в т.ч. «из журнала»), журнал, GUI (Flet, цветной лог, combobox), **ветка обновления** (`update`) и **`sync`** (.env/юниты).
- Обкатано вживую: деинсталляция; первичный деплой (после фикса `logs/*.py` — `import logs` чинился доставкой исходников). **Дальше:** обкатать `update`/`sync` вживую.
