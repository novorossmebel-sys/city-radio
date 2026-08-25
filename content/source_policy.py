"""Политика источников — раздел 2 редакционной политики владельца (2026-08-19) как код.

Каждый из 35 каналов получает роль и права: может ли он сам создать событие
(can_create_event), подтвердить чужое (can_confirm), дать мнение (can_opine),
попасть в дайджест напрямую без синтеза (can_publish_direct). Плюс:
- HUMOR_ONLY_CHANNELS — юмористический silo (раздел 17), не участвует в общем скоринге.
- FACT/OPINION-разделение кино-источников (раздел 14) — для синтеза "Стоит смотреть?".
- HEALTH_CLAIM_TRIGGERS — триггерные формулировки для карантина biohakrs (раздел 16).
- Keyword-списки для pre-filter DROP-01..03 (раздел 3) — проверяются ДО любого
  LLM-вызова, чтобы не тратить его на явный мусор.

Ключи везде — имя канала без @ (как в TelegramChannelSource.channel_username).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceRole:
    role: str
    can_create_event: bool
    can_confirm: bool  # True/False/None — None соответствует "⚠" документа (частично, с оговоркой)
    can_opine: bool
    can_publish_direct: str  # "always" | "digest" | "weekly" | "alert_only" | "verified_only" | "never"


SOURCE_MAP: dict[str, SourceRole] = {
    "nvrskadm":            SourceRole("OFFICIAL_LOCAL",  True,  True,  False, "always"),
    "chpnvrsk_official":   SourceRole("LOCAL_RADAR",      True,  None,  False, "alert_only"),
    "novorosstoday":       SourceRole("LOCAL_NEWS",       True,  True,  True,  "digest"),
    "first_nvrsk":         SourceRole("LOCAL_RADAR",      True,  None,  True,  "digest"),
    "kuda_idem_nvrsk":     SourceRole("LOCAL_DISCOVERY",  True,  False, True,  "weekly"),
    "novosti_efir":        SourceRole("GENERAL_RADAR",    True,  False, False, "never"),
    "kinopoisk_soon":      SourceRole("CINEMA_RADAR",     True,  True,  False, "digest"),
    "kinotvnews":          SourceRole("CINEMA_NEWS",      True,  True,  None,  "digest"),
    "KingOfYantar":        SourceRole("CINEMA_REVIEW",    False, False, True,  "digest"),
    "MaxAndSe":            SourceRole("CINEMA_ANALYSIS",  False, False, True,  "weekly"),
    "remizorro":           SourceRole("CINEMA_OPINION",   False, False, True,  "digest"),
    "FBChu":               SourceRole("CINEMA_OPINION",   False, False, True,  "weekly"),
    "seance_light":        SourceRole("CINEMA_DEEP",      False, False, True,  "weekly"),
    "filmres":             SourceRole("INDUSTRY",         True,  True,  True,  "weekly"),
    "tveda":               SourceRole("RECIPE",           True,  False, False, "digest"),
    "foodandscience":      SourceRole("FOOD_SCIENCE",     True,  True,  True,  "digest"),
    "thesaltmagazine":     SourceRole("FOOD_ANALYSIS",    True,  False, True,  "weekly"),
    "foodiscovery":        SourceRole("FOOD_DISCOVERY",   True,  False, True,  "weekly"),
    "biohakrs":            SourceRole("HEALTH_RADAR",     True,  False, False, "verified_only"),
    "leoday":              SourceRole("HUMOR_ONLY",       False, False, False, "digest"),
    "memachh":             SourceRole("HUMOR_ONLY",       False, False, False, "digest"),
    "mudak":               SourceRole("HUMOR_ONLY",       False, False, False, "digest"),
    # остальные каналы из sources.yaml (min2ru/blog_andychef/foodrumedia/culinary_abuse/
    # polkasserialami/kinopoisk/Cronenberg1664/movies_filmy/memachh-дубли/novohistoryinphoto/
    # vinoterria/yappi_restaurants/ed_prokino/kinopes) не перечислены в присланной таблице
    # владельца — трактуем как ARCHIVE-роль по умолчанию ниже, через .get() с фолбэком.
}

DEFAULT_ROLE = SourceRole("ARCHIVE", False, False, False, "never")


def role_for(channel_username: str) -> SourceRole:
    return SOURCE_MAP.get(channel_username.lstrip("@"), DEFAULT_ROLE)


# Раздел 17 — юмористический silo, вне общего scoring.
HUMOR_ONLY_CHANNELS = {"leoday", "memachh", "mudak"}

# Раздел 14 — факт vs мнение для кино-синтеза "Стоит смотреть?".
FACT_CINEMA_SOURCES = {"kinopoisk_soon", "kinotvnews"}
OPINION_CINEMA_SOURCES = {"KingOfYantar", "MaxAndSe", "remizorro", "FBChu", "seance_light", "kinopes"}

# Раздел 16 — карантин медицинских утверждений biohakrs.
HEALTH_CLAIM_SOURCE = "biohakrs"
HEALTH_CLAIM_TRIGGERS = (
    "лечит", "снижает риск", "улучшает", "помогает при", "учёные доказали", "% улучшения",
)

# Раздел 12 — источник статус-машины БПЛА/ЧС (уже есть как DANGER_SOURCE_CHANNELS в
# content/news.py — импортируем оттуда, а не дублируем список).

# --- Раздел 3: pre-filter — проверяется ДО любого LLM-вызова ---

# RULE DROP-01 — реклама.
AD_KEYWORDS = (
    "#реклама", "erid", "рекламодатель", "получить скидку", "оставьте заявку",
    "купить", "промокод", "акция", "специальная цена", "руб./мес", "заказать рекламу",
)

# RULE DROP-02 — самопромо (весь пост, а не одна строка footer'а — см. отдельно
# _strip_channel_footer в content/news.py для случая "полезный пост + промо-хвост").
SELF_PROMO_KEYWORDS = (
    "подписаться", "мы в max", "наш канал", "поделитесь", "присылайте новости", "заказать рекламу",
)

# RULE DROP-03 — конкурс/engagement bait.
ENGAGEMENT_KEYWORDS = (
    "угадайте", "найдите", "пишите в комментариях", "кто самый внимательный", "розыгрыш", "конкурс",
)


def matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


# --- Рубрика по содержанию, а не только по каналу-источнику ---
#
# sources.yaml жёстко привязывает "Норд-ост и погода" к единственному источнику —
# nvrskadm (официальный канал администрации). Но этот канал публикует далеко не только
# штормовые предупреждения — рейды полиции, подготовку к отопительному сезону, вообще
# всё, чем занимается администрация. Без переопределения такие посты всё равно выходили
# бы под шапкой "Норд-ост и погода", что явно вводит читателя в заблуждение (замечено
# владельцем 2026-08-25: рейд «Правопорядок» и совещание Минстроя пришли под этой
# рубрикой). Переопределяем рубрику по primary_type из LLM-классификации, только для
# этого канала — остальные каналы уже тематически однородны, трогать их не нужно.
NVRSKADM_RUBRIC_BY_TYPE = {
    "LOCAL_ALERT": "Норд-ост и погода",
    "LOCAL_UTILITY": "Вода, свет и ЖКХ",
    "LOCAL_STATUS": "Вода, свет и ЖКХ",
}
NVRSKADM_DEFAULT_RUBRIC = "Новороссийск сегодня"


def resolve_rubric(default_rubric: str, channel: str, primary_type: str) -> str:
    if channel.lstrip("@") == "nvrskadm":
        return NVRSKADM_RUBRIC_BY_TYPE.get(primary_type, NVRSKADM_DEFAULT_RUBRIC)
    return default_rubric
