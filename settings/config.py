"""Конфигурация DeployManager (из .env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Корень проекта DeployManager (для логов/audit).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Журнал деплоев (jsonl): кто/когда/SHA/ноды/результат.
AUDIT_LOG = os.getenv("AUDIT_LOG", os.path.join(ROOT, "logs", "deploy_audit.log"))

# ----- PostgreSQL (Program, через PgBouncer) -----
PG_DATABASE = os.getenv("PG_DATABASE", "Program")
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_USER = os.getenv("PG_USER")
PG_PASSWORD = os.getenv("PG_PASSWORD")
PG_PORT = int(os.getenv("PG_PORT", "6442"))

# ----- Telegram Desktop клиенты (суб-инструмент «Юзерботы (сессии)») -----
# TELEGRAM_APPS="<поле-FK в telegram.telegram>,<таблица-справочник в схеме telegram>".
# Первое — колонка с app_name клиента (по ней знаем, какой Desktop поднять при логине юзербота),
# второе — справочник клиентов (app_name PK, exec_path, workdir, icon, is_system). Дефолт рабочий.
_tg_apps = [p.strip() for p in os.getenv("TELEGRAM_APPS", "my_gram,telegram_apps").split(",")]
TG_APP_FIELD = _tg_apps[0] if _tg_apps and _tg_apps[0] else "my_gram"
TG_APPS_TABLE = _tg_apps[1] if len(_tg_apps) > 1 and _tg_apps[1] else "telegram_apps"

# ----- SSH (vova + passwordless sudo) -----
SSH_USER = os.getenv("SSH_USER", "vova")
SSH_KEY = str(Path(os.getenv("SSH_KEY", "~/.ssh/id_nodes")).expanduser())
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_CONNECT_TIMEOUT = int(os.getenv("SSH_CONNECT_TIMEOUT", "10"))
# rsync: таймаут бездействия I/O (сек) — обрывает зависшую передачу, чтобы деплой не висел вечно.
RSYNC_TIMEOUT = int(os.getenv("RSYNC_TIMEOUT", "120"))
# Пользователь для привилегированных шагов (юниты в /etc, systemctl). Если задан
# (напр. root) — вход под ним без sudo; пусто — текущий SSH_USER + passwordless sudo.
PRIV_USER = os.getenv("PRIV_USER", "")
# Отдельный приватный ключ для PRIV_USER (если ключ root лежит в другом месте, не как у vova).
# Пусто — используется общий SSH_KEY.
_priv_key = os.getenv("PRIV_KEY", "").strip()
PRIV_KEY = str(Path(_priv_key).expanduser()) if _priv_key else ""
# Пароли к зашифрованным ключам (если ключ под passphrase). Пусто — ключ без пароля.
SSH_KEY_PASSPHRASE = os.getenv("SSH_KEY_PASSPHRASE", "") or None
PRIV_KEY_PASSPHRASE = os.getenv("PRIV_KEY_PASSPHRASE", "") or None

# ----- systemd -----
SYSTEMD_DIR = "/etc/systemd/system"

# ----- rsync -----
# Что НЕ переносить на сервер. .env НЕ исключаем — его деплоим обязательно (.env.example — нет).
# ВАЖНО: в logs/ и files/ лежат и ИСХОДНИКИ пакета (logs/*.py, files/.gitkeep — нужны приложению),
# и рантайм (*.log, *.session). Режем именно рантайм:
#   *.log      — любые логи (раньше было logs/* — срезало и logs/__init__.py → краш `import logs`);
#   *.session  — сессии (Telethon-авторизация ноды); приложения работают и без них, не деплоим;
#   files/*    — прочий рантайм files/ (кроме .gitkeep — он в RSYNC_INCLUDES, чтобы папка создалась).
RSYNC_EXCLUDES = [
    ".git", ".git.backup", ".venv", "venv", "*.log", "*.session", "files/*",
    "__pycache__", "*.pyc", "*.egg-info", ".idea", "pictures/new",
    "*.md", ".env.example",
    ".claude", ".directory", ".vscode",     # dev-артефакты редакторов/инструментов — не деплоим
]
# NB: rsync-паттерн без слэша/wildcard матчит ТОЧНОЕ имя компонента — ".git" НЕ ловит ".git.backup"
# (и любые .git-* ). Бэкап git-папки приходится исключать отдельной записью, иначе он уезжает на ноду.
# Что вернуть обратно, даже если попало под exclude (структурный маркер рантайм-папки files/).
# --include идут ПЕРЕД --exclude (rsync: первое совпавшее правило выигрывает).
RSYNC_INCLUDES = ["files/.gitkeep"]
RSYNC_DELETE = os.getenv("RSYNC_DELETE", "0").strip().lower() in ("1", "true", "yes", "on")

# Имя файла-манифеста версии на сервере (git SHA + метаданные деплоя).
VERSION_FILE = "VERSION"

# Стартовая папка для диалога «Обзор…» в GUI (где лежат проекты).
PROJECTS_DIR = str(Path(os.getenv("PROJECTS_DIR", "~/PythonProjects")).expanduser())

# Корень репозитория Dispatcher2.0 (control-plane: GD/WD/CD/DispatcherCtl + common).
# Отдельный от PROJECTS_DIR ключ — чтобы DM настраивался на ЛЮБОЙ машине, где
# Dispatcher2.0 лежит не под ~/PythonProjects или под другим именем. По умолчанию —
# PROJECTS_DIR/Dispatcher2.0 (обратная совместимость, ключ можно не задавать).
DISPATCHER_DIR = str(Path(
    os.getenv("DISPATCHER_DIR", os.path.join(PROJECTS_DIR, "Dispatcher2.0"))
).expanduser())

# ----- Provisioning на ноде (последовательность из README/DEPLOY, перенесённая в код) -----
# venv → pip install -U pip → pip install -r requirements.txt → playwright install firefox
PROVISION = os.getenv("PROVISION", "1").strip().lower() in ("1", "true", "yes", "on")
PYTHON_BIN = os.getenv("PYTHON_BIN", "python3.11")   # интерпретатор для создания venv
VENV_DIR = os.getenv("VENV_DIR", "venv")             # имя venv в каталоге проекта (на сервере)
# Браузер для playwright (если он есть в requirements) — предлагается доустановить.
PLAYWRIGHT_BROWSER = os.getenv("PLAYWRIGHT_BROWSER", "firefox")
PROVISION_TIMEOUT = int(os.getenv("PROVISION_TIMEOUT", "900"))  # сек (pip + загрузка браузера)
