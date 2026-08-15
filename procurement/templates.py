from __future__ import annotations

import string


class StrictFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str) and key not in kwargs:
            raise ValueError(f"template variable is missing: {key}")
        return super().get_value(key, args, kwargs)


def render_template(value: str, context: dict[str, object]) -> str:
    return StrictFormatter().format(value, **context)

