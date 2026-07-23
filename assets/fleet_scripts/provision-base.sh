#!/usr/bin/env bash
#
# provision-base.sh — ОБЩАЯ база узла флота (для ЛЮБОГО типа: клиент ИЛИ член кластера).
# Запускать НА новом узле под root. Идемпотентно (повторный запуск безопасен).
# Делает: vova+root-ключи+polkit, hostname/tz, локали, пакеты, python3.11 (deadsnakes),
#         sshd-harden, fail2ban, UFW (только 22), needrestart/GPU/tmpfiles/nic drop-in'ы,
#         сборку+установку HAProxy-бинаря.
# НЕ делает: РОЛЕВУЮ часть — haproxy .cfg/юнит (клиент → provision-client.sh; член
#            кластера → provision-cluster-member.sh) и разворачивание watchdog/программ
#            (через диспетчер/DeployManager).
#
# Usage:
#   ./provision-base.sh --hostname <name> [--vova-pubkey "ssh-ed25519 ..."]   # все шаги подряд
#   ./provision-base.sh --step <id> [--hostname ...] [--vova-pubkey ...]      # ОДИН шаг (для DM)
#   ./provision-base.sh --list-steps                                          # id шагов по порядку
#
# Шаг `user` идёт ПЕРВЫМ намеренно: он кладёт ключи vova И root, после чего DeployManager
# гонит остальные шаги уже по ключу (пароль нужен только на `user`).
#
set -euo pipefail

HOSTNAME_NEW=""; VOVA_PUBKEY=""; STEP=""
while [[ $# -gt 0 ]]; do case "$1" in
  --hostname) HOSTNAME_NEW="$2"; shift 2 ;;
  --vova-pubkey) VOVA_PUBKEY="$2"; shift 2 ;;
  --step) STEP="$2"; shift 2 ;;
  --list-steps) printf '%s\n' user hostname locales packages python311 sshd fail2ban ufw dropins haproxy; exit 0 ;;
  *) echo "неизвестный аргумент: $1"; exit 2 ;;
esac; done

# root нужен только для фактического прогона шагов (--list-steps уже вышел выше).
[[ $EUID -eq 0 ]] || { echo "Запускать под root."; exit 1; }

HAPROXY_VER=3.1.0
G='\033[0;32m'; N='\033[0m'
step(){ echo -e "\n${G}━━━ $* ━━━${N}"; }

# ─────────────────────────────────────────────────────────────────────────────
# Шаги (каждый идемпотентен и самодостаточен — можно звать по одному через --step)
# ─────────────────────────────────────────────────────────────────────────────

step_user() {
  step "пользователь vova + ключи (vova И root) + polkit"
  if ! id vova &>/dev/null; then adduser --disabled-password --gecos "" vova; fi
  usermod -aG sudo vova
  if [[ -n "$VOVA_PUBKEY" ]]; then
    # vova authorized_keys
    install -d -m 700 -o vova -g vova /home/vova/.ssh
    grep -qF "$VOVA_PUBKEY" /home/vova/.ssh/authorized_keys 2>/dev/null || echo "$VOVA_PUBKEY" >> /home/vova/.ssh/authorized_keys
    chown vova:vova /home/vova/.ssh/authorized_keys; chmod 600 /home/vova/.ssh/authorized_keys
    # root authorized_keys — тот же ключ (PRIV_USER=root по ключу; весь флот так, DeployManager
    # заходит под root по ключу vova для юнитов/systemctl). Без этого мастер не зайдёт под root.
    install -d -m 700 /root/.ssh
    grep -qF "$VOVA_PUBKEY" /root/.ssh/authorized_keys 2>/dev/null || echo "$VOVA_PUBKEY" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
  else
    echo "  (--vova-pubkey не задан — ключ разложи вручную для vova И root)"
  fi
  # polkit-грант manage-units — чтобы `systemctl stop/start/restart/kill` под vova не требовал
  # интерактивной авторизации (иначе на Ubuntu 24.04 / polkit 124 падает). WD2/GD2/CD2 бегут
  # под vova. См. [[wd2-polkit-grant-2026-06-15]].
  install -d -m 755 /etc/polkit-1/rules.d
  cat > /etc/polkit-1/rules.d/49-watchdog-vova.rules <<'PKEOF'
// Watchdog2/Dispatcher run as User=vova and call `systemctl stop/start/restart/kill`
// directly. Grant vova manage-units so polkit doesn't demand interactive auth.
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.systemd1.manage-units" ||
         action.id == "org.freedesktop.systemd1.manage-unit-files") &&
        subject.user == "vova") {
        return polkit.Result.YES;
    }
});
PKEOF
}

step_hostname() {
  step "hostname + timezone"
  [[ -n "$HOSTNAME_NEW" ]] && { hostnamectl set-hostname "$HOSTNAME_NEW"; grep -q "$HOSTNAME_NEW" /etc/hosts || echo "127.0.1.1 $HOSTNAME_NEW" >> /etc/hosts; }
  timedatectl set-timezone Europe/Moscow
}

