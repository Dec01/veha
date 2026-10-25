#!/usr/bin/env python
"""Генератор демонстрационного набора данных для просмотрщика «Веха».

Весь набор — вымысел. Сюжет: розничная сеть «Сеть N» с интернет-магазином,
складом, биллингом и CRM. Имена, пути, топики, контейнеры и экраны придуманы:
всё построено из описанной ниже модели.

Файл — единственный источник правды для набора. Набор он пишет внутрь пакета, в
`veha/demo_data/`: оттуда его поднимает `veha demo`, и оттуда он попадает в колесо.
Копии рядом с генератором нет — разъехаться нечему.


    atlas.yaml                 — проектное знание (контейнеры, домены, экраны)
    app_map.yaml               — карта интеграций UI ↔ API ↔ шина ↔ хранилище
    atlas/default/default/     — артефакты агента partest-atlas, схема 2:
        openapi.json           — объявленный контракт
        db_schema.json         — схема базы
        ui_flow.json           — связи «элемент экрана → эндпоинт», снятые
        db_effects.json        — что тест сделал с таблицами
        bus_flow.json          — события шины вокруг тестов
        container_logs.json    — что контейнеры сказали о себе в логах
        specs.json             — требования аналитики
        runs.json              — история прогонов
    live/containers.json       — снимок состояния контейнеров стенда (для «Live»)
    live/mrs.json              — очередь merge request'ов (снимок адаптера)
    live/mr_checks.json        — отметки проверки МР (их пишет сервер)
    coverage.json              — покрытие по методике partest
    allure-results/            — результаты прогона
    screens/                   — манифест экранов и нарисованные заглушки
    logs/                      — лог прогона и логи контейнеров

Артефакты пишутся в формате агента, а не «похожем на него»: заголовок проверяется
`partest_atlas.artifacts.validate`, а полезная нагрузка там, где у агента есть чистая
функция сборки (контракт, схема базы, связи UI, требования, логи контейнеров), строится
ею же. Так демонстрация показывает ровно то, что сервер увидит на настоящей установке.

`live/` — не артефакты агента: у агента нет вида ни для состояния контейнеров, ни для
очереди проверки, а отметки проверки пишет сам сервер.

Нужны установленные `veha` (срезы истории снимает его код, `veha.runner.snapshot_of`)
и `partest-atlas` не ниже 0.2.1 (ставится вместе с пакетом `veha`).

Запуск:  python demo/generate.py   (из корня репозитория; из любого каталога — то же)
"""

from __future__ import annotations

import json
import random
import re
import shutil
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from partest_atlas.artifacts import SCHEMA_VERSION, Scope, validate
from partest_atlas.collect import container_logs as atlas_container_logs
from partest_atlas.collect import db_schema as atlas_db_schema
from partest_atlas.collect import openapi as atlas_openapi
from partest_atlas.collect import specs as atlas_specs
from partest_atlas.collect import history as atlas_history
from partest_atlas.collect import ui_flow as atlas_ui_flow

from veha.runner import snapshot_of

# Куда пишется набор: каталог данных пакета. Генератор в колесо не входит — он
# нужен тому, кто меняет модель, а не тому, кто смотрит демонстрацию.
ROOT = Path(__file__).resolve().parents[1] / "veha" / "demo_data"
RND = random.Random(20261018)

# Момент «последнего прогона»: набор должен выглядеть снятым вчера вечером.
RUN_START = datetime(2026, 9, 13, 19, 40, tzinfo=timezone(timedelta(hours=3)))
RUN_START_MS = int(RUN_START.timestamp() * 1000)

# Область и подпись артефактов. Набор — один проект, поэтому область по умолчанию.
# Подпись честная: артефакты собрал генератор демонстрации, а не сборщики агента.
SCOPE = Scope()
COLLECTOR = "veha-demo/generate"
# `artifacts.write` ставит текущее время, и набор переставал бы быть детерминированным
# при каждом запуске. Время сбора — «вечер прогона», как и всё остальное в наборе.
COLLECTED_AT = (RUN_START + timedelta(minutes=15)).isoformat(timespec="seconds")


# ===========================================================================
# 1. Модель ландшафта
# ===========================================================================

# ---- контейнеры и внешние системы ----------------------------------------
CONTAINERS = [
    ("storefront-api", "storefront-api", "service", 0, "shop-api",
     "Основной backend витрины. Обслуживает всё, что не размечено иначе."),
    ("checkout-worker", "checkout-worker", "worker", 1, "shop-checkout-worker",
     "Разбирает order.confirmed, просит авторизацию платежа."),
    ("warehouse-worker", "warehouse-worker", "worker", 1, "shop-wms-worker",
     "Резервирует и списывает остатки по событиям склада."),
    ("billing-worker", "billing-worker", "worker", 1, "shop-billing-worker",
     "Выписывает счета и проводит возвраты по событиям оплаты."),
    ("crm-worker", "crm-worker", "worker", 1, "shop-crm-worker",
     "Пересчитывает сегменты и начисляет бонусы."),
    ("notifier", "notifier", "worker", 1, "shop-notifier",
     "Рассылает уведомления по событиям шины."),
    ("payment-gateway", "Платёжный шлюз", "external", 0, "sandbox-payments",
     "Внешняя система. HTTP-эндпоинтов набора не обслуживает — её дёргает "
     "storefront-api при авторизации оплаты."),
    ("delivery-provider", "Служба доставки", "external", 0, "sandbox-delivery",
     "Внешняя система: расчёт и резерв доставки."),
    ("identity", "Провайдер входа", "auth", 0, "identity-provider",
     "Выдаёт токены. Эндпоинты учётных записей обслуживает storefront-api."),
    ("broker", "Шина сообщений", "bus", 0, "shop-broker", ""),
    ("db-primary", "PostgreSQL", "store", 0, "shop-db", ""),
    ("objectstore", "Объектное хранилище", "store", 0, "shop-objectstore", ""),
    ("web-ui", "Витрина (SPA)", "service", 0, "shop-web", ""),
]

# ---- домены: как делим площадку API --------------------------------------
DOMAINS = [
    ("shop", "Витрина", 0, ["Products", "Categories", "Variants", "Prices",
                            "Promotions", "Carts", "Reviews"]),
    ("orders", "Заказы", 0, ["Orders", "Payments", "Shipments", "Returns"]),
    ("warehouse", "Склад", 0, ["Stock", "Warehouses", "Suppliers",
                               "Purchase Orders"]),
    ("billing", "Биллинг", 1, ["Invoices", "Subscriptions", "Refunds", "Gift Cards"]),
    ("crm", "CRM", 1, ["Customers", "Segments", "Campaigns", "Loyalty"]),
    ("platform", "Платформа", 1, ["Accounts", "Roles & Permissions",
                                  "Notifications", "Enums", "Audit"]),
]

# ---- ресурсы API: ключ сегмента пути → подпись сервиса --------------------
# crud: какие из пяти стандартных операций есть у ресурса
RESOURCES = [
    # key, label, crud, extra
    ("products", "Products", "LCRUD", [
        ("GET", "/api/v1/products/search", "get_list_objects", "Поиск по каталогу"),
        ("GET", "/api/v1/products/{id}/variants", "get_by_parent", "Варианты товара"),
        ("POST", "/api/v1/products/{id}/archive", "action", "Снять с продажи"),
    ]),
    ("categories", "Categories", "LCRUD", [
        ("GET", "/api/v1/categories/tree", "get_static_object", "Дерево категорий"),
    ]),
    ("variants", "Variants", "LCRUD", [
        ("GET", "/api/v1/variants/product/{productId}", "get_by_parent",
         "Варианты по товару"),
    ]),
    ("prices", "Prices", "LCRUD", [
        ("GET", "/api/v1/prices/product/{productId}", "get_by_parent",
         "Цены по товару"),
        ("POST", "/api/v1/prices/bulk", "action", "Массовая переоценка"),
    ]),
    ("promotions", "Promotions", "LCRUD", [
        ("POST", "/api/v1/promotions/{id}/activate", "action", "Включить акцию"),
    ]),
    ("carts", "Carts", "LCRU", [
        ("POST", "/api/v1/carts/{id}/items", "action", "Положить в корзину"),
        ("DELETE", "/api/v1/carts/{id}/items/{itemId}", "delete_object",
         "Убрать из корзины"),
        ("POST", "/api/v1/carts/{id}/checkout", "action", "Оформить корзину"),
    ]),
    ("reviews", "Reviews", "LCRUD", [
        ("GET", "/api/v1/reviews/product/{productId}", "get_by_parent",
         "Отзывы о товаре"),
        ("POST", "/api/v1/reviews/{id}/publish", "action", "Опубликовать отзыв"),
    ]),
    ("orders", "Orders", "LCRUD", [
        ("GET", "/api/v1/orders/{id}/lines", "get_by_parent", "Строки заказа"),
        ("GET", "/api/v1/orders/customer/{customerId}", "get_by_parent",
         "Заказы клиента"),
        ("POST", "/api/v1/orders/{id}/confirm", "action", "Подтвердить заказ"),
        ("POST", "/api/v1/orders/{id}/cancel", "action", "Отменить заказ"),
        ("POST", "/api/v1/orders/export", "action", "Выгрузить реестр"),
        ("GET", "/api/v1/orders/report/sales", "get_list_objects",
         "Отчёт по продажам"),
    ]),
    ("payments", "Payments", "LCRU", [
        ("GET", "/api/v1/payments/order/{orderId}", "get_by_parent",
         "Оплаты заказа"),
        ("POST", "/api/v1/payments/{id}/capture", "action", "Списать средства"),
        ("GET", "/api/v1/payments/{id}/receipt", "get_dynamic_object", "Чек"),
    ]),
    ("shipments", "Shipments", "LCRUD", [
        ("GET", "/api/v1/shipments/order/{orderId}", "get_by_parent",
         "Отгрузки заказа"),
        ("POST", "/api/v1/shipments/{id}/dispatch", "action", "Передать в доставку"),
    ]),
    ("returns", "Returns", "LCRU", [
        ("POST", "/api/v1/returns/{id}/approve", "action", "Согласовать возврат"),
        ("GET", "/api/v1/returns/order/{orderId}", "get_by_parent",
         "Возвраты по заказу"),
    ]),
    ("stock", "Stock", "LCR", [
        ("GET", "/api/v1/stock/product/{productId}", "get_by_parent",
         "Остатки по товару"),
        ("GET", "/api/v1/stock/warehouse/{warehouseId}", "get_by_parent",
         "Остатки по складу"),
        ("POST", "/api/v1/stock/moves", "action", "Провести перемещение"),
        ("POST", "/api/v1/stock/{id}/recount", "action", "Пересчитать позицию"),
    ]),
    ("warehouses", "Warehouses", "LCRUD", []),
    ("suppliers", "Suppliers", "LCRUD", [
        ("GET", "/api/v1/suppliers/{id}/products", "get_by_parent",
         "Товары поставщика"),
    ]),
    ("purchases", "Purchase Orders", "LCRUD", [
        ("GET", "/api/v1/purchases/{id}/lines", "get_by_parent", "Строки закупки"),
        ("POST", "/api/v1/purchases/{id}/approve", "action", "Согласовать закупку"),
        ("POST", "/api/v1/purchases/{id}/receive", "action", "Принять поставку"),
    ]),
    ("invoices", "Invoices", "LCRU", [
        ("GET", "/api/v1/invoices/{id}/lines", "get_by_parent", "Строки счёта"),
        ("POST", "/api/v1/invoices/{id}/send", "action", "Отправить счёт"),
    ]),
    ("subscriptions", "Subscriptions", "LCRUD", [
        ("POST", "/api/v1/subscriptions/{id}/pause", "action", "Приостановить"),
    ]),
    ("refunds", "Refunds", "LCR", [
        ("POST", "/api/v1/refunds/{id}/confirm", "action", "Подтвердить возврат"),
    ]),
    ("gift-cards", "Gift Cards", "LCR", [
        ("POST", "/api/v1/gift-cards/{id}/redeem", "action", "Погасить карту"),
    ]),
    ("customers", "Customers", "LCRUD", [
        ("GET", "/api/v1/customers/{id}/addresses", "get_by_parent", "Адреса"),
        ("GET", "/api/v1/customers/search", "get_list_objects", "Поиск клиента"),
    ]),
    ("segments", "Segments", "LCRUD", [
        ("POST", "/api/v1/segments/{id}/recalculate", "action", "Пересчитать"),
    ]),
    ("campaigns", "Campaigns", "LCRUD", [
        ("POST", "/api/v1/campaigns/{id}/launch", "action", "Запустить кампанию"),
        ("GET", "/api/v1/campaigns/{id}/targets", "get_by_parent", "Аудитория"),
    ]),
    ("loyalty", "Loyalty", "LCR", [
        ("GET", "/api/v1/loyalty/customer/{customerId}", "get_by_parent",
         "Бонусный счёт клиента"),
        ("POST", "/api/v1/loyalty/{id}/award", "action", "Начислить бонусы"),
    ]),
    ("accounts", "Accounts", "LCRUD", [
        ("GET", "/api/v1/accounts/me", "get_by_self", "Текущая учётная запись"),
    ]),
    ("roles", "Roles & Permissions", "LCR", [
        ("GET", "/api/v1/roles/{id}/permissions", "get_by_parent", "Права роли"),
    ]),
    ("notifications", "Notifications", "LCR", [
        ("GET", "/api/v1/notifications/templates", "get_static_object", "Шаблоны"),
    ]),
    ("enums", "Enums", "", [
        ("GET", "/api/v1/enums/order-statuses", "get_static_object",
         "Статусы заказа"),
        ("GET", "/api/v1/enums/payment-methods", "get_static_object",
         "Способы оплаты"),
        ("GET", "/api/v1/enums/units", "get_static_object", "Единицы измерения"),
        ("GET", "/api/v1/enums/periods", "get_static_object", "Периоды отчётов"),
        ("GET", "/api/v1/enums/return-reasons", "get_static_object",
         "Причины возврата"),
    ]),
    ("audit", "Audit", "LR", [
        ("GET", "/api/v1/audit/object/{objectId}", "get_by_parent",
         "История объекта"),
    ]),
]

CRUD_TEMPLATES = {
    "L": ("GET", "/api/v1/{r}", "get_list_objects", "Список"),
    "C": ("POST", "/api/v1/{r}", "post_create_object", "Создание"),
    "R": ("GET", "/api/v1/{r}/{{id}}", "get_dynamic_object", "Карточка"),
    "U": ("PUT", "/api/v1/{r}/{{id}}", "put_object", "Изменение"),
    "D": ("DELETE", "/api/v1/{r}/{{id}}", "delete_object", "Удаление"),
}

SUBTYPE_LABEL = {
    "get_list_objects": "GET LIST OBJECTS",
    "get_dynamic_object": "GET DYNAMIC OBJECT",
    "get_static_object": "GET STATIC OBJECT",
    "get_by_parent": "GET BY PARENT",
    "get_by_self": "GET BY SELF / SCOPE",
    "post_create_object": "POST CREATE OBJECT",
    "put_object": "PUT OBJECT",
    "delete_object": "DELETE OBJECT",
    "action": "ACTION",
}

# Ось B методики partest: какие проверки положены подтипу.
TC_BY_SUBTYPE = {
    "get_list_objects": ["RequestDefault", "RequestElements", "RequestFilters",
                         "RequestSorting", "RequestPagination", "RequestPermissions"],
    "get_dynamic_object": ["RequestDefault", "RequestElements", "RequestNotFound",
                           "RequestPermissions"],
    "get_static_object": ["RequestDefault", "RequestElements"],
    "get_by_parent": ["RequestDefault", "RequestElements", "RequestNotFound",
                      "RequestPermissions"],
    "get_by_self": ["RequestDefault", "RequestElements", "RequestPermissions"],
    "post_create_object": ["RequestDefault", "RequestElements", "RequestIncorrectBody",
                           "RequestExtraData", "RequestCrossField",
                           "RequestPermissions"],
    "put_object": ["RequestDefault", "RequestElements", "RequestUpdateObject",
                   "RequestIncorrectBody", "RequestNotFound", "RequestCrossField",
                   "RequestPermissions"],
    "delete_object": ["RequestDefault", "RequestLifecycleDelete", "RequestNotFound",
                      "RequestPermissions"],
    "action": ["RequestDefault", "RequestNotFound", "RequestCrossField",
               "RequestPermissions"],
}

# Насколько густо покрыт сервис. Дыры намеренные: инструмент нужен, чтобы их
# было видно, а ровное поле ничего не показывает.
COVERAGE_PROFILE = {
    "orders": 1.00, "payments": 1.00, "products": 1.00, "carts": 0.95,
    "categories": 1.00, "customers": 0.95, "prices": 0.90, "variants": 0.90,
    "shipments": 0.85, "purchases": 0.80, "warehouses": 0.85, "suppliers": 0.80,
    "stock": 0.65, "invoices": 0.60, "returns": 0.60, "enums": 1.00,
    "promotions": 0.45, "campaigns": 0.40, "subscriptions": 0.35,
    "segments": 0.30, "loyalty": 0.30, "accounts": 0.75, "roles": 0.50,
    "refunds": 0.15, "notifications": 0.10, "audit": 0.05,
    "reviews": 0.55, "gift-cards": 0.25,
}

