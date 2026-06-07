"""GUI-бэкенд интерактива (Flet) для core.ui: ask/confirm/checkbox через диалоги.

Ядро в async-флоу вызывает await ui.ask/confirm/checkbox; здесь создаём asyncio.Future,
показываем модальный диалог, кнопка кладёт результат в future (всё в одном event-loop —
никаких потоков). Это и даёт чек-боксы/диалоги без терминала.
"""
import asyncio

import flet as ft


def _ok_button(text: str, on_click) -> ft.Button:
    """Позитивная кнопка подтверждения (Да/OK) — светло-зелёная, чёрный текст."""
    return ft.Button(content=ft.Text(text, color=ft.Colors.BLACK),
                     bgcolor=ft.Colors.LIGHT_GREEN_400, on_click=on_click)


def _no_button(text: str, on_click) -> ft.Button:
    """Кнопка отрицания (Нет/Отмена) — приглушённый (не яркий) красный, чёрный текст."""
    return ft.Button(content=ft.Text(text, color=ft.Colors.BLACK),
                     bgcolor=ft.Colors.RED_200, on_click=on_click)


def _title_with_close(text: str, on_close) -> ft.Row:
    """Заголовок диалога с крестиком закрытия справа (всегда виден, даже у длинных окон)."""
    return ft.Row(
        [ft.Text(text, weight=ft.FontWeight.BOLD, expand=True),
         ft.IconButton(icon=ft.Icons.CLOSE, tooltip="Закрыть", on_click=on_close)],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN)


