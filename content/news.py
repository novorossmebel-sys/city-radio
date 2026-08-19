"""Модуль новостей: источник → верификация/рерайт через YandexGPT → черновик на модерацию.

Пайплайн (см. sources.yaml и docs/plan_city_radio.md §4, §8):
  1. fetch()             — забрать сырой текст с источника
  2. verify_and_rewrite() — YandexGPT проверяет и переписывает, не меняя сути
  3. Draft                — уходит владельцу на согласование (публикация — отдельный шаг,
                             сюда пока не входит: очередь модерации в engine.py ещё не построена)

Почему YandexGPT, а не Claude: Anthropic не обслуживает Россию (юрлицо/ИП, зарегистрированное
в России, подпадает под ограничение по владению из Supported Regions Policy — см. обсуждение
в чате 2026-08-14). YandexGPT — российский сервис, без этой проблемы.

Реализованные адаптеры источников:
  - RSS (см. RssSource — используется для проверки механики пайплайна на реальном тексте;
    ни одна из 20 согласованных рубрик RSS не использует).
  - Telegram (см. TelegramChannelSource) — через Telethon (MTProto), отдельным
    аккаунтом-наблюдателем, залогиненным заранее через telethon_login.py. Bot API для этого
    не годится: боту отдаются посты только тех каналов, где он сам администратор, а
    novosti_efir/chpnvrsk_official — чужие каналы.
  - VK (см. VkGroupSource) — wall.get открытого паблика по VK_API_TOKEN. На 2026-08-15
    ни одна согласованная рубрика VK не использует (все перешли на Telegram, см. sources.yaml) —
    адаптер оставлен готовым на случай, если VK-источник понадобится снова.
  - Погода и море (см. YandexPogodaSource) — температура воды, УФ-индекс, магнитные бури,
    пыльца, фаза Луны, восход/закат/световой день — всё одним запросом со страницы
    Яндекс.Погоды (статический HTML, без JS/API-ключа).
  - Афиша/события (см. AfishaRuSource) — концерты/театр/стендап/вечеринки/дети с afisha.ru
    (статический HTML, в отличие от afisha.yandex.ru не требует JS-рендеринга). Кино на этой
    же странице — витрина проката без реальных сеансов, туда не годится.
Кино (сеансы) — не реализовано, нужен источник с реальным расписанием по кинотеатрам
(kinomonitor.ru — есть в sources.yaml, но точная ссылка на новороссийский кинотеатр сети
не подтверждена).
Вызов при отсутствии адаптера — NotImplementedError с этим же объяснением.
"""
import base64
import html
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import feedparser
import requests

from config import settings
from content.base import Draft
from content.images import normalize_photo
from content.llm import call_yandexgpt, extract_json

SYSTEM_PROMPT = """Ты — редактор городского новостного канала «City Radio» (Новороссийск).
Тебе присылают сырой текст поста из источника и рубрику, к которой он относится.

Твоя задача — переписать текст своими словами для мессенджер-канала, СТРОГО соблюдая:
1. Не менять суть и факты. Никаких деталей, которых нет в оригинале — не выдумывай и не додумывай.
2. Не копируй дословно — перескажи ПОЛНОСТЬЮ, своими словами, ничего не пропуская и не сокращая
   по смыслу. Заголовок короткий, тело — полный пересказ (не ужимай искусственно до нескольких
   предложений, если в оригинале материала больше), стиль мессенджера.
3. Если рубрика про происшествия/ЧП/ЖКХ и в тексте нет ясного подтверждения (только слухи, один
   неофициальный источник, домыслы) — не сглаживай это, а укажи в concerns и поставь needs_manual_review=true.
4. Если рубрика про здоровье — только общие сведения, без советов лечения/диагнозов; если текст
   не является общим сведением, а похож на медицинский совет — отметь в concerns.
5. Если оригинал непонятен, обрублен или, похоже, вырван из контекста — needs_manual_review=true.
6. HTML-разметка Telegram допустима в body: <b>жирный</b>, <i>курсив</i> — больше ничего.
7. Каналы-источники часто публикуют рекламу вперемешку с обычным контентом. Определи,
   является ли этот конкретный пост рекламой/промо (продажа товара или услуги, промокод,
   призыв "закажи сейчас"/"успей купить", спонсорская вставка, реферальная ссылка на бота
   и т.п.) — поставь is_advertisement=true. Не путай с новостью О рекламе/бизнесе — важен
   именно призыв читателя что-то купить/заказать/перейти по ссылке для покупки.

Ответ — ТОЛЬКО валидный JSON, без markdown-обёртки (без ```), без текста до/после, строго по схеме:
{"title": "...", "body": "...", "concerns": ["..."], "needs_manual_review": true, "is_advertisement": false}
где concerns — пустой массив [], если замечаний нет."""

