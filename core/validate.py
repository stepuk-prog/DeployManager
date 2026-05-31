"""Валидация соответствия service-файлов проекта и записей program.programdata.

Сверяем:
  • service_name (имя файла) — есть ли запись в programdata;
  • folder (БД) ↔ WorkingDirectory/ExecStart (файл).
При несоответствии — предупреждение и интерактивный запрос: что менять (БД, файл, пропустить, отмена).
"""
import glob
import os
import re
from dataclasses import dataclass

from core import ui
from database import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)

_WORKDIR_RE = re.compile(r"^WorkingDirectory=(.+)$", re.MULTILINE)
_EXEC_RE = re.compile(r"^ExecStart=(.+)$", re.MULTILINE)
_RESTART_RE = re.compile(r"^Restart=(.+)$", re.MULTILINE)


@dataclass
class LocalService:
    name: str            # имя файла, напр. binodex-1m-otc.service
    path: str            # абсолютный путь к файлу
    working_dir: str | None
    exec_start: str | None  # строка ExecStart= целиком
    restart: str | None  # значение Restart= (None если строки нет → systemd по умолч. 'no')
    is_template: bool    # шаблонный юнит (имя с '@')

    @property
    def restart_enabled(self) -> bool:
        """systemd-автоперезапуск включён (любое значение, кроме 'no')."""
        return bool(self.restart) and self.restart.strip().lower() != "no"

    @property
    def venv_dir(self) -> str | None:
        """Имя каталога venv из ExecStart (компонент перед '/bin/'). На серверах ожидается 'venv'."""
        if not self.exec_start:
            return None
        binpath = self.exec_start.split()[0]  # первый токен — путь к интерпретатору
        parts = binpath.split("/")
        if "bin" in parts:
            i = parts.index("bin")
            if i > 0:
                return parts[i - 1]
        return None


def parse_service_file(path: str) -> LocalService:
    text = open(path, encoding="utf-8").read()
    wd = _WORKDIR_RE.search(text)
    ex = _EXEC_RE.search(text)
    rs = _RESTART_RE.search(text)
    name = os.path.basename(path)
    return LocalService(
        name=name, path=path,
        working_dir=wd.group(1).strip() if wd else None,
        exec_start=ex.group(1).strip() if ex else None,
        restart=rs.group(1).strip() if rs else None,
        is_template="@" in name,
    )


def list_local_services(project_dir: str) -> list[LocalService]:
    paths = sorted(glob.glob(os.path.join(project_dir, "systemd", "*.service")))
    return [parse_service_file(p) for p in paths]


def _rewrite_service_path(svc: LocalService, new_folder: str) -> None:
    """Заменить путь установки в WorkingDirectory и префиксе ExecStart на new_folder."""
    text = open(svc.path, encoding="utf-8").read()
    old = (svc.working_dir or "").rstrip("/")
    new = new_folder.rstrip("/")
    if old:
        text = text.replace(old, new)
    text = _WORKDIR_RE.sub(f"WorkingDirectory={new}", text)
    with open(svc.path, "w", encoding="utf-8") as f:
        f.write(text)
    svc.working_dir = new
    logger.info("Файл %s: путь → %s", svc.name, new)


def _set_restart_no(svc: LocalService) -> None:
    """Выключить автоперезапуск: Restart=<...> → Restart=no (для совместимости с Dispatcher)."""
    text = open(svc.path, encoding="utf-8").read()
    text = _RESTART_RE.sub("Restart=no", text)
    with open(svc.path, "w", encoding="utf-8") as f:
        f.write(text)
    svc.restart = "no"
    logger.info("Файл %s: Restart → no", svc.name)


def _set_venv(svc: LocalService, old: str, new: str) -> None:
    """Заменить каталог venv в ExecStart: /<old>/bin/ → /<new>/bin/."""
    text = open(svc.path, encoding="utf-8").read()
    text = text.replace(f"/{old}/bin/", f"/{new}/bin/")
    with open(svc.path, "w", encoding="utf-8") as f:
        f.write(text)
    logger.info("Файл %s: venv %s → %s в ExecStart", svc.name, old, new)


def _check_venv(svc: LocalService) -> None:
    """ExecStart должен указывать на серверный venv (config.VENV_DIR, обычно 'venv', не '.venv')."""
    vd = svc.venv_dir
    if not vd or vd == config.VENV_DIR:
        return
    print(f"  {svc.name:26} ⚠️ ExecStart использует venv '{vd}', "
          f"а на серверах ожидается '{config.VENV_DIR}'")
    ans = ui.ask(f"      [f] исправить (/{vd}/bin → /{config.VENV_DIR}/bin) / [s] оставить", "s").lower()
    if ans == "f":
        _set_venv(svc, vd, config.VENV_DIR)
        print("      ✅ Исправлено.")
    else:
        print("      ⏭️  Оставлено как есть.")


