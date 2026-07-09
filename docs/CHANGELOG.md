# Changelog

Все значимые изменения DeployManager. Формат — по разделам Added / Changed / Fixed.

## 2026-07-09

### Added
- **«Настроить ноду» ФАЗА 2 — член кластера (пошаговый визард)** (`core/setup_cluster_member.py`).
  Замена мёртвой cluster-ноды (Patroni/etcd) — опасная процедура (`node_replacement.md`: swap
  членства etcd с окном quorum 2/2, basebackup, чистка мёртвого IP на живых нодах). Поэтому НЕ
  «один клик», а **пошаговый визард**: каждый шаг = заголовок + подробное пояснение (что/зачем/риск)
  + точные команды → `[Выполнить / Пропустить / Отмена]`. 14 шагов: 0 бэкап · 1a локаль / 1b UFW ·
  2a-2e install (PG16/etcd/Patroni/HAProxy-из-исходников/users) · 3 конфиги (v1 вручную, scp из
  `CLUSTER_CONFIG_DIR`) · **4 ⚠️ etcd-swap** · 5 старт+basebackup · **6 ⚠️ чистка IP (swap-node-ip.sh)** ·
  7 HAProxy клиентов · 8 регистрация+WD+Reporter. Опасные шаги (4/6, danger) — усиленное
  подтверждение. Автоматизирована безопасная подготовка; quorum-критичное — под явным контролем
  оператора. Хук из формы «Настроить ноду» (тип «Элемент кластера»). Конфиг `CLUSTER_CONFIG_DIR`.
  Real-прогон — на живой узел (dry-run печатает план). *Follow-up: авто-копирование конфигов (шаг 3).*
- **Reporter — кнопка деплоя на cluster-ноды** (`core/reporter.py`, кнопка «📊 Reporter» в
  control-plane ряду + CLI `--action reporter`). Reporter — Patroni `on_role_change`→primary
  callback (шлёт статус кластера в Telegram), НЕ dispatcher-компонент и НЕ systemd-служба.
  Переиспользует стандартные примитивы (`rsync_project`+`provision`), но со спецификой:
  источник `REPORTER_DIR` (дефолт `CLUSTERS_DIR/programs/reporter`), владелец
  `postgres:patroni_group`, вместо юнита — `leader_callback.sh`→`/usr/local/bin` + лог-файл,
  `patroni.yml` только ПРОВЕРЯЕТСЯ (не правим — дисраптивно). Только cluster-ноды (claster=true).
  Флоу: mkdir+chown vova → rsync → provision(venv+pip) → chown postgres:patroni_group 775 →
  callback+лог → verify patroni.yml. Конфиг: `REPORTER_DIR/REMOTE/OWNER/CALLBACK/LOG`, `PATRONI_YML`.
- **Cookies/Binodex — три категории кнопок** (`gui/app.py`, `database/db.py`). Вместо одного
  «Обновить старый» (был только Options) — **Options** / **OTC Screen** / **Crypta Screen**,
  каждая со своим источником аккаунтов: `settings.option_setting.cookies_pocket` (6),
  `settings.screen_otc.user_id` (35), `settings.screen_crypto.cookies_binodex` (9). Подпись
  кнопки — `program_name` по binodex-аккаунту (`program_names_by_account`). «Добавить новый»
  без изменений. Флоу создания кук общий (Privy, mode='old'). Новые методы БД:
  `binodex_otc_screen_accounts`, `binodex_crypto_accounts`, `program_names_by_account`.
- **Операционные скрипты флота — кнопками** (`core/scripts.py`, ряд «Скрипты флота»
  в GUI + CLI `--action <key>`). Декларативный реестр `SCRIPTS` (новый скрипт = одна
  запись + файл в `assets/`, без правок cli/gui — как `tools.TOOLS`). Скрипты:
  `pw_sweep` (Playwright-свип, scope=node), `audit_cluster` (read-only свод здоровья),
  `whitelist_ip` (fail2ban+UFW по IP), `swap_node_ip` (смена IP узла в конфигах,
  danger). Два scope: **local** — скрипт сам обходит флот, гоняем во временной папке;
  **node** — пикер узла (или «все клиентские») → upload → run под root. Apply-скрипты:
  dry-run → подтверждение → `--apply`. Аргументы декларативно (позиц./флаг/булев,
  валидация IP). audit-запись на каждый запуск.
