"""GUI-бэкенд интерактива (Flet) для core.ui: ask/confirm/checkbox через диалоги.

Ядро в async-флоу вызывает await ui.ask/confirm/checkbox; здесь создаём asyncio.Future,
показываем модальный диалог, кнопка кладёт результат в future (всё в одном event-loop —
никаких потоков). Это и даёт чек-боксы/диалоги без терминала.
"""
import asyncio

import flet as ft


class FletUi:
    def __init__(self, page: ft.Page):
        self.page = page

    async def ask(self, prompt: str, default: str = "") -> str:
        fut = asyncio.get_running_loop().create_future()
        field = ft.TextField(value=default, autofocus=True, expand=True)

        def ok(_):
            if not fut.done():
                fut.set_result((field.value or default).strip() or default)
            self.page.pop_dialog()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(prompt), content=field,
            actions=[ft.Button(content=ft.Text("OK"), on_click=ok)]))
        return await fut

    async def confirm(self, prompt: str) -> bool:
        fut = asyncio.get_running_loop().create_future()

        def done(val):
            def h(_):
                if not fut.done():
                    fut.set_result(val)
                self.page.pop_dialog()
            return h

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Подтверждение"),
            content=ft.Text(prompt, width=440),
            actions=[ft.Button(content=ft.Text("Да"), on_click=done(True)),
                     ft.Button(content=ft.Text("Нет"), on_click=done(False))]))
        return await fut

    async def select(self, title: str, labels: list[str], default_index: int = 0) -> int | None:
        """Компактный диалог: пояснение + кнопки-варианты в ряд + «Отмена» (→ None)."""
        fut = asyncio.get_running_loop().create_future()

        def choose(idx):
            def h(_):
                if not fut.done():
                    fut.set_result(idx)
                self.page.pop_dialog()
            return h

        actions = [ft.Button(content=ft.Text(lab), on_click=choose(i)) for i, lab in enumerate(labels)]
        actions.append(ft.TextButton(content=ft.Text("Отмена"), on_click=choose(None)))
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Выбор"),
            content=ft.Text(title, width=440),
            actions=actions, actions_alignment=ft.MainAxisAlignment.END))
        return await fut

    async def checkbox(self, title: str, labels: list[str], default_all: bool = False) -> list[int]:
        """Компактный диалог-список: пояснение + чек-боксы + «OK»/«Отмена» в ряд."""
        fut = asyncio.get_running_loop().create_future()
        boxes = [ft.Checkbox(label=lab, value=default_all) for lab in labels]

        def finish(cancel):
            def h(_):
                if not fut.done():
                    fut.set_result([] if cancel else [i for i, b in enumerate(boxes) if b.value])
                self.page.pop_dialog()
            return h

        content = ft.Column(
            [ft.Text(title), *boxes],
            scroll=ft.ScrollMode.AUTO, tight=True, spacing=6, width=440,
            height=min(360, 64 + 34 * len(boxes)))
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Выбор нод"), content=content,
            actions=[ft.TextButton(content=ft.Text("Отмена"), on_click=finish(True)),
                     ft.Button(content=ft.Text("OK"), on_click=finish(False))],
            actions_alignment=ft.MainAxisAlignment.END))
        return await fut