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


def resolve_cluster(region: str, requested: str = "") -> str:
    inferred = infer_cluster(region)
    if requested and requested not in REGIONAL_CLUSTERS:
        raise ValueError("unsupported regional cluster")
    if requested and inferred and requested != inferred:
        raise ValueError("regional cluster conflicts with region")
    return requested or inferred
