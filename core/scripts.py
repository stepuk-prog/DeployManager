"""Дополнительные операционные скрипты флота — кнопками в DM.

DM самодостаточен: скрипты ВЕНДОРЕНЫ в `assets/fleet_scripts/` (не ссылки на
Clusters), а реестр узлов `_nodes.sh` ГЕНЕРИТСЯ на лету из `vocabulary.nodes`
(БД — источник правды топологии). Внешних зависимостей от других проектов нет.

Декларативный реестр `SCRIPTS`: запись описывает bash-скрипт, его scope,
аргументы (промпты → флаги) и apply-гейт. Новый скрипт = одна запись + файл в
assets, без правок cli/gui (кнопки строятся из реестра, как `tools.TOOLS`).

Два scope:
  • "local"  — скрипт сам обходит флот (`source _nodes.sh` + ssh root@ip). Гоняем
               ЛОКАЛЬНО во временной папке: копия скрипта + сгенерённый из БД
               `_nodes.sh`. Для apply-скриптов: dry-run → подтверждение → --apply.
  • "node"   — одно-хостовый скрипт: пикер узла (или «все клиентские») → upload
               по SFTP → запуск под root на узле, стрим в лог.

Аргументы (ArgSpec):
  • позиционный (flag=None)            → значение как есть
  • со значением (flag="--x")          → "--x", значение
  • булев (flag="--x", kind="bool")    → confirm; при «да» добавляется "--x"
  validate="ip" — проверка IPv4/IPv6; default — подстановка при пустом вводе.
"""
import asyncio
import getpass
import ipaddress
import os
import shlex
import shutil
import tempfile

from core import audit, ui
from database.db import Database
from classes.ssh_client import SshClient
from logs import get_logger
from settings import config

logger = get_logger(__name__)

_PRIV = config.PRIV_USER or "root"      # под кем гоняем node-scope скрипты (root по ключу)
# Вендоренные скрипты внутри DM (пакет), НЕ во внешнем репозитории.
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fleet_scripts")

# ── реестр ────────────────────────────────────────────────────────────────────
SCRIPTS: list[dict] = [
    {
        "key": "pw_sweep",
        "label": "Playwright-свип",
        "icon": "🧹",
        "color": "TEAL_600",
        "file": "pw_lock_sweep.sh",
        "scope": "node",
        "nodes": "clients",            # одно-хостовый; предлагаем клиентские узлы
        "args": [],
        "apply_flag": None,
        "danger": False,
        "desc": "Свип висячих Playwright-локов (идемпотентно, безопасно) на узле/клиентах.",
    },
    {
        "key": "audit_cluster",
        "label": "Аудит кластера",
        "icon": "🩺",
        "color": "TEAL_600",
        "file": "audit-cluster.sh",
        "scope": "local",
        "args": [],
        "apply_flag": None,            # read-only, гейт не нужен
        "danger": False,
        "desc": "Read-only свод здоровья: etcd/Patroni/HAProxy/systemd по всему флоту.",
    },
    {
        "key": "whitelist_ip",
        "label": "Whitelist IP",
        "icon": "🛡️",
        "color": "TEAL_600",
        "file": "whitelist-ip.sh",
        "scope": "local",
        "args": [
            {"prompt": "IP для whitelist", "flag": None, "validate": "ip"},
            {"prompt": "Порты (через пробел)", "flag": "--ports", "default": "22"},
        ],
        "apply_flag": "--apply",       # dry-run → подтверждение → apply
        "danger": False,
        "desc": "fail2ban ignoreip + UFW allow для IP по всему флоту (идемпотентно).",
    },
    {
        "key": "swap_node_ip",
        "label": "Смена IP узла",
        "icon": "🔀",
        "color": "AMBER_700",
        "file": "swap-node-ip.sh",
        "scope": "local",
        "args": [
            {"prompt": "OLD IP (старый)", "flag": "--old", "validate": "ip"},
            {"prompt": "NEW IP (новый)", "flag": "--new", "validate": "ip"},
            {"prompt": "Перезапустить сервисы после смены (--reload)?",
             "flag": "--reload", "kind": "bool"},
        ],
        "apply_flag": "--apply",
        "danger": True,                # правит конфиги Patroni/etcd/pg_hba на cluster-нодах
        "desc": "Заменить OLD_IP→NEW_IP в конфигах живых cluster-нод (бэкап+sweep).",
    },
]

