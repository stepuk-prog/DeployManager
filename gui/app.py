"""Flet-окно DeployManager. Переиспользует CLI-ядро (cli.run) с GUI-бэкендом ui +
перенаправлением вывода в лог-панель. Ветки те же: new / add / check / manage / uninstall.
"""
import sys
from argparse import Namespace

import flet as ft

import cli
from classes.manifest import local_version
from core import ui
from gui.backend import FletUi
from gui.log_sink import LogSink


async def main(page: ft.Page):
    page.title = "DeployManager"
    try:
        page.window.width, page.window.height = 980, 760
    except Exception:
        pass

    state = {"path": ""}
    path_field = ft.TextField(label="Папка проекта", read_only=True, expand=True)
    version_lbl = ft.Text("—")
    log_view = ft.ListView(expand=True, auto_scroll=True, spacing=1)
    file_picker = ft.FilePicker()
    try:
        page.services.append(file_picker)
    except Exception:
        page.services = [file_picker]

    sink = LogSink(log_view, page)
    flet_ui = FletUi(page)

    async def choose_project(_):
        path = await file_picker.get_directory_path(dialog_title="Выберите папку проекта")
        if not path:
            return
        state["path"] = path
        path_field.value = path
        try:
            lv = local_version(path)
            version_lbl.value = f"{lv.short} ({lv.branch}){'  DIRTY' if lv.dirty else ''}"
        except Exception as ex:
            version_lbl.value = f"не git: {ex}"
        page.update()

    branch_buttons: list[ft.Control] = []

    def set_busy(b: bool):
        for btn in branch_buttons:
            btn.disabled = b
        page.update()

    async def run_branch(action: str):
        if not state["path"]:
            sink.write("Сначала выберите папку проекта (кнопка «Обзор…»).\n")
            return
        set_busy(True)
        old_stdout = sys.stdout
        sys.stdout = sink
        ui.set_backend(flet_ui)
        try:
            args = Namespace(project=state["path"], action=action, nodes=None,
                             dry_run=False, yes=False, command=None)
            await cli.run(args)
        except SystemExit:
            pass
        except Exception as ex:
            sink.write(f"\n‼️ Ошибка: {ex}\n")
        finally:
            sys.stdout = old_stdout
            ui.set_backend(None)
            set_busy(False)
            sink.write("— готово —\n")

    def branch(label: str, action: str, **kw) -> ft.Control:
        btn = ft.Button(content=ft.Text(label),
                        on_click=lambda e, a=action: page.run_task(run_branch, a), **kw)
        branch_buttons.append(btn)
        return btn

    page.add(
        ft.Row([path_field, ft.Button(content=ft.Text("Обзор…"), on_click=choose_project)]),
        ft.Row([ft.Text("Версия:"), version_lbl]),
        ft.Row([
            branch("Деплой с нуля", "new"),
            branch("Добавить сервер", "add"),
            branch("Проверить версии", "check"),
            branch("Управление", "manage"),
            branch("Деинсталляция", "uninstall"),
        ], wrap=True),
        ft.Container(content=log_view, expand=True, padding=8,
                     border=ft.border.all(1, ft.Colors.GREY), border_radius=6),
    )