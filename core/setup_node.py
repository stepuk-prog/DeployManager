"""«Настроить ноду» — turnkey-ввод нового узла флота (фаза 1: обычный узел).

Контур (согласован с Vlad):
  форма(IP, root-pass, hostname, server_name) → гард дубля по IP
  0. base bootstrap: одноразовый ПАРОЛЬНЫЙ коннект root@IP → залить provision-base.sh →
     прогнать (создаёт vova+ключ, hardening, HAProxy-бинарь; ВЫКЛЮЧАЕТ password-auth).
  1. ДИАЛОГ «тип ноды»: [Обычный узел] | [Элемент кластера → ⛧ заглушка фазы 1].
  2. ролевой client-хвост: provision-client.sh --tail-only (по ключу root) → haproxy_client.
  3. verify: key-доступ vova + haproxy_client active.
  4. whitelist: ПОКАЗАТЬ команду whitelist-ip.sh → по подтверждению прогнать по флоту.
  5. регистрация (поздняя!): INSERT vocabulary.nodes (claster=false) — только по здоровому узлу.
  6. deploy Watchdog на эту ноду (движок infra_deploy) → is_online=true.
  + audit-запись.

Повторный прогон безопасен: если key-доступ vova уже есть (база отработала, password-auth
уже off) — фаза 0 пропускается.
"""
import asyncio
import getpass
import ipaddress
import os
import shlex

from core import audit, infra_deploy, ui
from core.deploy import print_deploy_results
from database.db import Database
from classes.ssh_client import SshClient
from logs import get_logger
from settings import config

logger = get_logger(__name__)

_PRIV = config.PRIV_USER or "root"        # под кем привилегированные операции по ключу
_REMOTE_BASE = "/root/provision-base.sh"
_REMOTE_CLIENT = "/root/provision-client.sh"
_REMOTE_SWEEP = "/root/pw_lock_sweep.sh"   # рядом с client — provision-client зовёт через $DIR


def _scripts() -> tuple[str, str, str, str] | None:
    """Пути к ВЕНДОРЕННЫМ bash-примитивам в assets/fleet_scripts (DM самодостаточен —
    не зависит от наличия репозитория Clusters). None — если чего-то нет.
    pw_lock_sweep.sh кладётся рядом с provision-client.sh на узле (тот зовёт его через $DIR)."""
    from core.scripts import BUNDLED_DIR
    base, client, wl, sweep = (os.path.join(BUNDLED_DIR, f) for f in
                               ("provision-base.sh", "provision-client.sh",
                                "whitelist-ip.sh", "pw_lock_sweep.sh"))
    for p in (base, client, wl, sweep):
        if not os.path.isfile(p):
            print(f"🛑 Нет вендоренного скрипта: {p}")
            return None
    return base, client, wl, sweep


def _read_pubkey() -> str | None:
    """Публичный ключ vova (SSH_KEY + .pub) для раскладки authorized_keys на новом узле."""
    pub = config.SSH_KEY + ".pub"
    if not os.path.isfile(pub):
        print(f"🛑 Нет публичного ключа {pub} — нужен для vova authorized_keys "
              f"(рядом с SSH_KEY={config.SSH_KEY})")
        return None
    return open(pub, encoding="utf-8").read().strip()