# ---- база данных ----------------------------------------------------------
# таблица: (владелец-контейнер, строк, [колонки сверх id/created_at/updated_at])
# колонка: имя:тип или имя->таблица (внешний ключ)
TABLES = {
    # витрина
    "category": ("storefront-api", 42, ["code:varchar(64)", "title:varchar(256)",
                                        "parent_id->category", "sort_order:int4",
                                        "is_active:bool"]),
    "product": ("storefront-api", 1840, ["sku:varchar(64)", "title:varchar(256)",
                                         "category_id->category", "unit:varchar(16)",
                                         "supplier_id->supplier", "is_active:bool",
                                         "archived_at:timestamp"]),
    "product_variant": ("storefront-api", 5210, ["product_id->product",
                                                 "barcode:varchar(64)",
                                                 "size:varchar(32)",
                                                 "color:varchar(32)",
                                                 "weight_g:int4"]),
    "product_media": ("storefront-api", 6120, ["product_id->product",
                                               "kind:varchar(16)",
                                               "url:varchar(512)", "sort_order:int4"]),
    "product_attribute": ("storefront-api", 9330, ["product_id->product",
                                                   "name:varchar(64)",
                                                   "value:varchar(256)"]),
    "price_list": ("storefront-api", 12, ["code:varchar(32)", "title:varchar(128)",
                                          "currency:varchar(3)",
                                          "valid_from:timestamp"]),
    "price_entry": ("storefront-api", 18400, ["price_list_id->price_list",
                                              "product_id->product",
                                              "amount:numeric(14,2)",
                                              "discount_pct:numeric(5,2)"]),
    "promotion": ("storefront-api", 34, ["code:varchar(32)", "title:varchar(128)",
                                         "kind:varchar(32)", "percent:numeric(5,2)",
                                         "starts_at:timestamp", "ends_at:timestamp",
                                         "is_active:bool"]),
    "promotion_rule": ("storefront-api", 88, ["promotion_id->promotion",
                                              "field:varchar(64)",
                                              "operator:varchar(16)",
                                              "value:varchar(128)"]),
    "cart": ("storefront-api", 970, ["customer_id->customer", "status:varchar(32)",
                                     "total:numeric(14,2)"]),
    "cart_item": ("storefront-api", 3120, ["cart_id->cart",
                                           "variant_id->product_variant",
                                           "qty:int4", "price:numeric(14,2)"]),
    # заказы
    "order_header": ("storefront-api", 4380, ["number:varchar(32)",
                                              "customer_id->customer",
                                              "status:varchar(32)",
                                              "total:numeric(14,2)",
                                              "placed_at:timestamp",
                                              "warehouse_id->warehouse"]),
    "order_line": ("storefront-api", 14920, ["order_id->order_header",
                                             "variant_id->product_variant",
                                             "qty:int4", "price:numeric(14,2)",
                                             "discount:numeric(14,2)"]),
    "order_status_history": ("storefront-api", 19800, ["order_id->order_header",
                                                       "status:varchar(32)",
                                                       "changed_by:varchar(128)",
                                                       "changed_at:timestamp"]),
    "payment": ("storefront-api", 4110, ["order_id->order_header",
                                         "method:varchar(32)", "status:varchar(32)",
                                         "amount:numeric(14,2)",
                                         "external_ref:varchar(64)"]),
    "payment_attempt": ("checkout-worker", 6740, ["payment_id->payment",
                                                  "attempt_no:int4",
                                                  "result:varchar(32)",
                                                  "response_code:varchar(16)"]),
    "shipment": ("storefront-api", 3960, ["order_id->order_header",
                                          "warehouse_id->warehouse",
                                          "carrier:varchar(64)",
                                          "status:varchar(32)",
                                          "tracking_no:varchar(64)"]),
    "shipment_item": ("storefront-api", 12300, ["shipment_id->shipment",
                                                "order_line_id->order_line",
                                                "qty:int4"]),
    "return_request": ("storefront-api", 410, ["order_id->order_header",
                                               "reason:varchar(64)",
                                               "status:varchar(32)",
                                               "amount:numeric(14,2)"]),
    "return_line": ("storefront-api", 690, ["return_id->return_request",
                                            "order_line_id->order_line", "qty:int4"]),
    # склад
    "warehouse": ("warehouse-worker", 7, ["code:varchar(32)", "title:varchar(128)",
                                          "city:varchar(64)", "is_active:bool"]),
    "stock_item": ("warehouse-worker", 11200, ["warehouse_id->warehouse",
                                               "variant_id->product_variant",
                                               "qty:int4", "reserved_qty:int4",
                                               "min_qty:int4"]),
    "stock_move": ("warehouse-worker", 47600, ["stock_item_id->stock_item",
                                               "kind:varchar(32)", "qty:int4",
                                               "document_ref:varchar(64)",
                                               "moved_at:timestamp"]),
    "inventory_count": ("warehouse-worker", 120, ["warehouse_id->warehouse",
                                                  "status:varchar(32)",
                                                  "counted_at:timestamp"]),
    "supplier": ("warehouse-worker", 58, ["code:varchar(32)", "title:varchar(256)",
                                          "contact_email:varchar(256)",
                                          "is_active:bool"]),
    "purchase_order": ("warehouse-worker", 640, ["number:varchar(32)",
                                                 "supplier_id->supplier",
                                                 "warehouse_id->warehouse",
                                                 "status:varchar(32)",
                                                 "total:numeric(14,2)"]),
    "purchase_order_line": ("warehouse-worker", 4300, [
        "purchase_order_id->purchase_order", "variant_id->product_variant",
        "qty:int4", "price:numeric(14,2)"]),
    # биллинг
    "invoice": ("billing-worker", 4020, ["number:varchar(32)",
                                         "order_id->order_header",
                                         "customer_id->customer",
                                         "status:varchar(32)",
                                         "total:numeric(14,2)",
                                         "issued_at:timestamp"]),
    "invoice_line": ("billing-worker", 13800, ["invoice_id->invoice",
                                               "title:varchar(256)", "qty:int4",
                                               "amount:numeric(14,2)",
                                               "tax_rate_id->tax_rate"]),
    "tax_rate": ("billing-worker", 5, ["code:varchar(16)", "percent:numeric(5,2)"]),
    "refund": ("billing-worker", 380, ["invoice_id->invoice",
                                       "return_id->return_request",
                                       "status:varchar(32)",
                                       "amount:numeric(14,2)"]),
    "subscription_plan": ("billing-worker", 9, ["code:varchar(32)",
                                                "title:varchar(128)",
                                                "period:varchar(16)",
                                                "amount:numeric(14,2)"]),
    "subscription": ("billing-worker", 730, ["customer_id->customer",
                                             "plan_id->subscription_plan",
                                             "status:varchar(32)",
                                             "next_charge_at:timestamp"]),
    # CRM
    "customer": ("storefront-api", 8900, ["code:varchar(32)",
                                          "full_name:varchar(256)",
                                          "email:varchar(256)",
                                          "phone:varchar(32)", "is_active:bool"]),
    "customer_address": ("storefront-api", 11400, ["customer_id->customer",
                                                   "kind:varchar(16)",
                                                   "city:varchar(64)",
                                                   "street:varchar(256)"]),
    "customer_segment": ("crm-worker", 24, ["code:varchar(32)",
                                            "title:varchar(128)",
                                            "size:int4",
                                            "recalculated_at:timestamp"]),
    "segment_member": ("crm-worker", 26400, ["segment_id->customer_segment",
                                             "customer_id->customer",
                                             "added_at:timestamp"]),
    "campaign": ("crm-worker", 61, ["code:varchar(32)", "title:varchar(128)",
                                    "segment_id->customer_segment",
                                    "status:varchar(32)",
                                    "starts_at:timestamp"]),
    "campaign_target": ("crm-worker", 8700, ["campaign_id->campaign",
                                             "customer_id->customer",
                                             "state:varchar(32)"]),
    "loyalty_account": ("crm-worker", 8900, ["customer_id->customer",
                                             "points:int4", "tier:varchar(16)"]),
    "loyalty_transaction": ("crm-worker", 34500, [
        "loyalty_account_id->loyalty_account", "kind:varchar(16)", "points:int4",
        "document_ref:varchar(64)"]),
    # платформа
    "account": ("storefront-api", 64, ["login:varchar(128)", "email:varchar(256)",
                                       "full_name:varchar(256)",
                                       "is_active:bool",
                                       "external_id:varchar(64)"]),
    "role": ("storefront-api", 11, ["code:varchar(32)", "title:varchar(128)"]),
    "account_role": ("storefront-api", 142, ["account_id->account",
                                             "role_id->role"]),
    "access_right": ("storefront-api", 96, ["code:varchar(64)",
                                            "title:varchar(128)"]),
    "role_access_right": ("storefront-api", 430, ["role_id->role",
                                                  "access_right_id->access_right"]),
    "notification": ("notifier", 51200, ["template_code:varchar(64)",
                                         "customer_id->customer",
                                         "delivery_channel:varchar(16)",
                                         "status:varchar(32)",
                                         "sent_at:timestamp"]),
    "notification_template": ("notifier", 18, ["code:varchar(64)",
                                               "delivery_channel:varchar(16)",
                                               "subject:varchar(256)"]),
    "review": ("storefront-api", 7400, ["product_id->product",
                                        "customer_id->customer", "rating:int2",
                                        "body:text", "status:varchar(16)"]),
    "review_vote": ("storefront-api", 21300, ["review_id->review",
                                              "customer_id->customer",
                                              "is_helpful:bool"]),
    "gift_card": ("billing-worker", 960, ["code:varchar(32)",
                                          "balance:numeric(14,2)",
                                          "customer_id->customer",
                                          "expires_at:timestamp"]),
    "audit_event": ("storefront-api", 148000, ["object_type:varchar(64)",
                                               "object_id:int8",
                                               "action:varchar(32)",
                                               "actor:varchar(128)",
                                               "happened_at:timestamp"]),
}

# ---- шина -----------------------------------------------------------------
TOPICS = [
    "order.confirmed", "order.cancelled", "payment.authorized", "payment.failed",
    "invoice.issued", "shipment.created", "stock.reserved", "stock.replenished",
    "return.requested", "campaign.launched", "loyalty.points.awarded",
    "notification.sent",
]

# ---- экраны и элементы ----------------------------------------------------
SCREENS = [
    ("catalog_products", "Каталог товаров", "/#/catalog/products",
     "src/ui/pages/catalog_products_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=11, label="Каталог товаров"),
         dict(kind="toolbar", x=11, y=14, w=46, h=9),
         dict(kind="button", x=78, y=14, w=19, h=9, label="Добавить товар"),
         dict(kind="table", x=11, y=28, w=86, h=66, rows=8,
              label="SKU · Название · Категория · Цена"),
     ]),
    ("product_card", "Карточка товара", "/#/catalog/products/{productId}",
     "src/ui/pages/product_card_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="hero", x=8, y=0, w=92, h=24, label="Карточка товара"),
         dict(kind="button", x=76, y=5, w=20, h=9, label="Добавить вариант"),
         dict(kind="panel", x=11, y=28, w=42, h=34, label="Параметры товара"),
         dict(kind="panel", x=56, y=28, w=41, h=34, label="Остатки по складам"),
         dict(kind="table", x=11, y=66, w=86, h=30, rows=4,
              label="Вариант · Штрихкод · Цена"),
     ]),
    ("orders_list", "Заказы", "/#/orders", "src/ui/pages/orders_page.py", [
        dict(kind="sidebar", x=0, y=0, w=8, h=100),
        dict(kind="header", x=8, y=0, w=92, h=11, label="Заказы"),
        dict(kind="toolbar", x=11, y=14, w=40, h=9),
        dict(kind="button", x=76, y=14, w=21, h=9, label="Новый заказ"),
        dict(kind="table", x=11, y=28, w=86, h=66, rows=9,
             label="Номер · Клиент · Статус · Сумма"),
    ]),
    ("order_card", "Карточка заказа", "/#/orders/{orderId}",
     "src/ui/pages/order_card_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="hero", x=8, y=0, w=92, h=22, label="Заказ"),
         dict(kind="button", x=76, y=5, w=20, h=9, label="Подтвердить"),
         dict(kind="panel", x=11, y=26, w=42, h=30, label="Оплата"),
         dict(kind="panel", x=56, y=26, w=41, h=30, label="Отгрузка"),
         dict(kind="table", x=11, y=60, w=86, h=36, rows=5,
              label="Товар · Кол-во · Цена · Скидка"),
     ]),
    ("warehouse_stock", "Остатки на складе", "/#/warehouse/stock",
     "src/ui/pages/warehouse_stock_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=11, label="Остатки"),
         dict(kind="toolbar", x=11, y=14, w=52, h=9),
         dict(kind="button", x=78, y=14, w=19, h=9, label="Перемещение"),
         dict(kind="table", x=11, y=28, w=86, h=66, rows=8,
              label="Склад · Вариант · Остаток · Резерв"),
     ]),
    ("purchase_order", "Закупка", "/#/warehouse/purchases/{purchaseId}",
     "src/ui/pages/purchase_order_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=12, label="Закупка"),
         dict(kind="panel", x=11, y=16, w=42, h=36, label="Поставщик и условия"),
         dict(kind="panel", x=56, y=16, w=41, h=36, label="Приёмка"),
         dict(kind="table", x=11, y=56, w=86, h=40, rows=5,
              label="Вариант · Кол-во · Цена"),
     ]),
    ("billing_invoices", "Счета", "/#/billing/invoices",
     "src/ui/pages/billing_invoices_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=11, label="Счета"),
         dict(kind="toolbar", x=11, y=14, w=46, h=9),
         dict(kind="button", x=78, y=14, w=19, h=9, label="Выставить счёт"),
         dict(kind="table", x=11, y=28, w=86, h=66, rows=7,
              label="Счёт · Заказ · Статус · Сумма"),
     ]),
    ("crm_customers", "Клиенты", "/#/crm/customers",
     "src/ui/pages/crm_customers_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=11, label="Клиенты"),
         dict(kind="toolbar", x=11, y=14, w=52, h=9),
         dict(kind="table", x=11, y=28, w=86, h=66, rows=8,
              label="Клиент · Сегмент · Бонусы · Статус"),
     ]),
    ("crm_campaigns", "Кампании", "/#/crm/campaigns",
     "src/ui/pages/crm_campaigns_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=11, label="Кампании"),
         dict(kind="button", x=76, y=14, w=21, h=9, label="Новая кампания"),
         dict(kind="table", x=11, y=28, w=86, h=66, rows=6,
              label="Кампания · Сегмент · Статус"),
     ]),
    ("admin_accounts", "Учётные записи", "/#/admin/accounts",
     "src/ui/pages/admin_accounts_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=11, label="Учётные записи"),
         dict(kind="toolbar", x=11, y=14, w=52, h=9),
         dict(kind="table", x=11, y=28, w=86, h=66, rows=7,
              label="Логин · ФИО · Роль · Статус"),
     ]),
    ("reports_sales", "Отчёт по продажам", "/#/reports/sales",
     "src/ui/pages/reports_sales_page.py", [
         dict(kind="sidebar", x=0, y=0, w=8, h=100),
         dict(kind="header", x=8, y=0, w=92, h=11, label="Продажи"),
         dict(kind="toolbar", x=11, y=14, w=40, h=9),
         dict(kind="panel", x=11, y=26, w=86, h=44, label="Динамика продаж"),
         dict(kind="table", x=11, y=72, w=86, h=24, rows=3,
              label="Период · Заказов · Выручка"),
     ]),
]

# testid, screen, подпись, слот, вызовы, что рисует, примечание
CONTROLS = [
    ("catalog-products-table", "catalog_products", "Таблица товаров", (54, 60),
     ["GET /api/v1/products"], ["таблица товаров"], ""),
    ("catalog-create-product-btn", "catalog_products", "Добавить товар", (87, 18),
     ["GET /api/v1/categories", "POST /api/v1/products",
      "POST /api/v1/prices"],
     ["модалка «Новый товар»", "шаг «Цена и остаток»"],
     "визард из двух шагов; цена создаётся отдельным POST"),
    ("catalog-filter-category", "catalog_products", "Фильтр по категории", (30, 18),
     ["GET /api/v1/categories/tree"], ["выпадающее дерево категорий"], ""),
    ("catalog-search-input", "catalog_products", "Поиск по каталогу", (18, 18),
     ["GET /api/v1/products/search"], ["подсказки поиска"], ""),
    ("product-summary-panel", "product_card", "Параметры товара", (32, 44),
     ["GET /api/v1/products/{id}", "GET /api/v1/enums/units"],
     ["название, категория, единица"], ""),
    ("product-variants-table", "product_card", "Таблица вариантов", (54, 80),
     ["GET /api/v1/products/{id}/variants"], ["грид вариантов"], ""),
    ("product-add-variant-btn", "product_card", "Добавить вариант", (86, 9),
     ["POST /api/v1/variants"], ["модалка «Новый вариант»"], ""),
    ("product-stock-widget", "product_card", "Остатки по складам", (76, 44),
     ["GET /api/v1/stock/product/{productId}"], ["полоски остатка по складам"], ""),
    ("product-price-input", "product_card", "Цена товара", (40, 50),
     ["PUT /api/v1/prices/{id}"], ["подсказка «цена обновлена»"],
     "отрицательная цена → 400, поле краснеет"),
    ("orders-table", "orders_list", "Список заказов", (54, 60),
     ["GET /api/v1/orders"], ["таблица заказов"], ""),
    ("orders-filter-status", "orders_list", "Фильтр по статусу", (24, 18),
     ["GET /api/v1/enums/order-statuses"], ["набор чипов статусов"], ""),
    ("orders-create-btn", "orders_list", "Новый заказ", (86, 18),
     ["GET /api/v1/customers", "POST /api/v1/carts",
      "POST /api/v1/carts/{id}/items", "POST /api/v1/orders"],
     ["модалка «Новый заказ»"],
     "каскад Клиент → Корзина → Позиции → Заказ"),
    ("orders-export-btn", "orders_list", "Выгрузить реестр", (66, 18),
     ["POST /api/v1/orders/export"], ["тост «Файл готов»"], ""),
    ("order-header-panel", "order_card", "Шапка заказа", (32, 12),
     ["GET /api/v1/orders/{id}"], ["номер, клиент, статус"], ""),
    ("order-lines-table", "order_card", "Строки заказа", (54, 78),
     ["GET /api/v1/orders/{id}/lines"], ["грид строк"], ""),
    ("order-confirm-btn", "order_card", "Подтвердить заказ", (86, 9),
     ["POST /api/v1/orders/{id}/confirm"],
     ["статус «Подтверждён»", "плашка «Ожидается оплата»"],
     "после подтверждения событие уходит в шину"),
    ("order-cancel-btn", "order_card", "Отменить заказ", (66, 9),
     ["POST /api/v1/orders/{id}/cancel"], ["модалка подтверждения"], ""),
    ("order-payment-panel", "order_card", "Оплата", (32, 40),
     ["GET /api/v1/payments/order/{orderId}", "POST /api/v1/payments",
      "GET /api/v1/enums/payment-methods"],
     ["состояние оплаты", "кнопка «Списать»"], ""),
    ("order-shipment-panel", "order_card", "Отгрузка", (76, 40),
     ["GET /api/v1/shipments/order/{orderId}", "POST /api/v1/shipments"],
     ["трек-номер, перевозчик"], ""),
    ("order-return-btn", "order_card", "Оформить возврат", (46, 9),
     ["GET /api/v1/enums/return-reasons", "POST /api/v1/returns"],
     ["модалка «Возврат»"], ""),
    ("stock-table", "warehouse_stock", "Таблица остатков", (54, 60),
     ["GET /api/v1/stock"], ["грид остатков"], ""),
    ("stock-warehouse-filter", "warehouse_stock", "Фильтр по складу", (30, 18),
     ["GET /api/v1/warehouses"], ["выпадающий список складов"], ""),
    ("stock-move-btn", "warehouse_stock", "Перемещение", (87, 18),
     ["POST /api/v1/stock/moves"], ["модалка «Перемещение»"], ""),
    ("stock-recount-btn", "warehouse_stock", "Пересчитать позицию", (66, 18),
     ["POST /api/v1/stock/{id}/recount"], ["тост «Позиция пересчитана»"], ""),
    ("purchase-header-panel", "purchase_order", "Поставщик и условия", (32, 34),
     ["GET /api/v1/purchases/{id}", "GET /api/v1/suppliers"],
     ["поставщик, склад, сумма"], ""),
    ("purchase-lines-table", "purchase_order", "Строки закупки", (54, 76),
     ["GET /api/v1/purchases/{id}/lines"], ["грид строк закупки"], ""),
    ("purchase-approve-btn", "purchase_order", "Согласовать закупку", (76, 34),
     ["POST /api/v1/purchases/{id}/approve"], ["статус «Согласована»"], ""),
    ("purchase-receive-btn", "purchase_order", "Принять поставку", (76, 44),
     ["POST /api/v1/purchases/{id}/receive"],
     ["статус «Принята»", "плашка «Остатки обновлены»"],
     "приёмка публикует событие пополнения"),
    ("invoices-table", "billing_invoices", "Таблица счетов", (54, 60),
     ["GET /api/v1/invoices"], ["таблица счетов"], ""),
    ("invoice-issue-btn", "billing_invoices", "Выставить счёт", (87, 18),
     ["POST /api/v1/invoices"], ["модалка «Новый счёт»"], ""),
    ("invoice-send-btn", "billing_invoices", "Отправить счёт", (66, 18),
     ["POST /api/v1/invoices/{id}/send"], ["тост «Счёт отправлен»"], ""),
    ("invoice-refund-btn", "billing_invoices", "Вернуть оплату", (46, 18),
     ["POST /api/v1/refunds"], ["модалка «Возврат оплаты»"], ""),
    ("customers-table", "crm_customers", "Таблица клиентов", (54, 60),
     ["GET /api/v1/customers"], ["таблица клиентов"], ""),
    ("customers-create-btn", "crm_customers", "Добавить клиента", (87, 18),
     ["POST /api/v1/customers"], ["модалка «Новый клиент»"], ""),
    ("customer-segment-chip", "crm_customers", "Сегмент клиента", (30, 18),
     ["GET /api/v1/segments"], ["чип сегмента"], ""),
    ("customer-loyalty-panel", "crm_customers", "Бонусный счёт", (76, 40),
     ["GET /api/v1/loyalty/customer/{customerId}"], ["баланс бонусов"], ""),
    ("campaigns-table", "crm_campaigns", "Таблица кампаний", (54, 60),
     ["GET /api/v1/campaigns"], ["таблица кампаний"], ""),
    ("campaign-create-btn", "crm_campaigns", "Новая кампания", (86, 18),
     ["GET /api/v1/segments", "POST /api/v1/campaigns"],
     ["модалка «Новая кампания»"], ""),
    ("campaign-launch-btn", "crm_campaigns", "Запустить кампанию", (66, 18),
     ["POST /api/v1/campaigns/{id}/launch"], ["статус «Идёт»"], ""),
    ("accounts-table", "admin_accounts", "Таблица учётных записей", (54, 60),
     ["GET /api/v1/accounts"], ["таблица записей"], ""),
    ("account-role-select", "admin_accounts", "Роль", (30, 18),
     ["GET /api/v1/roles"], ["выпадающий список ролей"], ""),
    ("account-toggle-active", "admin_accounts", "Активировать / отключить", (92, 40),
     ["PUT /api/v1/accounts/{id}"], ["тост «Запись отключена»"], ""),
    ("accounts-audit-link", "admin_accounts", "История изменений", (66, 18),
     ["GET /api/v1/audit"], ["панель истории"], ""),
    ("sales-report-chart", "reports_sales", "Динамика продаж", (54, 44),
     ["GET /api/v1/orders/report/sales"], ["график выручки"], ""),
    ("sales-period-picker", "reports_sales", "Период", (24, 18),
     ["GET /api/v1/enums/periods"], ["выбор периода"], ""),
    ("sales-export-btn", "reports_sales", "Выгрузить", (44, 18),
     ["POST /api/v1/orders/export"], ["файл выгрузки"], ""),
]

