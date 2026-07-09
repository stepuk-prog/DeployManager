"""«Настроить ноду» ФАЗА 2 — ввод/замена ЧЛЕНА кластера (Patroni/etcd).

Опасная процедура (docs/operations/node_replacement.md в Clusters): замена мёртвой cluster-ноды
= swap членства etcd (окно quorum 2/2), basebackup, чистка мёртвого IP на живых нодах (пропуск →
split-brain). Поэтому — НЕ «один клик», а ПОШАГОВЫЙ визард: каждый шаг показывает подробное
пояснение (что/зачем/риск) + точные команды, оператор выбирает [Выполнить / Пропустить / Отмена].
Безопасная подготовка (локаль/пакеты/UFW/конфиги) автоматизируется; quorum-критичные шаги
(etcd-swap, старт+basebackup, чистка) — под явным подтверждением, с усиленным предупреждением.

Команды взяты из проверенного ранбука (подстановка NODE_NAME/NEW_IP/OLD_IP/живых нод). Реальный
прогон — на живой новый узел (dry-run печатает план). Источник конфигов — CLUSTER_CONFIG_DIR.
"""
import getpass
import os
import shlex
from dataclasses import dataclass, field

from core import audit, ui
from classes.ssh_client import SshClient
from database.db import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)

_PRIV = config.PRIV_USER or "root"
# Файлы, где на ЖИВЫХ cluster-нодах остаются ссылки на старый IP (Шаг 6 ранбука — критично).
_DEAD_IP_FILES = (
    "/etc/patroni/patroni.yml", "/etc/systemd/system/servermanager2.service",
    "/etc/postgresql/16/main/pg_hba.conf", "/etc/etcd-defrag.env",
    "/etc/etcd/etcd-*.yml", "/home/vova/.zshrc", "/root/.zshrc",
)


@dataclass
class Step:
    """Один шаг визарда. target: 'new'=новый узел(root) | 'live'=каждая живая cluster-нода |
    'manual'=показать оператору (сам делает) | 'info'=только пояснение. cmd — bash (или None)."""
    title: str
    explain: str
    target: str = "new"
    cmd: str | None = None
    danger: bool = False


@dataclass
class Params:
    node_name: str          # cluster1|cluster2|cluster3 — имя члена (заменяемого)
    new_ip: str
    old_ip: str
    hostname: str
    live_ips: list = field(default_factory=list)     # 2 живые cluster-ноды
    client_ips: list = field(default_factory=list)
    admin_ip: str = "94.124.166.126"


# ── справочные блоки команд (из node_replacement.md, подстановка через .format) ──
def _ufw_cluster(p: Params) -> str:
    a, b = (p.live_ips + ["", ""])[:2]
    clients = " ".join(p.client_ips)
    return (
        f'A={a}; B={b}; CL="{clients}"; ADM={p.admin_ip}\n'
        'for ip in $A $B; do ufw allow from $ip to any port 2380 proto tcp comment "etcd peer"; done\n'
        'for ip in $A $B $CL $ADM; do ufw allow from $ip to any port 2379 proto tcp comment "etcd client"; done\n'
        'for ip in $A $B $CL $ADM; do ufw allow from $ip to any port 5432 proto tcp comment "PG repl"; done\n'
        'for ip in $CL $ADM; do ufw allow from $ip to any port 6442 proto tcp comment "HAProxy"; done\n'
        'for ip in $A $B $CL $ADM; do ufw allow from $ip to any port 6543 proto tcp comment "PgBouncer"; done\n'
        'for ip in $A $B $CL $ADM; do ufw allow from $ip to any port 8008 proto tcp comment "Patroni REST"; done'
    )


def _install_pg() -> str:
    return (
        "apt-get install -y curl ca-certificates gnupg lsb-release\n"
        "install -d /usr/share/postgresql-common/pgdg\n"
        "curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc\n"
        'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list\n'
        "apt-get update && apt-get install -y postgresql-16 postgresql-client-16 postgresql-contrib-16 pgbouncer\n"
        "pg_dropcluster 16 main --stop; systemctl stop postgresql; systemctl disable postgresql"
    )


