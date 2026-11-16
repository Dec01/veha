# Образ «Вехи»: `veha serve` над каталогом проекта, подключённым томом только на чтение.
#
#   docker build -t veha:local .
#   docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
#       -p 127.0.0.1:8770:8770 -v "$PWD/<проект>:/data:ro" veha:local
#
# Порт — только на 127.0.0.1 хоста: `-p 8770:8770` публикует его на все интерфейсы,
# `--network host` открывает сервер в сеть, на Linux проброс портов обходит ufw.
#
# Что в образе: Python и пакет veha, собранный из этого дерева (`pip install .`, а не с
# PyPI — образ совпадает с исходниками, из которых собран; незакоммиченное тоже уезжает,
# для выпуска собирать из чистого клона или `git archive`). pytest и allure-pytest в
# образе есть — их тянет partest. Барьер для запуска наборов из вебморды — не состав
# образа, а то, что в команде нет `--allow-run`, и `veha serve` отказывается сочетать
# `--allow-run` с адресом не на loopback.
#
# Сеть: сборке нужен индекс пакетов (зависимости veha). Работающий сервер никуда не
# ходит — ни телеметрии, ни загрузок при старте, ни внешних шрифтов. Это держит код Вехи,
# а не образ: исходящий трафик из контейнера ничем не ограничен, закрывайте его сетью.
#
# Запись: в режиме просмотра сервер не пишет ничего — ни в /data, ни куда-либо ещё,
# поэтому корень монтируется `:ro`, а файловая система контейнера может быть read-only.

# Закреплён проверенный и безопасный минимум проекта; старые линии не поддерживаются.
ARG PYTHON_VERSION=3.14.4
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