- **Самодостаточность (по требованию Vlad — DM не зависит от чужих репозиториев):**
  скрипты ВЕНДОРЕНЫ в `assets/fleet_scripts/` (не ссылки на Clusters), а реестр узлов
  `_nodes.sh` ГЕНЕРИТСЯ на лету из `vocabulary.nodes` (`_gen_nodes_sh`) — БД источник
  правды топологии, дубля IP-списка нет.

- **Cookies/Binodex — прокси как в боевом BinoOptions** (`tools/cookies/`). Вкладка
  Binodex собирает cookies через :50100-HTTP-прокси из `settings.proxy_data` (scope
  binodex) с локальным релеем (Proxy-Authorization — Firefox не умеет socks5-auth).
  Порт `settings/proxy.py` (`get_active_proxies`/`get_unused_proxy`) + вендоренный
  `settings/local_proxy.py` (stdlib-релей). В визарде выбор «Войти через прокси» /
  «Войти напрямую»; при пустом пуле/сбое релея — direct-фолбэк. Релей гасится в
  `BrowserSession.close()` (+ явный стоп при неудачном launch). `database/db.py`:
  `get_active_proxies(scope='binodex')`.

### Changed
- **Пути к исходникам компонентов — конфигурируемы через env** (`core/infra_deploy.py`).
  Локальный исходник каждого control-plane компонента резолвится из env `<KEY>_DIR`
  (`GD_DIR`/`WD_DIR`/`CD_DIR`/`DISPATCHERCTL_DIR`) + `COMMON_DIR`, с фолбэком на
  `DISPATCHER_DIR/<project_subdir>`. Раньше все жёстко = подпапки `DISPATCHER_DIR` → теперь DM
  настраивается на любую раскладку (компоненты могут лежать где угодно). `InfraComponent.source_dir`
  (+ `env_base` от него) / `_common_dir()`. Задокументировано в `.env.example`.
- **«Настроить ноду» самодостаточна — provision-скрипты вендорены** (`core/setup_node.py`,
  `assets/fleet_scripts/`). `_scripts()` читал `provision-base.sh`/`provision-client.sh`/
  `whitelist-ip.sh` из `CLUSTERS_DIR/scripts` — теперь из вендоренных `assets/fleet_scripts`
  (байт-в-байт копии Clusters). DM больше НЕ требует чекаута Clusters для настройки узла.
  `CLUSTERS_DIR` остался только базой по умолчанию для `REPORTER_DIR` (reporter — целый проект,
  деплоим по пути). При обновлении provision-скриптов в Clusters — пере-вендорить в assets.
- **Cookies/Binodex «Добавить новый» — критерий смягчён до `mail`+`mail_app_pass`**
  (`database/db.py` `telegram_new_accounts`). Убрано требование `api_id`/`api_hash` (Telegram-API
  юзербота, к binodex-логину не относятся) — теперь «свободным» считается любой аккаунт с рабочей
  почтой без сохранённого binodex-storage_state. (Свободных стало 32 vs. меньше при api-фильтре.)
- **Cookies/Binodex — `setup_site` больше НЕ трогает тему** (`apps/binodex.py`). Убран блок
  выбора/переключения темы (по указанию — тему оставляем как есть); масштаб свечи/графика
  прокликивается как прежде.

