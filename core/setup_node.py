"""«Настроить ноду» — turnkey-ввод нового узла флота (фаза 1: обычный узел).

Пошаговый мастер с диалогом на КАЖДЫЙ шаг (Выполнить / Пропустить / Отмена мастера) и
resume-ЖУРНАЛОМ (logs/setup_node/<ip>.json): первый запуск пишет введённые данные + прогресс,
повторный — спрашивает ТОЛЬКО IP, грузит журнал и продолжает с первого не-выполненного шага.

Шаги:
  base (provision-base.sh --step <id>, идут по SSH):
    user (vova+root-ключи+polkit — ПЕРВЫЙ, единственный требует пароль) → hostname → locales
    → packages → python311 → sshd → fail2ban → ufw → dropins → haproxy
  фазы клиента (Python-действия):
    client_tail (haproxy_client) → whitelist (по флоту) → register (vocabulary.nodes) → wd (Watchdog+online)

Auth: `user` кладёт ключи vova И root → дальнейшие шаги идут по ключу. На resume, если ключ уже
пускает, пароль не запрашивается вовсе. Тип «Элемент кластера» после base уходит в фазу-2 визард.
"""
import getpass
import ipaddress
import os
import shlex
import shutil
import tempfile

from core import audit, infra_deploy, setup_state, ui
from core.deploy import print_deploy_results
from core.scripts import _gen_nodes_sh, _run_local
from database.db import Database
from classes.ssh_client import SshClient
from logs import get_logger
from settings import config

logger = get_logger(__name__)

_PRIV = config.PRIV_USER or "root"
_REMOTE_BASE = "/root/provision-base.sh"
_REMOTE_CLIENT = "/root/provision-client.sh"
_REMOTE_SWEEP = "/root/pw_lock_sweep.sh"

# base-шаги: (id, заголовок, пояснение, длинный_ли_прогон)
_BASE_STEPS = [
    ("user", "Пользователь vova + ключи (vova и root) + polkit",
     "Создаёт vova, кладёт SSH-ключ для vova И root (root по ключу нужен всему флоту для "
     "юнитов/systemctl), даёт polkit-грант на systemctl. ПЕРВЫЙ шаг — после него мастер "
     "работает по ключу без пароля.", False),
    ("hostname", "Hostname + timezone",
     "Задаёт системный hostname узла и timezone Europe/Moscow.", False),
    ("locales", "Локали ru_RU + en_US",
     "Генерирует локали ru_RU/en_US — критично для совместимости с кластером.", False),
    ("packages", "Базовые пакеты (apt)",
     "Ставит build-стек, сеть и утилиты: git/curl/ufw/fail2ban/build-essential/ethtool/…", True),
    ("python311", "Python 3.11 (deadsnakes)",
     "Ставит python3.11 — venv-стандарт флота для WD/GD/CD (на Ubuntu 24.04 дефолт 3.12, "
     "без 3.11 деплой WD падает).", True),
    ("sshd", "SSHd hardening",
     "MaxSessions/MaxStartups/ClientAlive*, перезапуск ssh.", False),
    ("fail2ban", "fail2ban",
     "jail.local (systemd backend, ignoreip) + enable/restart.", False),
    ("ufw", "UFW (только SSH)",
     "deny incoming, allow 22, enable. Ролевые порты (БД) откроет шаг whitelist.", False),
    ("dropins", "Drop-in'ы (needrestart / GPU / tmpfiles / nic)",
     "needrestart (не авто-рестартить демоны) + apt GPU-blacklist + tmpfiles-уборка "
     "браузер-профилей + nic-ring-tune.", False),
    ("haproxy", "HAProxy 3.1.0 из исходников",
     "Скачивает (по IPv4) и собирает бинарь haproxy — общий для клиента и члена кластера.", True),
]