class FletUi:
    def __init__(self, page: ft.Page):
        self.page = page
        self.status_label: ft.Text | None = None   # строка статуса прогресса (задаёт приложение)

    def progress(self, text: str) -> None:
        """Обновить строку статуса долгой операции (спиннер переключает само приложение)."""
        if self.status_label is not None:
            self.status_label.value = text
            self.page.update()

    async def ask(self, prompt: str, default: str = "", cancelable: bool = False,
                  ok_label: str = "✅ OK", cancel_label: str = "✖️ Отмена") -> str | None:
        """Ввод строки. cancelable=True — добавляет «Отмена» (→ None = отмена операции)."""
        fut = asyncio.get_running_loop().create_future()
        field = ft.TextField(value=default, autofocus=True, expand=True)

        def ok(_):
            if not fut.done():
                fut.set_result((field.value or default).strip() or default)
            self.page.pop_dialog()

        def cancel(_):
            if not fut.done():
                fut.set_result(None)
            self.page.pop_dialog()

        actions = [_ok_button(ok_label, ok)]
        if cancelable:
            actions = [_no_button(cancel_label, cancel), _ok_button(ok_label, ok)]
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(prompt), content=field, actions=actions,
            actions_alignment=ft.MainAxisAlignment.END))
        return await fut

    async def radio(self, title: str, labels: list[str], default_index: int = 0,
                    ok_label: str = "✅ Продолжить", cancel_label: str = "✖️ Отмена") -> int | None:
        """Выбор ровно одного варианта радиокнопками → индекс или None (отмена)."""
        fut = asyncio.get_running_loop().create_future()
        default = default_index if 0 <= default_index < len(labels) else 0
        group = ft.RadioGroup(
            value=str(default),
            content=ft.Column([ft.Radio(value=str(i), label=lab) for i, lab in enumerate(labels)],
                              tight=True, spacing=4))

        def ok(_):
            v = group.value
            if not fut.done():
                fut.set_result(int(v) if v is not None and v.isdigit() else None)
            self.page.pop_dialog()

        def cancel(_):
            if not fut.done():
                fut.set_result(None)
            self.page.pop_dialog()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=_title_with_close("Выбор", cancel),
            content=ft.Column([ft.Text(title), group], tight=True, spacing=10, width=440),
            actions=[_no_button(cancel_label, cancel), _ok_button(ok_label, ok)],
            actions_alignment=ft.MainAxisAlignment.END))
        return await fut

    async def confirm(self, prompt: str, danger: bool = False,
                      ok_label: str = "✅ Да", cancel_label: str = "✖️ Нет") -> bool:
        fut = asyncio.get_running_loop().create_future()

        def done(val):
            def h(_):
                if not fut.done():
                    fut.set_result(val)
                self.page.pop_dialog()
            return h

        if danger:
            title = ft.Text("⚠️ Внимание", color=ft.Colors.RED, weight=ft.FontWeight.BOLD)
            content = ft.Text(prompt, width=440, color=ft.Colors.RED)
            yes = ft.Button(content=ft.Text("🗑️ Да", color=ft.Colors.WHITE),
                            bgcolor=ft.Colors.RED, on_click=done(True))
            # в опасном диалоге «Нет» — безопасный выход, не алармим: нейтрально-серая
            no = ft.Button(content=ft.Text("✖️ Нет", color=ft.Colors.BLACK),
                           bgcolor=ft.Colors.GREY_400, on_click=done(False))
        else:
            title = ft.Text("Подтверждение")
            content = ft.Text(prompt, width=440)
            yes = _ok_button(ok_label, done(True))
            no = _no_button(cancel_label, done(False))

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=title, content=content, actions=[yes, no]))
        return await fut

    async def select(self, title: str, labels: list[str], default_index: int = 0) -> int | None:
        """Выбор одного варианта (→ индекс / None при отмене). Мало коротких вариантов —
        кнопки в ряд; много/длинные — прокручиваемый вертикальный список."""
        fut = asyncio.get_running_loop().create_future()

        def choose(idx):
            def h(_):
                if not fut.done():
                    fut.set_result(idx)
                self.page.pop_dialog()
            return h

        cancel = _no_button("✖️ Отмена", choose(None))
        header = _title_with_close("Выбор", choose(None))   # крестик всегда виден вверху
        compact = len(labels) <= 4 and all(len(lab) <= 24 for lab in labels)
        if compact:                                    # варианты — кнопками в ряд
            actions = [ft.Button(content=ft.Text(lab), on_click=choose(i))
                       for i, lab in enumerate(labels)]
            actions.append(cancel)
            dialog = ft.AlertDialog(
                modal=True, title=header, content=ft.Text(title, width=440),
                actions=actions, actions_alignment=ft.MainAxisAlignment.END)
        else:                                          # длинный список — вертикально, со скроллом
            items = [ft.Text(title)] + [
                ft.Button(content=ft.Text(lab, text_align=ft.TextAlign.LEFT),
                          on_click=choose(i), width=560)
                for i, lab in enumerate(labels)]
            content = ft.Column(items, scroll=ft.ScrollMode.AUTO, tight=True, spacing=4,
                                width=580, height=min(440, 60 + 42 * len(labels)))
            dialog = ft.AlertDialog(
                modal=True, title=header, content=content,
                actions=[cancel], actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dialog)
        return await fut

    async def combobox(self, title: str, labels: list[str], default_index: int = 0) -> int | None:
        """Выбор одного варианта выпадающим списком (combobox) + «OK»/«Отмена»/крестик."""
        fut = asyncio.get_running_loop().create_future()
        default = default_index if 0 <= default_index < len(labels) else 0
        dd = ft.Dropdown(
            options=[ft.dropdown.Option(key=str(i), text=lab) for i, lab in enumerate(labels)],
            value=str(default), width=520)

        def finish(idx):
            def h(_):
                if not fut.done():
                    fut.set_result(idx)
                self.page.pop_dialog()
            return h

        def ok(_):
            v = dd.value
            finish(int(v) if v is not None and v.isdigit() else None)(_)

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=_title_with_close("Выбор программы", finish(None)),
            content=ft.Column([ft.Text(title), dd], tight=True, spacing=10, width=540),
            actions=[_no_button("✖️ Отмена", finish(None)),
                     _ok_button("✅ OK", ok)],
            actions_alignment=ft.MainAxisAlignment.END))
        return await fut

    async def checkbox(self, title: str, labels: list[str], default_all: bool = False,
                       default_checked: list[bool] | None = None, ok_label: str = "✅ OK",
                       cancel_label: str = "✖️ Отмена", danger: bool = False,
                       dialog_title: str = "Выбор нод") -> list[int]:
        """Компактный диалог-список: пояснение + чек-боксы + кнопки подтверждения/отмены.
        default_checked — по-элементная предотметка (напр. ноды, где программа уже стоит).
        danger=True — кнопка подтверждения красная (деструктив, напр. удаление файлов)."""
        fut = asyncio.get_running_loop().create_future()
        checked = (default_checked if default_checked and len(default_checked) == len(labels)
                   else [default_all] * len(labels))
        boxes = [ft.Checkbox(label=lab, value=checked[i]) for i, lab in enumerate(labels)]

        def finish(cancel):
            def h(_):
                if not fut.done():
                    fut.set_result([] if cancel else [i for i, b in enumerate(boxes) if b.value])
                self.page.pop_dialog()
            return h

        ok_btn = (ft.Button(content=ft.Text(ok_label, color=ft.Colors.WHITE),
                            bgcolor=ft.Colors.RED, on_click=finish(False))
                  if danger else _ok_button(ok_label, finish(False)))
        content = ft.Column(
            [ft.Text(title), *boxes],
            scroll=ft.ScrollMode.AUTO, tight=True, spacing=6, width=440,
            height=min(360, 64 + 34 * len(boxes)))
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=_title_with_close(dialog_title, finish(True)), content=content,
            actions=[_no_button(cancel_label, finish(True)), ok_btn],
            actions_alignment=ft.MainAxisAlignment.END))
        return await fut