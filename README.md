# DeployManager

Инструмент деплоя проектов (BinoOptions и др.) на ноды и проверки актуальности версий.

Читает серверы из `Program.vocabulary.nodes` и записи программ из `program.programdata`
(та же БД, что у диспетчера ProgramManager2.0). Заливка — **rsync** под `vova`,
service-файлы и `systemctl` — через **passwordless sudo**. Версия фиксируется в файле
`VERSION` (git SHA) для последующей сверки «актуально / отстало».

## Возможности (MVP)

- **Деплой**: rsync кода → `programdata.folder`, установка `systemd/*.service` в
  `/etc/systemd/system` + `daemon-reload`, запись манифеста `VERSION`.
- **Валидация** перед деплоем: имя service-файла ↔ `programdata.service_name`,
  путь `WorkingDirectory`/`ExecStart` ↔ `programdata.folder`. При несоответствии —
  запрос «что менять»: поправить БД, поправить файл, пропустить или отменить.
- **Статус версий**: опрос нод (читает `VERSION`), сравнение с локальным git SHA →
  `up-to-date / stale / missing / unreachable`.
- Выбор нод — вручную из online (`vocabulary.nodes`); связанные с программой
  (`program_data_view`) помечаются `*`.

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