GENERATION_SYSTEM_PROMPT = """Ты — редактор городского новостного канала «City Radio» (Новороссийск).
У этой рубрики нет источника — контент придумывается моделью с нуля по заданию, а не
пересказывается из чужого текста.

Требования:
1. Пиши по-русски, живым языком мессенджер-канала, без канцелярита и вступлений вроде
   "Сегодня мы хотим рассказать".
2. Не выдавай выдумку за проверенный факт с научной серьёзностью — если по заданию это
   несерьёзный/развлекательный жанр (гороскоп и т.п.), тон должен явно быть шуточным,
   чтобы читатель не принял это за настоящее предсказание.
3. Заголовок короткий, тело — 2-5 предложений, если в задании не сказано иное.
4. HTML-разметка Telegram допустима в body: <b>жирный</b>, <i>курсив</i> — больше ничего.

Ответ — ТОЛЬКО валидный JSON, без markdown-обёртки (без ```), без текста до/после, строго по схеме:
{"title": "...", "body": "..."}"""

# Официальные каналы экстренных сообщений (@nvrskadm, @chpnvrsk_official) почти никогда не
# приходят с полезной картинкой от самой администрации — вместо этого владелец подготовил
# готовые иллюстрации под конкретный тип опасности (2026-08-17, папка "фото опасность"/
# инструкция.txt) и попросил подставлять их вместо/при отсутствии фото из первоисточника.
# Привязано к каналу-источнику, а не к рубрике — эти каналы используются сразу в нескольких
# рубриках (Норд-ост и погода, Происшествия/ЧП, Вода-свет-ЖКХ), см. sources.yaml.
DANGER_SOURCE_CHANNELS = {"nvrskadm", "chpnvrsk_official"}
DANGER_PHOTOS_DIR = Path("assets/danger_photos")
DANGER_PHOTO_RULES = [
    ("bek.jpg", ("бэк", "безэкипажн")),
    ("pozhar.jpg", ("пожар",)),
    ("veter.jpg", ("ветер", "порывист")),
    ("shtorm.jpg", ("гроза", "подтоплен", "паводок", "ливен", "шторм")),
]

# У БПЛА-опасности, в отличие от остальных категорий, есть картинки не только на
# Новороссийск, но и на соседние города и на "весь край" (2026-08-17) — выбираем по тому,
# сколько муниципальных образований названо в тексте: одно из наших — его картинка, больше
# одного (типовая рассылка сразу по нескольким районам) — общая картинка по краю.
BPLA_KRAI_DISTRICTS = ("абинск", "анапа", "геленджик", "крымск", "новороссийск", "темрюк")
BPLA_CITY_PHOTOS = {
    "новороссийск": "bpla.jpg",
    "анапа": "bpla_anapa.jpg",
    "геленджик": "bpla_gelendzhik.jpg",
}
BPLA_KRAI_PHOTO = "bpla_krai.jpg"


def _bpla_photo_for(text: str) -> list:
    lowered = text.lower()
    matched = [d for d in BPLA_KRAI_DISTRICTS if d in lowered]
    if len(matched) == 1 and matched[0] in BPLA_CITY_PHOTOS:
        filename = BPLA_CITY_PHOTOS[matched[0]]
    elif matched:
        filename = BPLA_KRAI_PHOTO  # несколько районов сразу (или район без своей картинки)
    else:
        filename = BPLA_CITY_PHOTOS["новороссийск"]  # город явно не назван — берём наш по умолчанию
    path = DANGER_PHOTOS_DIR / filename
    return [str(path)] if path.exists() else []


def _danger_photo_for(text: str) -> list:
    lowered = text.lower()
    if any(kw in lowered for kw in ("бпла", "беспилот")):
        return _bpla_photo_for(text)
    for filename, keywords in DANGER_PHOTO_RULES:
        if any(kw in lowered for kw in keywords):
            path = DANGER_PHOTOS_DIR / filename
            if path.exists():
                return [str(path)]
    return []


# Источники экстренных сообщений (см. DANGER_SOURCE_CHANNELS) в конце поста часто добавляют
# рекламу собственного канала — в наших постах ей не место (владелец, 2026-08-17): "ссылки на
# канал в самом тексте... их обязательно нужно удалять". Вырезаем такие строки на входе,
# до любого рерайта — это защищает и LLM-путь, и запасной путь без рерайта (см. ниже).
FOOTER_LINE_MARKERS = (
    "подпишитесь", "подписаться", "наш канал", "связаться с админом", "заказать рекламу",
)


