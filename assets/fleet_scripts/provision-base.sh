#!/usr/bin/env bash
#
# provision-base.sh — ОБЩАЯ база узла флота (для ЛЮБОГО типа: клиент ИЛИ член кластера).
# Запускать НА новом узле под root. Идемпотентно (повторный запуск безопасен).
# Делает: hostname/tz, локали, пакеты (incl build-essential/python-стек), vova+sudo+
#         ключ+polkit, sshd-harden, fail2ban, UFW (только 22), needrestart-дропин,
#         GPU-blacklist, tmpfiles-уборку, nic-ring-tune, сборку+установку HAProxy-бинаря.
# НЕ делает: РОЛЕВУЮ часть — haproxy .cfg/юнит (клиент → provision-client.sh; член
#            кластера → provision-cluster-member.sh) и разворачивание watchdog/программ
#            (через диспетчер/DeployManager).
#
# Usage:  ./provision-base.sh --hostname <name> [--vova-pubkey "ssh-ed25519 ..."]
#
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Запускать под root."; exit 1; }

HOSTNAME_NEW=""; VOVA_PUBKEY=""
while [[ $# -gt 0 ]]; do case "$1" in
  --hostname) HOSTNAME_NEW="$2"; shift 2 ;;
  --vova-pubkey) VOVA_PUBKEY="$2"; shift 2 ;;
  *) echo "неизвестный аргумент: $1"; exit 2 ;;
esac; done

HAPROXY_VER=3.1.0
G='\033[0;32m'; N='\033[0m'
step(){ echo -e "\n${G}━━━ $* ━━━${N}"; }

step "1/9 hostname + timezone"
[[ -n "$HOSTNAME_NEW" ]] && { hostnamectl set-hostname "$HOSTNAME_NEW"; grep -q "$HOSTNAME_NEW" /etc/hosts || echo "127.0.1.1 $HOSTNAME_NEW" >> /etc/hosts; }
timedatectl set-timezone Europe/Moscow

step "2/9 локали (ru_RU + en_US) — критично для совместимости с кластером"
apt-get update -qq
apt-get install -y -qq locales
sed -i 's/^# *ru_RU\.UTF-8/ru_RU.UTF-8/; s/^# *en_US\.UTF-8/en_US.UTF-8/' /etc/locale.gen
locale-gen

step "3/9 пакеты"
apt-get install -y -qq vim tmux htop git curl wget unzip tree socat netcat-openbsd \
  jq dos2unix net-tools ethtool build-essential libssl-dev libpcre3-dev zlib1g-dev rsyslog ufw fail2ban needrestart

step "4/9 пользователь vova"
if ! id vova &>/dev/null; then adduser --disabled-password --gecos "" vova; fi
usermod -aG sudo vova
if [[ -n "$VOVA_PUBKEY" ]]; then
  install -d -m 700 -o vova -g vova /home/vova/.ssh
  grep -qF "$VOVA_PUBKEY" /home/vova/.ssh/authorized_keys 2>/dev/null || echo "$VOVA_PUBKEY" >> /home/vova/.ssh/authorized_keys
  chown vova:vova /home/vova/.ssh/authorized_keys; chmod 600 /home/vova/.ssh/authorized_keys
else
  echo "  (--vova-pubkey не задан — ключ разложи вручную)"
fi

# Права vova для управляющего слоя (WD2/GD2/CD2 бегут под vova):
# polkit-грант manage-units — чтобы `systemctl stop/start/restart/kill/reset-failed`
# под vova не требовал интерактивной авторизации (иначе на Ubuntu 24.04 / polkit 124
# падает). См. [[wd2-polkit-grant-2026-06-15]].
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

step "5/9 sshd hardening"
sshd_set(){ grep -qE "^$1 " /etc/ssh/sshd_config && sed -i "s/^$1 .*/$1 $2/" /etc/ssh/sshd_config || echo "$1 $2" >> /etc/ssh/sshd_config; }
sshd_set MaxSessions 10; sshd_set MaxStartups 10
sshd_set ClientAliveInterval 30; sshd_set ClientAliveCountMax 10
systemctl restart ssh 2>/dev/null || systemctl restart sshd

step "6/9 fail2ban (backend=systemd для Ubuntu 24.04)"
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

step "7/9 UFW (deny incoming, разрешён только SSH — ролевые порты открывает роль-скрипт)"
ufw --force reset >/dev/null
ufw default deny incoming; ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw --force enable

step "8/9 needrestart drop-in — не авто-рестартить управляющий слой при апгрейде библиотек"
install -d /etc/needrestart/conf.d
cat > /etc/needrestart/conf.d/zz-no-autorestart-cluster.conf <<'EOF'
# Ops: prevent needrestart from auto-restarting cluster-critical / management
# daemons after library upgrades (libssl / libsystemd refresh via unattended-
# upgrades). Does NOT affect crash recovery (systemd Restart= still works) nor
# manual `systemctl restart`. Anchored (^...$) so cron.service /
# networkd-dispatcher.service are NOT matched.
# Rationale: 2026-06-10 06:02 systemd-upgrade bounced patroni; 2026-06-11 06:06
# libssl-upgrade bounced the whole Dispatcher 2.0 layer + bots, outside coordination.
# On client nodes (claster=f) only ^watchdog\.service$ matches; the rest are no-ops,
# but the file is kept identical fleet-wide for maintainability.
$nrconf{override_rc}->{qr(^patroni\.service$)}           = 0;
$nrconf{override_rc}->{qr(^etcd\.service$)}              = 0;
$nrconf{override_rc}->{qr(^haproxy(_client)?\.service$)} = 0;
$nrconf{override_rc}->{qr(^pgbouncer\.service$)}         = 0;
$nrconf{override_rc}->{qr(^dispatcher\.service$)}        = 0;  # GD  (cluster nodes only)
$nrconf{override_rc}->{qr(^watchdog\.service$)}          = 0;  # WD  (all fleet nodes)
$nrconf{override_rc}->{qr(^cron-dispatcher\.service$)}   = 0;  # CD
$nrconf{override_rc}->{qr(^servermanager2\.service$)}    = 0;  # ServerManager2 bot
EOF