def _install_etcd() -> str:
    return (
        "ETCD_VER=v3.5.18; cd /tmp\n"
        "curl -sL https://github.com/etcd-io/etcd/releases/download/${ETCD_VER}/etcd-${ETCD_VER}-linux-amd64.tar.gz -o /tmp/etcd.tar.gz\n"
        "tar xzf /tmp/etcd.tar.gz -C /tmp\n"
        "install -m 755 /tmp/etcd-${ETCD_VER}-linux-amd64/etcd /usr/local/bin/etcd\n"
        "install -m 755 /tmp/etcd-${ETCD_VER}-linux-amd64/etcdctl /usr/local/bin/etcdctl\n"
        "rm -rf /tmp/etcd.tar.gz /tmp/etcd-${ETCD_VER}-linux-amd64"
    )


def _install_patroni() -> str:
    return (
        "apt-get install -y python3-venv\n"
        "python3 -m venv /opt/patroni-venv\n"
        "/opt/patroni-venv/bin/pip install -U pip wheel\n"
        '/opt/patroni-venv/bin/pip install "patroni[etcd3,psycopg2]==4.0.4"\n'
        "ln -sf /opt/patroni-venv/bin/patroni /usr/local/bin/patroni\n"
        "ln -sf /opt/patroni-venv/bin/patronictl /usr/local/bin/patronictl"
    )


def _install_haproxy() -> str:
    return (
        "apt-get install -y build-essential libssl-dev libpcre3-dev zlib1g-dev rsyslog\n"
        "cd /usr/local/src && wget -q https://www.haproxy.org/download/3.1/src/haproxy-3.1.0.tar.gz\n"
        "tar -xzf haproxy-3.1.0.tar.gz && cd haproxy-3.1.0\n"
        'make -j"$(nproc)" TARGET=linux-glibc USE_OPENSSL=1 USE_PCRE=1 USE_ZLIB=1 && make install\n'
        "ln -sf /usr/local/sbin/haproxy /usr/sbin/haproxy"
    )


def _users_dirs() -> str:
    return (
        "groupadd -rf etcd_group; id etcd &>/dev/null || useradd -r -g etcd_group -s /usr/sbin/nologin -d /var/lib/etcd etcd\n"
        "groupadd -rf patroni_group; usermod -aG patroni_group postgres\n"
        "groupadd -rf haproxy; id haproxy &>/dev/null || useradd -r -g haproxy -s /usr/sbin/nologin -d /var/lib/haproxy haproxy\n"
        "mkdir -p /etc/etcd /etc/patroni /var/lib/etcd /var/log/postgresql /var/lib/haproxy\n"
        "chown -R etcd:etcd_group /var/lib/etcd && chmod 700 /var/lib/etcd\n"
        "chown postgres:postgres /var/log/postgresql; chown haproxy:haproxy /var/lib/haproxy"
    )


def _locale() -> str:
    return (
        "apt-get install -y locales\n"
        'sed -i "s/^# *ru_RU\\.UTF-8/ru_RU.UTF-8/" /etc/locale.gen\n'
        'sed -i "s/^# *en_US\\.UTF-8/en_US.UTF-8/" /etc/locale.gen\n'
        "locale-gen"
    )