def _strip_channel_footer(text: str) -> str:
    kept = [
        line for line in text.split("\n")
        if not any(marker in line.lower() for marker in FOOTER_LINE_MARKERS)
    ]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return cleaned.strip()


# Рубрика "Погода и море" — сезонная иллюстрация: у YandexPogodaSource своих фото нет
# (это скрейпинг цифр, не постов), а владелец подготовил по одной картинке на сезон
# (2026-08-17, папка "погода"/задача.txt) — выбор по месяцу публикации.
SEASON_RUBRIC = "Погода и море"
SEASON_PHOTOS_DIR = Path("assets/season_photos")
SEASON_PHOTO_BY_MONTH = {
    12: "zima.png", 1: "zima.png", 2: "zima.png",
    3: "vesna.png", 4: "vesna.png", 5: "vesna.png",
    6: "leto.png", 7: "leto.png", 8: "leto.png",
    9: "osen.png", 10: "osen.png", 11: "osen.png",
}


def _season_photo_for(month: int) -> list:
    filename = SEASON_PHOTO_BY_MONTH.get(month)
    if not filename:
        return []
    path = SEASON_PHOTOS_DIR / filename
    return [str(path)] if path.exists() else []


@dataclass
class RawItem:
    title: str
    text: str
    url: str
    source_label: str  # короткое имя источника для подписи "Источник: ..." (не сырой текст поста)
    image_paths: list = field(default_factory=list)  # локальные пути к скачанным фото поста


class RssSource:
    """Читает RSS-ленту. Рабочий адаптер — проверено на novorab.ru/feed/."""

    def __init__(self, name: str, feed_url: str):
        self.name = name
        self.feed_url = feed_url

    def fetch(self, limit: int = 5) -> list:
        parsed = feedparser.parse(self.feed_url)
        items = []
        for entry in parsed.entries[:limit]:
            text = getattr(entry, "summary", "") or getattr(entry, "title", "")
            items.append(RawItem(title=entry.title, text=text, url=entry.link, source_label=self.name))
        return items


MEDIA_CACHE_DIR = Path("media_cache")  # скачанные фото источников, gitignored, чистится после публикации
MAX_ALBUM_PHOTOS = 10  # ограничение самого Telegram на размер media group


class TelegramChannelSource:
    """Читает публичный Telegram-канал через Telethon (MTProto).

    В отличие от Bot API не требует, чтобы наш бот был администратором канала — логинимся
    обычным аккаунтом-наблюдателем (см. telethon_login.py) и читаем историю канала так же,
    как её видел бы обычный подписчик.

    Скачивает фото поста (в т.ч. альбомы — несколько фото под одной подписью, у Telethon
    это несколько сообщений с одинаковым grouped_id). Видео сознательно не скачивается —
    см. обсуждение 2026-08-17: файлы намного тяжелее фото (лимит Bot API на загрузку —
    50 МБ), скачивание через Telethon кратно медленнее и чувствительнее к уже привычным
    здесь сетевым таймаутам; ни один из текущих источников не заточен под видео-контент.
    """

    def __init__(self, channel_username: str):
        self.channel_username = channel_username.lstrip("@")

    def _download_photos(self, client, messages: list) -> list:
        paths = []
        for msg in messages[:MAX_ALBUM_PHOTOS]:
            if not msg.photo:
                continue
            MEDIA_CACHE_DIR.mkdir(exist_ok=True)
            dest = MEDIA_CACHE_DIR / f"{uuid.uuid4().hex}.jpg"
            try:
                client.download_media(msg, file=str(dest))
                normalize_photo(str(dest))
                paths.append(str(dest))
            except Exception as e:
                print(f"[news] не смог скачать фото {msg.id} из @{self.channel_username}: {e}")
        return paths

    def fetch(self, limit: int = 5) -> list:
        from telethon.sync import TelegramClient

        if not settings.TELETHON_API_ID or not settings.TELETHON_API_HASH:
            raise RuntimeError("TELETHON_API_ID / TELETHON_API_HASH не заданы в .env")

        items = []
        with TelegramClient(
            "telethon_observer", int(settings.TELETHON_API_ID), settings.TELETHON_API_HASH
        ) as client:
            # берём с запасом: альбом — это несколько "сырых" сообщений на один логический пост
            raw = list(client.iter_messages(self.channel_username, limit=max(limit * 4, 20)))

            group = []
            current_gid = object()  # заведомо не равен ничьему grouped_id на первом шаге

            def flush(group_msgs):
                if not group_msgs or len(items) >= limit:
                    return
                text = next((m.message.strip() for m in group_msgs if m.message and m.message.strip()), "")
                text = _strip_channel_footer(text)
                if not text:
                    return
                title = text.split("\n", 1)[0][:120]
                anchor = group_msgs[0]
                url = f"https://t.me/{self.channel_username}/{anchor.id}"
                image_paths = self._download_photos(client, group_msgs)
                items.append(RawItem(
                    title=title, text=text, url=url,
                    source_label=f"@{self.channel_username}", image_paths=image_paths,
                ))

            for msg in raw:
                gid = msg.grouped_id
                if gid is not None and gid == current_gid:
                    group.append(msg)
                    continue
                flush(group)
                if len(items) >= limit:
                    return items
                group = [msg]
                current_gid = gid
            flush(group)

        return items