### Fixed
- **Cookies/Binodex — понятная ошибка при отказе IMAP-логина** (`gui/flows.py`,
  `apps/imap_code.py`). IMAP-коннект вынесен в отдельный try ДО запуска браузера: при отказе почты
  (`[AUTHENTICATIONFAILED] Invalid credentials` — неверный/протухший Gmail app-пароль) статус теперь
  говорит «проверь mail/mail_app_pass в telegram.telegram» и НЕ врёт про «окно оставлено открытым»
  (браузер в этом случае не открывался). `_fmt_err` декодирует bytes-ошибку imaplib (было «b'...'»).
  `imap_connect` защитно чистит пробелы в app-пароле (Gmail показывает его группами по 4).
- **Cookies/Binodex — повторная модалка логина после входа** (`settings/config.py`).
  `BINODEX_TRADE` указывал на `https://app.binodex.app/trade` — ДРУГОЙ origin, чем страница
  логина (`binodex.app`). Privy держит сессию в `localStorage` (привязан к origin), поэтому на
  поддомене `app.binodex.app` сессии нет → Privy снова показывает «Log in or sign up». Исправлено
  на `https://binodex.app/trade` (тот же origin, что логин; и ровно то, что грузят боты —
  `binodex_settings.trade_url`). storage_state теперь снимается с правильного origin.
- **Cookies/Binodex — пустая страница в Playwright (протухший CDN-кэш)** (`gui/flows.py`
  `_bust_stale_assets`). Cloudflare-эдж отдавал устаревший `/assets/app.js` (static-имя,
  cf-cache HIT ~сутки), ссылающийся на уже удалённый локаль-чанк → тот 404-ил с MIME
  text/plain → Firefox блокировал ES-модуль (NS_ERROR_CORRUPTED_CONTENT) → SPA не
  бутстрапился (пустая страница, кнопки логина нет). Обычный браузер грузил из старого
  кэша — отсюда «в браузере открывается, в Playwright нет». Фикс: `page.route` добавляет
  cache-bust query к static-именованным entry (`app.js`/`app.css`) → CF MISS → origin
  отдаёт свежий app.js с живыми чанками. Проверено: body-len 0→4171, login_open 0→1.
- **«Настроить ноду»: «Отмена» на полях формы** (`core/setup_node.py`). Форма
  (IP/hostname/server_name/пароль) звала `ui.ask` без `cancelable=True` — только OK,
  чистого выхода не было. Теперь `cancelable=True`: «✖️ Отмена» → выход до bootstrap'а,
  ничего не тронув.

## 2026-07-08

### Added
- **«Настроить ноду» (turnkey ввод нового узла флота, фаза 1 — обычный узел)** —
  `core/setup_node.py`. Кнопка GUI «🖥️ Настроить ноду» (зелёная, control-plane ряд) +
  CLI `--action setup-node` / пункт меню `[8]`. Папку проекта не требует (БД+SSH, как infra).
  Контур: форма (IP, root-pass, hostname, server_name) → гард дубля по IP →
  **0** базовый bootstrap (одноразовый ПАРОЛЬНЫЙ коннект root → `provision-base.sh`;
  выключает password-auth) → **1** диалог типа ноды (обычный | элемент кластера=заглушка
  фазы 2) → **2** ролевой `provision-client.sh --tail-only` (haproxy_client) →
  **3** verify (key-доступ + haproxy_client) → **4** whitelist (показ команды →
  прогон `whitelist-ip.sh` по подтверждению) → **5** ПОЗДНЯЯ регистрация
  `vocabulary.nodes` (claster=false, только по здоровому узлу) → **6** деплой Watchdog
  (движок infra_deploy) → is_online=true. Повторный прогон безопасен (есть key-доступ →
  фаза 0 пропускается). audit-запись. OS-bootstrap остаётся bash в Clusters — DM его гоняет.
- `database/db.py`: `find_node_by_ip` (гард дубля), `create_node(...) RETURNING id`
  (поздняя пропись, без ghost-строк), `set_node_online`.
- `classes/ssh_client.py`: `bootstrap_run` (одноразовый парольный root + SFTP + стрим —
  голый узел до раскладки ключа), `upload` (SFTP по ключу), `run_stream` (живой лог
  длинных прогонов apt/сборки).