SCRIPT_KEYS = [s["key"] for s in SCRIPTS]


def get_script(key: str) -> dict | None:
    return next((s for s in SCRIPTS if s["key"] == key), None)


def _script_path(spec: dict) -> str | None:
    """Путь к вендоренному скрипту в assets/fleet_scripts. None — если файла нет."""
    p = os.path.join(BUNDLED_DIR, spec["file"])
    if not os.path.isfile(p):
        print(f"🛑 Нет вендоренного скрипта: {p}")
        return None
    return p


# ── реестр узлов из БД (заменяет внешний _nodes.sh) ───────────────────────────
def _gen_nodes_sh(nodes: list) -> str:
    """Сгенерировать _nodes.sh (CLUSTER_IPS/CLIENT_IPS/ALL_IPS/IP_NAME/ssh_node)
    из vocabulary.nodes — чтобы local-скрипты обходили флот без внешнего файла."""
    cluster = [n for n in nodes if n["claster"]]
    clients = [n for n in nodes if not n["claster"]]
    name = lambda n: n["server_name"] or n["hostname"]
    cl = " ".join(shlex.quote(n["ip_address"]) for n in cluster)
    cli = " ".join(shlex.quote(n["ip_address"]) for n in clients)
    ip_name = "\n".join(f"  [{shlex.quote(n['ip_address'])}]={shlex.quote(name(n))}"
                        for n in nodes)
    return (
        "#!/usr/bin/env bash\n"
        "# СГЕНЕРИРОВАНО DeployManager из vocabulary.nodes (online) — НЕ редактировать.\n"
        "# DM самодостаточен: топология берётся из БД, не из внешнего репозитория.\n"
        f"CLUSTER_IPS=({cl})\n"
        f"CLIENT_IPS=({cli})\n"
        'ALL_IPS=("${CLUSTER_IPS[@]}" "${CLIENT_IPS[@]}")\n'
        f"declare -A IP_NAME=(\n{ip_name}\n)\n"
        "SSH_USER=root\n"
        "SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)\n"
        'ssh_node() { local ip="$1"; shift; ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" "$@"; }\n'
    )