async def _run_local(cmd: list[str], cwd: str) -> int:
    """Локальный subprocess со стримингом вывода в лог-панель (stdout+stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    assert proc.stdout is not None
    async for raw in proc.stdout:
        print(raw.decode("utf-8", "replace").rstrip("\n"))
    await proc.wait()
    return proc.returncode if proc.returncode is not None else 1


async def run_setup_node(db: Database, ssh: SshClient, *, dry_run: bool = False) -> None:
    tag = "[DRY] " if dry_run else ""
    paths = _scripts()
    if not paths:
        return
    base_sh, client_sh, whitelist_sh, sweep_sh = paths
    scripts_dir = os.path.dirname(base_sh)
    vova_pubkey = _read_pubkey()
    if vova_pubkey is None:
        return

    # ── форма (у каждого поля есть «Отмена» → выход из мастера; ничего ещё не тронуто) ──
    async def _field(prompt: str, default: str = "") -> str | None:
        """Строковое поле формы с «Отмена». None = оператор отменил мастер (выше — return)."""
        v = await ui.ask(prompt, default, cancelable=True)
        return None if v is None else v.strip()

    ip = await _field("IP нового узла")
    if ip is None:
        print("Отмена.")
        return
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        print(f"🛑 Некорректный IP: {ip!r}")
        return
    dup = await db.find_node_by_ip(ip)
    if dup:
        print(f"🛑 Узел с IP {ip} уже в vocabulary.nodes "
              f"(id={dup['id']}, {dup['server_name'] or dup['hostname']}). Отмена.")
        return
    hostname = await _field("Системный hostname (напр. n-node8)")
    if hostname is None:
        print("Отмена.")
        return
    if not hostname:
        print("🛑 hostname обязателен.")
        return
    server_name = await _field("Имя ноды в системе (server_name — дисплей в отчётах)", hostname)
    if server_name is None:
        print("Отмена.")
        return
    password = await ui.ask("Пароль root нового узла (только первый коннект)", cancelable=True)
    if password is None:
        print("Отмена.")
        return
    if not dry_run and not password:
        print("🛑 Нужен пароль root для первого (парольного) коннекта.")
        return

    print(f"\n{tag}━━━ Настройка узла: {server_name} ({hostname}) @ {ip} ━━━\n")

    # ── 0. базовый bootstrap (пароль) ──
    base_cmd = (f"bash {_REMOTE_BASE} --hostname {shlex.quote(hostname)} "
                f"--vova-pubkey {shlex.quote(vova_pubkey)}")
    if dry_run:
        print(f"{tag}0. bootstrap root@{ip} (пароль): upload {base_sh} → {_REMOTE_BASE}; run:\n   {base_cmd}")
    elif await ssh.ping(ip):
        print(f"⏩ key-доступ vova@{ip} уже есть — базовый bootstrap пропускаю (повторный прогон).")
    else:
        print(f"━━━ 0/6 базовый bootstrap (root@{ip} по паролю) ━━━")
        r = await ssh.bootstrap_run(ip, password, [(base_sh, _REMOTE_BASE)],
                                    base_cmd, config.SETUP_BOOTSTRAP_TIMEOUT, echo=print)
        if not r.ok:
            print(f"🛑 base bootstrap не удался: {r.stderr or ('exit ' + str(r.exit_status))}")
            return

    # ── 1. диалог типа ──
    idx = await ui.select(
        "Тип ноды:",
        ["Обычный узел (Playwright/боты, claster=false)", "Элемент кластера (Patroni/etcd)"],
        details=["haproxy_client + Watchdog + регистрация", "⛧ в разработке (фаза 2)"],
    )
    if idx is None:
        print("Отмена (база настроена).")
        return
    if idx == 1:
        # ФАЗА 2: член кластера (Patroni/etcd) — пошаговый визард (база уже настроена выше).
        from core.setup_cluster_member import run_setup_cluster_member
        await run_setup_cluster_member(db, ssh, new_ip=ip, hostname=hostname, dry_run=dry_run)
        return

    # ── ОБЫЧНЫЙ УЗЕЛ ──
    # 2. ролевой client-хвост (по ключу root)
    client_cmd = f"bash {_REMOTE_CLIENT} --tail-only"
    if dry_run:
        print(f"{tag}2. upload {client_sh} → {ip}:{_REMOTE_CLIENT} "
              f"(+ {sweep_sh} → {_REMOTE_SWEEP}); run({_PRIV}): {client_cmd}")
    else:
        print("━━━ 2/6 ролевой client-хвост (haproxy_client) ━━━")
        # pw_lock_sweep.sh кладём РЯДОМ (provision-client установит его в /usr/local/bin через $DIR)
        if not await ssh.upload(ip, sweep_sh, _REMOTE_SWEEP, user=_PRIV, mode=0o755):
            print("🛑 Не удалось залить pw_lock_sweep.sh.")
            return
        if not await ssh.upload(ip, client_sh, _REMOTE_CLIENT, user=_PRIV, mode=0o755):
            print("🛑 Не удалось залить provision-client.sh.")
            return
        r = await ssh.run_stream(ip, client_cmd, timeout=300, echo=print, user=_PRIV)
        if not r.ok:
            print(f"🛑 client-хвост не удался: {r.stderr or ('exit ' + str(r.exit_status))}")
            return

    # 3. verify узла
    if dry_run:
        print(f"{tag}3. verify: ssh ping vova@{ip} + systemctl is-active haproxy_client")
    else:
        print("━━━ 3/6 проверка узла ━━━")
        if not await ssh.ping(ip):
            print(f"🛑 Нет key-доступа vova@{ip} после провижина — стоп.")
            return
        st = await ssh.run(ip, "systemctl is-active haproxy_client", user=_PRIV)
        print(f"   haproxy_client: {st.stdout or st.stderr or '?'}")

    # 4. whitelist — ПОКАЗАТЬ и прогнать по подтверждению
    ports = config.SETUP_CLIENT_PORTS
    wl_display = f"scripts/whitelist-ip.sh {ip} --ports \"{ports}\" --apply"
    print(f"\n{tag}4. Whitelist IP на кластере (доступ узла к БД). Команда:\n   {wl_display}")
    if not dry_run:
        if await ui.confirm(f"Прогнать whitelist для {ip} (порты: {ports}) по всему флоту?"):
            rc = await _run_local(["bash", whitelist_sh, ip, "--ports", ports, "--apply"],
                                  cwd=scripts_dir)
            print("   ✅ whitelist применён." if rc == 0 else f"   ⚠️ whitelist rc={rc} (проверь вывод).")
        else:
            print("   ⏩ whitelist пропущен — прогони вручную ДО старта WD (иначе WD не достучится до БД).")

    # 5. регистрация в системе (поздняя — узел уже настроен)
    node_id = None
    if dry_run:
        print(f"{tag}5. INSERT vocabulary.nodes(hostname={hostname}, ip={ip}, "
              f"server_name={server_name}, claster=false)")
    else:
        print("━━━ 5/6 регистрация в системе ━━━")
        node_id = await db.create_node(hostname, ip, server_name, claster=False)
        print(f"   ✅ vocabulary.nodes id={node_id}")

    # 6. деплой Watchdog + запуск + online
    if dry_run:
        print(f"{tag}6. infra_deploy WD → узел → /health → is_online=true")
    else:
        print("━━━ 6/6 деплой Watchdog ━━━")
        node = await db.find_node_by_ip(ip)          # свежая запись (id/server_name/claster)
        res = await infra_deploy.deploy_component_to_node(ssh, node, component="WD")
        print_deploy_results([res])
        if res.ok:
            await db.set_node_online(node_id, True)
            print("   ✅ Watchdog развёрнут, узел online.")
        else:
            print(f"   ⚠️ WD-деплой не прошёл ({res.step}: {res.detail}). Узел зарегистрирован — "
                  f"доставь WD вручную через ветку «Инфра-компонент».")

    audit.write({
        "action": "setup-node", "type": "client", "dry_run": dry_run,
        "ip": ip, "hostname": hostname, "server_name": server_name, "node_id": node_id,
        "operator": getpass.getuser(),
    })
    print(f"\n{tag}✅ Готово: {server_name} ({ip}) настроен как обычный узел.")


__all__ = ["run_setup_node"]