def _build_steps(p: Params) -> list[Step]:
    nn = p.node_name
    return [
        Step("0. Бэкап (pg_dumpall с лидера)",
             "Страховка ПЕРЕД любыми изменениями членства. Логический дамп всей БД с текущего "
             "Patroni-лидера (не с нового узла!). 16ГБ БД ≈ 5-7 мин + gzip. Если замена пойдёт "
             "катастрофически — из дампа можно восстановиться. Делается ВРУЧНУЮ на лидере "
             "(нужен PG_PASSWORD, лидера определи `patronictl list`).",
             target="manual",
             cmd="# на ЛИДЕРЕ:\nBACKUP=/root/cluster_backup_$(date +%F_%H%M).sql.gz\n"
                 'nohup bash -c "PGPASSWORD=<PG_PASSWORD> pg_dumpall -h 127.0.0.1 -U postgres | gzip > $BACKUP" &'),
        Step("1a. Русская локаль (ru_RU.UTF-8)",
             "КРИТИЧНО на Ubuntu 24.04: локали ru_RU нет по умолчанию, а patroni.yml её требует "
             "(lc_messages/lc_monetary/...). Без неё PostgreSQL НЕ стартует после basebackup "
             "(FATAL: invalid value for parameter lc_messages). Генерим ru_RU + en_US.",
             cmd=_locale()),
        Step("1b. UFW-правила cluster-узла",
             "Открываем порты кластера ТОЛЬКО для нужных источников: 2380(etcd peer)=живые "
             "cluster-ноды; 2379(etcd client)/5432(PG repl)/6543(PgBouncer)/8008(Patroni REST)="
             "cluster+клиенты+админ; 6442(HAProxy)=клиенты+админ. ⚠️ Без 6543/8008 у клиентов "
             "Patroni-aware HAProxy покажет узел как L4TOUT (грабля №4).",
             cmd=_ufw_cluster(p)),
        Step("2a. PostgreSQL 16 (PGDG) + pgbouncer",
             "Ставим PG16 из официального репозитория PGDG. Дефолтный кластер PG удаляем "
             "(pg_dropcluster) и глушим systemd-postgresql — своим кластером управляет Patroni, "
             "не systemd.",
             cmd=_install_pg()),
        Step("2b. etcd 3.5.18 (бинарный tarball)",
             "etcd — DCS (хранилище состояния кластера, лидер-лок). Ставим ту же версию, что на "
             "остальных нодах (3.5.18), из бинарного релиза в /usr/local/bin. Пока НЕ запускаем.",
             cmd=_install_etcd()),
        Step("2c. Patroni 4.0.4 (venv)",
             "Patroni — оркестратор PG-кластера (промоушен/failover через etcd). На 24.04 system "
             "pip заблокирован (PEP 668) → ставим в /opt/patroni-venv, симлинки в /usr/local/bin.",
             cmd=_install_patroni()),
        Step("2d. HAProxy 3.1.0 (из исходников)",
             "HAProxy на cluster-ноде (порт 6442) — точка входа клиентов, Patroni-aware "
             "(httpchk /primary). Собирается из исходников (make) — небыстро (~1-2 мин). Тот же "
             "билд, что на остальных cluster-нодах.",
             cmd=_install_haproxy()),
        Step("2e. Users/groups + каталоги",
             "Служебные пользователи/группы (etcd/etcd_group, patroni_group для postgres, haproxy) "
             "и каталоги с правами — как на действующих нодах (иначе сервисы не стартуют / права).",
             cmd=_users_dirs()),
        Step("3. Конфиги кластера",
             f"Копируем из {config.CLUSTER_CONFIG_DIR} конфиги члена «{nn}»: etcd-{nn}.yml "
             f"(initial-cluster-state=existing!), patroni-{nn}.yml, общие haproxy.cfg/pgbouncer.ini/"
             "pg_hba.conf/postgresql.conf/userlist.txt + юниты etcd/patroni/haproxy/pgbouncer + "
             "leader_callback.sh. ⚠️ Если IP нового узла ОТЛИЧАЕТСЯ от старого — подставить NEW_IP "
             "в etcd/patroni конфиги (sed). systemd НЕ enable — сначала etcd-swap. В v1 — ВРУЧНУЮ "
             "(деликатно: per-node файлы + подстановка IP; авто-копирование — follow-up).",
             target="manual", cmd=_render_configs_cmd(p)),
        Step("4. ⚠️ etcd swap: remove dead + add new",
             "САМАЯ ОПАСНАЯ ФАЗА. На живой ноде: `etcdctl member list` → взять member ID мёртвого "
             f"«{nn}» → `member remove <id>` → `member add {nn} --peer-urls=http://{p.new_ip}:2380`. "
             "МЕЖДУ remove и стартом etcd на новом узле кластер живёт на quorum 2/2 (отказ ещё "
             "одного = полная остановка). Окно — минуты. Делай ТОЛЬКО когда новый узел полностью "
             "готов (шаги 1-3). Выполняется на живой cluster-ноде — команды показаны, оператор "
             "подтверждает и сверяет вывод.",
             target="manual", danger=True,
             cmd=f"# на ЖИВОЙ cluster-ноде:\netcdctl member list\n"
                 f"etcdctl member remove <DEAD_MEMBER_ID>\n"
                 f"etcdctl member add {nn} --peer-urls=http://{p.new_ip}:2380"),
        Step("5. Старт etcd → Patroni → basebackup",
             "Сразу после member add: на новом узле `systemctl start etcd` (проверить 3 healthy) → "
             "`systemctl start patroni` (пойдёт basebackup 16ГБ, 5-15 мин, state=creating replica → "
             "running, Lag=0) → `systemctl enable --now haproxy pgbouncer`. Мониторить "
             "`patronictl list` + `du -sh /var/lib/postgresql/16/main`.",
             target="new", danger=True,
             cmd="systemctl start etcd && sleep 5 && systemctl is-active etcd\n"
                 "etcdctl --endpoints=http://127.0.0.1:2379 member list\n"
                 "systemctl start patroni && sleep 10 && patronictl -c /etc/patroni/patroni.yml list\n"
                 "# дождаться state=running Lag=0, затем:\nsystemctl enable --now haproxy pgbouncer"),
        Step("6. ⚠️ Чистка мёртвого IP на ЖИВЫХ нодах (swap-node-ip.sh)",
             f"КРИТИЧНО (пропуск = split-brain через сутки, инцидент 05-24). На двух живых "
             f"cluster-нодах остаются ссылки на старый IP {p.old_ip} в patroni.yml/servermanager2/"
             "pg_hba/etcd-defrag/.zshrc → Patroni таймаутит на мёртвый endpoint → ложный failover. "
             "Автоматизировано вендоренным swap-node-ip.sh (бэкап+sed+reload). Запускается ЛОКАЛЬНО "
             "(обходит cluster-ноды сам).",
             target="local", danger=True,
             cmd=f"bash swap-node-ip.sh --old {p.old_ip} --new {p.new_ip} --apply --reload"),
        Step("7. HAProxy на клиентах (старый IP → новый)",
             f"У клиентов в /etc/haproxy/haproxy.cfg прописан старый IP cluster-ноды (backend "
             f"cluster_entrypoints/leader_pgbouncer). Заменить {p.old_ip}→{p.new_ip} по всем "
             "клиентам + reload haproxy_client. (Если IP не менялся — шаг пропустить.)",
             target="manual",
             cmd="# по каждому клиенту:\n"
                 f"sed -i 's/{p.old_ip}/{p.new_ip}/g' /etc/haproxy/haproxy.cfg\n"
                 "haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl reload haproxy_client.service"),
        Step("8. Регистрация в БД + Watchdog/Reporter",
             f"INSERT vocabulary.nodes (hostname={nn}, ip={p.new_ip}, claster=true) → id в "
             "/opt/Watchdog/.env NODE_ID. Развернуть Watchdog (кнопка «WD»/infra), Reporter "
             "(кнопка «📊 Reporter»), скопировать vova/.ssh-ключи с живой ноды, cron ServerReport. "
             "Часть — существующими кнопками DM.",
             target="manual",
             cmd="# INSERT в БД (или отдельная авто-регистрация), затем кнопки WD + Reporter"),
    ]


