# DeployManager

Инструмент деплоя проектов (BinoOptions и др.) на ноды и проверки актуальности версий.

Читает серверы из `Program.vocabulary.nodes` и записи программ из `program.programdata`
(та же БД, что у диспетчера ProgramManager2.0). Заливка — **rsync** под `vova`,
service-файлы и `systemctl` — через **passwordless sudo**. Версия фиксируется в файле
`VERSION` (git SHA) для последующей сверки «актуально / отстало».

## Три ветки при запуске

1. **Деплой нового проекта (с нуля)** — на чистые серверы.
2. **Добавить сервер** к существующему деплою — сначала показывает карту развёртывания
   (где уже стоит + версии), затем деплой на выбранные новые ноды.
3. **Проверить версии на серверах** (vs локальной) — дашборд: программа → ноды,
   `leader/standby`, `running`, версия и отставание в коммитах; опц. обновление
   фактического состояния (`running`/`systemd_error`) через `systemctl`.

## Что делает деплой

- **Валидация** service-файлов: `service_name`↔`programdata` (+создание записи),
  путь↔`folder`, `Restart` (выключен под Dispatcher), `venv` в `ExecStart`.
- **Preflight**: защита от затирания; уже развёрнутые ноды пропускаются (обновление —
  отдельная ветка), предупреждение о рассинхроне версий при добавлении серверов.
- **Раскатка**: `ssh mkdir -p` → rsync (код+`.env`, без `*.md`/`.env.example`) →
  provision (venv/pip/playwright) → `systemd/*.service` в `/etc` (sudo) + `daemon-reload`
  → манифест `VERSION` → привязка к нодам (`service_status` standby) + отчёт.
- **Пост-деплой**: хэш-сверка содержимого, статус версий, audit-лог.

## Режимы запуска

- Интерактивно: меню из 3 веток.
- Неинтерактивно (CI/скрипты): `--action new|add|check|create|state`,
  `--project PATH`, `--nodes all|имена/ip/номера`, `--dry-run`, `--yes`.

## Установка

```bash
cd DeployManager
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # заполнить PG_* и SSH_*
```

`.env`: креды БД `Program` (через PgBouncer) и SSH (`SSH_USER=vova`, `SSH_KEY` —
приватный ключ для входа на ноды).

## Запуск

```bash
.venv/bin/python main.py
```

Интерактивно: папка проекта → версия → валидация → выбор нод → подтверждение → деплой → статус.

## Структура

```
DeployManager/
├── settings/config.py     # .env: PG_*, SSH_*, rsync excludes
├── database/db.py         # asyncpg: vocabulary.nodes, programdata, program_data_view
├── classes/
│   ├── ssh_client.py      # asyncssh: run / run sudo / read_file / ping
│   ├── deployer.py        # rsync + установка юнитов (sudo) + запись VERSION
│   └── manifest.py        # git-версия проекта + парсинг VERSION с ноды
├── core/
│   ├── validate.py        # сверка БД ↔ service-файлы + интерактивное разрешение
│   ├── status.py          # актуальность версий по нодам
│   └── deploy.py          # оркестрация деплоя
├── cli.py / main.py       # интерактивный вход
└── requirements.txt
```

## Дальше (по мере расширения задач)

- Предвыбор/ограничение целей по `program_data_view` (leader/standby).
- Перезапуск сервисов и согласование с диспетчером (не трогать активный leader).
- Хэш-сверка содержимого (помимо `VERSION`).
- Pull-режим («хаб») как альтернативная стратегия деплоя.