def _scripts() -> tuple[str, str, str, str] | None:
    """Пути к вендоренным bash-примитивам (base, client, whitelist, sweep). None — чего-то нет."""
    from core.scripts import BUNDLED_DIR   # ленивый импорт — уважает monkeypatch в тестах
    base, client, wl, sweep = (os.path.join(BUNDLED_DIR, f) for f in
                               ("provision-base.sh", "provision-client.sh",
                                "whitelist-ip.sh", "pw_lock_sweep.sh"))
    for p in (base, client, wl, sweep):
        if not os.path.isfile(p):
            print(f"🛑 Нет вендоренного скрипта: {p}")
            return None
    return base, client, wl, sweep


def _read_pubkey() -> str | None:
    pub = config.SSH_KEY + ".pub"
    if not os.path.isfile(pub):
        print(f"🛑 Нет публичного ключа {pub} — нужен для authorized_keys vova И root "
              f"(рядом с SSH_KEY={config.SSH_KEY})")
        return None
    return open(pub, encoding="utf-8").read().strip()


async def _field(prompt: str, default: str = "") -> str | None:
    v = await ui.ask(prompt, default, cancelable=True)
    return None if v is None else v.strip()


async def run_setup_node(db: Database, ssh: SshClient, *, dry_run: bool = False) -> None:
    tag = "[DRY] " if dry_run else ""
    paths = _scripts()
    if not paths:
        return
    base_sh, client_sh, whitelist_sh, sweep_sh = paths
    vova_pubkey = _read_pubkey()
    if vova_pubkey is None:
        return

    # ── 1. IP (всегда спрашиваем первым) ──
    ip = await _field("IP узла")
    if ip is None:
        print("Отмена.")
        return
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        print(f"🛑 Некорректный IP: {ip!r}")
        return

    # ── 2. журнал: resume или первый запуск ──
    journal = setup_state.load(ip)
    if journal:
        done = [s for s, v in journal.get("steps", {}).items() if v == setup_state.DONE]
        print(f"\n📒 Журнал найден: {journal['server_name']} ({journal['hostname']}), "
              f"тип={journal['node_type']}. Выполнено шагов: {len(done)}. Продолжаю с незакрытых.")
        hostname = journal["hostname"]
        server_name = journal["server_name"]
        node_type = journal["node_type"]
    else:
        # первый запуск — опрашиваем и СРАЗУ пишем журнал (повторно спрашивать не будем)
        existing = await db.find_node_by_ip(ip)
        hostname = await _field("Системный hostname (напр. n-node8)",
                                (existing or {}).get("hostname", ""))
        if hostname is None:
            print("Отмена.")
            return
        if not hostname:
            print("🛑 hostname обязателен.")
            return
        server_name = await _field("Имя ноды (server_name — дисплей в отчётах)",
                                   (existing or {}).get("server_name", "") or hostname)
        if server_name is None:
            print("Отмена.")
            return
        idx = await ui.select(
            "Тип ноды:",
            ["Обычный узел (Playwright/боты, claster=false)", "Элемент кластера (Patroni/etcd)"],
            details=["haproxy_client + Watchdog + регистрация", "фаза 2: пошаговый визард замены"],
        )
        if idx is None:
            print("Отмена.")
            return
        node_type = "cluster" if idx == 1 else "client"
        if dry_run:
            # dry-run НЕ персистит журнал — только in-memory для прохода по шагам.
            journal = {"ip": ip, "hostname": hostname, "server_name": server_name,
                       "node_type": node_type, "node_id": None, "steps": {}}
        else:
            journal = setup_state.create(ip, hostname, server_name, node_type)
            print(f"📒 Журнал заведён: logs/setup_node/{ip}.json")

    print(f"\n{tag}━━━ Настройка узла: {server_name} ({hostname}) @ {ip} · тип={node_type} ━━━\n")

    # ── 3. доступ: ключ (resume) или пароль (только для шага user на первом прогоне) ──
    key_ok = False if dry_run else await ssh.ping(ip)
    password = None
    need_password = (not key_ok) and not setup_state.is_done(journal, "user")
    if need_password and not dry_run:
        password = await ui.ask("Пароль root узла (нужен только для шага 'user')", cancelable=True)
        if password is None:
            print("Отмена.")
            return
        if not password:
            print("🛑 Нужен пароль root для первого коннекта (шаг 'user').")
            return
    elif key_ok:
        print("⏩ key-доступ уже есть — пароль не требуется.")

    # состояние прогона (мутируется по ходу)
    ctx = {"key": key_ok, "uploaded": False}

    async def _ensure_uploaded() -> bool:
        if ctx["uploaded"]:
            return True
        if not await ssh.upload(ip, base_sh, _REMOTE_BASE, user=_PRIV, mode=0o755):
            print("🛑 Не удалось залить provision-base.sh.")
            return False
        ctx["uploaded"] = True
        return True

    async def _run_base(step_id: str, timeout: int) -> bool:
        args = f"--hostname {shlex.quote(hostname)} --vova-pubkey {shlex.quote(vova_pubkey)}"
        cmd = f"bash {_REMOTE_BASE} --step {step_id} {args}"
        if step_id == "user" and not ctx["key"]:
            # единственный парольный коннект: заливает скрипт и кладёт ключи vova+root
            r = await ssh.bootstrap_run(ip, password, [(base_sh, _REMOTE_BASE)], cmd, 300, echo=print)
            if r.ok:
                ctx["key"] = True          # ключи легли — дальше по ключу
                ctx["uploaded"] = True
            else:
                print(f"🛑 {r.stderr or ('exit ' + str(r.exit_status))}")
            return r.ok
        if not await _ensure_uploaded():
            return False
        r = await ssh.run_stream(ip, cmd, timeout=timeout, echo=print, user=_PRIV)
        if not r.ok:
            print(f"🛑 {r.stderr or ('exit ' + str(r.exit_status))}")
        return r.ok

    # ── фазы клиента (Python-действия) ──
    async def _act_client_tail() -> bool:
        for local, remote in ((sweep_sh, _REMOTE_SWEEP), (client_sh, _REMOTE_CLIENT)):
            if not await ssh.upload(ip, local, remote, user=_PRIV, mode=0o755):
                print(f"🛑 Не удалось залить {os.path.basename(local)}.")
                return False
        r = await ssh.run_stream(ip, f"bash {_REMOTE_CLIENT} --tail-only",
                                 timeout=300, echo=print, user=_PRIV)
        if r.ok:
            st = await ssh.run(ip, "systemctl is-active haproxy_client", user=_PRIV)
            print(f"   haproxy_client: {st.stdout or st.stderr or '?'}")
        return r.ok

    async def _act_whitelist() -> bool:
        ports = config.SETUP_CLIENT_PORTS
        nodes = await db.get_online_nodes()
        tmp = tempfile.mkdtemp(prefix="wl_setup_")
        try:
            shutil.copy(whitelist_sh, tmp)
            with open(os.path.join(tmp, "_nodes.sh"), "w", encoding="utf-8") as f:
                f.write(_gen_nodes_sh(nodes))
            os.chmod(os.path.join(tmp, "whitelist-ip.sh"), 0o755)
            rc = await _run_local(["bash", "whitelist-ip.sh", ip, "--ports", ports, "--apply"], cwd=tmp)
            return rc == 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _act_register() -> bool:
        existing = await db.find_node_by_ip(ip)
        if existing:
            setup_state.set_field(journal, "node_id", existing["id"])
            print(f"   ⏩ уже в vocabulary.nodes id={existing['id']}")
            return True
        node_id = await db.create_node(hostname, ip, server_name, claster=False)
        setup_state.set_field(journal, "node_id", node_id)
        print(f"   ✅ vocabulary.nodes id={node_id}")
        return True

    async def _act_wd() -> bool:
        node = await db.find_node_by_ip(ip)
        if not node:
            print("   🛑 узел не зарегистрирован (шаг register) — WD ставить некуда.")
            return False
        res = await infra_deploy.deploy_component_to_node(ssh, node, component="WD")
        print_deploy_results([res])
        if res.ok:
            await db.set_node_online(node["id"], True)
            print("   ✅ Watchdog развёрнут, узел online.")
            return True
        print(f"   ⚠️ WD-деплой не прошёл ({res.step}: {res.detail}).")
        return False

    _CLIENT_STEPS = [
        ("client_tail", "Ролевой client-хвост (haproxy_client)",
         "provision-client.sh --tail-only: настраивает haproxy_client — доступ узла к БД кластера.",
         _act_client_tail),
        ("whitelist", "Whitelist по флоту",
         f"Открывает на всех online-нодах UFW-allow с {ip} на порты «{config.SETUP_CLIENT_PORTS}» + "
         f"fail2ban ignoreip. Без него WD не достучится до БД кластера.", _act_whitelist),
        ("register", "Регистрация в vocabulary.nodes",
         "INSERT узла (claster=false) — поздняя, только по здоровому узлу. Идемпотентно.",
         _act_register),
        ("wd", "Деплой Watchdog + online",
         "Раскатывает WD (код+common+venv+юниты+.env); при успехе выставляет is_online=true.",
         _act_wd),
    ]

    # ── единый шаговый цикл с диалогом + журналом ──
    async def _do_step(step_id: str, title: str, explain: str, runner) -> str:
        """Возврат: 'ok' | 'cancel' | 'fail'. Уже-done по журналу → авто-пропуск."""
        if setup_state.is_done(journal, step_id):
            print(f"⏩ [{title}] уже выполнен (журнал) — пропуск.")
            return "ok"
        prompt = f"{title}\n\n{explain}"
        if dry_run:
            print(f"{tag}шаг '{step_id}': {title}")
            return "ok"
        idx = await ui.select(prompt, ["Выполнить", "Пропустить", "Отмена мастера"],
                              colors=["green", "teal", "red"])
        if idx is None or idx == 2:
            print("⏹ Отмена. Прогресс в журнале — перезапуск продолжит отсюда.")
            return "cancel"
        if idx == 1:
            setup_state.set_step(journal, step_id, setup_state.SKIPPED)
            print(f"⏭ [{title}] пропущен оператором.")
            return "ok"
        print(f"\n━━━ {title} ━━━")
        try:
            ok = await runner()
        except Exception as e:                 # noqa: BLE001 — любой сбой шага фиксируем в журнал
            logger.exception("шаг %s упал", step_id)
            print(f"🛑 исключение: {e}")
            ok = False
        setup_state.set_step(journal, step_id, setup_state.DONE if ok else setup_state.FAILED)
        if not ok:
            print(f"🛑 [{title}] не удалось. Журнал сохранён — перезапуск продолжит отсюда.")
        return "ok" if ok else "fail"

    # base-шаги
    for step_id, title, explain, is_long in _BASE_STEPS:
        timeout = config.SETUP_BOOTSTRAP_TIMEOUT if is_long else 300
        res = await _do_step(step_id, title, explain,
                             lambda sid=step_id, to=timeout: _run_base(sid, to))
        if res != "ok":
            return

    # ветка по типу
    if node_type == "cluster":
        print("\n━━━ Элемент кластера → фаза-2 визард ━━━")
        if not dry_run:
            from core.setup_cluster_member import run_setup_cluster_member
            await run_setup_cluster_member(db, ssh, new_ip=ip, hostname=hostname, dry_run=dry_run)
        return

    # фазы клиента
    for step_id, title, explain, runner in _CLIENT_STEPS:
        res = await _do_step(step_id, title, explain, runner)
        if res != "ok":
            return

    audit.write({
        "action": "setup-node", "type": node_type, "dry_run": dry_run,
        "ip": ip, "hostname": hostname, "server_name": server_name,
        "node_id": journal.get("node_id"), "operator": getpass.getuser(),
    })
    print(f"\n{tag}✅ Готово: {server_name} ({ip}) настроен как обычный узел.")


__all__ = ["run_setup_node"]
