"""`atlas.yaml` глазами сервера.

Файл один на агента и на сервер: агент берёт из него разделы сбора, сервер — разделы
ландшафта (контейнеры, домены, экраны, логи, наборы прогона). Читает и разбирает файл
`partest_atlas.config` — тот же код, что у агента, поэтому «битый файл» и «файла нет»
различаются одинаково по обе стороны, а каталог артефактов сервер ищет ровно там, куда
их кладёт агент.

Отличие одно: сервер читает файл **в указанном корне** и вверх по дереву не ходит.
Агента зовут из любого подкаталога проекта, а сервер поднимают над конкретным набором
(`veha serve --root …`), и подхватить чужой `atlas.yaml` из родительского каталога
значило бы молча показать не тот ландшафт.

Проектного знания в коде нет: ни хостов, ни путей, ни имён. Всё, чего нет в файле, —
пусто, а не чьё-то умолчание.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from partest_atlas import config as atlas_config

CONFIG_NAME = atlas_config.CONFIG_NAME

#: Раздел `atlas.yaml`, где проект говорит, куда его прогон кладёт отчёты. Раздел и
#: каждый ключ необязательны: нет ключа — прежняя раскладка корня данных.
REPORTS_SECTION = "reports"
#: Отчёты по умолчанию — относительно корня данных. Раскладка публичная (README,
#: «Каталог данных»): умолчания не меняются, ключи только добавляются.
DEFAULT_REPORTS = {"coverage": "coverage.json", "allure": "allure-results"}
#: Чем отчёт является на диске: покрытие — файл, Allure — каталог.
REPORT_KINDS = {"coverage": "file", "allure": "dir"}


@dataclass
class Container:
    """Исполняемая единица: контейнер, внешняя система или воркер шины.

    `match` — glob-шаблоны путей, которые обслуживает контейнер. Первый подошедший
    шаблон выигрывает, поэтому специфичные объявляются раньше общих; `default: true`
    подбирает всё, что не совпало ни с чем.
    """

    key: str
    label: str
    kind: str = "service"           # service | worker | external | auth | store | bus
    match: List[str] = field(default_factory=list)
    default: bool = False
    note: str = ""
    tier: int = 0                   # ярус: чем больше, тем выше палуба в виде «Объём»
    names: List[str] = field(default_factory=list)   # имена контейнеров в среде запуска


@dataclass(frozen=True)
class ReportPath:
    """Где лежит отчёт прогона и откуда это известно.

    `path` — `None`, когда значение в `atlas.yaml` негодно (не строка, пустая строка,
    раздел не словарь). Негодная настройка не подменяется умолчанием: показать отчёт из
    корня, когда проект указал другое место, значило бы молча показать не тот прогон.
    """

    key: str                        # coverage | allure
    path: Optional[Path]
    configured: bool = False        # задан в atlas.yaml, а не взят по умолчанию
    error: str = ""                 # почему значение негодно

    @property
    def label(self) -> str:
        """Как назвать отчёт человеку: имя по умолчанию и, если задан, ключ."""
        name = DEFAULT_REPORTS[self.key]
        return f"{name} ({REPORTS_SECTION}.{self.key})" if self.configured else name

    def problem(self) -> str:
        """Что с отчётом не так, кроме «его нет». Пусто — либо в порядке, либо отсутствует.

        Без путей машины: строка уходит в вебморду и в статический снимок.
        """
        if self.error:
            return self.error
        if self.path is None:
            return ""
        want = REPORT_KINDS[self.key]
        if want == "file" and self.path.is_dir():
            return f"{self.label}: путь указывает на каталог, а нужен файл"
        if want == "dir" and self.path.exists() and not self.path.is_dir():
            return f"{self.label}: путь указывает на файл, а нужен каталог"
        return ""


@dataclass
class Config:
    """Прочитанная конфигурация плюс то, что пошло не так при чтении."""

    root: Path
    raw: Dict[str, Any]
    #: Конфигурация, прочитанная агентом. `None`, когда файла нет или он битый: тогда
    #: читать нечего, и подделывать объект агента руками сервер не должен.
    atlas: Optional[atlas_config.Config] = None
    #: Файл есть, но прочитать его нельзя. Сервер при этом поднимается с пустой
    #: конфигурацией и показывает ошибку, а не падает и не делает вид, что файла нет.
    error: str = ""

    @property
    def exists(self) -> bool:
        return (self.root / CONFIG_NAME).is_file()

    # ---------------------------------------------------------------- секции
    def section(self, name: str) -> Dict[str, Any]:
        value = self.raw.get(name)
        return dict(value) if isinstance(value, dict) else {}

    @property
    def artifacts_dir(self) -> Path:
        """Каталог артефактов — так же, как его считает агент.

        Без файла — каталог агента по умолчанию в корне: туда агент и положит артефакты
        проекта, который ничего не настроил.
        """
        if self.atlas is not None:
            return self.atlas.artifacts_dir
        return self.root / atlas_config.DEFAULT_ARTIFACTS_DIR

    def report(self, key: str) -> ReportPath:
        """Путь к отчёту прогона из раздела `reports` — или умолчание в корне.

        Относительный путь — от корня данных (там же лежит `atlas.yaml`), абсолютный —
        как есть. `~` не раскрывается — как у `artifacts_dir` и `app_map`.
        """
        default = DEFAULT_REPORTS[key]
        where = f"{REPORTS_SECTION}.{key}"
        section = self.raw.get(REPORTS_SECTION)
        if section is None:
            return ReportPath(key, self.root / default)
        if not isinstance(section, dict):
            return ReportPath(key, None, True, f"{REPORTS_SECTION}: ожидался словарь "
                                               f"{{coverage: путь, allure: путь}}, получено "
                                               f"{type(section).__name__}")
        value = section.get(key)
        if value is None:
            return ReportPath(key, self.root / default)
        if not isinstance(value, str):
            return ReportPath(key, None, True,
                              f"{where}: ожидалась строка пути, получено {type(value).__name__}")
        if not value.strip() or "\x00" in value:
            return ReportPath(key, None, True, f"{where}: пустой или недопустимый путь")
        return ReportPath(key, self.path(value), True)

    @property
    def coverage_file(self) -> Optional[Path]:
        """Отчёт покрытия partest (`reports.coverage`, по умолчанию `coverage.json` в корне).

        Единственный источник этого пути: граф, срез покрытия раннера и снимок читают его
        отсюда. `None` — значение в `atlas.yaml` негодно (:meth:`report_problems`).
        """
        return self.report("coverage").path

    @property
    def allure_dir(self) -> Optional[Path]:
        """Каталог результатов Allure (`reports.allure`, по умолчанию `allure-results`).

        Единственный источник этого пути: граф, карточка теста, вложения, очистка и снимок
        читают его отсюда. `None` — значение в `atlas.yaml` негодно.
        """
        return self.report("allure").path

    def report_problems(self) -> List[str]:
        """Негодные значения `reports` и пути не того вида (каталог вместо файла и
        наоборот). Отсутствующий отчёт — не проблема, а законное состояние: о нём
        говорят `hasCoverage`/`hasAllure`."""
        return [p for p in (self.report(k).problem() for k in DEFAULT_REPORTS) if p]

    def inside_root(self, path: Path) -> bool:
        """Лежит ли *path* после разрешения ссылок строго внутри корня данных."""
        try:
            real = Path(path).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return self.root in real.parents

    @property
    def containers(self) -> List[Container]:
        out: List[Container] = []
        for key, spec in self.section("containers").items():
            spec = spec or {}
            out.append(Container(
                key=key,
                label=spec.get("label", key),
                kind=spec.get("kind", "service"),
                match=list(spec.get("match") or []),
                default=bool(spec.get("default")),
                note=spec.get("note", ""),
                tier=int(spec.get("tier", 0)),
                names=list(spec.get("container_names") or []),
            ))
        return out

    @property
    def screens(self) -> Dict[str, Any]:
        return self.section("screens")

    def path(self, value: Optional[str]) -> Optional[Path]:
        """Путь из конфигурации относительно корня. Пусто — `None`."""
        if not value:
            return None
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    # ------------------------------------------------------------- поведение
    def container_for(self, path: str) -> str:
        """Кто обслуживает путь. Возвращает ключ контейнера."""
        fallback = "unknown"
        for c in self.containers:
            if c.default:
                fallback = c.key
            for pattern in c.match:
                if fnmatch.fnmatch(path, pattern):
                    return c.key
        return fallback

    def container_key(self, name: str) -> Optional[str]:
        """Ключ контейнера по имени, под которым его знает среда запуска.

        Сборщик логов пишет то имя, которое отдал источник журналов, — обычно имя
        контейнера, а не ключ из `atlas.yaml`. Сводим по ключу и по `container_names`.
        """
        if not name:
            return None
        for c in self.containers:
            if name == c.key or name in c.names:
                return c.key
        return None


def load(root: Path) -> Config:
    """Прочитать `atlas.yaml` в корне *root*. Файла нет — пустая рабочая конфигурация."""
    root = Path(root).resolve()
    if not (root / CONFIG_NAME).is_file():
        return Config(root=root, raw={})
    try:
        cfg = atlas_config.load(root)
    except atlas_config.ConfigError as exc:
        return Config(root=root, raw={}, error=str(exc))
    return Config(root=root, raw=dict(cfg.raw), atlas=cfg)