# ── локальный запуск (скрипт сам обходит флот) ────────────────────────────────
async def _run_local(cmd: list[str], cwd: str) -> int:
    """Локальный subprocess со стримингом вывода в лог-панель (stdout+stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    assert proc.stdout is not None
    async for raw in proc.stdout:
        print(raw.decode("utf-8", "replace").rstrip("\n"))
    await proc.wait()
    return proc.returncode if proc.returncode is not None else 1


async def _collect_args(spec: dict) -> list[str] | None:
    """Собрать argv по ArgSpec через ui. None — оператор отменил ввод."""
    argv: list[str] = []
    for a in spec["args"]:
        flag = a.get("flag")
        if a.get("kind") == "bool":
            if await ui.confirm(a["prompt"]):
                argv.append(flag)
            continue
        raw = await ui.ask(a["prompt"], a.get("default", ""), cancelable=True)
        if raw is None:
            print("Отмена.")
            return None
        val = raw.strip() or a.get("default", "")
        if not val:
            print(f"🛑 Поле «{a['prompt']}» обязательно.")
            return None
        if a.get("validate") == "ip":
            try:
                ipaddress.ip_address(val)
            except ValueError:
                print(f"🛑 Некорректный IP: {val!r}")
                return None
        argv += ([flag, val] if flag else [val])
    return argv


async def _run_local_script(spec: dict, script: str, argv: list[str], db: Database,
                            *, dry_run: bool) -> None:
    """scope=local: во временной папке кладём копию скрипта + сгенерённый из БД
    _nodes.sh, скрипт сам фанится по флоту. apply_flag → dry-run→confirm→apply."""
    if dry_run:
        print(f"[DRY] bash {spec['file']} {' '.join(shlex.quote(c) for c in argv)}".rstrip())
        return
    nodes = [dict(r) for r in await db.get_online_nodes()]
    if not nodes:
        print("🛑 Нет online-узлов в vocabulary.nodes — некуда идти.")
        return
    workdir = tempfile.mkdtemp(prefix="dm-fleet-")
    try:
        run_sh = os.path.join(workdir, spec["file"])
        shutil.copy(script, run_sh)
        with open(os.path.join(workdir, "_nodes.sh"), "w", encoding="utf-8") as f:
            f.write(_gen_nodes_sh(nodes))
        base = ["bash", run_sh, *argv]
        apply_flag = spec.get("apply_flag")
        if apply_flag:
            print(f"━━━ dry-run (без {apply_flag}) ━━━")
            await _run_local(base, cwd=workdir)
            if not await ui.confirm(f"Применить ({apply_flag})?", danger=spec.get("danger", False)):
                print("⏩ Применение отменено — остался dry-run.")
                return
            print(f"━━━ apply ({apply_flag}) ━━━")
            rc = await _run_local(base + [apply_flag], cwd=workdir)
        else:
            rc = await _run_local(base, cwd=workdir)
        print("✅ Готово." if rc == 0 else f"⚠️ rc={rc} — проверь вывод.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _pick_nodes(db: Database, spec: dict) -> list[dict] | None:
    """scope=node: пикер узла (или «все»). None — отмена/нет узлов."""
    nodes = [dict(r) for r in await db.get_online_nodes()]
    want = spec.get("nodes", "clients")
    if want == "clients":
        nodes = [n for n in nodes if not n.get("claster")]
    elif want == "cluster":
        nodes = [n for n in nodes if n.get("claster")]
    if not nodes:
        print(f"🛑 Нет online-узлов ({want}).")
        return None
    labels = [f"{n['server_name'] or n['hostname']} ({n['ip_address']})" for n in nodes]
    labels.append(f"🌐 Все ({len(nodes)})")
    idx = await ui.select("Узел для запуска:", labels)
    if idx is None:
        print("Отмена.")
        return None
    return nodes if idx == len(nodes) else [nodes[idx]]


async def _run_node_script(spec: dict, ssh: SshClient, script: str, argv: list[str],
                           targets: list[dict], *, dry_run: bool) -> int:
    """scope=node: upload + run под root на каждом узле. Возвращает число ошибок."""
    remote = f"/tmp/{spec['file']}"
    cmd = f"bash {remote} {' '.join(shlex.quote(a) for a in argv)}".strip()
    errors = 0
    for n in targets:
        ip, name = n["ip_address"], n["server_name"] or n["hostname"]
        if dry_run:
            print(f"[DRY] {name} ({ip}): upload {script}→{remote}; run({_PRIV}): {cmd}")
            continue
        print(f"\n━━━ {name} ({ip}) ━━━")
        if not await ssh.upload(ip, script, remote, user=_PRIV, mode=0o755):
            print(f"🛑 {name}: не удалось залить {spec['file']}.")
            errors += 1
            continue
        r = await ssh.run_stream(ip, cmd, timeout=config.PROVISION_TIMEOUT, echo=print, user=_PRIV)
        if not r.ok:
            print(f"⚠️ {name}: rc={r.exit_status} {r.stderr or ''}".rstrip())
            errors += 1
    return errors


async def run_script(key: str, db: Database, ssh: SshClient, *, dry_run: bool = False) -> None:
    """Единая точка запуска скрипта из реестра (GUI-кнопка / CLI --action <key>)."""
    spec = get_script(key)
    if spec is None:
        print(f"🛑 Неизвестный скрипт: {key}")
        return
    script = _script_path(spec)
    if script is None:
        return
    tag = "[DRY] " if dry_run else ""
    print(f"{tag}━━━ {spec['icon']} {spec['label']} ━━━\n{spec['desc']}")

    argv = await _collect_args(spec)
    if argv is None:
        return

    target_desc = "локально (обход флота)"
    if spec["scope"] == "node":
        targets = await _pick_nodes(db, spec)
        if targets is None:
            return
        target_desc = ", ".join(n["server_name"] or n["hostname"] for n in targets)
        if not dry_run and not await ui.confirm(
                f"Запустить «{spec['label']}» на {len(targets)} узл(ах)?"):
            print("Отмена.")
            return
        errors = await _run_node_script(spec, ssh, script, argv, targets, dry_run=dry_run)
        if not dry_run:
            print(f"\n{'✅ Готово.' if errors == 0 else f'⚠️ Ошибок: {errors}.'}")
    else:
        await _run_local_script(spec, script, argv, db, dry_run=dry_run)

    audit.write({
        "action": "script", "script": key, "scope": spec["scope"],
        "args": argv, "target": target_desc, "dry_run": dry_run,
        "operator": getpass.getuser(),
    })


__all__ = ["SCRIPTS", "SCRIPT_KEYS", "get_script", "run_script"]