# Интеграции за пределами HTTP-ответа: from, kind, to, label
INTEGRATIONS = [
    ("POST /api/v1/orders/{id}/confirm", "publish", "topic:order.confirmed",
     "публикует подтверждение заказа"),
    ("POST /api/v1/orders/{id}/cancel", "publish", "topic:order.cancelled",
     "публикует отмену"),
    ("topic:order.confirmed", "consume", "svc:checkout-worker",
     "забирает заказ в оплату"),
    ("svc:checkout-worker", "publish", "topic:payment.authorized",
     "результат авторизации"),
    ("svc:checkout-worker", "publish", "topic:payment.failed",
     "отказ платёжного шлюза"),
    ("topic:payment.authorized", "consume", "svc:billing-worker",
     "выписывает счёт"),
    ("svc:billing-worker", "publish", "topic:invoice.issued", "счёт выписан"),
    ("topic:invoice.issued", "consume", "svc:notifier", "уведомляет клиента"),
    ("svc:notifier", "publish", "topic:notification.sent", "факт отправки"),
    ("POST /api/v1/shipments", "publish", "topic:shipment.created",
     "публикует отгрузку"),
    ("topic:shipment.created", "consume", "svc:warehouse-worker",
     "резервирует остаток"),
    ("svc:warehouse-worker", "publish", "topic:stock.reserved", "резерв поставлен"),
    ("POST /api/v1/purchases/{id}/receive", "publish", "topic:stock.replenished",
     "приёмка поставки"),
    ("topic:stock.replenished", "consume", "svc:warehouse-worker",
     "приходует остаток"),
    ("POST /api/v1/returns", "publish", "topic:return.requested",
     "публикует заявку на возврат"),
    ("topic:return.requested", "consume", "svc:billing-worker",
     "готовит возврат оплаты"),
    ("POST /api/v1/campaigns/{id}/launch", "publish", "topic:campaign.launched",
     "запуск рассылки"),
    ("topic:campaign.launched", "consume", "svc:crm-worker",
     "разворачивает аудиторию"),
    ("svc:crm-worker", "publish", "topic:loyalty.points.awarded",
     "начисление бонусов"),
    ("topic:loyalty.points.awarded", "consume", "svc:notifier",
     "уведомление о бонусах"),
    ("POST /api/v1/orders/export", "storage", "store:objectstore",
     "кладёт выгрузку в бакет"),
    ("POST /api/v1/invoices/{id}/send", "storage", "store:objectstore",
     "сохраняет PDF счёта"),
]

# Какие эндпоинты какие таблицы трогают.
STORAGE_WRITES = {
    "POST /api/v1/reviews": ["review"],
    "PUT /api/v1/reviews/{id}": ["review"],
    "POST /api/v1/reviews/{id}/publish": ["review", "audit_event"],
    "POST /api/v1/gift-cards": ["gift_card"],
    "POST /api/v1/gift-cards/{id}/redeem": ["gift_card", "audit_event"],
    "POST /api/v1/products": ["product", "product_attribute"],
    "PUT /api/v1/products/{id}": ["product"],
    "DELETE /api/v1/products/{id}": ["product", "product_media"],
    "POST /api/v1/products/{id}/archive": ["product", "audit_event"],
    "POST /api/v1/categories": ["category"],
    "PUT /api/v1/categories/{id}": ["category"],
    "POST /api/v1/variants": ["product_variant"],
    "PUT /api/v1/variants/{id}": ["product_variant"],
    "POST /api/v1/prices": ["price_entry"],
    "PUT /api/v1/prices/{id}": ["price_entry"],
    "POST /api/v1/prices/bulk": ["price_entry", "audit_event"],
    "POST /api/v1/promotions": ["promotion", "promotion_rule"],
    "POST /api/v1/promotions/{id}/activate": ["promotion"],
    "POST /api/v1/carts": ["cart"],
    "POST /api/v1/carts/{id}/items": ["cart_item", "cart"],
    "DELETE /api/v1/carts/{id}/items/{itemId}": ["cart_item"],
    "POST /api/v1/carts/{id}/checkout": ["cart", "order_header", "order_line"],
    "POST /api/v1/orders": ["order_header", "order_line", "order_status_history"],
    "PUT /api/v1/orders/{id}": ["order_header"],
    "DELETE /api/v1/orders/{id}": ["order_header", "order_line"],
    "POST /api/v1/orders/{id}/confirm": ["order_header", "order_status_history"],
    "POST /api/v1/orders/{id}/cancel": ["order_header", "order_status_history"],
    "POST /api/v1/payments": ["payment"],
    "PUT /api/v1/payments/{id}": ["payment"],
    "POST /api/v1/payments/{id}/capture": ["payment", "payment_attempt"],
    "POST /api/v1/shipments": ["shipment", "shipment_item"],
    "PUT /api/v1/shipments/{id}": ["shipment"],
    "DELETE /api/v1/shipments/{id}": ["shipment", "shipment_item"],
    "POST /api/v1/shipments/{id}/dispatch": ["shipment"],
    "POST /api/v1/returns": ["return_request", "return_line"],
    "POST /api/v1/returns/{id}/approve": ["return_request"],
    "POST /api/v1/stock/moves": ["stock_move", "stock_item"],
    "POST /api/v1/stock/{id}/recount": ["stock_item", "inventory_count"],
    "POST /api/v1/warehouses": ["warehouse"],
    "PUT /api/v1/warehouses/{id}": ["warehouse"],
    "DELETE /api/v1/warehouses/{id}": ["warehouse"],
    "POST /api/v1/suppliers": ["supplier"],
    "PUT /api/v1/suppliers/{id}": ["supplier"],
    "DELETE /api/v1/suppliers/{id}": ["supplier"],
    "POST /api/v1/purchases": ["purchase_order", "purchase_order_line"],
    "PUT /api/v1/purchases/{id}": ["purchase_order"],
    "POST /api/v1/purchases/{id}/approve": ["purchase_order"],
    "POST /api/v1/purchases/{id}/receive": ["purchase_order", "stock_item"],
    "POST /api/v1/invoices": ["invoice", "invoice_line"],
    "PUT /api/v1/invoices/{id}": ["invoice"],
    "POST /api/v1/invoices/{id}/send": ["invoice"],
    "POST /api/v1/refunds": ["refund"],
    "POST /api/v1/refunds/{id}/confirm": ["refund", "invoice"],
    "POST /api/v1/subscriptions": ["subscription"],
    "PUT /api/v1/subscriptions/{id}": ["subscription"],
    "POST /api/v1/subscriptions/{id}/pause": ["subscription"],
    "POST /api/v1/customers": ["customer", "customer_address"],
    "PUT /api/v1/customers/{id}": ["customer"],
    "DELETE /api/v1/customers/{id}": ["customer", "customer_address"],
    "POST /api/v1/segments": ["customer_segment"],
    "POST /api/v1/segments/{id}/recalculate": ["customer_segment", "segment_member"],
    "POST /api/v1/campaigns": ["campaign"],
    "PUT /api/v1/campaigns/{id}": ["campaign"],
    "POST /api/v1/campaigns/{id}/launch": ["campaign", "campaign_target"],
    "POST /api/v1/loyalty/{id}/award": ["loyalty_account", "loyalty_transaction"],
    "POST /api/v1/accounts": ["account", "account_role"],
    "PUT /api/v1/accounts/{id}": ["account", "account_role"],
    "DELETE /api/v1/accounts/{id}": ["account", "account_role"],
    "POST /api/v1/roles": ["role", "role_access_right"],
    "POST /api/v1/notifications": ["notification"],
}

STORAGE_READS = {
    "GET /api/v1/reviews": ["review"],
    "GET /api/v1/reviews/product/{productId}": ["review", "review_vote"],
    "GET /api/v1/gift-cards/{id}": ["gift_card"],
    "GET /api/v1/products": ["product", "category"],
    "GET /api/v1/products/{id}": ["product", "product_attribute", "product_media"],
    "GET /api/v1/products/search": ["product", "price_entry"],
    "GET /api/v1/products/{id}/variants": ["product_variant"],
    "GET /api/v1/categories": ["category"],
    "GET /api/v1/categories/tree": ["category"],
    "GET /api/v1/variants": ["product_variant"],
    "GET /api/v1/variants/product/{productId}": ["product_variant", "product"],
    "GET /api/v1/prices": ["price_entry", "price_list"],
    "GET /api/v1/prices/product/{productId}": ["price_entry", "product"],
    "GET /api/v1/promotions": ["promotion", "promotion_rule"],
    "GET /api/v1/carts/{id}": ["cart", "cart_item"],
    "GET /api/v1/orders": ["order_header", "customer"],
    "GET /api/v1/orders/{id}": ["order_header", "order_status_history"],
    "GET /api/v1/orders/{id}/lines": ["order_line", "product_variant"],
    "GET /api/v1/orders/customer/{customerId}": ["order_header", "customer"],
    "GET /api/v1/orders/report/sales": ["order_header", "order_line"],
    "GET /api/v1/payments": ["payment"],
    "GET /api/v1/payments/order/{orderId}": ["payment", "order_header"],
    "GET /api/v1/payments/{id}/receipt": ["payment", "payment_attempt"],
    "GET /api/v1/shipments": ["shipment"],
    "GET /api/v1/shipments/order/{orderId}": ["shipment", "shipment_item"],
    "GET /api/v1/returns": ["return_request"],
    "GET /api/v1/returns/order/{orderId}": ["return_request", "return_line"],
    "GET /api/v1/stock": ["stock_item", "warehouse"],
    "GET /api/v1/stock/product/{productId}": ["stock_item", "product_variant"],
    "GET /api/v1/stock/warehouse/{warehouseId}": ["stock_item", "warehouse"],
    "GET /api/v1/warehouses": ["warehouse"],
    "GET /api/v1/suppliers": ["supplier"],
    "GET /api/v1/suppliers/{id}/products": ["supplier", "product"],
    "GET /api/v1/purchases": ["purchase_order", "supplier"],
    "GET /api/v1/purchases/{id}": ["purchase_order"],
    "GET /api/v1/purchases/{id}/lines": ["purchase_order_line", "product_variant"],
    "GET /api/v1/invoices": ["invoice", "customer"],
    "GET /api/v1/invoices/{id}/lines": ["invoice_line", "tax_rate"],
    "GET /api/v1/refunds": ["refund"],
    "GET /api/v1/subscriptions": ["subscription", "subscription_plan"],
    "GET /api/v1/customers": ["customer"],
    "GET /api/v1/customers/search": ["customer"],
    "GET /api/v1/customers/{id}/addresses": ["customer_address"],
    "GET /api/v1/segments": ["customer_segment"],
    "GET /api/v1/campaigns": ["campaign", "customer_segment"],
    "GET /api/v1/campaigns/{id}/targets": ["campaign_target", "customer"],
    "GET /api/v1/loyalty": ["loyalty_account"],
    "GET /api/v1/loyalty/customer/{customerId}": ["loyalty_account",
                                                  "loyalty_transaction"],
    "GET /api/v1/accounts": ["account", "account_role", "role"],
    "GET /api/v1/accounts/me": ["account", "role_access_right"],
    "GET /api/v1/roles": ["role"],
    "GET /api/v1/roles/{id}/permissions": ["role_access_right", "access_right"],
    "GET /api/v1/notifications": ["notification"],
    "GET /api/v1/notifications/templates": ["notification_template"],
    "GET /api/v1/audit": ["audit_event"],
    "GET /api/v1/audit/object/{objectId}": ["audit_event"],
}


# ===========================================================================
# 2. Развёртка модели
# ===========================================================================
def build_endpoints() -> list[dict]:
    """Полный перечень эндпоинтов: пять стандартных операций плюс особые."""
    out: list[dict] = []
    for key, label, crud, extra in RESOURCES:
        for letter in crud:
            method, tpl, subtype, title = CRUD_TEMPLATES[letter]
            path = tpl.format(r=key)
            out.append({"method": method, "path": path, "service": key,
                        "serviceLabel": label, "subtype": subtype,
                        "title": f"{title}: {label}"})
        for method, path, subtype, title in extra:
            out.append({"method": method, "path": path, "service": key,
                        "serviceLabel": label, "subtype": subtype, "title": title})
    seen = set()
    for e in out:
        key = f"{e['method']} {e['path']}"
        assert key not in seen, f"дубль эндпоинта {key}"
        seen.add(key)
    return out


ENDPOINTS = build_endpoints()
BY_CALL = {f"{e['method']} {e['path']}": e for e in ENDPOINTS}


def check_model() -> None:
    """Модель обязана сходиться: иначе сцена соберётся наполовину."""
    for call in list(STORAGE_WRITES) + list(STORAGE_READS):
        assert call in BY_CALL, f"нет такого эндпоинта в каталоге: {call}"
    for tables in list(STORAGE_WRITES.values()) + list(STORAGE_READS.values()):
        for t in tables:
            assert t in TABLES, f"нет такой таблицы: {t}"
    for ctl in CONTROLS:
        for call in ctl[4]:
            assert call in BY_CALL, f"элемент UI зовёт несуществующий {call}"
    for _, _, chain, tables, topics in JOURNEYS:
        for method, path in chain:
            assert f"{method} {path}" in BY_CALL, f"сценарий зовёт {method} {path}"
        for t in tables:
            assert t in TABLES, f"сценарий трогает несуществующую {t}"
        for t in topics:
            assert t in TOPICS, f"сценарий шлёт в несуществующий топик {t}"
    for screen, _, method, path in OBSERVED_ONLY:
        assert f"{method} {path}" in BY_CALL, f"рекордер видел {method} {path}"
    screens = {s[0] for s in SCREENS}
    for c in CONTROLS:
        assert c[1] in screens, f"элемент на несуществующем экране: {c[1]}"
    for src, _, dst, _ in INTEGRATIONS:
        if not src.startswith(("topic:", "svc:", "store:")):
            assert src in BY_CALL, f"интеграция из несуществующего {src}"
        if dst.startswith("topic:"):
            assert dst[6:] in TOPICS, f"нет топика {dst}"
    for _, _, _, services in DOMAINS:
        for s in services:
            assert any(r[1] == s for r in RESOURCES), f"домен зовёт сервис {s}"
    labels = {r[1] for r in RESOURCES}
    covered = {s for _, _, _, ss in DOMAINS for s in ss}
    assert labels == covered, f"сервисы без домена: {labels - covered}"
    for table, (owner, _, cols) in TABLES.items():
        assert any(c[0] == owner for c in CONTAINERS), f"{table}: нет {owner}"
        for col in cols:
            if "->" in col:
                assert col.split("->")[1] in TABLES, f"{table}: FK на {col}"


# ===========================================================================
# 3. Вспомогательное: PNG-заглушки экранов
# ===========================================================================
FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "11110", "10001", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "11110", "10000", "10000", "10000", "11111"],
    "F": ["11111", "10000", "11110", "10000", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "11111", "10001", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "#": ["01010", "01010", "11111", "01010", "11111", "01010", "01010"],
    "«": ["00000", "00101", "01010", "10100", "01010", "00101", "00000"],
    "»": ["00000", "10100", "01010", "00101", "01010", "10100", "00000"],
}