class VkGroupSource:
    """Читает стену открытого VK-паблика (wall.get) — Палата №6, «Я люблю Новороссийск» и т.п."""

    API_VERSION = "5.199"

    def __init__(self, group_screen_name: str):
        self.group_screen_name = group_screen_name.lstrip("@")

    def fetch(self, limit: int = 5) -> list:
        if not settings.VK_API_TOKEN:
            raise RuntimeError("VK_API_TOKEN не задан в .env")

        params = {
            "domain": self.group_screen_name,
            "count": limit,
            "access_token": settings.VK_API_TOKEN,
            "v": self.API_VERSION,
        }
        r = requests.get("https://api.vk.com/method/wall.get", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"VK API вернул ошибку: {data['error']}")

        items = []
        for post in data["response"]["items"]:
            text = (post.get("text") or "").strip()
            if not text:
                continue
            title = text.split("\n", 1)[0][:120]
            url = f"https://vk.com/wall{post['owner_id']}_{post['id']}"
            items.append(RawItem(title=title, text=text, url=url, source_label=self.group_screen_name))
        return items


class YandexPogodaSource:
    """Читает погоду + море одним запросом со страницы Яндекс.Погоды (раздел watersports):
    температура воды, УФ-индекс (пик дня), магнитные бури, пыльца, фаза Луны, восход/закат,
    длина светового дня. По желанию владельца (2026-08-17) — не отдельно вода и отдельно
    погода, а один совмещённый показатель.

    Всё перечисленное есть в сыром HTML именно этой страницы (server-side рендеринг,
    Next.js RSC-поток) — не нужны отдельные подстраницы (uv-index, magnetic-storms, moon...)
    и не нужен JS/headless-браузер, достаточно обычного GET с браузерным User-Agent (без
    него Яндекс может отдать урезанную/иную вёрстку).
    """

    WATER_TEMP_RE = re.compile(r"waterspotTemperature.{0,400}?([+-]?\d+)°")
    AIR_TEMP_RE = re.compile(r"днём: ([+-]?\d+)°")
    UV_BLOCK_RE = re.compile(r"AppDetailedForecastBlock_block__title__\w+\">УФ-индекс.*?</section>", re.DOTALL)
    UV_VALUE_RE = re.compile(r"itemContent__\w+\"><span>(\d+)</span>")
    TABLE_ROW_RE = re.compile(
        r'AppDetailsCard_table__key__\w+">([^<]+)</span><span class="AppDetailsCard_table__dot__\w+"></span>'
        r'<span class="AppDetailsCard_table__value__\w+">([^<]+)</span>'
    )
    CARD_RE = re.compile(r'aria-label="([^"]{5,120})" class="AppWellBeingCard_content__\w+')
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    def __init__(self, url: str = "https://yandex.ru/pogoda/ru/novorossiysk/details/watersports"):
        self.url = url

    def fetch(self, limit: int = 1) -> list:
        r = requests.get(self.url, headers={"User-Agent": self.USER_AGENT}, timeout=30)
        r.raise_for_status()
        html = r.text

        water_match = self.WATER_TEMP_RE.search(html)
        water_temp = water_match.group(1) if water_match else None

        # "днём" — дневное значение из блока "Воздух" (там же утро/вечер/ночь), берём его
        # как основную температуру воздуха дня, а не текущий час
        air_match = self.AIR_TEMP_RE.search(html)
        air_temp = air_match.group(1) if air_match else None

        uv_peak = None
        uv_block = self.UV_BLOCK_RE.search(html)
        if uv_block:
            values = [int(v) for v in self.UV_VALUE_RE.findall(uv_block.group(0))]
            if values:
                uv_peak = max(values)  # пик УФ за день, а не текущий час — проще и надёжнее парсить

        table = dict(self.TABLE_ROW_RE.findall(html))
        sunrise = table.get("Восход")
        sunset = table.get("Закат")
        daylight = table.get("Световой день")

        # карточки самочувствия вида "Пыльца в Новороссийске: сорняки, полынь и ещё 1" —
        # ключ содержит "в <город>", поэтому сверяем по подстроке, а не по точному ключу
        cards = {}
        for label in self.CARD_RE.findall(html):
            if ":" not in label:
                continue
            key, value = label.split(":", 1)
            key, value = key.strip(), value.strip()
            if "Магнитные бури" in key:
                cards["Магнитные бури"] = value
            elif "Пыльца" in key:
                cards["Пыльца"] = value
            elif "Луны" in key:
                cards["Фазы Луны"] = value

        lines = []
        if air_temp:
            lines.append(f"Температура воздуха: {air_temp}°C")
        if water_temp:
            lines.append(f"Температура воды в море: {water_temp}°C")
        if uv_peak is not None:
            lines.append(f"УФ-индекс (пик дня): {uv_peak}")
        if "Магнитные бури" in cards:
            lines.append(f"Магнитные бури: {cards['Магнитные бури']}")
        if "Пыльца" in cards:
            lines.append(f"Пыльца: {cards['Пыльца']}")
        if "Фазы Луны" in cards:
            lines.append(f"Луна: {cards['Фазы Луны']}")
        if sunrise:
            lines.append(f"Восход: {sunrise}")
        if sunset:
            lines.append(f"Закат: {sunset}")
        if daylight:
            lines.append(f"Световой день: {daylight}")

        if not lines:
            raise RuntimeError(
                "не нашёл ни одного показателя на странице Яндекс.Погоды — вёрстка могла измениться"
            )

        if air_temp and water_temp:
            title = f"Погода и море: воздух {air_temp}°C, вода {water_temp}°C"
        elif water_temp:
            title = f"Погода и море: вода {water_temp}°C"
        else:
            title = "Погода и море сегодня"

        # разделитель вместо текстовой шапки "Погода и море в Новороссийске сегодня:" —
        # она дублировала рубрику и жирный заголовок над ней в готовом посте (2026-08-17)
        SEPARATOR = "════════════════"
        text = SEPARATOR + "\n" + "\n".join(lines)
        return [RawItem(title=title, text=text, url=self.url, source_label="Яндекс.Погода")]