step_locales() {
  step "локали (ru_RU + en_US) — критично для совместимости с кластером"
  apt-get update -qq
  apt-get install -y -qq locales
  sed -i 's/^# *ru_RU\.UTF-8/ru_RU.UTF-8/; s/^# *en_US\.UTF-8/en_US.UTF-8/' /etc/locale.gen
  locale-gen
}

step_packages() {
  step "пакеты (build-стек, сеть, утилиты)"
  apt-get install -y -qq vim tmux htop git curl wget unzip tree socat netcat-openbsd \
    jq dos2unix net-tools ethtool build-essential libssl-dev libpcre3-dev zlib1g-dev rsyslog ufw fail2ban needrestart
}

step_python311() {
  step "python3.11 (deadsnakes) — venv-стандарт флота для WD/GD/CD"
  # Флот держит control-plane venv на python3.11 (даже на Ubuntu 24.04, где дефолт 3.12).
  # Без этого infra-деплой WD/диспетчера падает: `python3.11 -m venv` → command not found.
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -qq
  apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
}

step_sshd() {
  step "sshd hardening"
  sshd_set(){ grep -qE "^$1 " /etc/ssh/sshd_config && sed -i "s/^$1 .*/$1 $2/" /etc/ssh/sshd_config || echo "$1 $2" >> /etc/ssh/sshd_config; }
  sshd_set MaxSessions 10; sshd_set MaxStartups 10
  sshd_set ClientAliveInterval 30; sshd_set ClientAliveCountMax 10
  systemctl restart ssh 2>/dev/null || systemctl restart sshd
}

step_fail2ban() {
  step "fail2ban (backend=systemd для Ubuntu 24.04)"
  cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
backend = systemd
ignoreip = 127.0.0.1/8 ::1 94.124.166.74

[sshd]
enabled = true
port = ssh
maxretry = 3
findtime = 10m
bantime = 24h
EOF
  systemctl enable --now fail2ban; systemctl restart fail2ban
}

step_ufw() {
  step "UFW (deny incoming, разрешён только SSH — ролевые порты открывает роль-скрипт)"
  ufw --force reset >/dev/null
  ufw default deny incoming; ufw default allow outgoing
  ufw allow 22/tcp comment 'ssh'
  ufw --force enable
}

step_dropins() {
  step "drop-in'ы: needrestart + apt GPU-blacklist + tmpfiles + nic-ring-tune"

  # needrestart: не авто-рестартить управляющий слой при апгрейде библиотек.
  install -d /etc/needrestart/conf.d
  cat > /etc/needrestart/conf.d/zz-no-autorestart-cluster.conf <<'EOF'
# Ops: prevent needrestart from auto-restarting cluster-critical / management
# daemons after library upgrades (libssl / libsystemd refresh via unattended-
# upgrades). Does NOT affect crash recovery (systemd Restart= still works) nor
# manual `systemctl restart`. Anchored (^...$) so cron.service /
# networkd-dispatcher.service are NOT matched.
$nrconf{override_rc}->{qr(^patroni\.service$)}           = 0;
$nrconf{override_rc}->{qr(^etcd\.service$)}              = 0;
$nrconf{override_rc}->{qr(^haproxy(_client)?\.service$)} = 0;
$nrconf{override_rc}->{qr(^pgbouncer\.service$)}         = 0;
$nrconf{override_rc}->{qr(^dispatcher\.service$)}        = 0;  # GD  (cluster nodes only)
$nrconf{override_rc}->{qr(^watchdog\.service$)}          = 0;  # WD  (all fleet nodes)
$nrconf{override_rc}->{qr(^cron-dispatcher\.service$)}   = 0;  # CD
$nrconf{override_rc}->{qr(^servermanager2\.service$)}    = 0;  # ServerManager2 bot
EOF

  # apt-blacklist GPU-стека — не авто-апгрейдить mesa/libdrm под живым Chromium.
  cat > /etc/apt/apt.conf.d/99-playwright-gpu-blacklist <<'EOF'
// Playwright/Chromium GPU/render-стек: НЕ авто-апгрейдить через unattended-upgrades.
// Подмена mesa/libdrm/libgbm/libva из-под живого Chromium ломает GL/GBM-рендерер →
// page.goto виснет на domcontentloaded → "Не смог инициализировать браузер" →
// WD crash-loop. Инцидент 2026-06-16 06:30 (node-5, otc-screen-*).
Unattended-Upgrade::Package-Blacklist {
    "mesa";
    "^libdrm";
    "^libgbm";
    "^libva";
    "^libllvm";
    "^libgl1-amber";
};
EOF

  # tmpfiles — авто-уборка протёкших браузер-профилей из /tmp (инцидент node-7: /tmp 280 ГБ).
  cat > /etc/tmpfiles.d/zz-browser-profile-leak.conf <<'EOF'
# Type Path                                Mode UID GID Age Argument
e /tmp/rust_mozprofile*                    -    -   -   12h -
e /tmp/firefox_profile_*                   -    -   -   12h -
e /tmp/playwright_firefoxdev_profile-*     -    -   -   12h -
e /tmp/playwright-artifacts-*              -    -   -   12h -
EOF
  systemctl enable --now systemd-tmpfiles-clean.timer 2>/dev/null || true
  systemd-tmpfiles --clean 2>/dev/null || true

  # nic-ring-tune — RX/TX ring NIC до аппаратного максимума (bond-aware, персистентно).
  cat > /usr/local/sbin/nic-ring-tune.sh <<'EOF'
#!/bin/bash
# Поднять RX/TX ring NIC до аппаратного максимума. Bond-aware (LACP/active-backup).
set -e
tune() {
  d="$1"
  mrx=$(ethtool -g "$d" 2>/dev/null | awk '/Pre-set/{p=1} p&&/^RX:/{print $2; exit}')
  mtx=$(ethtool -g "$d" 2>/dev/null | awk '/Pre-set/{p=1} p&&/^TX:/{print $2; exit}')
  [ -n "$mrx" ] && [ "$mrx" != "n/a" ] && ethtool -G "$d" rx "$mrx" 2>/dev/null || true
  [ -n "$mtx" ] && [ "$mtx" != "n/a" ] && ethtool -G "$d" tx "$mtx" 2>/dev/null || true
  logger -t nic-ring-tune "set $d rx=$mrx tx=$mtx"
}
IF=$(ip route show default | awk '/default/{print $5; exit}')
[ -z "$IF" ] && exit 0
if [ -d "/sys/class/net/$IF/bonding" ]; then
  for s in $(cat "/sys/class/net/$IF/bonding/slaves"); do tune "$s"; done
else
  tune "$IF"
fi
EOF
  chmod +x /usr/local/sbin/nic-ring-tune.sh
  cat > /etc/systemd/system/nic-ring-tune.service <<'EOF'
[Unit]
Description=Tune NIC ring buffers to hardware max
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/nic-ring-tune.sh

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now nic-ring-tune.service 2>/dev/null || true
}