class Canvas:
    """Минимальный растр: только то, что нужно для заглушки экрана."""

    def __init__(self, w: int, h: int, bg: tuple[int, int, int]):
        self.w, self.h = w, h
        self.buf = bytearray(bytes(bg) * (w * h))

    def rect(self, x, y, w, h, color, fill=True, width=1):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.w, int(x + w)), min(self.h, int(y + h))
        col = bytes(color)
        for py in range(y0, y1):
            if fill or py < y0 + width or py >= y1 - width:
                start = (py * self.w + x0) * 3
                self.buf[start:start + (x1 - x0) * 3] = col * (x1 - x0)
            else:
                for px in list(range(x0, min(x0 + width, x1))) + \
                        list(range(max(x0, x1 - width), x1)):
                    off = (py * self.w + px) * 3
                    self.buf[off:off + 3] = col

    def text(self, x, y, s, color, scale=2):
        cx = x
        for ch in s.upper():
            glyph = FONT.get(ch)
            if glyph is None:
                cx += 6 * scale
                continue
            for ry, row in enumerate(glyph):
                for rxi, bit in enumerate(row):
                    if bit == "1":
                        self.rect(cx + rxi * scale, y + ry * scale,
                                  scale, scale, color)
            cx += 6 * scale
        return cx

    def png(self, path: Path) -> None:
        raw = bytearray()
        for row in range(self.h):
            raw.append(0)
            raw += self.buf[row * self.w * 3:(row + 1) * self.w * 3]

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        head = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        # Байты сжатия зависят от сборки zlib у интерпретатора (zlib-ng в 3.14 сжимает
        # иначе), а картинка — нет. Файл с теми же пикселями не перезаписывается:
        # иначе каждый запуск генератора на другом Python пачкал бы git.
        if png_pixels(path) == (head, bytes(raw)):
            return
        blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                + chunk(b"IEND", b""))
        path.write_bytes(blob)


def png_pixels(path: Path):
    """(IHDR, раскодированные строки) уже лежащего PNG или `None`."""
    if not path.is_file():
        return None
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos, head, idat = 8, b"", b""
    while pos + 8 <= len(data):
        size, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        if tag == b"IHDR":
            head = body
        elif tag == b"IDAT":
            idat += body
        pos += 12 + size
    try:
        return head, zlib.decompress(idat)
    except zlib.error:
        return None


BG = (24, 27, 34)
PANEL = (33, 38, 48)
LINE = (58, 66, 80)
INK = (150, 162, 180)
ACCENT = (86, 156, 214)


def draw_screen(path: Path, title_latin: str, frame: list[dict]) -> None:
    """Нейтральная заглушка вместо снимка экрана.

    Настоящих скриншотов в наборе нет и не может быть: рисуем каркас по той
    же разметке, что описана в карте, и подписываем латиницей.
    """
    w, h = 1280, 800
    c = Canvas(w, h, BG)
    for block in frame:
        x = block["x"] * w / 100
        y = block["y"] * h / 100
        bw = block["w"] * w / 100
        bh = block["h"] * h / 100
        kind = block["kind"]
        c.rect(x, y, bw, bh, PANEL if kind != "button" else (43, 74, 106))
        c.rect(x, y, bw, bh, LINE, fill=False, width=2)
        if kind == "table":
            rows = block.get("rows", 5)
            step = bh / (rows + 1)
            for i in range(1, rows + 1):
                c.rect(x + 12, y + step * i, bw - 24, 2, LINE)
        if kind == "sidebar":
            for i in range(6):
                c.rect(x + 8, y + 30 + i * 46, bw - 16, 16, LINE)
        if kind in ("hero", "panel"):
            c.rect(x + 14, y + 16, bw * 0.5, 6, LINE)
            c.rect(x + 14, y + 34, bw * 0.32, 6, LINE)
    c.rect(0, h - 74, w, 74, PANEL)
    c.rect(0, h - 74, w, 3, ACCENT)
    c.text(26, h - 56, title_latin[:34], ACCENT, scale=3)
    c.text(26, h - 26, "DEMO PLACEHOLDER - SYNTHETIC DATA", INK, scale=2)
    c.png(path)


# ===========================================================================
# 4. Файлы проектного знания
# ===========================================================================
def yaml_quote(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def write_atlas_yaml() -> None:
    L: list[str] = []
    a = L.append
    a("# Конфигурация просмотрщика «Веха» — демонстрационный ландшафт.")
    a("#")
    a("# ВСЁ СОДЕРЖИМОЕ ВЫМЫШЛЕНО. Ландшафт «Сеть N»: интернет-магазин, склад,")
    a("# биллинг и CRM. Файл целиком создаётся generate.py — правки руками")
    a("# перетрутся при следующей генерации.")
    a("")
    a("project:")
    a("  name: Сеть N")
    a("  frontend_env: FRONTEND_URL")
    a("  api_prefix: /api/v1")
    a("")
    a("artifacts_dir: atlas")
    a("")
    a("# ---------------------------------------------------------------------")
    a("# Контейнеры и внешние системы. Первый подошедший шаблон выигрывает,")
    a("# `default: true` подбирает всё остальное.")
    a("# ---------------------------------------------------------------------")
    a("containers:")
    for key, label, kind, tier, cname, note in CONTAINERS:
        a(f"  {key}:")
        a(f"    label: {yaml_quote(label)}")
        a(f"    kind: {kind}")
        if tier:
            a(f"    tier: {tier}")
        if key == "storefront-api":
            a("    default: true")
            a("    match:")
            a('      - "/api/v1/*"')
        else:
            a("    match: []")
        a(f"    container_names: [{cname}]")
        if note:
            a(f"    note: {yaml_quote(note)}")
    a("")
    a("# Ручная карта интеграций. Это данные проекта, сервер про неё не знает,")
    a("# пока она не названа здесь.")
    a("app_map: app_map.yaml")
    a("")
    a("# Признак UI-теста в отчёте Allure: подстрока полного имени теста.")
    a("tests:")
    a('  ui_markers: ["src.ui"]')
    a("")
    a("# Вкладка «Live» читает снимки, а не стенд: сервер наружу не ходит.")
    a("live:")
    a("  snapshot_dir: live")
    a("  container_logs_dir: logs/containers")
    a("  poll_seconds: 60")
    a("  containers_ttl: 20")
    a("  mrs_ttl: 120")
    a("")
    a("# Куда backend идёт дальше, обслуживая путь.")
    a("depends:")
    for path, to, label in [
        ("/api/v1/payments*", "payment-gateway", "запрашивает авторизацию"),
        ("/api/v1/shipments*", "delivery-provider", "резервирует доставку"),
        ("/api/v1/accounts*", "identity", "сверяет учётную запись"),
        ("/api/v1/orders/*/confirm", "checkout-worker", "отдаёт заказ в оплату"),
        ("/api/v1/purchases/*/receive", "warehouse-worker", "проводит приёмку"),
        ("/api/v1/campaigns/*/launch", "crm-worker", "разворачивает аудиторию"),
    ]:
        a(f'  - path: "{path}"')
        a(f"    to: {to}")
        a(f"    label: {yaml_quote(label)}")
    a("")
    a("# Домены: как делим площадку API, чтобы она читалась и дышала.")
    a("domains:")
    for key, label, tier, services in DOMAINS:
        a(f"  {key}:")
        a(f"    label: {yaml_quote(label)}")
        a(f"    tier: {tier}")
        a("    services: [" + ", ".join(yaml_quote(s) for s in services) + "]")
    a("")
    a("# Экраны: заглушки, нарисованные генератором. Настоящих снимков нет.")
    a("screens:")
    a("  manifest: screens/manifest.json")
    a("  images_dir: screens")
    a("  featured:")
    for key in ("orders_list", "order_card", "catalog_products",
                "warehouse_stock", "billing_invoices"):
        a(f"    - {key}")
    a("  paths:")
    a('    order_card: "/#/orders/{id}"')
    a('    product_card: "/#/catalog/products/{id}"')
    a('    purchase_order: "/#/warehouse/purchases/{id}"')
    a('    orders_create_modal: "/#/orders"')
    a('    catalog_filters_open: "/#/catalog/products"')
    a("  alias:")
    for key, _, _, _, _ in SCREENS:
        a(f"    {key}: {key}")
    a("")
    a("database:")
    a("  label: PostgreSQL «shop»")
    a("  schemas: [public]")
    a("  exclude:")
    a("    - databasechangelog")
    a("    - databasechangeloglock")
    a("  count_rows: true")
    a("")
    a("db_effects:")
    a("  settle_sec: 12")
    a("  per_test_limit: 14")
    a("  include:")
    for mask in ("src/api/tests/orders/*", "src/api/tests/payments/*",
                 "src/api/tests/stock/*", "src/api/tests/invoices/*",
                 "src/api/tests/journeys/*"):
        a(f'    - "{mask}"')
    a("")
    a("bus_flow:")
    a("  topics: []")
    a("  settle_sec: 5")
    a("  per_test_limit: 12")
    a("  include:")
    a('    - "src/api/tests/journeys/*"')
    a("")
    a("runner:")
    a("  history_limit: 10")
    a("  suites:")
    for key, label, args, note in [
        ("smoke", "Дымовой", ["src/api/tests/orders", "src/api/tests/products", "-x"],
         "быстрая проверка, что стенд жив"),
        ("api", "API целиком", ["src/api/tests"], "полный прогон API"),
        ("ui", "UI целиком", ["src/ui/tests"], "браузерные тесты"),
        ("rbac", "Права доступа", ["src/api/tests/rbac"], ""),
        ("contract", "Контракт OpenAPI", ["src/api/tests/contract"], ""),
        ("journeys", "Пользовательские сценарии", ["src/api/tests/journeys"], ""),
        ("full", "Всё", ["src/api/tests", "src/ui/tests"], "API и UI подряд"),
    ]:
        a(f"    {key}:")
        a(f"      label: {yaml_quote(label)}")
        a("      args: [" + ", ".join(yaml_quote(x) for x in args) + "]")
        if note:
            a(f"      note: {yaml_quote(note)}")
        if key == "full":
            # только полный набор годится для тренда среднего покрытия
            a("      full: true")
    a("")
    a("logs:")
    a("  Прогон pytest: logs/pytest.log")
    a("  Сборщик артефактов: logs/collect.log")
    a("")
    a("container_logs:")
    a("  tail: 2000")
    a("  rules:")
    a("    - kind: table")
    a("      pattern: 'table \"(\\w+)\"'")
    a("      known_only: true")
    a("    - kind: topic")
    a("      pattern: '\\b([a-z][a-z0-9-]*(?:\\.[a-z][a-z0-9-]*){1,})\\b'")
    a("      known_only: true")
    a("    - kind: listener")
    a("      pattern: '\\b(\\w+(?:Listener|Consumer))\\b'")
    a("")
    a("specs:")
    a("  sources:")
    for f, label, prefix in SPEC_SOURCES:
        a(f"    - file: {f}")
        a(f"      label: {yaml_quote(label)}")
        a(f"      prefix: {prefix}")
    a("  heading_styles:")
    a("    Heading1: 1")
    a("    Heading2: 2")
    a("    Heading3: 3")
    a("  min_chars: 40")
    a("  api_roots: []")
    a("")
    a("ui_flow:")
    a("  ignore_url:")
    for mask in ('*/static/*', '*.png', '*.svg', '*.woff*'):
        a(f'    - "{mask}"')
    a("  ignore_control:")
    a('    - "DEMO*"')
    a("  attribution_window_ms: 2500")
    a("")
    (ROOT / "atlas.yaml").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")


def write_app_map() -> None:
    L: list[str] = []
    a = L.append
    a("# Карта интеграций демонстрационного ландшафта «Сеть N».")
    a("#")
    a("# ВСЁ ВЫМЫШЛЕНО. Файл создаётся generate.py вместе с артефактами —")
    a("# так карта и снятые данные не расходятся.")
    a("#")
    a("# id узлов: screen:<key>, ui:<testid>, api:<METHOD /path>, topic:<name>,")
    a("#           store:<name>, svc:<name>")
    a("")
    a("services:")
    for key, label, kind, note in [
        ("storefront-api", "storefront-api", "backend", "основной API витрины"),
        ("checkout-worker", "checkout-worker", "backend",
         "оплата заказа, общается через шину"),
        ("warehouse-worker", "warehouse-worker", "backend", "остатки и резервы"),
        ("billing-worker", "billing-worker", "backend", "счета и возвраты"),
        ("crm-worker", "crm-worker", "backend", "сегменты и бонусы"),
        ("notifier", "notifier", "backend", "рассылка уведомлений"),
        ("identity", "Провайдер входа", "auth", ""),
        ("db-primary", "PostgreSQL «shop»", "db", ""),
        ("broker", "Шина сообщений", "bus", ""),
        ("objectstore", "Объектное хранилище", "storage", ""),
    ]:
        a(f"  {key}:")
        a(f"    label: {yaml_quote(label)}")
        a(f"    kind: {kind}")
        if note:
            a(f"    note: {yaml_quote(note)}")
    a("")
    a("screens:")
    for key, label, path, page_object, frame in SCREENS:
        a(f"  {key}:")
        a(f"    label: {yaml_quote(label)}")
        a(f"    path: {yaml_quote(path)}")
        a(f"    page_object: {page_object}")
        a("    frame:")
        for b in frame:
            parts = ", ".join(
                f"{k}: {yaml_quote(v) if isinstance(v, str) else v}"
                for k, v in b.items())
            a(f"      - {{{parts}}}")
    a("")
    a("# Элементы UI и эндпоинты, которые они дёргают.")
    a("ui_elements:")
    for testid, screen, label, slot, calls, renders, note in CONTROLS:
        a(f"  - testid: {testid}")
        a(f"    slot: {{x: {slot[0]}, y: {slot[1]}}}")
        a(f"    screen: {screen}")
        a(f"    label: {yaml_quote(label)}")
        a("    calls:")
        for c in calls:
            a(f"      - {yaml_quote(c)}")
        if renders:
            a("    renders: [" + ", ".join(yaml_quote(r) for r in renders) + "]")
        if note:
            a(f"    note: {yaml_quote(note)}")
    a("")
    a("integrations:")
    for src, kind, dst, label in INTEGRATIONS:
        a(f"  - from: {yaml_quote(src)}")
        a(f"    kind: {kind}")
        a(f"    to: {dst}")
        a(f"    label: {yaml_quote(label)}")
    a("")
    a("storage_writes:")
    for call, tables in STORAGE_WRITES.items():
        a(f"  {yaml_quote(call)}: [" + ", ".join(tables) + "]")
    a("")
    a("storage_reads:")
    for call, tables in STORAGE_READS.items():
        a(f"  {yaml_quote(call)}: [" + ", ".join(tables) + "]")
    a("")
    a("auth:")
    a("  provider: svc:identity")
    a("  note: Bearer-токен; без токена 401, отключённая запись 403")
    (ROOT / "app_map.yaml").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")


# ===========================================================================
# 5. Артефакты
# ===========================================================================
def write_artifact(kind: str, payload: dict) -> None:
    """Артефакт агента: заголовок схемы 2, путь по области, проверка контракта.

    `artifacts.write` здесь не годится только одним — он ставит текущее время сбора, а
    набор обязан быть детерминированным. Поэтому заголовок собирается тут, но проверяет
    его код агента: набор, не прошедший `validate`, не записывается вовсе.
    """
    doc = {"version": SCHEMA_VERSION, "kind": kind, "scope": SCOPE.as_dict(),
           "collected_at": COLLECTED_AT, "collector": COLLECTOR, **payload}
    problems = validate(doc)
    if problems:
        raise SystemExit(f"{kind}: артефакт не сходится с контрактом: {problems}")
    path = SCOPE.path(ROOT / "atlas", kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def write_live(name: str, data: dict) -> None:
    """Снимок для вкладки «Live» — файл сервера, а не артефакт агента."""
    path = ROOT / "live" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def write_openapi() -> None:
    ops = []
    for e in ENDPOINTS:
        # Пара эндпоинтов намеренно не попадает в контракт: расхождение
        # «дёргали, а в контракте нет» — одна из находок, ради которых
        # инструмент и нужен.
        if e["path"] in ("/api/v1/stock/{id}/recount",
                         "/api/v1/loyalty/{id}/award",
                         "/api/v1/orders/report/sales"):
            continue
        method = e["method"]
        base = {"POST": "201", "DELETE": "204"}.get(method, "200")
        responses = [base]
        # Ошибочные коды объявлены не везде — это и есть предмет сверки.
        if e["service"] in ("orders", "payments", "products", "customers",
                            "categories", "carts"):
            responses += ["400", "404"] if "{id}" in e["path"] else ["400"]
        params = [{"name": p.strip("{}"), "in": "path", "required": True}
                  for p in e["path"].split("/") if p.startswith("{")]
        if e["subtype"] == "get_list_objects":
            params += [{"name": "page", "in": "query", "required": False},
                       {"name": "size", "in": "query", "required": False}]
        ops.append({
            "method": method,
            "path": e["path"],
            "operationId": _operation_id(e),
            "tags": [f"{e['service']}-controller"],
            "deprecated": e["path"].endswith("/archive"),
            "params": params,
            "body": _schema_name(e) if method in ("POST", "PUT") else None,
            "responses": responses,
        })
    # объявлено, но не реализовано и не покрыто — «объявлен, не встречался»
    for method, path, service in [
        ("GET", "/api/v1/orders/{id}/timeline", "orders"),
        ("POST", "/api/v1/orders/{id}/split", "orders"),
        ("GET", "/api/v1/stock/alerts", "stock"),
        ("POST", "/api/v1/segments/import", "segments"),
        ("GET", "/api/v1/audit/export", "audit"),
        ("PUT", "/api/v1/notifications/templates/{id}", "notifications"),
    ]:
        ops.append({"method": method, "path": path,
                    "operationId": path.strip("/").replace("/", "_"),
                    "tags": [f"{service}-controller"], "deprecated": False,
                    "params": [{"name": p.strip("{}"), "in": "path",
                                "required": True}
                               for p in path.split("/") if p.startswith("{")],
                    "body": None,
                    "responses": ["200" if method == "GET" else "202"]})
    # Документ, который отдал бы сервис, — и разбор его тем же кодом, что у агента.
    paths: dict = {}
    schemas: dict = {}
    for op in ops:
        item = {
            "operationId": op["operationId"],
            "tags": op["tags"],
            "parameters": [dict(p) for p in op["params"]],
            "responses": {code: {"description": ""} for code in op["responses"]},
        }
        if op["deprecated"]:
            item["deprecated"] = True
        if op["body"]:
            schemas[op["body"]] = {"type": "object", "properties": {}}
            item["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{op['body']}"}}}}
        paths.setdefault(op["path"], {})[op["method"].lower()] = item
    document = {"openapi": "3.0.3",
                "info": {"title": "Сеть N — витрина и склад", "version": "2026.09.3"},
                "paths": paths, "components": {"schemas": schemas}}
    write_artifact("contract.openapi", atlas_openapi.build(document))


def _operation_id(e: dict) -> str:
    tail = e["path"].rstrip("/").split("/")[-1]
    verb = {"GET": "find", "POST": "create", "PUT": "update",
            "DELETE": "delete"}[e["method"]]
    if tail.startswith("{"):
        return f"{verb}ById"
    return f"{verb}{tail.replace('-', ' ').title().replace(' ', '')}"


def _schema_name(e: dict) -> str:
    core = e["service"].rstrip("s").replace("-", " ").title().replace(" ", "")
    return f"{core}Request"


