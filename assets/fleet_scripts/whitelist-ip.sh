#!/usr/bin/env bash
#
# whitelist-ip.sh — добавить IP в whitelist по всему флоту:
#   - fail2ban: ignoreip в /etc/fail2ban/jail.local + unban + reload
#   - UFW: allow from <IP> на нужные порты
# Идемпотентно: повторный запуск не плодит дубли.
#
# Usage: whitelist-ip.sh <IP> [--ports "p1 p2 ..."] [--apply]
#   default ports: 22 (просто SSH-доступ).
#   Для нового КЛАСТЕРНОГО доступа: --ports "22 5432 6543 6442 8008 2379"
#   Без --apply — dry-run (показывает команды).
#
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/_nodes.sh"

IP=""; PORTS="22"; DRY_RUN=true
while [[ $# -gt 0 ]]; do case "$1" in
  --ports) PORTS="$2"; shift 2 ;;
  --apply) DRY_RUN=false; shift ;;
  -*) echo "неизвестный флаг: $1"; exit 2 ;;
  *) IP="$1"; shift ;;
esac; done

[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Usage: $0 <IP> [--ports \"...\"] [--apply]"; exit 2; }

G='\033[0;32m'; Y='\033[1;33m'; N='\033[0m'
$DRY_RUN && echo -e "${Y}═══ DRY-RUN (для применения добавь --apply) ═══${N}" \
         || echo -e "${Y}═══ APPLY: whitelist $IP, порты [$PORTS] ═══${N}"

run() {  # run <ip> <cmd>
  local ip="$1" cmd="$2"
  if $DRY_RUN; then echo -e "  ${Y}[DRY $ip]${N} $cmd"
  else echo -e "  ${G}[RUN $ip]${N} $cmd"; ssh_node "$ip" "$cmd 2>&1" || true; fi
}

for ip in "${ALL_IPS[@]}"; do
  name=${IP_NAME[$ip]:-$ip}
  echo; echo -e "${G}━━━ $name ($ip) ━━━${N}"

  # 1. fail2ban ignoreip (добавляем только если ещё нет)
  run "$ip" "grep -q '^ignoreip' /etc/fail2ban/jail.local 2>/dev/null || echo 'ignoreip = 127.0.0.1/8 ::1' >> /etc/fail2ban/jail.local"
  run "$ip" "grep -qE '^ignoreip.*${IP//./\\.}' /etc/fail2ban/jail.local || sed -i '/^ignoreip/ s/\$/ $IP/' /etc/fail2ban/jail.local"
  run "$ip" "fail2ban-client set sshd unbanip $IP >/dev/null 2>&1 || true; fail2ban-client reload >/dev/null 2>&1 || systemctl reload fail2ban"

  # 2. UFW allow по портам
  for p in $PORTS; do
    run "$ip" "ufw allow from $IP to any port $p proto tcp comment 'whitelist $IP'"
  done
done

echo
$DRY_RUN && echo -e "${Y}Это был dry-run. Применить: $0 $IP --ports \"$PORTS\" --apply${NC:-$N}" \
         || echo -e "${G}Готово.${N} Проверка: ssh root@<ip> 'fail2ban-client get sshd ignoreip; ufw status | grep $IP'"