- `core/infra_deploy.py`: `deploy_component_to_node` — деплой одного control-plane
  компонента (WD) на ОДНУ ноду тем же движком, что полный инфра-деплой.
- `settings/config.py`: `CLUSTERS_DIR` (репо bash-примитивов), `SETUP_CLIENT_PORTS`
  (`22 6442 6543 8008`), `SETUP_BOOTSTRAP_TIMEOUT`.
- Тесты `tests/test_setup_node.py` (гарды IP/дубля/нет-скриптов/нет-ключа; dry-run без
  записей; заглушка кластерной ветки).

### Note
- Требует расщепления provision-скрипта в репозитории **Clusters**: `provision-base.sh`
  (общая база, шаги 1–9) + `provision-client.sh` (client-хвост, флаг `--tail-only`).
  Ветка «Элемент кластера» — заглушка (фаза 2: pg_basebackup/etcd-join/switchover).

## 2026-07-05 (2)

### Changed
- **Сверка версий инфра-компонентов (GD/WD/CD/DispatcherCtl) — паритет со стандартной веткой:**
  `core/status.py` теперь показывает **счётчик отставания** (колонка «ОТСТАВАНИЕ»:
  `отстаёт на N` / `впереди на N` / `разошлись (−b/+a)` / `вне истории репозитория`), как
  дашборд «Проверить версии». `check_status(..., project_dir)` считает лаг по git-истории.
- **Единый расчёт отставания** — `classes/manifest.py:lag_text(project_dir, node_commit,
  local_commit)`; `core/dashboard.py` больше не держит свою копию (`_lag`/`_count` удалены,
  импорт из manifest) — дашборд и инфра-сверка считают одинаково.
- Вызовы `status.check_status` передают `project_dir` (`core/infra_deploy.py`, `cli.py`).

## 2026-07-05

### Added
- **Реестр суб-инструментов `tools/`** — расширяемая точка для утилит, живущих в том же
  окне/CLI, но не про деплой. `tools/__init__.py`: `TOOLS` (дескрипторы key/**kind**/label/
  icon/color/module), `TOOL_KEYS`, `run_tool(key,db)` (flow), `build_screen(key,page,on_back)`
  (screen). Меню CLI (`[6]`/`[7]`) и кнопки GUI строятся ИЗ реестра → новый инструмент = одна
  запись + подпакет, без правок cli/gui. Не требуют папки проекта/SSH (гард уважает `TOOL_KEYS`).
  Два вида: **flow** (`async run(db)`, лог-панель, CLI+GUI) и **screen** (GUI-only экран со своим
  UI/жизненным циклом: `build_screen(page,on_back)->teardown`; навигатор `open_screen`/`go_home`).
- **Суб-инструмент «Юзерботы (сессии)»** (`tools/sessions/`, интеграция SessionManager, kind=flow)
  — логин юзербота (pyrofork; опц. Telethon) → `session_string` в `telegram.telegram`. Под-меню
  list/recover/create. Телеграм-методы БД — `database/tg.py` (`TelegramMixin` в `Database`).
  Зависимости `pyrofork`/`tgcrypto-pyrofork`; env `TELEGRAM_APPS`.
- **Суб-инструмент «Cookies»** (`tools/cookies/`, vendored CookiesProgram2, kind=screen) —
  вкладки OTC Option/OTC Screen/TradingView/Binodex, сбор cookies через **видимый браузер**
  (`async_playwright`). Самодостаточный суб-пакет (свои `settings/database/apps/classes/messages/
  gui/logs`, импорты namespaced `tools.cookies.*`; свои 2 пула БД `program`+`binodex`, jsonb-codec,
  контракт `execute_query→False`). Встроен как экран в окно DeployManager (`build_screen`, «← Назад»,
  teardown закрывает пулы/браузер и возвращает stdout). Зависимость `playwright` (firefox);
  env `PG_DB_BINODEX`/`TG_TOKEN`/`TG_CHANNEL`/`OTC_HEADLESS`/`BINODEX_HEADLESS`/`BINODEX_VW`/`BINODEX_VH`.

