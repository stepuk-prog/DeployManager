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
# Диагноз разделён: недоступный SSH — это НЕ «нет сокета». Раньше stderr ssh глушился
# `2>/dev/null`, и отказ в доступе выглядел как авария HAProxy (ложная тревога 03-09).
for ip in "${CLIENT_IPS[@]}"; do
  name=${IP_NAME[$ip]:-$ip}
  printf "  %-12s (%s): " "$name" "$ip"
  # Узел отдаёт СЫРОЙ csv, разбираем локально: так не нужно экранировать awk через ssh.
  if out=$(ssh_node "$ip" '
      pgrep -x haproxy >/dev/null 2>&1 || { echo NOPROC; exit 0; }
      [ -S /var/run/haproxy.sock ]     || { echo NOSOCK; exit 0; }
      command -v socat >/dev/null 2>&1 || { echo NOSOCAT; exit 0; }
      echo "show stat" | socat /var/run/haproxy.sock stdio 2>/dev/null
    ' 2>&1); then rc=0; else rc=$?; fi
  case "$out" in
    NOPROC)  echo -e "${R}haproxy не запущен${N} (юнит клиента — haproxy_client)"; continue ;;
    NOSOCK)  echo -e "${Y}нет сокета${N} /var/run/haproxy.sock"; continue ;;
    NOSOCAT) echo -e "${Y}на узле нет socat — проверить нечем${N}"; continue ;;
  esac
  if [ "$rc" -ne 0 ]; then
    echo -e "${R}SSH недоступен${N} (rc=$rc): $(echo "$out" | tail -1)"; continue
  fi
  # Колонки ищем ПО ЗАГОЛОВКУ csv, а не по номеру: набор полей растёт от версии к версии
  # haproxy, и захардкоженный $18 однажды поедет.
  read -r lbl addr <<<"$(awk -F, '
      /^#/ { for (i=1;i<=NF;i++) { h=$i; sub(/^# /,"",h); c[h]=i } ; next }
      $1=="leader_pgbouncer" && $2!="BACKEND" && $(c["status"])=="UP" { print $2, $(c["addr"]); exit }
    ' <<<"$out")"
  if [ -z "$lbl" ]; then
    echo -e "${R}HAProxy жив, но лидера в leader_pgbouncer нет${N}"; continue
  fi
  # Метка server'а в haproxy.cfg историческая и на части узлов ВРЁТ (node3 = cluster3
  # при живом клиенте node-3 на другом IP). Печатаем имя из БД по IP, метку — только
  # когда она расходится, чтобы расхождение было видно, а не путало.
  real=${IP_NAME[${addr%%:*}]:-}
  if [ -n "$real" ] && [ "$real" != "$lbl" ]; then
    echo -e "${G}лидер виден${N} → $real ($addr), метка в haproxy.cfg: ${Y}$lbl${N}"
  else
    echo -e "${G}лидер виден${N} → ${real:-$lbl} ($addr)"
  fi
done

# 4. dispatcher-managed программы не должны быть enabled
hdr "4. systemd-автозапуск прикладных программ (должно быть пусто)"
# Правило «ExecStart под /home/vova = прикладная программа» точное: инфра-боты
# (GD2/WD2/CD2) живут в /opt и под него не попадают. Исключение ровно одно —
# ServerManager2 в /opt так и не переехал, автозапуск ему положен. Не прячем, а
# выносим отдельной строкой: пропадёт из виду — забудем, что он ещё в /home/vova.
INFRA_UNITS=" servermanager2.service "
FOUND=0
INFRA_SEEN=""
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
  prog=""
  while IFS= read -r u; do
    [[ -n "$u" ]] || continue
    case "$INFRA_UNITS" in
      *" $u "*) INFRA_SEEN+="      $u @ $name"$'\n' ;;
      *)        prog+="      $u"$'\n' ;;
    esac
  done <<< "$bad"
  if [[ -n "$prog" ]]; then
    FOUND=1
    echo -e "  ${R}$name ($ip):${N}"; printf '%s' "$prog"
  fi
done
[[ $FOUND -eq 0 ]] && echo -e "  ${G}✅ ни одной dispatcher-managed программы в автозапуске${N}"
if [[ -n "$INFRA_SEEN" ]]; then
  echo -e "  ${G}инфра-боты в автозапуске (норма):${N}"; printf '%s' "$INFRA_SEEN"
fi
echo; echo -e "${G}Готово.${N}"