class AfishaRuSource:
    """Читает афишу событий (концерты/театр/стендап/вечеринки/дети) для города с afisha.ru.

    В отличие от afisha.yandex.ru (см. обсуждение 2026-08-17) — статический HTML,
    события с датами есть прямо в сыром ответе, JS/headless-браузер не нужен.

    Раздел "кино" на этой же странице (category == "фильм") — это витрина фильмов в
    прокате (жанр + синопсис), БЕЗ реального времени сеансов по конкретным кинотеатрам —
    не годится для рубрики "Кино (сеансы)" (там нужно расписание, не список новинок),
    поэтому такие карточки сюда сознательно не попадают.

    Вёрстка сайта содержит служебные NUL-байты внутри слов (похоже на защиту от
    примитивного парсинга/копирования) — вырезаем их при очистке текста.
    """

    ITEM_TITLE_RE = re.compile(
        r'aria-label="([^"]+)"[^>]*data-test="ITEM"><a class="CjnHd" data-test="LINK" href="([^"]+)"'
    )
    ITEM_DETAIL_RE = re.compile(
        r'<div class="TmmXT">([^<]+)</div><div class="VeVyd"[^>]*>([^<]+)</div>'
        r'<div class="gVGDC">([^<]+)</div>'
    )
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    def __init__(self, url: str = "https://www.afisha.ru/novorossijsk/"):
        self.url = url.rstrip("/") + "/"

    @staticmethod
    def _clean(text: str) -> str:
        return html.unescape(text.replace("\x00", "")).strip()

    def fetch(self, limit: int = 8) -> list:
        r = requests.get(self.url, headers={"User-Agent": self.USER_AGENT}, timeout=30)
        r.raise_for_status()
        html = r.text

        titles_links = self.ITEM_TITLE_RE.findall(html)
        details = self.ITEM_DETAIL_RE.findall(html)

        items = []
        for (title, href), (category, _title_dup, date_venue) in zip(titles_links, details):
            category = self._clean(category)
            if category == "фильм":
                continue  # витрина проката, не событие с датой/местом — см. докстринг

            title = self._clean(title)
            date_venue = self._clean(date_venue)
            url = href if href.startswith("http") else "https://www.afisha.ru" + href

            text = f"{category.capitalize()}: {title}\n{date_venue}"
            items.append(RawItem(title=title, text=text, url=url, source_label="afisha.ru"))
            if len(items) >= limit:
                break

        return items