def _render_configs_cmd(p: Params) -> str:
    """Оператор-runnable scp-команды шага 3 (v1 — вручную)."""
    nn, d, ip = p.node_name, config.CLUSTER_CONFIG_DIR, p.new_ip
    return (
        f"# с рабочей станции ({d}):\n"
        f"scp {d}/etcd-{nn}.yml       root@{ip}:/etc/etcd/etcd-{nn}.yml\n"
        f"scp {d}/patroni-{nn}.yml    root@{ip}:/etc/patroni/patroni.yml\n"
        f"scp {d}/pgbouncer.ini {d}/userlist.txt   root@{ip}:/etc/pgbouncer/\n"
        f"scp {d}/pg_hba.conf {d}/postgresql.conf  root@{ip}:/etc/postgresql/16/main/\n"
        f"scp {d}/haproxy.cfg         root@{ip}:/etc/haproxy/haproxy.cfg\n"
        f"scp {d}/etcd.service {d}/patroni.service {d}/haproxy.service {d}/pgbouncer.service  root@{ip}:/etc/systemd/system/\n"
        f"# юнит etcd.service ссылается на etcd-{nn}.yml (--config-file); при смене IP — sed OLD→{ip} в etcd/patroni.\n"
        "ssh root@%s 'systemctl daemon-reload'   # НЕ enable/start пока (сначала etcd-swap, шаг 4)" % ip)


