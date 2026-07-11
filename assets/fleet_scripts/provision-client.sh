#!/usr/bin/env bash
#
# provision-client.sh — РОЛЕВАЯ настройка КЛИЕНТСКОГО узла флота (claster=f).
# Запускать НА новом узле под root. Идемпотентно (повторный запуск безопасен).
# Делает: client haproxy.cfg + haproxy_client.service (localhost:6442 → лидер кластера).
#
# По умолчанию СНАЧАЛА гоняет общую базу (provision-base.sh: локали, пакеты, vova+
# ключ, sshd-harden, fail2ban, UFW, needrestart, GPU-blacklist, tmpfiles, nic-tune,
# сборку HAProxy-бинаря) — так `provision-client.sh --hostname ... --vova-pubkey ...`
# полностью настраивает клиентский узел одним прогоном (как раньше).
# С флагом --tail-only база пропускается (её уже прогнали отдельно, напр. из DeployManager
# перед диалогом «тип ноды») — ставится только ролевой client-хвост.
#
# НЕ делает: watchdog/прикладные программы — это через диспетчер/DeployManager.
# Для CLUSTER (DB) узлов используй provision-cluster-member.sh / docs/node_replacement.md.
#
# Usage:  ./provision-client.sh --hostname <name> [--vova-pubkey "ssh-ed25519 ..."]
#         ./provision-client.sh --tail-only          # только client-хвост (база уже прогнана)
#
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Запускать под root."; exit 1; }

HOSTNAME_NEW=""; VOVA_PUBKEY=""; TAIL_ONLY=false
while [[ $# -gt 0 ]]; do case "$1" in
  --hostname) HOSTNAME_NEW="$2"; shift 2 ;;
  --vova-pubkey) VOVA_PUBKEY="$2"; shift 2 ;;
  --tail-only) TAIL_ONLY=true; shift ;;
  *) echo "неизвестный аргумент: $1"; exit 2 ;;
esac; done

# IP узлов кластера (источник правды — ConfigFiles/nodes.md)
CLUSTER1=190.2.151.183; CLUSTER2=2.58.67.41; CLUSTER3=91.219.61.76
G='\033[0;32m'; N='\033[0m'
step(){ echo -e "\n${G}━━━ $* ━━━${N}"; }

DIR="$(dirname "$(readlink -f "$0")")"

# --- общая база (если не --tail-only) ---
if ! $TAIL_ONLY; then
  base_args=()
  [[ -n "$HOSTNAME_NEW" ]] && base_args+=(--hostname "$HOSTNAME_NEW")
  [[ -n "$VOVA_PUBKEY" ]] && base_args+=(--vova-pubkey "$VOVA_PUBKEY")
  bash "$DIR/provision-base.sh" "${base_args[@]}"
fi

# HAProxy-бинарь ставит база — без неё client-хвост бессмыслен.
command -v haproxy >/dev/null || { echo "haproxy не установлен — сперва прогони provision-base.sh"; exit 1; }

step "client haproxy.cfg + haproxy_client.service"
# На узле без дистрибутивного haproxy каталог /etc/haproxy не создаётся
# (сборка из исходников ставит только бинарь) — создаём явно.
install -d -m 755 /etc/haproxy
cat > /etc/haproxy/haproxy.cfg <<EOF
global
    stats socket /var/run/haproxy.sock mode 660 level admin group vova
    log /dev/log local0
    log /dev/log local1 notice
    user haproxy
    group haproxy
    daemon
    maxconn 5000

defaults
    log global
    mode tcp
    option tcplog
    option log-health-checks
    option tcpka
    timeout connect 2s
    timeout client  15m
    timeout server  15m
    retries 2
    option redispatch

frontend postgres_frontend
    bind 127.0.0.1:6442
    use_backend leader_pgbouncer if { nbsrv(leader_pgbouncer) gt 0 }
    default_backend cluster_entrypoints

backend leader_pgbouncer
    mode tcp
    balance leastconn
    option httpchk OPTIONS /primary
    http-check expect status 200
    default-server inter 2s fall 2 rise 1
    server cluster1 ${CLUSTER1}:6543 check port 8008
    server cluster2 ${CLUSTER2}:6543 check port 8008
    server cluster3 ${CLUSTER3}:6543 check port 8008