step_haproxy() {
  step "HAProxy $HAPROXY_VER из исходников (бинарь — общий для клиента и члена кластера)"
  if ! haproxy -v 2>/dev/null | grep -q "$HAPROXY_VER"; then
    cd /usr/local/src
    # -4: форсируем IPv4. haproxy.org отдаёт AAAA; на узле без IPv6-маршрута wget дёргал
    # IPv6 → "network failure" (exit 4). Инцидент VIDEO-3 2026-07-23. -4 берёт A-запись.
    wget -4 -q "https://www.haproxy.org/download/${HAPROXY_VER%.*}/src/haproxy-${HAPROXY_VER}.tar.gz"
    tar -xzf "haproxy-${HAPROXY_VER}.tar.gz"; cd "haproxy-${HAPROXY_VER}"
    make -j"$(nproc)" TARGET=linux-glibc USE_OPENSSL=1 USE_PCRE=1 USE_ZLIB=1
    make install
    ln -sf /usr/local/sbin/haproxy /usr/sbin/haproxy
  fi
  id haproxy &>/dev/null || { groupadd -r haproxy; useradd -r -g haproxy -s /usr/sbin/nologin -d /var/lib/haproxy haproxy; }
  install -d -o haproxy -g haproxy /var/lib/haproxy
}

# ─────────────────────────────────────────────────────────────────────────────
# Диспетчер: один шаг (--step) или все подряд (standalone).
# Порядок ВАЖЕН: user первым (кладёт ключи) → дальше остальное.
# ─────────────────────────────────────────────────────────────────────────────
ALL_STEPS=(user hostname locales packages python311 sshd fail2ban ufw dropins haproxy)

run_step() {
  case "$1" in
    user) step_user ;;
    hostname) step_hostname ;;
    locales) step_locales ;;
    packages) step_packages ;;
    python311) step_python311 ;;
    sshd) step_sshd ;;
    fail2ban) step_fail2ban ;;
    ufw) step_ufw ;;
    dropins) step_dropins ;;
    haproxy) step_haproxy ;;
    *) echo "🛑 неизвестный шаг: $1 (см. --list-steps)"; exit 2 ;;
  esac
}

if [[ -n "$STEP" ]]; then
  run_step "$STEP"
  echo -e "\n${G}✅ шаг '$STEP' выполнен.${N}"
else
  for s in "${ALL_STEPS[@]}"; do run_step "$s"; done
  echo -e "\n${G}✅ База узла настроена.${N}"
  echo "Дальше — РОЛЕВАЯ часть: клиент → provision-client.sh --tail-only ; член кластера → provision-cluster-member.sh"
fi