### Changed
- **Дашборд «Проверить версии»** (`core/dashboard.py`): версии — **один список нод на папку**
  (одна папка = один код; кэш чтения `VERSION` по `(folder,ip)` и `git rev-list` по коммиту),
  без повтора по каждому service-юниту; сервисы — компактный реестр имён.
- **«Проверка состояния сервисов»** (`core/state.py`): **сводка-строка на сервис** (leader +
  агрегат «все остановлены»/«все active», поимённо только отклонения active/failed/offline)
  вместо стены `нода × сервис`.
- `gui/log_sink.py`: подсветка новых значений (`все active`/`остановлены`/`✗ NODE`/`🔌 offline`/`failed`).
- `gui/app.py`: навигатор экранов (screen-инструменты переключают страницу); гард «без проекта»
  уважает `tools.TOOL_KEYS`. `database/db.py`: `Database(TelegramMixin)`.

### Note
- Зависимости: `requirements.txt` += `pyrofork`, `tgcrypto-pyrofork`, `playwright`.
- Оригиналы `../SessionManager` и `../CookiesProgram2` НЕ тронуты (интеграция = копия внутрь).
- Боевые флоу (живой логин юзербота; сбор cookies через видимый браузер, Telegram-уведомления) —
  ПИШУТ в прод-БД, выполняет оператор вручную.

## 2026-07-01

### Added
- **Деплой control-plane компонентов диспетчера (GD / WD / CD / DispatcherCtl)** —
  `core/infra_deploy.py`, В ОБХОД `programdata`/`service_status` (это инфраструктура, не
  бот-программы). Декларативный реестр `INFRA_COMPONENTS` (локальный путь, remote, юниты,
  набор нод, common, .env-маппинг). **Guard**: cluster-only компоненты (GD/CD/DispatcherCtl)
  ставятся ТОЛЬКО на `vocabulary.nodes.claster=true`; WD — на все online.
- **CLI**: `--action infra --component {GD,WD,CD,DispatcherCtl}` (+ `--check` / `--dry-run`);
  интерактивный пункт меню `[5]`.
- **GUI**: ряд кнопок control-plane (indigo, отдельный цвет) — по нажатию выпадает меню
  действий (как у обычных программ), папку проекта выбирать не нужно.
- **Меню операций компонента** (паритет со стандартными ветками): Деплой с нуля / Добавить
  ноду (только ноды без компонента) / Sync .env+юниты / Перезапуск / Сверка версий /
  Управление (start·stop·restart) / Предпросмотр (dry-run) / Деинсталляция.
- **Рендер `.env` из БД**: единый секрет-блок компонента — gitignored `env/<KEY>.env`
  (шаблоны `env/*.env.example`); DeployManager рендерит финальный `.env` на ноду =
  база + идентичность из `vocabulary.nodes` (`*_NODE_ID→id`, `WATCHDOG_NODE_IP→ip_address`,
  `*_NODE_NAME`/`GD_NODE_HOSTNAME→server_name`), `chmod 600`. Нет базы → `.env` не пишется
  (прод сохраняется). CD/DispatcherCtl — `.env` полностью единый (идентичность не нужна).
- **common разделяемо**: rsync → `/opt/common` + `pip install -e` в venv компонента (deps из
  `common/setup.py install_requires`; импорт `common` — через `PYTHONPATH=/opt` в юнитах).
- **Сверка версий** для инфры (`core/status`, programdata не нужна) — тот же readout
  `up-to-date / stale / missing / unreachable`, всегда как pre-flight перед деплоем.

### Changed
- `deployer.rsync_project(..., extra_excludes=[".env"])` — при инфра-деплое прод-`.env` на
  ноде НЕ затирается (рендерится отдельно из базы+БД).