def _unimplemented_source(source_type: str) -> None:
    needs = {
        "web": "адаптер под конкретный сайт (структура страницы у каждого источника своя)",
        "manual": "источник помечен как ручной в sources.yaml — автоматического чтения не будет",
    }
    raise NotImplementedError(
        f"Адаптер источника '{source_type}' не реализован. Нужно: {needs.get(source_type, '?')}"
    )


YANDEX_ART_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
YANDEX_ART_OPERATION_URL = "https://llm.api.cloud.yandex.net/operations/{}"


SCENE_DESCRIPTION_SYSTEM = """Ты помогаешь составить короткое описание сцены для нейросети,
рисующей иллюстрацию к новости. Правила:
1. НЕ называй и не описывай внешность конкретных реальных людей (актёров, политиков,
   знаменитостей, местных жителей) — вместо этого опиши место действия, предметы,
   атмосферу, тему новости без привязки к конкретному лицу.
2. Если в новости в принципе есть люди — либо не включай людей в сцену вовсе, либо
   только обобщённо: силуэты, толпа издалека, человек со спины.
3. Ответ — только сама фраза-описание сцены (на русском), без пояснений и кавычек,
   не длиннее 200 символов."""


def _build_scene_description(rubric: str, title: str, body: str) -> str:
    """Просит YandexGPT (гораздо послушнее YandexART к запретам) переформулировать тему
    поста в безопасное описание сцены без реальных лиц — прямая инструкция внутри самого
    промта YandexART оказалась ненадёжной (владелец, 2026-08-18): при явном упоминании
    известных людей (актёров и т.п.) модель всё равно рисовала крупные портретные лица,
    даже с отрицательным весом на "close-up face, portrait" в отдельном сообщении."""
    try:
        raw = call_yandexgpt(
            SCENE_DESCRIPTION_SYSTEM,
            f"Рубрика: {rubric}\nЗаголовок: {title}\nТекст: {body[:400]}",
        )
        return raw.strip()[:200] or title
    except Exception:
        return title  # LLM недоступен — используем хотя бы заголовок как тему