step "8b/9 apt-blacklist GPU-стека — не авто-апгрейдить mesa/libdrm под живым Chromium"
# Unattended-upgrades, подменяя графический стек (mesa/libgbm/libdrm/libva/libllvm)
# из-под живого Playwright-Chromium, ломает GL/GBM-рендерер: page.goto виснет на
# domcontentloaded → "Не смог инициализировать браузер" → Watchdog crash-loop.
# Инцидент 2026-06-16 06:30 (node-5, otc-screen-*). В отличие от needrestart-дропина
# выше (он про авто-РЕСТАРТ демонов), здесь блокируем авто-АПГРЕЙД самих библиотек.
# Эти пакеты обновлять ВРУЧНУЮ в окне с остановкой ботов; секьюрити-патчи остального
# продолжают ставиться автоматически. Безвреден на узлах без Playwright.
cat > /etc/apt/apt.conf.d/99-playwright-gpu-blacklist <<'EOF'
// Playwright/Chromium GPU/render-стек: НЕ авто-апгрейдить через unattended-upgrades.
// Подмена mesa/libdrm/libgbm/libva из-под живого Chromium ломает GL/GBM-рендерер →
// page.goto виснет на domcontentloaded → "Не смог инициализировать браузер" →
// WD crash-loop. Инцидент 2026-06-16 06:30 (node-5, otc-screen-*).
// Эти пакеты обновлять ВРУЧНУЮ в окне с остановкой бот-ов. Секьюрити-патчи
// остального продолжают ставиться автоматически.
Unattended-Upgrade::Package-Blacklist {
    "mesa";
    "^libdrm";
    "^libgbm";
    "^libva";
    "^libllvm";
    "^libgl1-amber";
};
EOF

step "8c/9 tmpfiles — авто-уборка протёкших браузер-профилей из /tmp"
# Браузер-боты (Playwright/Selenium-geckodriver) создают временные профили в /tmp
# и НЕ удаляют при крэше/рестарте → орфаны копятся. Инцидент 2026-06-28: на node-7
# за 503 дня uptime /tmp забился на 280 ГБ (rust_mozprofile*/firefox_profile_*/
# playwright-*). systemd-tmpfiles тип 'e' чистит по mtime КАЖДОГО файла внутри
# (рекурсивно): активный профиль постоянно пишется → его свежие файлы остаются →
# профиль НЕ удаляется; орфан без записи >12ч → вычищается. Безвреден на узлах без
# браузер-ботов (нечего матчить). Оставляет пустой каталог-обрубок (КБ) — некритично.
cat > /etc/tmpfiles.d/zz-browser-profile-leak.conf <<'EOF'
# Type Path                                Mode UID GID Age Argument
e /tmp/rust_mozprofile*                    -    -   -   12h -
e /tmp/firefox_profile_*                   -    -   -   12h -
e /tmp/playwright_firefoxdev_profile-*     -    -   -   12h -
e /tmp/playwright-artifacts-*              -    -   -   12h -
EOF
systemctl enable --now systemd-tmpfiles-clean.timer 2>/dev/null || true
systemd-tmpfiles --clean 2>/dev/null || true

step "8d/9 nic-ring-tune — RX/TX ring NIC до аппаратного максимума (bond-aware)"
# Лечит локальные FIFO-дропы пакетов (на cluster2 было ~2млн). Персистентно через
# oneshot-юнит (после ребута ring сбрасывается в дефолт). Bond-aware (LACP/active-
# backup). См. [[nic-ring-tune-fornex-2026-06-21]]. Безвреден везде (ставит max).
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

step "9/9 HAProxy $HAPROXY_VER из исходников (бинарь — общий для клиента и члена кластера)"
if ! haproxy -v 2>/dev/null | grep -q "$HAPROXY_VER"; then
  cd /usr/local/src
  wget -q "https://www.haproxy.org/download/${HAPROXY_VER%.*}/src/haproxy-${HAPROXY_VER}.tar.gz"
  tar -xzf "haproxy-${HAPROXY_VER}.tar.gz"; cd "haproxy-${HAPROXY_VER}"
  make -j"$(nproc)" TARGET=linux-glibc USE_OPENSSL=1 USE_PCRE=1 USE_ZLIB=1
  make install
  ln -sf /usr/local/sbin/haproxy /usr/sbin/haproxy
fi
id haproxy &>/dev/null || { groupadd -r haproxy; useradd -r -g haproxy -s /usr/sbin/nologin -d /var/lib/haproxy haproxy; }
install -d -o haproxy -g haproxy /var/lib/haproxy

echo -e "\n${G}✅ База узла настроена.${N}"
echo "Дальше — РОЛЕВАЯ часть: клиент → provision-client.sh --tail-only ; член кластера → provision-cluster-member.sh"