- `db.get_online_nodes` теперь отдаёт колонку `claster` (для guard; доступ по имени —
  остальным веткам безвредно).
- `cli.run` — выбор режима вынесен ДО запроса папки проекта (инфра-ветка проект не требует).

### Note
- Реальный деплой требует полного коммита `Dispatcher2.0` (VERSION = git SHA; иначе DIRTY —
  на ноды поедет незакоммиченный код).
- **Reporter** пока не включён (владелец postgres:patroni_group, запуск через Patroni
  `leader_callback.sh`, не systemd — ломает модель; добавить отдельным путём).
- Форум-темы новых нод WD заводит сам на старте (`common/forum.py`, если `tg_topic_*_id`
  NULL) — ручного шага в деплое нет.

## 2026-06-26

### Changed
- **Управление сервисом — через GlobalDispatcher (§13), а не raw `watchdog_instruction`.**
  `core/watchdog.manage` больше НЕ ставит сырую инструкцию агенту на конкретной ноде —
  подаёт намерение (start/stop/restart) в `dispatcher.control_request` (`source='dm'`), а
  размещение и исполнение (с honest-verify) делает GD: `start` — лучшая нода по rang,
  `stop`/`restart` — лидер. Поэтому **выбор конкретной ноды убран** (привязки показываются
  как инфо). Перед подачей DeployManager **включает диспетчера** (`programdata.dispatcher=true`):
  GD управляет только `dispatcher=true` (иначе намерение терминируется как `NonDispatcher`).
  Исход поллится из `control_request` (completed / failed / cancelled / NonDispatcher) с
  понятным сообщением — таймаута-зависания больше нет.
- **Убраны raw-методы** `db.insert_dm_event` / `queue_instruction` / `get_instruction` и
  per-node health-check в `manage` (GD верифицирует сам). `manage(db, project_dir, command)` —
  сигнатура без `ssh`/`preselect`.

### Note
- Требует GD с регистрацией source `'dm'` (OPERATOR_SOURCES + SOURCE_LABELS→DeployManager) —
  задеплоено на cluster1/2/3 2026-06-26.

## 2026-06-17

### Fixed
- **Юниты в подкаталогах `systemd/` теперь находятся.** `list_local_services` искал только
  `systemd/*.service` (плоско) — у проектов вроде `BinodexScreens` юниты разложены по
  `systemd/OTC|Binary|Crypto/…`, поэтому в корне оставались лишь шаблоны (`@`) и «программ не
  найдено». Поиск стал рекурсивным (`systemd/**/*.service`); у `LocalService` добавлены `rel`
  (путь от `systemd/`) и `group` (каталог-порция). Имя юнита по-прежнему basename (в `/etc`
  оно плоское и уникальное); дубли имён в разных подкаталогах — предупреждение.
- **Установка юнита из подкаталога.** `Deployer.install_services` копировал
  `cp {folder}/systemd/{имя}` плоско и промахивался мимо `systemd/OTC/…`; теперь ищет источник
  по имени в дереве (`find -print -quit`). Та же правка пути учтена в хэш-сверке `sync_config`
  (локальный путь юнита берётся из `.rel`).

### Added
- **Выбор юнитов порциями по каталогам.** В деплое (`cli.select_services`) при нескольких
  подкаталогах `systemd/` — сперва чек-бокс каталогов-порций (OTC/Binary/Crypto/корень), затем
  уточнение юнитов внутри выбранных (все предотмечены, лишние снимаются). В «Управлении»
  (`core/watchdog.manage`) — выбор каталога-порции перед списком программ. Плоский `systemd/`
  (один каталог) шаг порций пропускает — поведение прежних проектов не меняется.

## 2026-06-10

