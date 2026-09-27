from __future__ import annotations

import re


REGIONAL_CLUSTERS: dict[str, tuple[str, ...]] = {
    "cluster_2": (
        # Central Black Earth + Southern Russia
        # Воронежская область = production project cluster (М4)
        # Краснодарский край  = same cluster per spec
        "воронеж",
        "белгород",
        "курск",
        "липецк",
        "тамбов",
        "орел",
        "краснодар",
        "ростов",
        "ставропол",
        "адыге",
        "калмык",
    ),
    "cluster_1": (
        # Ural / Siberia / Far East
        "янао",
        "ямало",
        "ноябрьск",
        "хмао",
        "ханты",
        "тюмен",
        "свердлов",
        "екатеринбург",
        "омск",
        "новосибирск",
        "томск",
        "кемеров",
        "алтай",
        "красноярск",
        "иркутск",
        "якут",
        "бурят",
        "владивосток",
        "хабаровск",
        "приморск",
        "сахалин",
    ),
}


TAX_REGION_PREFIXES: dict[str, str] = {
    "01": "Республика Адыгея",
    "03": "Республика Бурятия",
    "08": "Республика Калмыкия",
    "14": "Республика Саха Якутия",
    "22": "Алтайский край",
    "23": "Краснодарский край",
    "24": "Красноярский край",
    "25": "Приморский край",
    "26": "Ставропольский край",
    "27": "Хабаровский край",
    "31": "Белгородская область",
    "36": "Воронежская область",
    "38": "Иркутская область",
    "42": "Кемеровская область",
    "46": "Курская область",
    "48": "Липецкая область",
    "54": "Новосибирская область",
    "55": "Омская область",
    "57": "Орловская область",
    "61": "Ростовская область",
    "65": "Сахалинская область",
    "66": "Свердловская область",
    "68": "Тамбовская область",
    "70": "Томская область",
    "72": "Тюменская область",
    "86": "ХМАО Югра",
    "89": "ЯНАО",
}

REGION_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("ноябрьск", "ЯНАО г Ноябрьск"),
    ("новосибирск", "Новосибирская область"),
    ("екатеринбург", "Свердловская область"),
    ("воронеж", "Воронежская область"),
    ("краснодар", "Краснодарский край"),
    ("ростов", "Ростовская область"),
    ("тюмень", "Тюменская область"),
    ("омск", "Омская область"),
)


def normalize_region(value: str) -> str:
    return " ".join(re.findall(r"[0-9a-zа-я]+", value.casefold().replace("ё", "е")))


def infer_cluster(region: str) -> str:
    normalized = normalize_region(region)
    matches = [
        cluster
        for cluster, markers in REGIONAL_CLUSTERS.items()
        if any(marker in normalized for marker in markers)
    ]
    return matches[0] if len(matches) == 1 else ""


def infer_region(value: str, tax_id: str = "") -> str:
    """Infer only from explicit city text or the official first two INN digits."""
    normalized = normalize_region(value)
    for marker, region in REGION_NAME_HINTS:
        if marker in normalized:
            return region
    digits = "".join(re.findall(r"\d", tax_id))
    return TAX_REGION_PREFIXES.get(digits[:2], "") if len(digits) >= 10 else ""


def resolve_cluster(region: str, requested: str = "") -> str:
    inferred = infer_cluster(region)
    if requested and requested not in REGIONAL_CLUSTERS:
        raise ValueError("unsupported regional cluster")
    if requested and inferred and requested != inferred:
        raise ValueError("regional cluster conflicts with region")
    return requested or inferred