def generate_illustration(rubric: str, title: str, body: str = "") -> str | None:
    """Фоллбэк-иллюстрация через YandexART, когда у поста нет вообще никакого фото —
    владелец попросил (2026-08-18), чтобы каждый пост выходил с фото/видео: без этого
    текстовые посты растягиваются на всю ширину чата и ломают единообразную ленту
    (см. content/images.py). Возвращает путь к файлу в media_cache или None при сбое —
    вызывающий код должен просто оставить пост без фото, а не падать.

    Промт для YandexART строится не по сырому тексту поста, а по безопасному описанию
    сцены от YandexGPT (см. _build_scene_description) — так надёжнее исключаются
    выдуманные лица реальных людей, чем прямым запретом внутри промта картинки.

    YandexART жёстко ограничивает промт 500 символами (иначе 400 Bad Request) — отсюда
    короткая служебная часть и финальная обрезка про запас."""
    if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {settings.YANDEX_API_KEY}",
    }
    scene = _build_scene_description(rubric, title, body)
    # инструкция про лица остаётся и здесь — защита на случай, если YandexGPT всё же
    # пропустит имя в описании сцены
    constraint = (
        "Иллюстрация БЕЗ портретов и крупных лиц людей — покажи тему через место, "
        "предметы и символы; если люди есть в кадре — только со спины, силуэтом или "
        "далеко на фоне, лица неразличимы. "
    )
    suffix = f" Рубрика «{rubric}», Новороссийск. Фотореалистично, без текста на изображении."
    prompt = f"{constraint}Сцена: {scene}{suffix}"
    if len(prompt) > 500:
        prompt = prompt[:497] + "..."
    payload = {
        "modelUri": f"art://{settings.YANDEX_FOLDER_ID}/yandex-art/latest",
        "generationOptions": {
            "seed": random.randint(0, 2**31 - 1),
            "aspectRatio": {"widthRatio": 4, "heightRatio": 5},
        },
        "messages": [{"weight": 1, "text": prompt}],
    }
    try:
        r = requests.post(YANDEX_ART_URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        op_id = r.json()["id"]

        op_url = YANDEX_ART_OPERATION_URL.format(op_id)
        for _ in range(20):  # генерация обычно укладывается в 10-30с
            time.sleep(3)
            op = requests.get(op_url, headers=headers, timeout=30).json()
            if op.get("done"):
                if "error" in op:
                    print(f"[news] YandexART вернул ошибку: {op['error']}")
                    return None
                MEDIA_CACHE_DIR.mkdir(exist_ok=True)
                dest = MEDIA_CACHE_DIR / f"{uuid.uuid4().hex}.jpg"
                dest.write_bytes(base64.b64decode(op["response"]["image"]))
                normalize_photo(str(dest))
                return str(dest)
        print("[news] YandexART не успел сгенерировать за отведённое время")
        return None
    except requests.RequestException as e:
        print(f"[news] сетевая ошибка YandexART: {e}")
        return None


def _ensure_image(rubric: str, title: str, body: str, image_paths: list) -> list:
    if image_paths:
        return image_paths
    generated = generate_illustration(rubric, title, body)
    return [generated] if generated else []


def verify_and_rewrite(rubric: str, item: RawItem) -> Draft | None:
    """Прогоняет сырой текст через YandexGPT: проверка + рерайт без искажения смысла.

    Модель иногда отказывается отвечать (фильтры безопасности YandexGPT) вместо JSON —
    в этом случае одна "плохая" новость не должна ронять весь прогон: она просто
    уходит на ручную проверку как есть, с текстом отказа в concerns.

    Возвращает None, если пост определён как реклама (см. SYSTEM_PROMPT, п.7) — владелец
    явно попросил (2026-08-17), чтобы такие посты вообще не доходили до модерации, а не
    просто помечались; вызывающий код должен пробовать следующий источник по кругу.
    """
    user_content = (
        f"Рубрика: {rubric}\n"
        f"Заголовок источника: {item.title}\n"
        f"Текст источника:\n{item.text}"
    )

    raw = call_yandexgpt(SYSTEM_PROMPT, user_content)

    image_paths = item.image_paths
    if item.source_label.lstrip("@") in DANGER_SOURCE_CHANNELS:
        matched = _danger_photo_for(f"{item.title} {item.text}")
        if matched:
            image_paths = matched  # готовая иллюстрация опасности заменяет фото источника

    try:
        data = extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        return Draft(
            rubric=rubric,
            source_name=item.source_label,
            source_url=item.url,
            original_title=item.title,
            original_text=item.text,
            title=item.title,
            body=item.text,
            concerns=[f"модель не вернула рерайт (возможно, отказ фильтра): {raw[:300]}"],
            needs_manual_review=True,
            image_paths=_ensure_image(rubric, item.title, item.text, image_paths),
        )

    if data.get("is_advertisement", False):
        return None

    return Draft(
        rubric=rubric,
        source_name=item.source_label,
        source_url=item.url,
        original_title=item.title,
        original_text=item.text,
        title=data["title"],
        body=data["body"],
        concerns=data.get("concerns", []),
        needs_manual_review=data.get("needs_manual_review", False),
        image_paths=_ensure_image(rubric, data["title"], data["body"], image_paths),
    )


def generate_draft(rubric: str, task: str) -> Draft:
    """Черновик без источника — контент придумывается моделью с нуля по заданию (см.
    sources.yaml, source_type: manual с пометкой "генерация, не агрегация": Путешествия,
    Гороскоп). source_url пустой — Draft.to_post() сам не покажет строку "Источник",
    даже если владелец включит переключатель (см. content/base.py)."""
    raw = call_yandexgpt(GENERATION_SYSTEM_PROMPT, f"Рубрика: {rubric}\nЗадание: {task}")

    try:
        data = extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        return Draft(
            rubric=rubric,
            source_name="YandexGPT (генерация)",
            source_url="",
            original_title="",
            original_text=task,
            title=rubric,
            body=raw,
            concerns=[f"модель не вернула JSON (возможно, отказ фильтра): {raw[:300]}"],
            needs_manual_review=True,
            image_paths=_ensure_image(rubric, rubric, raw, []),
        )

    return Draft(
        rubric=rubric,
        source_name="YandexGPT (генерация)",
        source_url="",
        original_title="",
        original_text=task,
        title=data["title"],
        body=data["body"],
        image_paths=_ensure_image(rubric, data["title"], data["body"], []),
    )


def build_structured_draft(rubric: str, item: RawItem) -> Draft:
    """Черновик без LLM-рерайта — для источников со структурированными данными
    (конкретные показатели, не текст новости), где нечего проверять на выдумки, а
    сведение построчных данных в один абзац только портит читаемость. Каждая строка
    item.text остаётся отдельной строкой поста."""
    image_paths = item.image_paths
    if rubric == SEASON_RUBRIC:
        image_paths = _season_photo_for(datetime.now().month) or image_paths
    image_paths = _ensure_image(rubric, item.title, item.text, image_paths)

    return Draft(
        rubric=rubric,
        source_name=item.source_label,
        source_url=item.url,
        original_title=item.title,
        original_text=item.text,
        title=item.title,
        body=item.text,
        image_paths=image_paths,
    )


def _print_draft(draft: Draft) -> None:
    print("=" * 70)
    print(f"[{draft.rubric}] {draft.source_url}")
    print("-- ОРИГИНАЛ " + "-" * 40)
    print(draft.original_title)
    print(draft.original_text[:500])
    print("-- ЧЕРНОВИК " + "-" * 40)
    print(f"<b>{draft.title}</b>")
    print(draft.body)
    if draft.concerns:
        print(f"⚠ Замечания: {'; '.join(draft.concerns)}")
    print(f"Нужна ручная проверка: {'да' if draft.needs_manual_review else 'нет'}")


def run_rss_test(feed_url: str, rubric: str = "Тест механики (RSS)", limit: int = 2) -> None:
    """Локальный прогон пайплайна на реальном RSS-источнике.

    Ни одна из согласованных рубрик не использует RSS (см. sources.yaml) — это тест
    механики fetch → verify_and_rewrite на живом тексте, не привязанный к конкретной рубрике.
    """
    if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
        print("[news] YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы в .env — рерайт недоступен.")
        return

    source = RssSource(name="novorab.ru", feed_url=feed_url)
    items = source.fetch(limit=limit)

    if not items:
        print(f"[news] источник {feed_url} не отдал ни одной записи")
        return

    for item in items:
        draft = verify_and_rewrite(rubric, item)
        if draft is None:
            print(f"[news] пропущено (реклама): {item.title}")
            continue
        _print_draft(draft)


def run_rss_test_to_moderation(feed_url: str, rubric: str = "Тест механики (RSS)", limit: int = 1) -> None:
    """Как run_rss_test, но черновики уходят боту-модератору в Telegram, а не в консоль."""
    if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
        print("[news] YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы в .env — рерайт недоступен.")
        return

    from moderation import ModerationBot  # локальный импорт — модерация не нужна остальному модулю

    bot = ModerationBot()
    if not bot.token or not bot.owner_chat_id:
        print("[news] TELEGRAM_BOT_TOKEN / OWNER_TELEGRAM_CHAT_ID не заданы в .env — отправлять некуда.")
        return

    source = RssSource(name="novorab.ru", feed_url=feed_url)
    items = source.fetch(limit=limit)
    if not items:
        print(f"[news] источник {feed_url} не отдал ни одной записи")
        return

    for item in items:
        draft = verify_and_rewrite(rubric, item)
        if draft is None:
            print(f"[news] пропущено (реклама): {item.title}")
            continue
        bot.send_draft(draft)
        print(f"[news] черновик отправлен на модерацию: {draft.title}")

    # Отправка и приём кнопок должны жить в одном процессе — pending хранится в памяти
    # конкретного объекта ModerationBot, поэтому слушаем кнопки этим же ботом, а не отдельным.
    bot.poll_forever()


def run_telegram_test(channel_username: str, rubric: str, limit: int = 2) -> None:
    """Локальный прогон пайплайна на реальном Telegram-канале (см. TelegramChannelSource).

    Требует заранее выполненного telethon_login.py (сессия telethon_observer.session).
    """
    if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
        print("[news] YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы в .env — рерайт недоступен.")
        return

    source = TelegramChannelSource(channel_username)
    items = source.fetch(limit=limit)

    if not items:
        print(f"[news] канал @{source.channel_username} не отдал ни одной записи")
        return

    for item in items:
        draft = verify_and_rewrite(rubric, item)
        if draft is None:
            print(f"[news] пропущено (реклама): {item.title}")
            continue
        _print_draft(draft)


def run_sea_test(rubric: str = "Погода и море") -> None:
    """Локальный прогон пайплайна на странице Яндекс.Погоды (см. YandexPogodaSource).

    Без LLM-рерайта (см. build_structured_draft) — сами данные уже проверенные факты,
    а пересказ прозой ломает построчный формат, который просил владелец (2026-08-17)."""
    source = YandexPogodaSource()
    items = source.fetch()
    if not items:
        print("[news] страница Яндекс.Погоды не отдала данных")
        return

    for item in items:
        draft = build_structured_draft(rubric, item)
        _print_draft(draft)


def run_vk_test(group_screen_name: str, rubric: str, limit: int = 2) -> None:
    """Локальный прогон пайплайна на реальном VK-паблике (см. VkGroupSource)."""
    if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
        print("[news] YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы в .env — рерайт недоступен.")
        return

    source = VkGroupSource(group_screen_name)
    items = source.fetch(limit=limit)

    if not items:
        print(f"[news] паблик vk.com/{source.group_screen_name} не отдал ни одной записи")
        return

    for item in items:
        draft = verify_and_rewrite(rubric, item)
        if draft is None:
            print(f"[news] пропущено (реклама): {item.title}")
            continue
        _print_draft(draft)


if __name__ == "__main__":
    run_rss_test("https://novorab.ru/feed/")