### Added
- **Очистка логов на нодах** в ветке «Проверить версии» (`core/cleanup.py: clear_logs`,
  вызывается ПЕРЕД проверкой лишних файлов): на каждой развёрнутой online-ноде проекта —
  параллельный read-only поиск `*.log` (с размерами; тяжёлые каталоги не обходятся) → чек-бокс
  (все предотмечены, danger, «🧹 Обнулить»/«✖️ Отмена») → **`truncate -s 0`** отмеченных.
  Truncate, а не `rm`: у работающего сервиса дескриптор остаётся валидным (процесс пишет дальше,
  место освобождается сразу). Версия ноды НЕ сверяется (чистка логов от версии не зависит —
  достаточно наличия `VERSION`). Поддержан `--dry-run` (показывает объём к освобождению).

## 2026-06-02

### Added
- **Лёгкая доустановка юнитов** к уже развёрнутому проекту (в preflight веток «с нуля»/«добавить
  сервер»): если на ноде есть `VERSION` и КОД совпадает с локальным (sha256-сверка `verify_node`),
  то вместо полного передеплоя предлагается доставить недостающие service-файлы
  (`deployer.sync_units`: rsync `systemd/` → `cp` в `/etc` + `daemon-reload`) и настроить связи в
  `dispatcher.service_status` — без rsync кода/provision/playwright.
- **Выбор service-файлов (= программ)** перед операцией (`_select_services`, checkbox): каждый юнит
  это отдельная запись `programdata`. Шаблонные юниты (`@`) не предлагаются и не ставятся в `/etc`.
- **Отдельная сверка `requirements.txt`**: если изменился — лёгкий путь доставляет код-папку и
  ставит зависимости (`provision`: venv + `pip install -r`, без playwright).
- **Предотметка нод**: ноды со связанной программой приходят с заполненным чек-боксом
  (`ui.checkbox(default_checked=…)`).
- Хэш-сверка `verify_node` учитывает и **untracked-файлы** (`git ls-files --others --exclude-standard`).
- БД-миграции в `scripts/`: `watchdog_source_dm.sql` (разрешён `source='dm'` в
  `watchdog_instruction`) и `dm_manual_event.sql` (тип события `DM_MANUAL` в `error_types`).
- rsync: таймаут бездействия I/O (`RSYNC_TIMEOUT`, `rsync --timeout` + `asyncio.wait_for`) —
  деплой не виснет на оборванной передаче; SSH-keepalive — отвал зависших соединений.
- Валидация: проверка **абсолютности путей** (`WorkingDirectory`/`ExecStart`, `folder` в БД) —
  относительный путь → жёсткий стоп (иначе rsync/cp бьют мимо домашней папки).

### Changed
- **Ветка обновления (`update`) — только синхронизация кода, без перезапуска.** Сервисы не трогаем
  (запуск/рестарт — отдельно через «Управление»/диспетчер), чтобы не конфликтовать с
  leader/standby и политикой диспетчера (`status`, `RUNNING_DISABLED`).
- **Диалоги валидации и preflight — кнопками** (`ui.select`/`ui.confirm`), а не текстовым полем
  «впиши букву». Однозначные надписи без двусмысленных стрелок: «Записать в БД из файла».
- `provision`/playwright спрашивается только при наличии нод под полный деплой (для лёгкой
  доустановки не нужно).
- При расхождении путей файл↔БД явно подсвечивается природа (регистр/слэши/пробелы); `folder=NULL`
  обрабатывается отдельной понятной веткой.

### Fixed
- **Команды управления через watchdog (`source='dm'`) падали у агента-диспетчера** на записи в
  `error_handling_log` (`error_log_id` NOT NULL). DeployManager теперь создаёт служебное событие в
  `service_error_log` (`handled=true`, `DM_MANUAL`) и кладёт его id в `watchdog_instruction.log_id`;
  строка не видна в `service_error_view` — GD её не подхватывает (без двойных инструкций/алертов).
- Относительные (без ведущего `/`) пути в service-файле и `programdata.folder` ломали деплой
  (`cp: cannot stat …`, rsync в `~/home/vova/…`) — теперь ловятся на валидации/в `cli.run`.
