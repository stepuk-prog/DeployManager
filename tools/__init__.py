"""Реестр суб-инструментов DeployManager.

Каждый суб-инструмент — подпакет `tools/<key>/`. Меню CLI (`cli.run`) и кнопки GUI
(`gui/app.py`) строятся ИЗ этого реестра → новый инструмент добавляется одной записью
здесь + подпакетом, без правок cli/gui.

Дескриптор: key (action-имя в CLI/GUI), kind, label, icon, color (имя атрибута ft.Colors),
module, + для screen — builder (имя корутины `build_screen(page, on_back)`).
kind:
  "flow"   — CLI/лог-панельный инструмент: `async run(db)` (stdout → лог). Работает и в CLI, и в GUI.
  "screen" — GUI-only экран (свой UI/браузер): `async <builder>(page, on_back) -> teardown`.
             Кнопка GUI переключает страницу на экран инструмента; из CLI недоступен.
Инструменты работают с БД Program, но НЕ требуют выбора папки проекта/SSH (как control-plane).
"""
import importlib

TOOLS = [
    {
        "key": "sessions",
        "kind": "flow",
        "label": "Юзерботы (сессии)",
        "icon": "👤",
        "color": "TEAL_600",
        "module": "tools.sessions",
    },
    {
        "key": "cookies",
        "kind": "screen",
        "label": "Cookies (OTC/Screen/TV/Binodex)",
        "icon": "🍪",
        "color": "BROWN_600",
        "module": "tools.cookies.gui.app",
        "builder": "build_screen",
    },
]

# Множество action-ключей суб-инструментов — для гардов в cli/gui (не требуют проекта).
TOOL_KEYS = {t["key"] for t in TOOLS}


def get_tool(key: str) -> dict | None:
    """Дескриптор инструмента по key (или None)."""
    return next((t for t in TOOLS if t["key"] == key), None)


def flow_tools() -> list[dict]:
    """Инструменты kind=flow (доступны в CLI и GUI через run(db))."""
    return [t for t in TOOLS if t["kind"] == "flow"]


def screen_tools() -> list[dict]:
    """Инструменты kind=screen (GUI-only экран)."""
    return [t for t in TOOLS if t["kind"] == "screen"]


async def run_tool(key: str, db) -> None:
    """Запустить flow-инструмент по key: импорт модуля и вызов его `run(db)`."""
    tool = get_tool(key)
    if tool is None or tool["kind"] != "flow":
        raise ValueError(f"Не flow-инструмент: {key}")
    module = importlib.import_module(tool["module"])
    await module.run(db)


async def build_screen(key: str, page, on_back):
    """Построить экран screen-инструмента: импорт модуля и вызов его builder. → teardown-корутина."""
    tool = get_tool(key)
    if tool is None or tool["kind"] != "screen":
        raise ValueError(f"Не screen-инструмент: {key}")
    module = importlib.import_module(tool["module"])
    builder = getattr(module, tool["builder"])
    return await builder(page, on_back)