def write_db_schema() -> None:
    tables = []
    for name, (owner, rows, cols) in TABLES.items():
        columns = [
            {"name": "id", "type": "int8", "nullable": False, "default": None,
             "pk": True, "fk": None},
            {"name": "created_at", "type": "timestamp", "nullable": True,
             "default": "now()", "pk": False, "fk": None},
            {"name": "updated_at", "type": "timestamp", "nullable": True,
             "default": None, "pk": False, "fk": None},
        ]
        for col in cols:
            if "->" in col:
                cname, target = col.split("->")
                columns.append({
                    "name": cname, "type": "int8",
                    "nullable": cname.endswith("_id") and target == name,
                    "default": None, "pk": False,
                    "fk": {"table": target, "column": "id",
                           "onDelete": "CASCADE" if target in
                           ("order_header", "cart", "invoice", "purchase_order")
                           else "NO ACTION"}})
            else:
                cname, ctype = col.split(":")
                columns.append({"name": cname, "type": ctype,
                                "nullable": not cname.endswith(
                                    ("code", "title", "status", "number")),
                                "default": "false" if ctype == "bool" else None,
                                "pk": False, "fk": None})
        tables.append({"schema": "public", "name": name, "rows": rows,
                       "owner": owner, "columns": columns})
    # Строки, какими их вернул бы information_schema, — и сборка тем же кодом, что у
    # агента. Короткие имена типов разворачиваются обратно в длинные: агент сам
    # сворачивает их своим словарём.
    long_type = {v: k for k, v in atlas_db_schema.TYPE_ALIASES.items()}
    col_rows, pk_rows, fk_rows, count_rows = [], [], [], []
    for t in tables:
        count_rows.append({"table_name": t["name"], "rows": t["rows"]})
        for c in t["columns"]:
            short, _, length = c["type"].partition("(")
            if short == "varchar" and length:
                data_type, max_len = long_type[short], int(length.rstrip(")"))
            else:
                data_type, max_len = long_type.get(c["type"], c["type"]), None
            col_rows.append({
                "table_schema": "public", "table_name": t["name"],
                "column_name": c["name"], "data_type": data_type,
                "is_nullable": "YES" if c["nullable"] else "NO",
                "column_default": c["default"], "character_maximum_length": max_len})
            if c["pk"]:
                pk_rows.append({"table_schema": "public", "table_name": t["name"],
                                "column_name": c["name"]})
            if c["fk"]:
                fk_rows.append({"table_schema": "public", "table_name": t["name"],
                                "column_name": c["name"], "ref_table": c["fk"]["table"],
                                "ref_column": c["fk"]["column"],
                                "delete_rule": c["fk"]["onDelete"]})
    write_artifact("db.schema", atlas_db_schema.build(
        col_rows, pk_rows, fk_rows, count_rows,
        schemas=("public",), label="PostgreSQL «shop»"))


# ---- тесты ----------------------------------------------------------------
UI_SUITES = [
    ("catalog", "catalog_products", "Каталог / список товаров"),
    ("catalog", "product_card", "Каталог / карточка товара"),
    ("orders", "orders_list", "Заказы / реестр"),
    ("orders", "order_card", "Заказы / карточка"),
    ("warehouse", "warehouse_stock", "Склад / остатки"),
    ("warehouse", "purchase_order", "Склад / закупка"),
    ("billing", "billing_invoices", "Биллинг / счета"),
    ("crm", "crm_customers", "CRM / клиенты"),
    ("crm", "crm_campaigns", "CRM / кампании"),
    ("admin", "admin_accounts", "Администрирование / записи"),
    ("reports", "reports_sales", "Отчёты / продажи"),
]

TEST_NAMES = {
    "get_list_objects": ["default", "elements", "filters", "sorting",
                         "pagination", "permissions"],
    "get_dynamic_object": ["default", "elements", "not_found", "permissions"],
    "get_static_object": ["default", "elements"],
    "get_by_parent": ["default", "elements", "not_found", "permissions"],
    "get_by_self": ["default", "elements", "permissions"],
    "post_create_object": ["success", "elements", "incorrect_body",
                           "extra_data", "cross_field", "permissions"],
    "put_object": ["success", "elements", "update_object", "incorrect_body",
                   "not_found", "cross_field", "permissions"],
    "delete_object": ["success", "lifecycle", "not_found", "permissions"],
    "action": ["success", "not_found", "cross_field", "permissions"],
}
TC_BY_NAME = {
    "default": "RequestDefault", "elements": "RequestElements",
    "filters": "RequestFilters", "sorting": "RequestSorting",
    "pagination": "RequestPagination", "permissions": "RequestPermissions",
    "not_found": "RequestNotFound", "success": "RequestDefault",
    "incorrect_body": "RequestIncorrectBody", "extra_data": "RequestExtraData",
    "cross_field": "RequestCrossField", "update_object": "RequestUpdateObject",
    "lifecycle": "RequestLifecycleDelete",
}
EXPECT_BY_NAME = {
    "not_found": "404", "incorrect_body": "400", "extra_data": "400",
    "cross_field": "400", "permissions": "403", "lifecycle": "409",
}


def executed_tc(e: dict) -> list[str]:
    """Какие проверки оси B реально выполнены для эндпоинта."""
    all_tc = TC_BY_SUBTYPE[e["subtype"]]
    share = COVERAGE_PROFILE[e["service"]]
    # ровное деление выглядит искусственно — трясём в пределах профиля
    share = min(1.0, max(0.0, share + RND.uniform(-0.18, 0.12)))
    n = int(round(len(all_tc) * share))
    if share >= 0.99:
        n = len(all_tc)
    return all_tc[:n]


SPEC_SOURCES = [
    ("docs/specs/10-shop-data-model-v2.docx", "10. Модель данных магазина", "REQ"),
    ("docs/specs/11-orders-and-payments-api.docx", "11. Заказы и оплаты", "API"),
    ("docs/specs/12-warehouse-operations.docx", "12. Складские операции", "WMS"),
]


# ===========================================================================
# 6. Прогон: тесты, шаги, снятые связи
# ===========================================================================
GROUP_OF = {
    "get_list_objects": "list", "get_dynamic_object": "crud",
    "get_static_object": "dict", "get_by_parent": "queries",
    "get_by_self": "scope", "post_create_object": "crud",
    "put_object": "crud", "delete_object": "crud", "action": "actions",
}

# Где болит: у этих сервисов прогон красный. Точечно, а не по всей сцене —
# иначе слой «Прогон» превращается в равномерную заливку.
FAILING = {
    "stock": 0.30, "invoices": 0.22, "campaigns": 0.25, "refunds": 0.30,
    "shipments": 0.12,
}

# Падение обязано сходиться с шагом: если шаг ждал 404, разбор не может
# говорить про 200. Поэтому вариант отказа выбирается один раз и определяет
# и текст, и то, что показано в дереве шагов.
ERROR_KINDS = [
    # ключ, заголовок, разбор, что вернулось вместо ожидаемого
    ("status500", "AssertionError: Status code: expected {expect}, got 500",
     "Expected: {expect}\nActual: 500\nHint: контейнер отвечал ошибкой — "
     "см. вкладку Live, состояние стенда на момент прогона", "500"),
    ("body", "AssertionError: Response body validation failed",
     "Expected: поле «total» присутствует и не пусто\nActual: поля нет\n"
     "Hint: контракт объявляет total обязательным", None),
    ("stale", "AssertionError: Status code: expected {expect}, got 200",
     "Expected: {expect}\nActual: 200\n"
     "Hint: удалённая запись всё ещё отдаётся", "200"),
    ("timeout", "TimeoutError: запрос не уложился в 30 с",
     "Expected: ответ за 30 с\nActual: таймаут\n"
     "Hint: очередь не разбиралась, консьюмер лежал", "—"),
    ("sum", "AssertionError: Sum of lines does not match order total",
     "Expected: 18450.00\nActual: 18300.00\nHint: скидка применена дважды",
     None),
]


class Corpus:
    """Собранный прогон: тесты, вызовы, снятые сборщиками связи."""

    def __init__(self) -> None:
        self.tests: list[dict] = []
        self.ui_links: list[dict] = []
        self.db_effects: list[dict] = []
        self.bus_events: list[dict] = []
        self.clock = RUN_START_MS
        self._seen: set[str] = set()

    # --------------------------------------------------------------- тесты
    def add(self, *, module: str, cls: str, name: str, feature: str,
            story: str, calls: list[tuple[str, str, str]], tags: list[str],
            reqs: list[str], status: str, ms: int, is_ui: bool = False,
            params: list[tuple[str, str]] | None = None) -> dict:
        nodeid = f"{module}::{cls}::{name}" if cls else f"{module}::{name}"
        full = (module[:-3].replace("/", ".") + (f".{cls}" if cls else "")
                + f"#{name}")
        start = self.clock
        # прогон идёт последовательно: следующий кейс стартует после текущего
        self.clock += ms + RND.randint(30, 210)
        rec = {
            "module": module, "cls": cls, "name": name, "nodeid": nodeid,
            "fullName": full, "feature": feature, "story": story,
            "calls": calls, "tags": tags, "reqs": reqs, "status": status,
            "start": start, "ms": ms, "isUi": is_ui,
            "params": params or [],
        }
        assert nodeid not in self._seen, f"дубль nodeid: {nodeid}"
        self._seen.add(nodeid)
        self.tests.append(rec)
        return rec


def path_slug(path: str) -> str:
    """`/api/v1/stock/product/{productId}` → `stock_product_by_id`.

    Имя теста обязано быть уникальным: два кейса с одинаковым nodeid — это
    один кейс в отчёте, и часть прогона молча исчезает.
    """
    parts = []
    for seg in path.replace("/api/v1/", "").split("/"):
        parts.append("by_id" if seg.startswith("{") else seg.replace("-", "_"))
    return "_".join(parts)