- `deployer` rsync без таймаута мог виснуть на stalled-передаче; `manifest._git` молча отдавал
  `dirty=False` при сбое git; `verify` зависал на `sha256sum --` при пустом списке файлов; `audit`
  терял запись на несериализуемом поле (теперь `default=str`).
- `uninstall`: недоступные ноды не зондируются (`ping` + try), привязка в БД снимается только
  после успешного `stop/disable/rm` на ноде (не оставляем рассинхрон).

### Internal
- Дедуп: `node_flags` вынесена в `core/deploy.py` (использует `cli`/`update`); `_ssh_cmd`/`_run_rsync`
  в `deployer` (убран тройной дубль построения ssh-команды и обвязки rsync).
- Мёртвый код убран (`SERVICE_GLOB`, неиспользуемые `logger`/импорты). `core/ui.py`: `_BACKEND: Any`.
- Аудит кода (ошибки/обработка/дубли/мёртвый код/падения/зависания/оптимизация) и устранение находок.

## 2026-06-01

### Added
- **Ветка обновления** (`core/update.py`): синхронизация уже развёрнутых нод до текущей локальной
  версии (rsync кода → provision → install_services → write_version, без preflight-пропуска) +
  журнал/audit + restart через watchdog. В «Проверить версии» при рассинхроне версий всплывает
  предложение синхронизировать (диалог, только при mismatch — исчезает после синхронизации).
- **Действие `sync`** (`core/sync_config.py`, CLI `--action sync` / меню `[4]` / GUI-кнопка ♻️):
  обновить `.env` и/или service-файлы на привязанных нодах без полного передеплоя; юниты с
  `daemon-reload`; перед записью — хэш-сверка (идентичные файлы не трогаем); опц. restart.
- **Третий режим поиска при деинсталляции** — «из журнала деплоя» (`db.journal_programs()`):
  только то, что ставили этим инструментом.
- **`ui.combobox`** (GUI — выпадающий список) для выбора программы по `program_name`.
- SSH: **`PRIV_KEY`** (отдельный ключ для root) и **`SSH_KEY_PASSPHRASE`/`PRIV_KEY_PASSPHRASE`**
  (зашифрованные ключи).
- GUI: кнопка **«Очистить лог»**, крестик-закрытие в диалогах, эмодзи на кнопках.
- Деплой: per-node отчёт об установке юнитов и о привязке к нодам (с явным ❌ при сбое).

### Changed
- **rsync-исключения** режут рантайм (`*.log`, `*.session`, `files/*`), а не папки `logs/`/`files/`
  целиком; `files/.gitkeep` возвращается через `RSYNC_INCLUDES` (`--include` перед `--exclude`).
- «Проверить версии»: фактическое состояние (`running`) опрашивается и пишется в БД **по умолчанию**
  (без лишнего вопроса), дашборд показывает свежие данные.
- Лог GUI: **раскраска по каждому значению** (статус leader/standby/unavailable, run-state
  active/inactive, рассинхрон версий) — зелёный/янтарный/красный по смыслу.
- Кнопки диалогов цветные: Да/OK — светло-зелёные, Нет/Отмена — приглушённо-красные; в danger-
  диалоге «Да» — красная, «Нет» — серая. Диалоги компактные.

### Fixed
- **Сервис не стартовал на ноде** (`ImportError: cannot import name 'init_logger' from 'logs'`):
  rsync-исключение `logs/*` срезало исходники пакета `logs/__init__.py`/`log_init.py`. Теперь
  исходники в `logs/`/`files/` доезжают, рантайм — нет.
- Хэш-сверка деплоя больше не даёт ложных `[missing]` (учитывает реальные include/exclude rsync).
- SSH: понятная ошибка при невалидном ключе (какой ключ/путь/причина — не `.pub`, не PuTTY `.ppk`,
  зашифрован, права 0644) вместо голого `Invalid private key`. `asyncssh.KeyImportError`
  (подкласс `ValueError`) теперь обрабатывается.
- При обновлении версии больше не предлагается переустановка playwright (браузер уже на ноде).