def _check_restart(svc: LocalService) -> None:
    """Restart должен быть выключен под Dispatcher. При включённом — предупредить и спросить."""
    if not svc.restart_enabled:
        return
    print(f"  {svc.name:26} ⚠️ Restart={svc.restart} (включён)")
    print("      Под Dispatcher автоперезапуск systemd должен быть ВЫКЛЮЧЕН (Restart=no):")
    print("      иначе systemd сам поднимет упавший сервис и сломает failover диспетчера.")
    ans = ui.ask("      [f] исправить (Restart=no) / [s] оставить", "s").lower()
    if ans == "f":
        _set_restart_no(svc)
        print("      ✅ Исправлено.")
    else:
        print("      ⏭️  Оставлено как есть.")


def _norm(p: str | None) -> str:
    return (p or "").rstrip("/")


async def validate_paths(db: Database, project_dir: str) -> bool:
    """Сверка + интерактивное разрешение. True — можно деплоить, False — отмена."""
    local = list_local_services(project_dir)
    if not local:
        print("⚠️  В systemd/ нет service-файлов — нечего валидировать.")
        return True

    names = [s.name for s in local if not s.is_template]
    records = await db.find_programs_by_service(names)
    db_by_name = {r["service_name"]: r for r in records}

    print("\n── Валидация service-файлов ↔ programdata ──")
    all_ok = True
    for svc in local:
        _check_venv(svc)     # ExecStart → серверный venv (не .venv)
        _check_restart(svc)  # Restart должен быть выключен под Dispatcher (для всех юнитов)
        if svc.is_template:
            print(f"  {svc.name:26} — шаблон (в БД не проверяется)")
            continue
        rec = db_by_name.get(svc.name)
        if rec is None:
            print(f"  {svc.name:26} ❌ нет записи в programdata")
            all_ok = False
            if not await _resolve_missing(db, svc):
                return False
            continue
        if _norm(svc.working_dir) == _norm(rec["folder"]):
            print(f"  {svc.name:26} ✅ путь совпадает ({rec['folder']})")
        else:
            all_ok = False
            print(f"  {svc.name:26} ❌ путь расходится:")
            print(f"      файл: {svc.working_dir}")
            print(f"      БД:   {rec['folder']}")
            if not await _resolve_mismatch(db, svc, rec):
                return False

    if all_ok:
        print("Все пути согласованы.\n")
    return True


async def _resolve_missing(db: Database, svc: LocalService) -> bool:
    """Юнита нет в programdata. Возвращает True — продолжать деплой."""
    from core.programdata import create_record_interactive
    while True:
        print("    Записи в programdata нет. Что делаем?")
        print(f"      [n] создать запись сейчас (service_name={svc.name}, folder={svc.working_dir})")
        print("      [o] добавить отдельно/позже (продолжить деплой без записи)")
        print("      [a] отмена деплоя")
        ans = ui.ask("    Выбор [n/o/a]", "a").lower()
        if ans == "n":
            await create_record_interactive(db, service_name=svc.name, folder=svc.working_dir)
            return True
        if ans == "o":
            print("    ⏭️  Продолжаю без записи (добавишь отдельно).")
            return True
        if ans == "a":
            print("    🛑 Отмена.")
            return False
        print("    Не понял, повтори.")


async def _resolve_mismatch(db: Database, svc: LocalService, rec) -> bool:
    """Интерактивно разрешить расхождение пути. Возвращает True — продолжать."""
    while True:
        print("    Что менять?")
        print(f"      [d] БД → записать путь файла ({svc.working_dir}) в programdata.folder")
        print(f"      [f] файл → записать путь из БД ({rec['folder']}) в service-файл")
        print("      [s] пропустить (оставить как есть)")
        print("      [a] отмена деплоя")
        ans = ui.ask("    Выбор [d/f/s/a]", "a").lower()
        if ans == "d":
            await db.update_program_folder(rec["program_id"], _norm(svc.working_dir))
            print("    ✅ БД обновлена.")
            return True
        if ans == "f":
            _rewrite_service_path(svc, rec["folder"])
            print("    ✅ Файл обновлён.")
            return True
        if ans == "s":
            print("    ⏭️  Пропущено (расхождение осталось).")
            return True
        if ans == "a":
            print("    🛑 Отмена.")
            return False
        print("    Не понял, повтори.")
