#!/usr/bin/env bash
#
# swap-node-ip.sh — автоматизация Шага 6 из docs/node_replacement.md.
# После etcdctl member remove/add заменяет OLD_IP → NEW_IP в конфигах на ЖИВЫХ
# cluster-нодах (Patroni/etcd/servermanager2/pg_hba/etcd-defrag/.zshrc), делает
# бэкап каждого файла и финальный grep-sweep. Без этого через часы/сутки ловится
# ложный failover (инцидент 2026-05-24, см. грабля №7).
#
# Usage: swap-node-ip.sh --old <OLD_IP> --new <NEW_IP> [--apply] [--reload]
#   без --apply  — dry-run (показывает, где есть OLD_IP)
#   --reload     — после sed: daemon-reload + restart servermanager2 + patroni reload
#
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/_nodes.sh"

OLD=""; NEW=""; DRY_RUN=true; RELOAD=false
while [[ $# -gt 0 ]]; do case "$1" in
  --old) OLD="$2"; shift 2 ;; --new) NEW="$2"; shift 2 ;;
  --apply) DRY_RUN=false; shift ;; --reload) RELOAD=true; shift ;;
  *) echo "неизвестный аргумент: $1"; exit 2 ;;
esac; done
[[ "$OLD" =~ ^[0-9.]+$ && "$NEW" =~ ^[0-9.]+$ ]] || { echo "Usage: $0 --old <IP> --new <IP> [--apply] [--reload]"; exit 2; }

G='\033[0;32m'; Y='\033[1;33m'; N='\033[0m'
OLD_RE=${OLD//./\\.}
TS=$(date +%Y%m%d-%H%M%S)

FILES=(
  /etc/patroni/patroni.yml
  /etc/systemd/system/servermanager2.service
  /etc/postgresql/16/main/pg_hba.conf
  /etc/etcd-defrag.env
  /home/vova/.zshrc
  /root/.zshrc
)

$DRY_RUN && echo -e "${Y}═══ DRY-RUN: $OLD → $NEW (для применения --apply) ═══${N}" \
         || echo -e "${Y}═══ APPLY: $OLD → $NEW на cluster-нодах ═══${N}"

for ip in "${CLUSTER_IPS[@]}"; do
  name=${IP_NAME[$ip]:-$ip}
  echo; echo -e "${G}━━━ $name ($ip) ━━━${N}"
  for f in "${FILES[@]}"; do
    if $DRY_RUN; then
      hits=$(ssh_node "$ip" "grep -l '$OLD_RE' $f /etc/etcd/etcd-*.yml 2>/dev/null" 2>/dev/null) || true
      [[ -n "$hits" ]] && echo "$hits" | sed 's/^/  [есть OLD] /'
    else
      ssh_node "$ip" "
        for ff in $f /etc/etcd/etcd-*.yml; do
          [ -f \"\$ff\" ] && grep -q '$OLD_RE' \"\$ff\" 2>/dev/null || continue
          cp \"\$ff\" \"\$ff.bak-$TS\"
          sed -i 's|$OLD_RE|$NEW|g' \"\$ff\"
          echo '  FIXED: '\$ff
        done"
    fi
  done
  if ! $DRY_RUN && $RELOAD; then
    echo "  -- reload --"
    ssh_node "$ip" "systemctl daemon-reload; systemctl restart servermanager2.service 2>/dev/null || true"
  fi
done

if ! $DRY_RUN && $RELOAD; then
  echo; echo -e "${G}Patroni reload (cluster-wide)${N}"
  ssh_node "${CLUSTER_IPS[0]}" "patronictl -c /etc/patroni/patroni.yml reload postgres-cluster --force" || true
fi

echo; echo -e "${G}━━━ Финальный sweep (не должно остаться $OLD) ━━━${N}"
for ip in "${CLUSTER_IPS[@]}"; do
  name=${IP_NAME[$ip]:-$ip}
  left=$(ssh_node "$ip" "grep -rn '$OLD_RE' /etc/patroni /etc/etcd /etc/systemd/system/servermanager2.service /etc/postgresql /etc/etcd-defrag.env /home/vova/.zshrc /root/.zshrc 2>/dev/null | grep -v '\.bak-'" 2>/dev/null) || true
  [[ -z "$left" ]] && echo -e "  ${G}$name: чисто${N}" || { echo -e "  ${Y}$name: остались ссылки:${N}"; echo "$left" | sed 's/^/      /'; }
done
$DRY_RUN && echo -e "\n${Y}Это dry-run. Применить: $0 --old $OLD --new $NEW --apply --reload${N}"
