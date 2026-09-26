"""Current input aliases; the hash-pinned historic migration module stays intact."""
from .regions import infer_cluster as legacy_infer_cluster, normalize_region
from .regions import resolve_cluster as legacy_resolve_cluster


def _input(region: str) -> str:
    return 'Орёл' if normalize_region(region) == 'орловская область' else region


def infer_cluster(region: str) -> str:
    return legacy_infer_cluster(_input(region))


def resolve_cluster(region: str, requested: str = '') -> str:
    return legacy_resolve_cluster(_input(region), requested)
