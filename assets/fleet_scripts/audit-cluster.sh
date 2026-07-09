#!/usr/bin/env bash
#
# audit-cluster.sh — read-only свод здоровья кластера и флота.
# Ничего не меняет. Проверяет:
#   1. etcd      — endpoint health всех 3 членов (3/3 healthy?)
#   2. Patroni   — patronictl list (роли, TL, Lag)
#   3. HAProxy   — show stat на клиентах (виден ли лидер)
#   4. systemd   — dispatcher-managed программы НЕ должны быть `enabled`
#                  (кастомные unit'ы из /home/vova; см. памятку про service_enabled)
#
# Usage: audit-cluster.sh [--dry-run] [--quick]
#   --dry-run  только показать, на какие узлы пойдём (без подключений)
#   --quick    только etcd + patroni (пропустить обход клиентов)
#
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/_nodes.sh"

DRY_RUN=false; QUICK=false
for a in "$@"; do case "$a" in
  --dry-run) DRY_RUN=true ;; --quick) QUICK=true ;;
  *) echo "неизвестный аргумент: $a"; exit 2 ;;
esac; done

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
hdr() { echo; echo -e "${G}━━━ $* ━━━${N}"; }

if $DRY_RUN; then
  echo -e "${Y}DRY-RUN${N} — узлы кластера: ${CLUSTER_IPS[*]}"
  echo "клиенты: ${CLIENT_IPS[*]}"
  exit 0
fi

# 1. etcd health (с любого живого cluster-узла)
hdr "1. etcd endpoint health"
EP=$(IFS=,; echo "${CLUSTER_IPS[*]/%/:2379}"); EP="http://${EP//,/,http://}"
for ip in "${CLUSTER_IPS[@]}"; do
  if ssh_node "$ip" "etcdctl --endpoints=$EP --dial-timeout=5s endpoint health 2>&1"; then break; fi
done

# 2. Patroni
hdr "2. patronictl list"
for ip in "${CLUSTER_IPS[@]}"; do
  if ssh_node "$ip" "patronictl -c /etc/patroni/patroni.yml list 2>&1"; then break; fi
done

if $QUICK; then echo; echo -e "${G}quick-режим: клиенты пропущены${N}"; exit 0; fi

# 3. HAProxy на клиентах — виден ли лидер
hdr "3. HAProxy show stat (клиенты)"
for ip in "${CLIENT_IPS[@]}"; do
  name=${IP_NAME[$ip]:-$ip}
  printf "  %-12s (%s): " "$name" "$ip"
  ssh_node "$ip" "echo 'show stat' | socat /var/run/haproxy.sock stdio 2>/dev/null | awk -F, '/leader_pgbouncer/ && \$18==\"UP\"{print \$2\" UP\"}' | head -1" 2>/dev/null \
    | grep -q UP && echo -e "${G}лидер виден${N}" || echo -e "${Y}лидер не виден / нет сокета${N}"
done

# 4. dispatcher-managed программы не должны быть enabled
hdr "4. systemd-автозапуск прикладных программ (должно быть пусто)"
FOUND=0
for ip in "${ALL_IPS[@]}"; do
  name=${IP_NAME[$ip]:-$ip}
  bad=$(ssh_node "$ip" '
    for u in $(systemctl list-unit-files --state=enabled --no-legend --type=service 2>/dev/null | awk "{print \$1}"); do
      fp=$(systemctl show -p FragmentPath --value "$u" 2>/dev/null)
      case "$fp" in /etc/systemd/system/*) ;; *) continue;; esac
      ex=$(systemctl show -p ExecStart --value "$u" 2>/dev/null)
      wd=$(systemctl show -p WorkingDirectory --value "$u" 2>/dev/null)
      case "$ex$wd" in */home/vova/*) echo "$u";; esac
    done' 2>/dev/null) || true
  if [[ -n "$bad" ]]; then
    FOUND=1
    echo -e "  ${R}$name ($ip):${N}"; echo "$bad" | sed 's/^/      /'
  fi
done
[[ $FOUND -eq 0 ]] && echo -e "  ${G}✅ ни одной dispatcher-managed программы в автозапуске${N}"
echo; echo -e "${G}Готово.${N}"
