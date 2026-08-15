"""Read-only дашборд: программы проекта → ноды (status/running) → версия + отставание.

Источники: programdata (программы по service-файлам), dispatcher.service_status
(привязки leader/standby + running), VERSION на ноде (SSH). Отставание считается через
git rev-list между SHA ноды и локальным (если оба в истории проекта).
"""
import asyncio
import shlex

from classes.manifest import lag_text as _lag, parse_manifest
from classes.ssh_client import SshClient
from core import ui
from core.validate import list_local_services
from database import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)

# Исход чтения VERSION с ноды (см. _read_manifest).
VER_OK = "ok"                  # манифест прочитан
VER_MISSING = "missing"        # ноды достали, файла нет / битый
VER_UNREACHABLE = "unreachable"  # нода не ответила (таймаут/обрыв SSH)
_VER_REASON = {VER_MISSING: "нет VERSION", VER_UNREACHABLE: "нода не ответила"}


async def show(ssh: SshClient, db: Database, project_dir: str, local) -> list[dict]:
    """Печатает дашборд и возвращает список нод, которые НАДО синхронизировать, в формате
    [{ip, name, commit, lag, unknown}] — отставшие (версия != локальной) плюс те, у кого
    версию выяснить не удалось (`unknown=True`): «версия неизвестна» — это не «актуальна»."""
    svcs = list_local_services(project_dir)
    names = [s.name for s in svcs if not s.is_template]
    records = await db.find_programs_by_service(names)
    print(f"\n══ Дашборд проекта · программ: {len(records)} · локально {local.short} ({local.branch})"
          f"{' DIRTY' if local.dirty else ''} ══")
    if not records:
        print("  Программы проекта не найдены в programdata.")
        return []
    ui.progress("Опрос версий на нодах…")
    # привязки всех записей заранее (нужны и для версий по нодам, и для перечня сервисов)
    binds: dict[int, list] = {rec["program_id"]: await db.get_service_bindings(rec["program_id"])
                              for rec in records}
    # группируем по folder: одна папка = один код = одна версия на ноду (не по каждому сервису)
    groups: dict[str, list] = {}
    for rec in records:
        groups.setdefault((rec["folder"] or "").rstrip("/"), []).append(rec)

    stale: dict[str, dict] = {}                       # ip → инфо (одна нода — один раз)
    lag_cache: dict[str, str] = {}                    # node_commit → текст отставания (git rev-list)
    for folder, recs in groups.items():
        # объединяем ноды всех сервисов папки (версия читается по ноде, а не по сервису)
        nodes_by_ip: dict[str, str] = {}
        for rec in recs:
            for b in binds[rec["program_id"]]:
                nodes_by_ip.setdefault(b["ip_address"], b["server_name"] or b["ip_address"])
        # ── версии по нодам: один раз на папку (один код = одна версия на ноду) ──
        print(f"\nКод: {folder or '(folder не задан в programdata)'}")
        if not folder:
            print("  версия неизвестна — путь установки не указан")
        elif not nodes_by_ip:
            print("  — нет привязок в dispatcher.service_status")
        else:
            ips = list(nodes_by_ip)
            mans = await asyncio.gather(*[_read_manifest(ssh, ip, folder) for ip in ips])
            for ip, (state, man) in zip(ips, mans):
                node = nodes_by_ip[ip]
                if state != VER_OK:
                    # Версию выяснить не удалось → нода идёт в синхронизацию (раньше молча
                    # выпадала и оставалась со старым кодом). Транспортный сбой тоже включаем:
                    # честная ошибка деплоя лучше тихого пропуска.
                    reason = _VER_REASON.get(state, state)
                    print(f"  🔌 {node:16} {reason:18} → версия неизвестна, беру в синхронизацию")
                    if ip not in stale:
                        stale[ip] = {"ip": ip, "name": node, "commit": None,
                                     "lag": f"версия неизвестна ({reason})", "unknown": True}
                    continue
                nc = man.get("commit", "")
                if nc not in lag_cache:
                    lag_cache[nc] = _lag(project_dir, nc, local.commit)
                lag = lag_cache[nc]
                icon = "✅" if lag == "up-to-date" else "⚠️"
                print(f"  {icon} {node:16} {lag:18} {man.get('short') or nc[:9]}")
                if nc and nc != local.commit and ip not in stale:
                    stale[ip] = {"ip": ip, "name": node, "commit": nc, "lag": lag, "unknown": False}
        # ── сервисы этой папки: только реестр имён (детальное состояние/leader — в сводке
        #    «Проверка состояния сервисов» выше; здесь не повторяем список нод по каждому юниту) ──
        disp = sum(1 for r in recs if r["dispatcher"])
        roster = ", ".join(r["service_name"] for r in recs)
        print(f"Сервисы ({len(recs)}, под диспетчером {disp}): {roster}")
    ui.progress("")
    return list(stale.values())


async def _read_manifest(ssh: SshClient, ip: str, folder: str) -> tuple[str, dict | None]:
    """Прочитать VERSION с ноды → (состояние, манифест).

    Различаем «файла нет» и «нода не ответила» (2026-08-15). Раньше оба случая давали None,
    нода печаталась как «🔌 нет VERSION» и МОЛЧА выпадала из синхронизации: update берёт ровно
    тех, кого дашборд положил в stale. Так BinoOptions уехал на 5 нод из 7 — NODE-3/NODE-4
    остались со старым кодом, причём именно на NODE-4 в тот момент работал бот.

    Отсутствие манифеста — не «актуальна», а «версия НЕизвестна»: такую ноду надо
    синхронизировать, а не пропускать. Транспортный сбой различаем по exit_status=255
    (его ставит SshClient.run на таймаут/обрыв); прочий ненулевой код — это `cat`, не нашедший
    файл, либо нет прав.
    """
    path = f"{folder}/{config.VERSION_FILE}"
    res = await ssh.run(ip, f"cat {shlex.quote(path)}", timeout=15)
    if res.ok:
        man = parse_manifest(res.stdout)
        return (VER_OK, man) if man else (VER_MISSING, None)   # пустой/битый JSON = нет версии
    return (VER_UNREACHABLE if res.exit_status == 255 else VER_MISSING), None