# Образ «Вехи»: `veha serve` над каталогом проекта, подключённым томом только на чтение.
#
#   docker build -t veha:local .
#   docker run --rm -p 127.0.0.1:8770:8770 -v "$PWD/<проект>:/data:ro" veha:local
#
# Что в образе: Python и пакет veha, собранный из этого дерева (`pip install .`, а не с
# PyPI — образ совпадает с исходниками, из которых собран). Чего в нём нет: `--allow-run`
# и окружения тестов проекта (pytest, partest-atlas проекта, стенд) — запуск наборов из
# вебморды в образе невозможен по устройству, а не по договорённости.
#
# Сеть: сборке нужен индекс пакетов (зависимости veha); работающему серверу — никакой.
# Ни телеметрии, ни загрузок при старте, ни внешних шрифтов и библиотек в вебморде.
#
# Запись: в режиме просмотра сервер не пишет ничего — ни в /data, ни куда-либо ещё,
# поэтому корень монтируется `:ro`, а файловая система контейнера может быть read-only.

# Минорная версия закреплена: на ней прогоняется весь набор тестов (3.10, 3.11, 3.14).
ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Только то, что нужно для сборки пакета (.dockerignore держит остальное снаружи).
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY veha ./veha
RUN pip install . && rm -rf /build

# Пользователь без root и без домашнего каталога: серверу нечего хранить.
RUN groupadd --system --gid 10001 veha \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin veha
USER 10001:10001
WORKDIR /data

EXPOSE 8770

# Внутри контейнера слушать loopback нельзя — до него не дойдёт проброс порта, поэтому
# 0.0.0.0 (сервер предупредит об этом при старте). Наружу порт публикуется только на
# 127.0.0.1 хоста (compose.example.yaml): входа во вебморду нет. Host по умолчанию —
# 127.0.0.1:8770 и localhost:8770; другой порт хоста или имя — VEHA_ALLOWED_HOSTS.
ENTRYPOINT ["veha"]
CMD ["serve", "--root", "/data", "--host", "0.0.0.0", "--port", "8770", "--no-browser"]