# ── выполнение ────────────────────────────────────────────────────────────────
async def _run_step(ssh: SshClient, p: Params, st: Step, *, dry_run: bool) -> str:
    """Показать шаг (пояснение+команды) и по выбору выполнить/пропустить/отменить.
    Возвращает 'ok' | 'skip' | 'cancel' | 'fail'."""
    print(f"\n{'━'*70}\n{'⚠️  ' if st.danger else ''}{st.title}\n{'─'*70}\n{st.explain}")
    if st.cmd:
        print("\nКоманды:\n" + "\n".join("    " + l for l in st.cmd.splitlines()))
    if st.target == "info":
        return "ok"
    if st.target == "manual":
        print("\n↳ Шаг выполняется ВРУЧНУЮ оператором (показан выше).")
        idx = await ui.select("Шаг сделан вручную?",
                              ["Отметить выполненным (продолжить)", "Пропустить", "Отмена мастера"])
        return {0: "ok", 1: "skip", None: "cancel"}.get(idx, "cancel")

    opts = ["Выполнить", "Пропустить", "Отмена мастера"]
    idx = await ui.select(("⚠️ ОПАСНЫЙ шаг. " if st.danger else "") + "Что делаем?", opts,
                          colors=(["red", "blue", "blue"] if st.danger else None))
    if idx is None or idx == 2:
        return "cancel"
    if idx == 1:
        return "skip"
    if dry_run:
        print(f"[DRY] выполнил бы на {st.target}.")
        return "ok"
    if st.danger and not await ui.confirm(f"Точно выполнить «{st.title}»?", danger=True):
        return "skip"

    # цель выполнения
    if st.target == "new":
        r = await ssh.run_stream(p.new_ip, f"bash -lc {shlex.quote(st.cmd)}", timeout=1800,
                                 echo=print, user=_PRIV)
        return "ok" if r.ok else "fail"
    if st.target == "live":
        ok = True
        for ip in p.live_ips:
            print(f"  → живая нода {ip}")
            r = await ssh.run_stream(ip, f"bash -lc {shlex.quote(st.cmd)}", timeout=300,
                                     echo=print, user=_PRIV)
            ok = ok and r.ok
        return "ok" if ok else "fail"
    if st.target == "local":   # swap-node-ip.sh — локально из assets/fleet_scripts
        from core.scripts import BUNDLED_DIR, _run_local
        script = os.path.join(BUNDLED_DIR, "swap-node-ip.sh")
        rc = await _run_local(["bash", script, "--old", p.old_ip, "--new", p.new_ip,
                               "--apply", "--reload"], cwd=BUNDLED_DIR)
        return "ok" if rc == 0 else "fail"
    return "skip"


async def run_setup_cluster_member(db: Database, ssh: SshClient, *, new_ip: str, hostname: str,
                                    dry_run: bool = False) -> None:
    """Пошаговый визард ввода/замены члена кластера. new_ip/hostname — из формы «Настроить ноду»."""
    print("\n⛧ ФАЗА 2: член кластера (Patroni/etcd) — ПОШАГОВЫЙ визард (замена мёртвой cluster-ноды).")
    print("Опасная процедура: этапы 4/6 меняют членство etcd и живые ноды. Каждый шаг — с пояснением "
          "и подтверждением. Реальный прогон — на подготовленный новый узел.\n")

    # топология из БД
    nodes = [dict(r) for r in await db.get_online_nodes()]
    cluster = [n for n in nodes if n.get("claster")]
    clients = [n for n in nodes if not n.get("claster")]
    names = [n["server_name"] or n["hostname"] for n in cluster]
    idx = await ui.select("Какого члена кластера ЗАМЕНЯЕТ этот узел (имя мёртвой ноды)?",
                          names, details=[n["ip_address"] for n in cluster])
    if idx is None:
        print("Отмена.")
        return
    dead = cluster[idx]
    node_name = (dead["server_name"] or dead["hostname"]).strip()
    old_ip = dead["ip_address"]
    live = [n["ip_address"] for n in cluster if n["ip_address"] != old_ip]

    p = Params(node_name=node_name, new_ip=new_ip, old_ip=old_ip, hostname=hostname,
               live_ips=live, client_ips=[n["ip_address"] for n in clients])
    print(f"Замена: член «{node_name}» {old_ip} → новый {new_ip}. Живые cluster-ноды: {', '.join(live)}. "
          f"Клиентов: {len(p.client_ips)}.")
    if not await ui.confirm("Топология верна, начинаем пошагово?"):
        print("Отмена.")
        return

    steps = _build_steps(p)
    done, skipped = [], []
    for st in steps:
        res = await _run_step(ssh, p, st, dry_run=dry_run)
        if res == "cancel":
            print(f"\n🛑 Мастер отменён на «{st.title}». Выполнено: {len(done)}, пропущено: {len(skipped)}.")
            break
        if res == "fail":
            print(f"\n🛑 Шаг «{st.title}» упал. Останавливаюсь — разберись вручную, потом продолжи.")
            break
        (done if res == "ok" else skipped).append(st.title)
    else:
        print(f"\n✅ Визард пройден. Выполнено: {len(done)}, пропущено вручную/оператором: {len(skipped)}.")

    audit.write({"action": "setup-cluster-member", "dry_run": dry_run, "node_name": node_name,
                 "new_ip": new_ip, "old_ip": old_ip, "done": done, "skipped": skipped,
                 "operator": getpass.getuser()})


__all__ = ["run_setup_cluster_member"]