def build_corpus() -> Corpus:
    c = Corpus()

    # ---- API: по одному тесту на выполненную проверку оси B --------------
    for e in ENDPOINTS:
        service = e["service"]
        group = GROUP_OF[e["subtype"]]
        module = f"src/api/tests/{service}/test_{service}_{group}.py"
        cls = ("Test" + service.title().replace("-", "")
               + group.title())
        done = EXECUTED[f"{e['method']} {e['path']}"]
        names = [n for n in TEST_NAMES[e["subtype"]]
                 if TC_BY_NAME[n] in done]
        fail_share = FAILING.get(service, 0.02)
        for n in names:
            expect = EXPECT_BY_NAME.get(
                n, {"POST": "201", "DELETE": "204"}.get(e["method"], "200"))
            status = "passed"
            if RND.random() < fail_share:
                status = "failed" if RND.random() > 0.25 else "broken"
            calls = [(e["method"], e["path"], expect)]
            # часть кейсов сначала засевает данные — так появляются честные
            # связи «вызывались в одном тесте»
            seed = SEED_FOR.get(service)
            if seed and n in ("not_found", "success", "update_object",
                              "lifecycle", "cross_field"):
                calls = [seed, (e["method"], e["path"], expect)]
            slug = path_slug(e["path"])
            c.add(module=module, cls=cls,
                  name=f"test_{e['method'].lower()}_{slug}_{n}",
                  feature=f"{e['serviceLabel']} / {GROUP_TITLE[group]}",
                  story=e["title"],
                  calls=calls, tags=[service, GROUP_TITLE[group]],
                  reqs=REQ_FOR_ENDPOINT.get(f"{e['method']} {e['path']}", []),
                  status=status,
                  ms=RND.randint(90, 420))

    # ---- сквозные сценарии ----------------------------------------------
    for key, title, chain, tables, topics in JOURNEYS:
        module = f"src/api/tests/journeys/test_{key}.py"
        cls = "Test" + "".join(p.title() for p in key.split("_"))
        rec = c.add(module=module, cls=cls, name=f"test_{key}",
                    feature="Сквозные сценарии",
                    story=title,
                    calls=[(m, p, {"POST": "201", "DELETE": "204"}.get(m, "200"))
                           for m, p in chain],
                    tags=["journey", key], reqs=JOURNEY_REQS.get(key, []),
                    status="passed" if key != "stock_replenishment" else "failed",
                    ms=RND.randint(4200, 9800))
        for t in tables:
            c.db_effects.append({
                "test": rec["name"], "nodeid": rec["nodeid"], "table": t,
                "insert": RND.randint(1, 4),
                "update": RND.randint(0, 2) if t not in CONSUMER_TABLES else 0,
                "delete": 0, "shared": t in CONSUMER_TABLES})
        for topic in topics:
            c.bus_events.append({
                "test": rec["name"], "nodeid": rec["nodeid"], "topic": topic,
                "events": RND.randint(1, 3), "scope": "test",
                "httpCalls": len(chain), "httpWrites": max(1, len(chain) // 2),
                "lastHttp": round(rec["start"] / 1000 + rec["ms"] / 1000, 2),
                "lastWrite": round(rec["start"] / 1000 + rec["ms"] / 1400, 2),
                "firstTs": round(rec["start"] / 1000 + rec["ms"] / 1200, 2),
                "lastTs": round(rec["start"] / 1000 + rec["ms"] / 1100, 2),
                "afterWrite": 1, "afterHttp": 0,
                "windowSec": round(rec["ms"] / 1000 + 5, 2)})

    # ---- позадачные эффекты для наблюдаемых наборов ----------------------
    watched = ("orders", "payments", "stock", "invoices")
    for rec in list(c.tests):
        if rec["isUi"] or "/journeys/" in rec["module"]:
            continue
        service = rec["module"].split("/")[3]
        if service not in watched or RND.random() > 0.45:
            continue
        for method, path, _ in rec["calls"]:
            call = f"{method} {path}"
            for table in STORAGE_WRITES.get(call, []):
                if table in CONSUMER_TABLES:
                    continue
                c.db_effects.append({
                    "test": rec["name"], "nodeid": rec["nodeid"],
                    "table": table,
                    "insert": 1 if method in ("POST", "PUT") else 0,
                    "update": 1 if method in ("PUT", "POST") else 0,
                    "delete": 1 if method == "DELETE" else 0,
                    "shared": False})
            if method == "GET":
                for table in STORAGE_READS.get(call, [])[:2]:
                    c.db_effects.append({
                        "test": rec["name"], "nodeid": rec["nodeid"],
                        "table": table, "insert": 0, "update": 0,
                        "delete": 0, "shared": False})

    # сессионный срез: прогон в целом задел ещё несколько таблиц
    for table in ("audit_event", "order_status_history", "product_attribute"):
        c.db_effects.append({"test": "", "nodeid": "", "table": table,
                             "scope": "session",
                             "insert": RND.randint(20, 300),
                             "update": RND.randint(0, 40),
                             "delete": 0, "shared": True})

    # ---- UI-прогон и снятые связи «элемент → эндпоинт» -------------------
    by_screen: dict[str, list[tuple]] = {}
    for ctl in CONTROLS:
        by_screen.setdefault(ctl[1], []).append(ctl)
    screen_path = {s[0]: s[2] for s in SCREENS}
    for suite, screen, feature in UI_SUITES:
        module = f"src/ui/tests/{suite}/test_{screen}.py"
        cls = "Test" + "".join(p.title() for p in screen.split("_"))
        controls = by_screen.get(screen, [])
        for i, ctl in enumerate(controls):
            testid, _, label, _, calls, _, _ = ctl
            status = "passed"
            if RND.random() < 0.08:
                status = "failed" if RND.random() > 0.3 else "broken"
            rec = c.add(module=module, cls=cls,
                        name=f"test_{testid.replace('-', '_')}[chromium]",
                        feature=feature, story=label,
                        calls=[(m_p.split(" ")[0], m_p.split(" ", 1)[1],
                                "200") for m_p in calls],
                        tags=["ui", suite], reqs=[], status=status,
                        ms=RND.randint(1800, 7400), is_ui=True,
                        params=[("browser", "chromium")])
            for m_p in calls:
                method, path = m_p.split(" ", 1)
                c.ui_links.append({
                    "screen": screen_path[screen], "testid": testid,
                    "control": label, "action": "click",
                    "method": method, "path": path,
                    "status": int({"POST": 201, "DELETE": 204}.get(method, 200)),
                    "ms": round(RND.uniform(90, 680), 1),
                    "confidence": "direct" if i % 4 else "inferred",
                    "test": rec["name"], "nodeid": rec["nodeid"]})

    # Элементы, которых в ручной карте нет: рекордер увидел их на прогоне.
    # Ради них вкладка «Связи» и показывает, где карта отстала от продукта.
    for screen, control, method, path in OBSERVED_ONLY:
        rec = next((t for t in c.tests
                    if t["isUi"] and screen in t["module"]), None)
        c.ui_links.append({
            "screen": screen_path[screen], "testid": None, "control": control,
            "action": "click", "method": method, "path": path,
            "status": int({"POST": 201, "DELETE": 204}.get(method, 200)),
            "ms": round(RND.uniform(120, 900), 1), "confidence": "inferred",
            "test": rec["name"] if rec else "", "nodeid": rec["nodeid"] if rec else ""})
    return c


GROUP_TITLE = {"list": "список", "crud": "жизненный цикл", "dict": "справочник",
               "queries": "выборки", "scope": "область видимости",
               "actions": "операции"}

# Чем тест засевает данные перед основным вызовом.
SEED_FOR = {
    "orders": ("POST", "/api/v1/orders", "201"),
    "payments": ("POST", "/api/v1/payments", "201"),
    "products": ("POST", "/api/v1/products", "201"),
    "customers": ("POST", "/api/v1/customers", "201"),
    "invoices": ("POST", "/api/v1/invoices", "201"),
    "purchases": ("POST", "/api/v1/purchases", "201"),
    "shipments": ("POST", "/api/v1/shipments", "201"),
    "returns": ("POST", "/api/v1/returns", "201"),
    "campaigns": ("POST", "/api/v1/campaigns", "201"),
    "variants": ("POST", "/api/v1/variants", "201"),
    "carts": ("POST", "/api/v1/carts", "201"),
}

# Таблицы, которые в прогоне пишет не API, а консьюмер шины.
CONSUMER_TABLES = {"notification", "loyalty_transaction", "segment_member",
                   "audit_event"}

JOURNEYS = [
    ("order_to_invoice", "Заказ → оплата → счёт → уведомление",
     [("POST", "/api/v1/customers"), ("POST", "/api/v1/carts"),
      ("POST", "/api/v1/carts/{id}/items"), ("POST", "/api/v1/orders"),
      ("POST", "/api/v1/orders/{id}/confirm"), ("POST", "/api/v1/payments"),
      ("POST", "/api/v1/payments/{id}/capture"), ("GET", "/api/v1/invoices"),
      ("GET", "/api/v1/orders/{id}")],
     ["order_header", "order_line", "payment", "notification"],
     ["order.confirmed", "payment.authorized", "invoice.issued",
      "notification.sent"]),
    ("order_cancellation", "Отмена подтверждённого заказа",
     [("POST", "/api/v1/orders"), ("POST", "/api/v1/orders/{id}/confirm"),
      ("POST", "/api/v1/orders/{id}/cancel"), ("GET", "/api/v1/orders/{id}")],
     ["order_header", "notification"],
     ["order.confirmed", "order.cancelled", "notification.sent"]),
    ("return_and_refund", "Возврат товара и возврат оплаты",
     [("POST", "/api/v1/orders"), ("POST", "/api/v1/returns"),
      ("POST", "/api/v1/returns/{id}/approve"), ("POST", "/api/v1/refunds"),
      ("POST", "/api/v1/refunds/{id}/confirm")],
     ["return_request", "refund", "notification"],
     ["return.requested", "notification.sent"]),
    ("stock_replenishment", "Закупка → приёмка → остатки",
     [("POST", "/api/v1/suppliers"), ("POST", "/api/v1/purchases"),
      ("POST", "/api/v1/purchases/{id}/approve"),
      ("POST", "/api/v1/purchases/{id}/receive"), ("GET", "/api/v1/stock")],
     ["purchase_order", "stock_item"],
     ["stock.replenished", "stock.reserved"]),
    ("shipment_flow", "Отгрузка заказа",
     [("POST", "/api/v1/orders"), ("POST", "/api/v1/shipments"),
      ("POST", "/api/v1/shipments/{id}/dispatch"),
      ("GET", "/api/v1/shipments/order/{orderId}")],
     ["shipment", "shipment_item"],
     ["shipment.created", "stock.reserved"]),
    ("loyalty_award", "Бонусы за оплаченный заказ",
     [("POST", "/api/v1/orders"), ("POST", "/api/v1/orders/{id}/confirm"),
      ("POST", "/api/v1/payments"), ("GET", "/api/v1/loyalty/customer/{customerId}")],
     ["order_header", "loyalty_transaction"],
     ["payment.authorized", "loyalty.points.awarded"]),
    ("campaign_launch", "Кампания по сегменту",
     [("POST", "/api/v1/segments"), ("POST", "/api/v1/segments/{id}/recalculate"),
      ("POST", "/api/v1/campaigns"), ("POST", "/api/v1/campaigns/{id}/launch"),
      ("GET", "/api/v1/campaigns/{id}/targets")],
     ["campaign", "segment_member", "notification"],
     ["campaign.launched", "notification.sent"]),
    ("price_update", "Переоценка каталога",
     [("POST", "/api/v1/products"), ("POST", "/api/v1/prices"),
      ("POST", "/api/v1/prices/bulk"), ("GET", "/api/v1/products/search")],
     ["product", "price_entry", "audit_event"],
     ["notification.sent"]),
]

# Элементы, найденные рекордером, но не описанные в карте.
OBSERVED_ONLY = [
    ("orders_list", "Обновить список", "GET", "/api/v1/orders"),
    ("orders_list", "Строка заказа", "GET", "/api/v1/orders/{id}"),
    ("order_card", "Печать чека", "GET", "/api/v1/payments/{id}/receipt"),
    ("order_card", "История статусов", "GET", "/api/v1/audit/object/{objectId}"),
    ("catalog_products", "Строка товара", "GET", "/api/v1/products/{id}"),
    ("catalog_products", "Массовая переоценка", "POST", "/api/v1/prices/bulk"),
    ("product_card", "Архивировать", "POST", "/api/v1/products/{id}/archive"),
    ("warehouse_stock", "Остатки по складу", "GET",
     "/api/v1/stock/warehouse/{warehouseId}"),
    ("billing_invoices", "Строки счёта", "GET", "/api/v1/invoices/{id}/lines"),
    ("billing_invoices", "Подписки клиента", "GET", "/api/v1/subscriptions"),
    ("crm_customers", "Адреса клиента", "GET", "/api/v1/customers/{id}/addresses"),
    ("crm_customers", "Поиск клиента", "GET", "/api/v1/customers/search"),
    ("crm_campaigns", "Аудитория кампании", "GET", "/api/v1/campaigns/{id}/targets"),
    ("admin_accounts", "Права роли", "GET", "/api/v1/roles/{id}/permissions"),
    ("admin_accounts", "Моя запись", "GET", "/api/v1/accounts/me"),
    ("purchase_order", "Товары поставщика", "GET", "/api/v1/suppliers/{id}/products"),
    ("reports_sales", "Периоды", "GET", "/api/v1/enums/periods"),
]


# ===========================================================================
# 7. Требования аналитики
# ===========================================================================
# (источник, id, раздел, уровень, путь, заголовок, текст, якоря)
def spec_requirements() -> list[dict]:
    out: list[dict] = []

    def req(src: int, rid: str, section: str, level: int, path: list[str],
            title: str, lead: str, api=(), table=(), screen=(), method=()):
        out.append({
            "id": rid, "title": title, "section": section, "level": level,
            "path": list(path), "text": lead, "lead": lead,
            "chars": len(lead) * RND.randint(3, 9),
            "children": RND.randint(0, 6),
            "anchors": {k: list(v) for k, v in
                        (("api", api), ("table", table), ("screen", screen),
                         ("method", method)) if v},
            "source": SPEC_SOURCES[src][1],
        })

    # --- источник 10: модель данных ---------------------------------------
    req(0, "REQ-10", "10", 1, [], "Модель данных магазина",
        "Версия 2.0 (2026-08-14). Статус: к реализации. Документ описывает "
        "сущности витрины, заказов и склада и правила их связи.")
    req(0, "REQ-10.1", "10.1", 2, ["Модель данных магазина"], "Товар и варианты",
        "Товар — карточка в каталоге; вариант — конкретная позиция с "
        "штрихкодом. Удаление товара каскадом убирает медиа и атрибуты, "
        "но не варианты с движением на складе.",
        api=["/api/v1/products", "/api/v1/products/{id}"],
        table=["product", "product_variant", "product_media"],
        screen=["catalog_products", "product_card"])
    req(0, "REQ-10.2", "10.2", 2, ["Модель данных магазина"], "Цены и прайс-листы",
        "Цена хранится в разрезе прайс-листа и товара. Массовая переоценка "
        "создаёт новые записи, старые остаются для истории.",
        api=["/api/v1/prices", "/api/v1/prices/bulk"],
        table=["price_list", "price_entry"], method=["POST"])
    req(0, "REQ-10.3", "10.3", 2, ["Модель данных магазина"], "Категории",
        "Категория образует дерево произвольной глубины. Товар обязан "
        "принадлежать ровно одной категории.",
        api=["/api/v1/categories", "/api/v1/categories/tree"], table=["category"])
    req(0, "REQ-10.4", "10.4", 2, ["Модель данных магазина"], "Корзина",
        "Корзина живёт 30 суток, после чего очищается фоновой задачей. "
        "Оформление корзины создаёт заказ и переводит её в состояние closed.",
        api=["/api/v1/carts", "/api/v1/carts/{id}/checkout"],
        table=["cart", "cart_item"])
    req(0, "REQ-10.5", "10.5", 2, ["Модель данных магазина"], "Клиент и адреса",
        "У клиента может быть несколько адресов доставки; ровно один из них "
        "помечен основным.",
        api=["/api/v1/customers", "/api/v1/customers/{id}/addresses"],
        table=["customer", "customer_address"], screen=["crm_customers"])
    req(0, "REQ-10.6", "10.6", 2, ["Модель данных магазина"], "Сегменты и бонусы",
        "Сегмент пересчитывается по правилам не чаще раза в сутки. Бонусы "
        "начисляются только за оплаченный заказ.",
        api=["/api/v1/segments", "/api/v1/loyalty"],
        table=["customer_segment", "segment_member", "loyalty_account",
               "loyalty_transaction"])
    req(0, "REQ-10.7", "10.7", 2, ["Модель данных магазина"], "Журнал изменений",
        "Каждое изменение прикладного объекта пишется в журнал: тип, "
        "идентификатор, действие, автор и момент.",
        api=["/api/v1/audit", "/api/v1/audit/object/{objectId}"],
        table=["audit_event"])
    req(0, "REQ-10.8", "10.8", 2, ["Модель данных магазина"], "Уведомления",
        "Уведомление создаётся консьюмером по событию шины и хранит шаблон, "
        "канал и состояние отправки.",
        table=["notification", "notification_template"])
    req(0, "REQ-10.9", "10.9", 2, ["Модель данных магазина"],
        "Хранение персональных данных",
        "Адрес и телефон клиента хранятся в отдельной таблице и вычищаются "
        "по запросу на удаление в течение 30 суток.",
        api=["/api/v1/customers/{id}/erase"], table=["customer_address"])

    # --- источник 11: заказы и оплаты --------------------------------------
    req(1, "API-11", "11", 1, [], "Заказы и оплаты",
        "Версия 1.4 (2026-08-28). Описывает жизненный цикл заказа, оплату, "
        "отгрузку и возврат.")
    req(1, "API-11.1", "11.1", 2, ["Заказы и оплаты"], "Жизненный цикл заказа",
        "Заказ проходит состояния draft → confirmed → paid → shipped → done. "
        "Обратные переходы запрещены, кроме отмены до отгрузки.",
        api=["/api/v1/orders", "/api/v1/orders/{id}",
             "/api/v1/orders/{id}/confirm", "/api/v1/orders/{id}/cancel"],
        table=["order_header", "order_status_history"],
        screen=["orders_list", "order_card"])
    req(1, "API-11.2", "11.2", 2, ["Заказы и оплаты"], "Строки заказа",
        "Сумма заказа равна сумме строк с учётом скидок. Расхождение "
        "считается ошибкой данных и блокирует подтверждение.",
        api=["/api/v1/orders/{id}/lines"], table=["order_line"])
    req(1, "API-11.3", "11.3", 2, ["Заказы и оплаты"], "Подтверждение заказа",
        "Подтверждение публикует событие order.confirmed. Повторное "
        "подтверждение возвращает 409.",
        api=["/api/v1/orders/{id}/confirm"], method=["POST"],
        screen=["order_card"])
    req(1, "API-11.4", "11.4", 2, ["Заказы и оплаты"], "Оплата",
        "Оплата запрашивается у внешнего шлюза. Попытки логируются, повтор "
        "возможен трижды, после чего заказ помечается как проблемный.",
        api=["/api/v1/payments", "/api/v1/payments/{id}/capture"],
        table=["payment", "payment_attempt"])
    req(1, "API-11.5", "11.5", 2, ["Заказы и оплаты"], "Чек",
        "Чек доступен только по оплаченному платежу; по неоплаченному "
        "возвращается 404.",
        api=["/api/v1/payments/{id}/receipt"], method=["GET"])
    req(1, "API-11.6", "11.6", 2, ["Заказы и оплаты"], "Счёт",
        "Счёт выписывается консьюмером по событию payment.authorized и "
        "содержит строки с налоговой ставкой.",
        api=["/api/v1/invoices", "/api/v1/invoices/{id}/lines"],
        table=["invoice", "invoice_line", "tax_rate"],
        screen=["billing_invoices"])
    req(1, "API-11.7", "11.7", 2, ["Заказы и оплаты"], "Возврат",
        "Возврат возможен в течение 14 суток после отгрузки. Согласованный "
        "возврат порождает возврат оплаты.",
        api=["/api/v1/returns", "/api/v1/returns/{id}/approve",
             "/api/v1/refunds"],
        table=["return_request", "return_line", "refund"])
    req(1, "API-11.8", "11.8", 2, ["Заказы и оплаты"], "Отгрузка",
        "Отгрузка резервирует остаток и передаёт заказ службе доставки. "
        "Трек-номер приходит асинхронно.",
        api=["/api/v1/shipments", "/api/v1/shipments/{id}/dispatch"],
        table=["shipment", "shipment_item"])
    req(1, "API-11.9", "11.9", 2, ["Заказы и оплаты"], "Реестр и выгрузка",
        "Реестр заказов выгружается в файл; ссылка живёт сутки.",
        api=["/api/v1/orders/export", "/api/v1/orders/report/sales"],
        screen=["reports_sales"])
    req(1, "API-11.10", "11.10", 2, ["Заказы и оплаты"], "Частичная отгрузка",
        "Заказ может быть отгружен частями; каждая часть — отдельная "
        "отгрузка со своим статусом.",
        api=["/api/v1/orders/{id}/split"], method=["POST"])

    # --- источник 12: склад -------------------------------------------------
    req(2, "WMS-12", "12", 1, [], "Складские операции",
        "Версия 1.1 (2026-09-02). Остатки, перемещения, закупки и приёмка.")
    req(2, "WMS-12.1", "12.1", 2, ["Складские операции"], "Остатки",
        "Остаток хранится в разрезе склада и варианта. Резерв не может "
        "превышать доступное количество.",
        api=["/api/v1/stock", "/api/v1/stock/warehouse/{warehouseId}"],
        table=["stock_item", "warehouse"], screen=["warehouse_stock"])
    req(2, "WMS-12.2", "12.2", 2, ["Складские операции"], "Перемещения",
        "Любое изменение остатка оформляется движением с указанием "
        "документа-основания.",
        api=["/api/v1/stock/moves"], table=["stock_move"], method=["POST"])
    req(2, "WMS-12.3", "12.3", 2, ["Складские операции"], "Пересчёт",
        "Пересчёт позиции создаёт инвентаризацию и корректирующее движение.",
        api=["/api/v1/stock/{id}/recount"], table=["inventory_count"])
    req(2, "WMS-12.4", "12.4", 2, ["Складские операции"], "Закупка",
        "Закупка согласуется ответственным, после чего строки закупки "
        "становятся неизменяемыми.",
        api=["/api/v1/purchases", "/api/v1/purchases/{id}/approve"],
        table=["purchase_order", "purchase_order_line"],
        screen=["purchase_order"])
    req(2, "WMS-12.5", "12.5", 2, ["Складские операции"], "Приёмка",
        "Приёмка увеличивает остаток и публикует stock.replenished. "
        "Расхождение с закупкой фиксируется как недопоставка.",
        api=["/api/v1/purchases/{id}/receive"], table=["stock_item"])
    req(2, "WMS-12.6", "12.6", 2, ["Складские операции"], "Поставщики",
        "Поставщик хранит контакт и признак активности; неактивного нельзя "
        "выбрать в новой закупке.",
        api=["/api/v1/suppliers", "/api/v1/suppliers/{id}/products"],
        table=["supplier"])
    req(2, "WMS-12.7", "12.7", 2, ["Складские операции"], "Предупреждения",
        "Позиция ниже минимального остатка попадает в список предупреждений.",
        api=["/api/v1/stock/alerts"], method=["GET"])
    req(2, "WMS-12.8", "12.8", 2, ["Складские операции"], "Склады",
        "Склад имеет код, город и признак активности; отключение склада "
        "запрещено при ненулевом остатке.",
        api=["/api/v1/warehouses", "/api/v1/warehouses/{id}"],
        table=["warehouse"])
    return out


REQ_FOR_ENDPOINT: dict[str, list[str]] = {}
JOURNEY_REQS = {
    "order_to_invoice": ["API-11.1", "API-11.4", "API-11.6"],
    "order_cancellation": ["API-11.1"],
    "return_and_refund": ["API-11.7"],
    "stock_replenishment": ["WMS-12.4", "WMS-12.5"],
    "shipment_flow": ["API-11.8"],
    "loyalty_award": ["REQ-10.6"],
    "campaign_launch": ["REQ-10.6"],
    "price_update": ["REQ-10.2"],
}


def link_reqs_to_endpoints(reqs: list[dict]) -> None:
    """Явные ссылки тестов на требования — самая честная привязка."""
    for r in reqs:
        for path in r["anchors"].get("api", []):
            for e in ENDPOINTS:
                if e["path"] == path:
                    key = f"{e['method']} {e['path']}"
                    REQ_FOR_ENDPOINT.setdefault(key, [])
                    if len(REQ_FOR_ENDPOINT[key]) < 2 and RND.random() < 0.45:
                        REQ_FOR_ENDPOINT[key].append(r["id"])


# ===========================================================================
# 8. Покрытие по методике
# ===========================================================================
EXECUTED: dict[str, list[str]] = {}


def unseen_ratio(endpoints: list[dict]) -> float | None:
    """Доля непосещённых эндпоинтов — или `None`, если мерить было нечего.

    Считается по эндпоинтам прогона, и у прогона без эндпоинтов (упавшая
    фикстура до чтения спецификации, неслитый шард воркера, пустая спецификация)
    знаменателя нет. `0.0` там означало бы «ни один эндпоинт не остался
    непосещённым» — самое успокоительное число в отчёте, выданное прогоном,
    который не увидел ничего, и неотличимое от полного прогона. `None` (в JSON —
    `null`) умеет сказать «не измерялось» и не умеет притвориться замером.

    Так же считает владелец формата — `partest/reports/payload.py`. Ключ при
    этом всегда на месте: `null` — это «не измерялось», а отсутствие ключа
    по-прежнему «артефакт старше поля», и путать их нельзя. Измеренный ноль
    остаётся `0.0`.
    """
    if not endpoints:
        return None
    unseen = sum(1 for r in endpoints if r.get("kind") == "unseen")
    return round(unseen / len(endpoints), 4)


#: Правило среднего, которое пишет partest 2.7.0 (`meta.avgRule`): строки `exception`
#: вне каждого среднего и вне `covered`, но в счётчиках. Набор повторяет его, а не своё.
AVG_RULE = 2


def row_depth(key: str, done: list[str]) -> dict | None:
    """Глубина проверок строки (ось C, partest 2.7.0): из заполненных ячеек — в скольких
    вызов просил проверить статус и в скольких ещё и схему. Нет заполненной ячейки —
    `None`, «не измерено».

    Свой генератор на каждую строку — чтобы глубина не сдвинула значения остального
    набора, который берёт числа из общего `RND`.
    """
    cells = len(done)
    if not cells:
        return None
    r = random.Random(zlib.crc32(("depth " + key).encode("utf-8")))
    status = cells - (r.randint(1, max(1, cells // 3)) if r.random() < 0.3 else 0)
    schema = min(status, int(round(status * r.uniform(0.25, 0.9))))
    return {"cells": cells, "status": status, "schema": schema, "values": None}


def scored_stats(rows: list[dict]) -> dict:
    """Счётчики и среднее набора строк — как `partest.reports.payload.summarize` 2.7.0:
    строки `exception` считаются, но в среднее не входят; нечего усреднять — `None`."""
    exc = sum(1 for r in rows if r["status"] == "exception")
    scored = [r for r in rows if r["status"] != "exception"]
    return {
        "avg": round(sum(r["coverage"] for r in scored) / len(scored), 2) if scored else None,
        "calls": sum(r["calls"] for r in rows),
        "full": sum(1 for r in scored if r["status"] == "full"),
        "partial": sum(1 for r in scored if r["status"] == "partial"),
        "empty": sum(1 for r in scored if r["status"] not in ("full", "partial")),
        "exception": exc, "endpoints": len(rows), "scored": len(scored),
    }


def depth_summary(rows: list[dict]) -> dict | None:
    """`summary.depth` partest 2.7.0: ячейки по всем строкам вне `exception`, доли — от
    измеренных ячеек; ни одной — `None`."""
    cells = status = schema = 0
    for r in rows:
        if r["status"] == "exception" or not r.get("depth"):
            continue
        cells += r["depth"]["cells"]
        status += r["depth"]["status"]
        schema += r["depth"]["schema"]
    if not cells:
        return None
    return {"cells": cells, "status": status, "schema": schema, "values": None,
            "statusPct": round(status / cells * 100.0, 2),
            "schemaPct": round(schema / cells * 100.0, 2)}


def _by_avg(avg: float | None) -> tuple:
    """Порядок строк по среднему, как у partest: большее первым, без среднего — в конце."""
    return (avg is None, -(avg or 0.0))


def build_coverage(corpus: Corpus) -> dict:
    calls_by_ep: dict[str, int] = {}
    for t in corpus.tests:
        for method, path, _ in t["calls"]:
            key = f"{method} {path}"
            calls_by_ep[key] = calls_by_ep.get(key, 0) + RND.randint(1, 4)

    endpoints = []
    for e in ENDPOINTS:
        key = f"{e['method']} {e['path']}"
        all_tc = TC_BY_SUBTYPE[e["subtype"]]
        done = EXECUTED[key]
        missing = [t for t in all_tc if t not in done]
        cov = round(len(done) / len(all_tc) * 100, 2)
        status = "full" if not missing else ("empty" if not done else "partial")
        n = calls_by_ep.get(key, 0)
        avg = round(RND.uniform(78, 240), 1)
        endpoints.append({
            "method": e["method"], "path": e["path"], "service": e["service"],
            "serviceLabel": e["serviceLabel"],
            "subtype": SUBTYPE_LABEL[e["subtype"]], "subtypeKey": e["subtype"],
            # `kind` — не копия `status`. Владелец формата (partest,
            # `reports/analyzer.py::_kind_for`) говорит: нет вызовов в ЭТОМ
            # прогоне — `unseen`, и путать это с `empty` («вызывали, но ни одной
            # обязательной ячейки») нельзя. Набор повторял `status` и тем врал:
            # семь эндпоинтов без единого вызова числились «пустыми», а
            # `unseenRatio` выходил нулём — не измеренным, а полученным из
            # неверной разметки. `status` при этом остаётся прежним: на нём
            # держатся фильтры и раскраска просмотрщика.
            "calls": n, "coverage": cov, "status": status,
            "kind": "unseen" if n == 0 else status,
            "executed": done, "missing": missing,
            "description": e["title"],
            "depth": row_depth(key, done),
            "timing": {"n": n, "msAvg": avg, "msP50": round(avg * 0.96, 1),
                       "msP95": round(avg * 1.35, 1),
                       "msMin": round(avg * 0.6, 1),
                       "msMax": round(avg * (2.2 if n else 1), 1),
                       "byType": {tc: {"n": max(1, n // max(1, len(done))),
                                       "msAvg": round(avg * RND.uniform(.9, 1.2), 1),
                                       "msP95": round(avg * RND.uniform(1.2, 1.6), 1)}
                                  for tc in done}},
            "prev": {"coverage": max(0.0, round(cov - RND.uniform(0, 22), 2)),
                     "msAvg": round(avg * RND.uniform(0.9, 1.25), 1),
                     "deltaCoverage": 0.0, "deltaMs": 0.0},
        })
    for ep in endpoints:
        ep["prev"]["deltaCoverage"] = round(ep["coverage"] - ep["prev"]["coverage"], 2)
        ep["prev"]["deltaMs"] = round(ep["timing"]["msAvg"] - ep["prev"]["msAvg"], 1)

    def agg(rows, key_fn, label_fn):
        groups: dict = {}
        for r in rows:
            groups.setdefault(key_fn(r), (label_fn(r), []))[1].append(r)
        out = []
        for k, (label, items) in groups.items():
            st = scored_stats(items)
            out.append({"key": k, "label": label, "count": st["endpoints"],
                        "scored": st["scored"], "calls": st["calls"], "full": st["full"],
                        "partial": st["partial"], "empty": st["empty"],
                        "exception": st["exception"], "avgCoverage": st["avg"]})
        return sorted(out, key=lambda b: _by_avg(b["avgCoverage"]))

    services = agg(endpoints, lambda r: r["service"], lambda r: r["serviceLabel"])
    for s in services:
        s["totalEndpoints"] = s["count"]
        # правило 2: `exception` — не покрытая работа, `covered` — полные и частичные
        s["covered"] = s["full"] + s["partial"]
    subtypes = agg(endpoints, lambda r: r["subtypeKey"], lambda r: r["subtype"])

    by_method: dict = {}
    for r in endpoints:
        by_method.setdefault(r["method"], []).append(r)
    method_rows = []
    for method, items in sorted(by_method.items()):
        st = scored_stats(items)
        method_rows.append({"method": method, "count": st["endpoints"],
                            "calls": st["calls"], "avgCoverage": st["avg"],
                            "scored": st["scored"], "exception": st["exception"]})

    missing_count: dict = {}
    for r in endpoints:
        for tc in r["missing"]:
            missing_count[tc] = missing_count.get(tc, 0) + 1
    missing_top = sorted(({"tc": k, "count": v} for k, v in missing_count.items()),
                         key=lambda x: -x["count"])

    cells: dict = {}
    for r in endpoints:
        box = cells.setdefault((r["service"], r["subtypeKey"]),
                               {"service": r["service"],
                                "subtype": r["subtypeKey"], "values": [],
                                "count": 0})
        if r["status"] != "exception":
            box["values"].append(r["coverage"])
        box["count"] += 1
    cell_rows = []
    for box in cells.values():
        vals = box["values"]
        cell_rows.append({"service": box["service"], "subtype": box["subtype"],
                          "avg": round(sum(vals) / len(vals), 2) if vals else None,
                          "count": box["count"]})

    total_calls = sum(r["calls"] for r in endpoints)
    # Как `summarize()` partest 2.7.0: среднее — по строкам вне `exception`, нечего
    # усреднять — `None`. Набор обязан повторять поведение владельца формата, а не своё.
    totals = scored_stats(endpoints)
    avg_cov = totals["avg"]
    full, partial, empty = totals["full"], totals["partial"], totals["empty"]
    ms_n = sum(r["timing"]["n"] for r in endpoints)

    by_type: dict = {}
    for r in endpoints:
        for tc, box in r["timing"]["byType"].items():
            acc = by_type.setdefault(tc, {"type": tc, "n": 0, "sum": 0.0})
            acc["n"] += box["n"]
            acc["sum"] += box["msAvg"] * box["n"]
    timing_by_type = []
    for acc in by_type.values():
        avg = round(acc["sum"] / max(1, acc["n"]), 1)
        timing_by_type.append({"type": acc["type"], "n": acc["n"], "msAvg": avg,
                               "msP50": round(avg * 0.97, 1),
                               "msP95": round(avg * 1.4, 1),
                               "msMin": round(avg * 0.55, 1),
                               "msMax": round(avg * 2.6, 1)})
    timing_by_type.sort(key=lambda x: x["type"])

    by_sub: dict = {}
    for r in endpoints:
        acc = by_sub.setdefault(r["subtype"], {"subtype": r["subtype"], "n": 0,
                                               "sum": 0.0})
        acc["n"] += r["timing"]["n"]
        acc["sum"] += r["timing"]["msAvg"] * r["timing"]["n"]
    timing_by_subtype = []
    for acc in by_sub.values():
        avg = round(acc["sum"] / max(1, acc["n"]), 1)
        timing_by_subtype.append({"subtype": acc["subtype"], "n": acc["n"],
                                  "msAvg": avg, "msP50": round(avg * 0.96, 1),
                                  "msP95": round(avg * 1.38, 1),
                                  "msMin": round(avg * 0.58, 1),
                                  "msMax": round(avg * 2.4, 1)})
    timing_by_subtype.sort(key=lambda x: x["subtype"])

    ms_avg = round(sum(r["timing"]["msAvg"] * r["timing"]["n"]
                       for r in endpoints) / max(1, ms_n), 1)
    generated = RUN_START.strftime("%Y-%m-%d %H:%M")
    return {
        "meta": {"generated": generated, "engine": "partest",
                 "title": "Сеть N — покрытие API по методике",
                 "defaultExcluded": [], "workers": 1, "merged": False,
                 "partialRun": False, "callsTotal": total_calls,
                 "unseenRatio": unseen_ratio(endpoints),
                 # правило среднего (partest 2.7.0): тренд сравнивает средние только
                 # в пределах одного правила
                 "avgRule": AVG_RULE, "timingSamples": ms_n,
                 "timingUnmatched": RND.randint(40, 260),
                 # partest ≥ 2.5.0: opt-in-набор `slow` снят — прогон полный, но без
                 # медленных тестов. `deselected` выбора — те же снятые тесты: partest
                 # проводит их через `pytest_deselected`, а `partialRun` на них не смотрит
                 "selection": {"deselected": OPTIN_SLOW_OFF["slow"]["deselected"],
                               "optin": OPTIN_SLOW_OFF}},
        "timing": {"n": total_calls, "msAvg": ms_avg,
                   "msP50": round(ms_avg * 0.95, 2),
                   "msP95": round(ms_avg * 1.42, 2),
                   "msMax": round(ms_avg * 9.1, 2),
                   "byType": {r["type"]: {"n": r["n"], "msAvg": r["msAvg"],
                                          "msP95": r["msP95"]}
                              for r in timing_by_type}},
        "summary": {"avg": avg_cov, "calls": total_calls, "full": full,
                    "partial": partial, "empty": empty,
                    "exception": totals["exception"], "endpoints": len(endpoints),
                    "scored": totals["scored"], "depth": depth_summary(endpoints),
                    "avgAll": avg_cov,
                    "msAvg": ms_avg, "msP50": round(ms_avg * 0.95, 1),
                    "msP95": round(ms_avg * 1.42, 1),
                    "msMax": round(ms_avg * 9.1, 1), "msN": ms_n},
        "services": services,
        "subtypes": subtypes,
        "methods": method_rows,
        "missingTop": missing_top,
        "heatmap": {
            "services": [{"key": s["key"], "label": s["label"]}
                         for s in sorted(services, key=lambda x: x["label"])],
            "subtypes": [{"key": s["key"], "label": s["label"]}
                         for s in sorted(subtypes, key=lambda x: x["label"])],
            "cells": cell_rows,
        },
        "endpoints": endpoints,
        "timingBySubtype": timing_by_subtype,
        "timingByType": timing_by_type,
        "runs": {
            "current": {"generated": generated, "avg": avg_cov,
                        "calls": total_calls, "full": full, "partial": partial,
                        "empty": empty, "msAvg": ms_avg,
                        "msP95": round(ms_avg * 1.42, 1), "msN": ms_n,
                        "endpoints": len(endpoints)},
            "previous": {"generated": (RUN_START - timedelta(days=1)).strftime(
                             "%Y-%m-%d %H:%M"),
                         "avg": round(avg_cov - 6.4, 2), "calls":
                             int(total_calls * 0.82), "full": full - 9,
                         "partial": partial + 4, "empty": empty + 5,
                         "msAvg": round(ms_avg * 1.08, 1),
                         "msP95": round(ms_avg * 1.5, 1),
                         "msN": int(ms_n * 0.82), "endpoints": len(endpoints)},
            "deltaCoverage": 6.4, "deltaMs": round(-ms_avg * 0.08, 1),
        },
    }


# ===========================================================================
# 9. allure-results
# ===========================================================================
def uid(seed: str) -> str:
    """Устойчивый «uuid» из строки: одинаковый набор при каждом запуске."""
    r = random.Random(zlib.crc32(seed.encode("utf-8")))
    return "-".join("".join(r.choice("0123456789abcdef") for _ in range(n))
                    for n in (8, 4, 4, 4, 12))


def write_allure(corpus: Corpus) -> int:
    out = ROOT / "allure-results"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rnd = random.Random(4242)
    written = 0
    for t in corpus.tests:
        steps = []
        cursor = t["start"]
        fail_at = rnd.randrange(len(t["calls"])) if t["status"] != "passed" else -1
        kind = ("", "", "", None)
        if fail_at >= 0:
            expect_at_fail = t["calls"][fail_at][2]
            choices = [k for k in ERROR_KINDS
                       if k[0] != "stale" or expect_at_fail in ("404", "409")]
            kind = choices[rnd.randrange(len(choices))]
        attachments = []
        for i, (method, path, expect) in enumerate(t["calls"]):
            bad = (i == fail_at)
            dur = max(20, int(t["ms"] / max(1, len(t["calls"]))))
            got = kind[3] if bad else expect
            status_failed = bad and got is not None
            body_failed = bad and got is None
            checks = [
                {"name": (f"Status code: expected {expect}, got {got}"
                          if got is not None else
                          f"Status code: expected {expect}, got {expect}"),
                 "status": "failed" if status_failed else "passed",
                 "start": cursor + dur - 2, "stop": cursor + dur - 1},
                {"name": "Response body validation",
                 "status": ("failed" if body_failed else
                            ("skipped" if status_failed else "passed")),
                 "start": cursor + dur - 1, "stop": cursor + dur},
            ]
            step_atts = []
            if t["status"] != "passed" or rnd.random() < 0.08:
                for akind in ("request", "response"):
                    src = uid(f"{t['nodeid']}{i}{akind}") + "-attachment.json"
                    body = {"method": method, "url":
                            f"https://shop-api.example.com{path}",
                            "headers": {"Content-Type": "application/json",
                                        "Authorization": "Bearer <redacted>"},
                            "body": {"demo": True}} if akind == "request" else {
                            "status": (int(got) if bad and got not in (None, "—")
                                       else int(expect)),
                            "elapsedMs": dur,
                            "body": ({"message": "Internal Server Error"}
                                     if status_failed
                                     else {"id": rnd.randint(100, 9999)})}
                    (out / src).write_text(
                        json.dumps(body, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="\n")
                    step_atts.append({"name": akind, "source": src,
                                      "type": "application/json"})
                    written += 1
            steps.append({
                "name": f"HTTP {method} {path.replace('{id}', '4211')} "
                        f"→ expect status={expect}",
                "status": "failed" if bad else "passed",
                "steps": [{"name": "Validate response",
                           "status": "failed" if bad else "passed",
                           "steps": checks,
                           "start": cursor, "stop": cursor + dur}],
                "attachments": step_atts,
                "start": cursor, "stop": cursor + dur})
            cursor += dur
        labels = [
            {"name": "epic", "value": "Сеть N — витрина" if not t["isUi"]
             else "Сеть N — интерфейс"},
            {"name": "feature", "value": t["feature"]},
            {"name": "story", "value": t["story"]},
            {"name": "parentSuite", "value": t["module"][:-3].replace("/", ".")},
            {"name": "suite", "value": Path(t["module"]).stem},
            {"name": "subSuite", "value": t["cls"]},
            {"name": "framework", "value": "pytest"},
            {"name": "language", "value": "cpython3"},
            {"name": "host", "value": "demo-runner"},
            {"name": "thread", "value": f"{rnd.randint(1000, 9999)}-MainThread"},
            {"name": "package", "value": t["module"][:-3].replace("/", ".")},
        ] + [{"name": "tag", "value": tg} for tg in t["tags"]] \
          + [{"name": "tag", "value": r} for r in t["reqs"]]

        rec = {
            "name": t["story"] if t["isUi"] else t["name"],
            "status": t["status"],
            "steps": steps,
            "attachments": attachments,
            "parameters": [{"name": k, "value": v} for k, v in t["params"]],
            "start": t["start"], "stop": t["start"] + t["ms"],
            "uuid": uid(t["nodeid"]),
            "historyId": uid("h" + t["nodeid"]).replace("-", ""),
            "testCaseId": uid("c" + t["nodeid"]).replace("-", ""),
            "fullName": t["fullName"],
            "labels": labels,
        }
        if t["reqs"]:
            rec["links"] = [{"name": r, "url": f"spec://{r}", "type": "link"}
                            for r in t["reqs"]]
        if t["status"] != "passed":
            head = kind[1].format(expect=t["calls"][fail_at][2])
            body = kind[2].format(expect=t["calls"][fail_at][2])
            trace = ("Traceback (most recent call last):\n"
                     f"  File \"{t['module']}\", line {rnd.randint(20, 240)},"
                     f" in {t['name'].split('[')[0]}\n"
                     "    assert_response(response, expected)\n"
                     f"{head}")
            rec["statusDetails"] = {"message": f"{head}\n{body}", "trace": trace}
            src = uid("log" + t["nodeid"]) + "-attachment.txt"
            (out / src).write_text(
                "\n".join([f"[{i:03d}] шаг прогона: проверка {t['name']}"
                           for i in range(12)] + [head]), encoding="utf-8", newline="\n")
            attachments.append({"name": "log", "source": src,
                                "type": "text/plain"})
            written += 1
        (out / f"{rec['uuid']}-result.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        written += 1
    return written


# ===========================================================================
# 10. Логи, контейнеры, история прогонов, МРы
# ===========================================================================
LOG_CLASSES = {
    "storefront-api": ["com.example.shop.orders.service.OrderService",
                       "com.example.shop.catalog.service.ProductService",
                       "c.e.s.p.PaymentController"],
    "checkout-worker": ["com.example.shop.checkout.PaymentAuthorizationService",
                        "c.e.s.c.OrderConfirmedListener"],
    "warehouse-worker": ["com.example.shop.wms.StockMoveService",
                         "c.e.s.w.ShipmentCreatedListener"],
    "billing-worker": ["com.example.shop.billing.InvoiceService",
                       "c.e.s.b.PaymentAuthorizedListener"],
    "crm-worker": ["com.example.shop.crm.SegmentRecalculationService"],
    "notifier": ["com.example.shop.notify.NotificationDispatcher"],
}
LOG_LISTENERS = {
    "checkout-worker": ["OrderConfirmedListener"],
    "warehouse-worker": ["ShipmentCreatedListener", "StockReplenishedConsumer"],
    "billing-worker": ["PaymentAuthorizedListener", "ReturnRequestedListener"],
    "crm-worker": ["CampaignLaunchedListener"],
    "notifier": ["InvoiceIssuedListener", "LoyaltyAwardedConsumer"],
}
LOG_TOPICS = {
    "storefront-api": ["order.confirmed", "order.cancelled", "shipment.created",
                       "return.requested"],
    "checkout-worker": ["order.confirmed", "payment.authorized", "payment.failed"],
    "warehouse-worker": ["shipment.created", "stock.reserved",
                         "stock.replenished"],
    "billing-worker": ["payment.authorized", "invoice.issued",
                       "return.requested"],
    "crm-worker": ["campaign.launched", "loyalty.points.awarded"],
    "notifier": ["invoice.issued", "campaign.launched", "notification.sent",
                 "loyalty.points.awarded"],
}
CONTAINER_STATE = {"crm-worker": "restarting", "delivery-provider": "exited"}


def write_container_logs() -> None:
    rnd = random.Random(77)
    entries = []
    log_dir = ROOT / "logs" / "containers"
    if log_dir.exists():
        shutil.rmtree(log_dir)
    log_dir.mkdir(parents=True)
    tables_by_owner: dict[str, list[str]] = {}
    for name, (owner, _, _) in TABLES.items():
        tables_by_owner.setdefault(owner, []).append(name)
    for key, label, kind, tier, cname, note in CONTAINERS:
        if kind in ("bus", "store") and key != "broker":
            continue
        state = CONTAINER_STATE.get(key, "running")
        hits = {}
        tables = tables_by_owner.get(key, [])[:8]
        if tables:
            hits["table"] = {t: rnd.randint(3, 240) for t in tables}
        if LOG_TOPICS.get(key):
            hits["topic"] = {t: rnd.randint(12, 430) for t in LOG_TOPICS[key]}
        if LOG_LISTENERS.get(key):
            hits["listener"] = {t: rnd.randint(9, 310)
                                for t in LOG_LISTENERS[key]}
        if LOG_CLASSES.get(key):
            hits["class"] = {t: rnd.randint(15, 620) for t in LOG_CLASSES[key]}
        entries.append({
            "container": key, "name": cname, "ok": state == "running",
            "state": state,
            "status": {"running": "Up 4 days", "restarting":
                       "Restarting (1) 12 seconds ago",
                       "exited": "Exited (137) 6 hours ago"}[state],
            "image": f"registry.example.com/shop/{key}:2026.09.3",
            "uptime": "4 days" if state == "running" else "",
            "hits": hits,
        })
        # хвост лога рядом: вкладка Live показывает его по клику
        lines = []
        stamp = RUN_START
        for i in range(90):
            stamp = stamp + timedelta(seconds=rnd.randint(1, 40))
            cls = (LOG_CLASSES.get(key) or ["com.example.shop.App"])[
                i % len(LOG_CLASSES.get(key) or [1])]
            if state != "running" and i > 70:
                lines.append(f"{stamp:%Y-%m-%d %H:%M:%S} ERROR [{key}] {cls} "
                             f": Connection refused, retrying in 5s")
                continue
            topic = (LOG_TOPICS.get(key) or ["order.confirmed"])[
                i % len(LOG_TOPICS.get(key) or [1])]
            table = tables[i % len(tables)] if tables else "order_header"
            choice = i % 5
            if choice == 0:
                lines.append(f"{stamp:%Y-%m-%d %H:%M:%S} INFO  [{key}] {cls} "
                             f": received event {topic}, offset {1000 + i}")
            elif choice == 1:
                lines.append(f"{stamp:%Y-%m-%d %H:%M:%S} DEBUG [{key}] {cls} "
                             f": persisted 1 row into table \"{table}\"")
            elif choice == 2:
                lines.append(f"{stamp:%Y-%m-%d %H:%M:%S} INFO  [{key}] {cls} "
                             f": published {topic} in {rnd.randint(3, 90)} ms")
            elif choice == 3 and rnd.random() < 0.25:
                lines.append(f"{stamp:%Y-%m-%d %H:%M:%S} WARN  [{key}] {cls} "
                             f": slow query on table \"{table}\" "
                             f"({rnd.randint(900, 4200)} ms)")
            else:
                lines.append(f"{stamp:%Y-%m-%d %H:%M:%S} INFO  [{key}] {cls} "
                             f": health check ok")
        (log_dir / f"{key}.log").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8", newline="\n")
    # Артефакт агента — только находки, по имени контейнера в среде запуска. Журнал
    # перезапускающегося или упавшего контейнера источник не отдал: его находок нет.
    write_artifact("containers.logs", atlas_container_logs.build(
        {e["name"]: e["hits"] for e in entries if e["ok"]}))
    # Состояние контейнеров — снимок для вкладки «Live», к агенту отношения не имеет.
    write_live("containers.json", {
        "scannedAt": int(RUN_START.timestamp()) + 900,
        "containers": [{k: e[k] for k in ("container", "name", "state", "status",
                                          "image", "uptime")} for e in entries],
    })


#: opt-in-набор демонстрации (`partest_optin_markers = slow:RUN_SLOW`): в последнем
#: прогоне снят, в ночном — включён переменной. Так видно, что тренд их не смешивает.
OPTIN_SLOW_OFF = {"slow": {"state": "off", "env": "RUN_SLOW", "deselected": 14}}
OPTIN_SLOW_ENV = {"slow": {"state": "env", "env": "RUN_SLOW", "deselected": 0}}


def nightly_run(coverage: dict, started: datetime) -> dict:
    """Ночной полный прогон с `RUN_SLOW=1`: медленные тесты в нём были.

    Свой генератор случайных чисел — чтобы добавленный прогон не сдвинул значения
    остальных и разница в git осталась в этой записи.
    """
    rnd = random.Random(13)
    summary = coverage["summary"]
    night = {**coverage,
             "meta": {**coverage["meta"], "selection": {"deselected": 0,
                                                        "optin": OPTIN_SLOW_ENV}},
             "summary": {**summary, "avg": round(summary["avg"] + 1.9, 2)}}
    total = 585 + OPTIN_SLOW_OFF["slow"]["deselected"]
    failed = rnd.randint(1, 5)
    wall = rnd.randint(1500, 2400) * 1000
    return {
        "id": uid("run-nightly").replace("-", "")[:12],
        "suite": "full", "label": "Всё", "status": "done",
        "startedAt": started.timestamp(),
        "finishedAt": started.timestamp() + wall / 1000,
        "wallMs": wall, "total": total, "done": total,
        "passed": total - failed, "failed": failed, "broken": 0,
        "skipped": rnd.randint(0, 4), "current": "", "failures": [], "touched": [],
        "context": {}, "suiteFull": True,
        # Медленные тесты добирают по ячейке у нескольких эндпоинтов — среднее выше.
        # Срез снимает тот же код, что у сервера, с отчёта этого прогона.
        "coverage": snapshot_of(night, suite_full=True),
        "containers": {"total": 11, "down": 0, "restarting": 0, "bad": []},
    }


def write_runs(coverage: dict, corpus: Corpus) -> None:
    rnd = random.Random(9)
    runs = [nightly_run(coverage, RUN_START - timedelta(hours=18))]
    # Наборы раннера, кроме полного, — выбор по путям: медленных тестов в них нет, и
    # запись opt-in у их прогонов набор не показывает (сервер записал бы её, если бы
    # она была в отчёте прогона).
    subset = {**coverage, "meta": {k: v for k, v in coverage["meta"].items()
                                   if k != "selection"}}
    # Последний прогон — полный набор: тренд среднего покрытия строится только по
    # таким, и в истории демонстрации он должен быть.
    suites = [("smoke", "Дымовой"), ("api", "API целиком"),
              ("contract", "Контракт OpenAPI"), ("journeys",
                                                 "Пользовательские сценарии"),
              ("ui", "UI целиком"), ("full", "Всё")]
    started = RUN_START - timedelta(hours=9)
    for i, (suite, label) in enumerate(suites):
        started = started + timedelta(minutes=rnd.randint(45, 95))
        total = {"smoke": 46, "api": 512, "contract": 38, "journeys": 8,
                 "ui": 55, "full": 585}[suite]
        failed = rnd.randint(0, 6) if suite != "journeys" else 1
        wall = rnd.randint(90, 1500) * 1000
        nodeids = [t["nodeid"] for t in corpus.tests
                   if suite == "ui" and t["isUi"] or suite != "ui"][:total]
        runs.append({
            "id": uid(f"run{i}").replace("-", "")[:12],
            "suite": suite, "label": label, "status": "done",
            "startedAt": started.timestamp(),
            "finishedAt": started.timestamp() + wall / 1000,
            "wallMs": wall, "total": total, "done": total,
            "passed": total - failed, "failed": failed, "broken": 0,
            "skipped": rnd.randint(0, 4),
            "current": nodeids[-1] if nodeids else "",
            "failures": [t["nodeid"] for t in corpus.tests
                         if t["status"] != "passed"][:failed],
            "touched": nodeids[:120],
            "context": {"mr": {"iid": 118 + i}} if i in (1, 3, 5) else {},
            "suiteFull": suite == "full",
            # Срез снимает код сервера (`veha.runner.snapshot_of` — сводку берёт
            # `partest_atlas.collect.history.coverage_snapshot`): набор, не объявленный
            # полным, — частичный прогон без среднего и без долей глубины.
            "coverage": (snapshot_of(coverage, suite_full=True) if suite == "full"
                         else snapshot_of(subset, suite_full=False)),
            "containers": {"total": 11, "down": 1 if i > 2 else 0,
                           "restarting": 1 if i > 3 else 0,
                           "bad": ["sandbox-delivery"] if i > 2 else []},
        })
        if i in (1, 3, 5):
            runs[-1]["mr"] = {"iid": 118 + i,
                              "title": MR_TITLES[i % len(MR_TITLES)][1],
                              "branch": MR_TITLES[i % len(MR_TITLES)][2],
                              "url": "", "state": "merged"}
    write_artifact("runner.history", {"runs": runs})


MR_TITLES = [
    (118, "Заказ: запрет обратного перехода статуса", "feature/order-status-guard"),
    (119, "Склад: резерв не больше доступного остатка", "fix/stock-reserve-limit"),
    (120, "Счета: строки счёта с налоговой ставкой", "feature/invoice-tax-lines"),
    (121, "Каталог: массовая переоценка", "feature/bulk-price-update"),
    (122, "CRM: пересчёт сегмента по расписанию", "feature/segment-schedule"),
    (123, "Возвраты: согласование возврата оплаты", "feature/refund-approval"),
    (124, "Отгрузка: трек-номер приходит асинхронно", "fix/shipment-tracking"),
    (125, "Права: роль «кладовщик»", "feature/role-storekeeper"),
    (126, "Отчёты: выгрузка реестра заказов", "feature/orders-export"),
    (127, "Платежи: третья попытка авторизации", "fix/payment-retry"),
]


def write_mr_checks() -> None:
    rnd = random.Random(5)
    mrs, checks = [], {}
    for i, (iid, title, branch) in enumerate(MR_TITLES):
        state = "merged" if i > 2 else "opened"
        day = (RUN_START - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S+03:00")
        mrs.append({"iid": iid, "title": title, "state": state,
                    "author": ["dev-1", "dev-2", "qa-1",
                               "dev-3"][i % 4],
                    "url": "", "branch": branch,
                    "updated": day, "merged": day if state == "merged" else None})
        if state == "merged" and rnd.random() < 0.6:
            checks[str(iid)] = {
                "status": rnd.choice(["checked", "todo"]),
                "note": "прогон «API целиком»: 506 прошло",
                "at": (RUN_START - timedelta(days=i)).timestamp()}
    # Два файла, у каждого один писатель: список — снимок адаптера, отметки — сервер.
    write_live("mrs.json", {"mrs": mrs,
                            "project": "shop/storefront (демонстрационный набор)"})
    write_live("mr_checks.json", {"checks": checks})


def write_logs(corpus: Corpus) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    lines = [f"# прогон демонстрационного набора, {RUN_START:%Y-%m-%d %H:%M}",
             "platform win32 -- Python 3.11.9, pytest-8.3.2",
             f"collected {len(corpus.tests)} items", ""]
    for t in corpus.tests[:400]:
        mark = {"passed": "PASSED", "failed": "FAILED",
                "broken": "ERROR"}[t["status"]]
        lines.append(f"{t['nodeid']} {mark}")
    bad = [t for t in corpus.tests if t["status"] != "passed"]
    lines += ["", "=" * 30 + " short test summary info " + "=" * 30]
    for t in bad[:40]:
        lines.append(f"FAILED {t['nodeid']}")
    lines.append(f"{len(bad)} failed, {len(corpus.tests) - len(bad)} passed")
    (ROOT / "logs" / "pytest.log").write_text("\n".join(lines) + "\n",
                                              encoding="utf-8", newline="\n")
    collect = [f"{RUN_START:%Y-%m-%d %H:%M:%S} сборщик схемы: таблиц "
               f"{len(TABLES)}",
               f"{RUN_START:%Y-%m-%d %H:%M:%S} сборщик контракта: операций "
               f"{len(ENDPOINTS)}",
               f"{RUN_START:%Y-%m-%d %H:%M:%S} сборщик UI: связей "
               f"{len(corpus.ui_links)}",
               f"{RUN_START:%Y-%m-%d %H:%M:%S} сборщик шины: событий "
               f"{len(corpus.bus_events)}",
               f"{RUN_START:%Y-%m-%d %H:%M:%S} готово"]
    (ROOT / "logs" / "collect.log").write_text("\n".join(collect) + "\n",
                                               encoding="utf-8", newline="\n")


# ===========================================================================
# 11. Экраны
# ===========================================================================
EXTRA_SCENES = [
    ("orders_create_modal", "Заказы — модалка создания", "/#/orders",
     "orders_list"),
    ("catalog_filters_open", "Каталог — панель фильтров", "/#/catalog/products",
     "catalog_products"),
    ("order_confirm_modal", "Заказ — подтверждение", "/#/orders", "order_card"),
    ("warehouse_move_modal", "Склад — перемещение", "/#/warehouse/stock",
     "warehouse_stock"),
    ("crm_segment_panel", "CRM — панель сегмента", "/#/crm/customers",
     "crm_customers"),
]


def write_screens() -> None:
    out = ROOT / "screens"
    out.mkdir(parents=True, exist_ok=True)
    # Каталог не стирается целиком: PNG с теми же пикселями остаются как были (см.
    # `Canvas.png`). Лишние файлы прошлой генерации удаляются поштучно.
    keep = {f"{s[0]}.png" for s in SCREENS} | {f"{s[0]}.png" for s in EXTRA_SCENES} \
        | {"manifest.json"}
    for f in out.iterdir():
        if f.is_file() and f.name not in keep:
            f.unlink()
    scenes = []
    frames = {s[0]: s[4] for s in SCREENS}
    for key, label, path, _, frame in SCREENS:
        draw_screen(out / f"{key}.png", key.replace("_", " "), frame)
        scenes.append({"id": key, "title": label, "path": path,
                       "file": f"{key}.png", "full_page": True,
                       "optional": False})
    for key, label, path, base in EXTRA_SCENES:
        frame = list(frames[base])
        frame = frame + [dict(kind="panel", x=28, y=26, w=44, h=48,
                              label="Модальное окно")]
        draw_screen(out / f"{key}.png", key.replace("_", " "), frame)
        scenes.append({"id": key, "title": label, "path": path,
                       "file": f"{key}.png", "full_page": False,
                       "optional": True})
    (out / "manifest.json").write_text(json.dumps({
        "captured_at": RUN_START.isoformat(),
        "frontend_url": "https://shop.example.com",
        "viewport": {"width": 1280, "height": 800},
        "role": "demo",
        "scenes": scenes,
        "skipped": [],
    }, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


# ===========================================================================
# 12. Записи в форме агента
# ===========================================================================
def raw_screen(path: str) -> str:
    """Экран так, как его видит рекордер агента: `location.hash || location.pathname`.

    То есть `#/orders/42`, а не `/#/orders/{orderId}`: без ведущего `/` и с настоящим
    идентификатором. Сервер обязан привести это к виду манифеста сам.
    """
    p = path[1:] if path.startswith("/#") else path
    return re.sub(r"\{[^}]*\}", lambda m: str(40 + len(m.group(0))), p)


def ui_link(link: dict) -> dict:
    """Связь UI → API так, как её отдаёт `ui_flow.attribute` агента.

    Элемент назван своим `data-testid`; видимый текст агент не хранит. Достоверность —
    `direct` или `window`: «выведено по окну» у агента и есть `window`.
    """
    return {"screen": raw_screen(link["screen"]), "control": link.get("testid"),
            "action": link.get("action") or "click", "method": link["method"],
            "path": link["path"], "status": link.get("status"),
            "confidence": "direct" if link.get("confidence") == "direct" else "window"}


def db_record(eff: dict) -> dict:
    """Дельта таблицы так, как её пишет `db_effects.diff` агента."""
    return {"test": eff.get("test", ""), "nodeid": eff.get("nodeid", ""),
            "table": eff["table"], "insert": eff.get("insert", 0),
            "update": eff.get("update", 0), "delete": eff.get("delete", 0),
            "shared": bool(eff.get("shared")), "scope": eff.get("scope") or "test"}


def bus_record(ev: dict) -> dict:
    """События топика так, как их пишет `bus_flow.diff_offsets` агента."""
    return {"test": ev.get("test", ""), "nodeid": ev.get("nodeid", ""),
            "topic": ev["topic"], "events": ev.get("events", 0),
            "scope": ev.get("scope") or "test",
            "afterWrite": bool(ev.get("afterWrite")),
            "http": {"requests": ev.get("httpCalls", 0), "writes": ev.get("httpWrites", 0)}}


# ===========================================================================
# 13. Сборка набора
# ===========================================================================
def main() -> None:
    # Срез истории пишет код агента, и до 0.2.1 он не переносил `exception`, `scored`
    # и `depth`: набор, собранный старым агентом, вышел бы другим. Набор один — и
    # собирается одним агентом.
    if "scored" not in atlas_history.COVERAGE_KEYS:
        raise SystemExit("демонстрация собирается partest-atlas ≥ 0.2.1: срез истории "
                         "переносит exception, scored и depth только с этой версии")
    check_model()
    ROOT.mkdir(parents=True, exist_ok=True)
    # Артефакты прежней раскладки (плоский `atlas/`, схема 1) удаляются: сервер их
    # не читает, а лежащие рядом они путали бы, какой набор настоящий.
    for stale in (ROOT / "atlas", ROOT / "live"):
        if stale.exists():
            shutil.rmtree(stale)

    for e in ENDPOINTS:
        EXECUTED[f"{e['method']} {e['path']}"] = executed_tc(e)

    reqs = spec_requirements()
    link_reqs_to_endpoints(reqs)

    corpus = build_corpus()
    coverage = build_coverage(corpus)

    write_atlas_yaml()
    write_app_map()
    write_openapi()
    write_db_schema()
    write_artifact("ui.flow", atlas_ui_flow.build([ui_link(x) for x in corpus.ui_links]))
    effects = [db_record(x) for x in corpus.db_effects]
    # Агент пишет nodeid pytest целиком, с параметрами; Allure в `fullName` их не
    # пишет. Одна запись в наборе — в настоящей форме, чтобы сервер это соединял.
    first = next(e for e in effects if e["nodeid"] and e["scope"] == "test"
                 and "[" not in e["nodeid"])
    first["nodeid"] += "[case-1]"
    write_artifact("db.effects", {"records": effects})
    write_artifact("bus.flow", {"records": [bus_record(x) for x in corpus.bus_events]})
    write_artifact("specs.requirements", atlas_specs.build([
        atlas_specs.Requirement(
            id=r["id"], title=r["title"], section=r["section"], level=r["level"],
            text=r.get("text", ""), lead=r.get("lead", ""), source=r["source"],
            path=list(r.get("path") or []), children=r.get("children", 0),
            anchors=dict(r.get("anchors") or {}))
        for r in reqs]))
    write_container_logs()
    write_runs(coverage, corpus)
    write_mr_checks()
    write_logs(corpus)
    write_screens()
    (ROOT / "coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    files = write_allure(corpus)

    print(f"эндпоинтов {len(ENDPOINTS)} · таблиц {len(TABLES)} · "
          f"топиков {len(TOPICS)} · экранов {len(SCREENS) + len(EXTRA_SCENES)}")
    print(f"тестов {len(corpus.tests)} · файлов allure {files} · "
          f"связей UI {len(corpus.ui_links)} · эффектов БД "
          f"{len(corpus.db_effects)} · событий шины {len(corpus.bus_events)}")
    print(f"покрытие: среднее {coverage['summary']['avg']}%, "
          f"полных {coverage['summary']['full']}, "
          f"частичных {coverage['summary']['partial']}, "
          f"пустых {coverage['summary']['empty']}")


if __name__ == "__main__":
    main()
