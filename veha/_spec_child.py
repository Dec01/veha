"""Разбор `confpartest.py` в дочернем интерпретаторе — единственный разбор этого файла.

Запускается `python -I -c <этот текст>`, байты файла — на стандартном входе, ответ — одна
строка JSON на стандартном выходе. Только стандартная библиотека: модуль исполняется
отдельно от пакета, и `-I` не кладёт в `sys.path` ни текущий каталог, ни пользовательские
пути. Код проекта не исполняется: `ast.parse` и `ast.literal_eval`, больше ничего.

Почему в отдельном процессе: на Python 3.10 `ast.parse` на глубоко вложенном выражении
роняет интерпретатор переполнением стека, а не бросает исключение. Если упадёт этот
процесс, основной узнает об этом по коду возврата и напишет заготовку без разбора. Сам
основной процесс файл не разбирает вовсе — повторный разбор там упал бы так же.

Ответ: `{"error": "<имя исключения>"}` или
`{"assignments": N, "touched": bool, "value": "<тип узла>", "unpack": bool,
"entries": [{"key": str|null, "key_type": "...", "kind": str|null, "where": str|null,
"shape": "ok|short|notstr|nonlit"}]}`. Строки обрезаны до :data:`LIMIT` символов.
"""

import ast
import json
import sys

LIMIT = 4096
NAME = "swagger_files"
MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__",
            "__delitem__", "__ior__"}


def _is_name(node):
    return isinstance(node, ast.Name) and node.id == NAME


def _assigned(stmt):
    if isinstance(stmt, ast.Assign) and any(_is_name(t) for t in stmt.targets):
        return stmt.value
    if isinstance(stmt, ast.AnnAssign) and _is_name(stmt.target) and stmt.value is not None:
        return stmt.value
    return None


def _touched(tree, top):
    """Меняется ли словарь кодом вне присваиваний верхнего уровня. Узлы этих
    присваиваний собираются один раз — проход линейный; `ast.walk` не рекурсивен."""
    own = {id(n) for stmt in top for n in ast.walk(stmt)}
    for node in ast.walk(tree):
        store = isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del))
        if isinstance(node, ast.Name) and node.id == NAME and store and id(node) not in own:
            return True
        if isinstance(node, ast.Subscript) and _is_name(node.value) and store:
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and _is_name(node.func.value) and node.func.attr in MUTATORS:
            return True
    return False


def _cut(value):
    return value[:LIMIT] if isinstance(value, str) else None


def _entry(key, node):
    out = {"key": None, "key_type": "", "kind": None, "where": None, "shape": "nonlit"}
    try:
        name = ast.literal_eval(key)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        out["key_type"] = "nonlit"
        return out
    if not isinstance(name, str):
        out["key_type"] = type(name).__name__
        return out
    out["key"] = _cut(name)
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return out
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        out["shape"] = "short"
    elif not isinstance(value[0], str) or not isinstance(value[1], str):
        out["shape"] = "notstr"
    else:
        out.update(shape="ok", kind=_cut(value[0]), where=_cut(value[1]))
    return out


def analyse(data):
    """Словарь ответа для байтов *data* (в том же процессе — только для проверок)."""
    try:
        tree = ast.parse(data, filename="confpartest.py")
    except BaseException as exc:          # noqa: BLE001 — любое исключение — ответ
        return {"error": type(exc).__name__}
    top = [s for s in tree.body if _assigned(s) is not None]
    out = {"assignments": len(top), "touched": False, "value": "", "unpack": False,
           "entries": []}
    if not top:
        return out
    out["touched"] = _touched(tree, top)
    value = _assigned(top[-1])
    out["value"] = type(value).__name__
    if isinstance(value, ast.Dict):
        for key, node in zip(value.keys, value.values):
            if key is None:
                out["unpack"] = True
            else:
                out["entries"].append(_entry(key, node))
    return out


def main():
    print(json.dumps(analyse(sys.stdin.buffer.read())))


if __name__ == "__main__":
    main()