backend cluster_entrypoints
    mode tcp
    balance roundrobin
    option tcp-check
    default-server inter 5s fall 2 rise 1
    server cluster1 ${CLUSTER1}:6442 check
    server cluster2 ${CLUSTER2}:6442 check
    server cluster3 ${CLUSTER3}:6442 check
EOF
haproxy -c -f /etc/haproxy/haproxy.cfg

cat > /etc/systemd/system/haproxy_client.service <<'EOF'
[Unit]
Description=HAProxy Client Load Balancer
After=network.target

[Service]
ExecStart=/usr/local/sbin/haproxy -W -db -f /etc/haproxy/haproxy.cfg
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/haproxy_client.service   # юнит НЕ executable (см. ниже)
systemctl daemon-reload
systemctl enable --now haproxy_client.service

step "systemd-гигиена — снять +x с юнитов (иначе systemd флудит «marked executable»)"
# Юнит-файл НИКОГДА не должен быть executable. Деплой/rsync мог сохранить +x источника →
# systemd на каждом обращении пишет «Configuration file … is marked executable» (был флуд
# ~80K строк/сутки, journal раздувало до 200М). Снимаем со ВСЕХ .service (defensive).
find /etc/systemd/system -maxdepth 1 -name '*.service' -type f -perm -u+x -exec chmod -x {} + 2>/dev/null || true

step "apt — ОТКЛючить авто-апгрейды на бот-ноде (подмена system .so под живым Playwright → краш)"
# unattended-upgrades, обновляя ЛЮБУЮ system-библиотеку (mesa/libgraphite2/libfreetype/…),
# которую держит замапленной живой Playwright-Firefox → краш рендера → бот exit → само-выключение
# (status=false) → WD crash-loop. Точечный GPU-blacklist оказался whack-a-mole (libgraphite2
# 06-18 пробил его) → полностью ВЫКЛючаем авто-апгрейды. Апдейты — вручную в окне с остановкой
# ботов (пятница 23:30 МСК, боты погашены weekend-stop). ⚠️ ТОЛЬКО бот-ноды (client) — cluster
# DB-ноды НЕ трогаем (им нужны секьюрити-патчи, отдельный профиль).
cat > /etc/apt/apt.conf.d/99-no-auto-upgrade <<'EOF'
// Бот-нода (Playwright): авто-апгрейды ВЫКЛючены — любая system .so, подменённая под живым
// Firefox, роняет рендер → само-выключение бота → WD crash-loop (инциденты 2026-06-16 mesa,
// 2026-06-18 libgraphite2). Апдейты — вручную по расписанию (пятница 23:30, боты погашены).
APT::Periodic::Unattended-Upgrade "0";
EOF
systemctl disable --now apt-daily-upgrade.timer 2>/dev/null || true

step "Playwright lock-sweep → /usr/local/bin (снимает висячий firefox-lock, роняющий launch())"
# Свип удаляет висячие firefox-lock в ОБЩЕМ кэше ~/.cache/ms-playwright (роняют launch()) +
# протухшие .links. Юниты браузер-ботов зовут его в ExecStartPre (ставит DeployManager).
# Источник — sibling scripts/pw_lock_sweep.sh (DeployManager кладёт его рядом в $DIR при заливке).
if [[ -f "$DIR/pw_lock_sweep.sh" ]]; then
  install -m 0755 "$DIR/pw_lock_sweep.sh" /usr/local/bin/pw_lock_sweep.sh
  echo "   ✅ /usr/local/bin/pw_lock_sweep.sh"
else
  echo "   ⚠️ $DIR/pw_lock_sweep.sh не найден — свип НЕ установлен (доставь вручную: install -m755 pw_lock_sweep.sh /usr/local/bin/)."
fi

echo -e "\n${G}✅ Клиентский узел настроен.${N}"
echo "Дальше: 1) на кластере открыть этому IP доступ — scripts/whitelist-ip.sh <этот_IP> --apply"
echo "        2) зарегистрировать в vocabulary.nodes (claster=f) и развернуть watchdog (см. node_replacement.md шаг 8)"
echo "Проверка: psql -h 127.0.0.1 -p 6442 -U vova -d postgres -c 'select 1;'"
