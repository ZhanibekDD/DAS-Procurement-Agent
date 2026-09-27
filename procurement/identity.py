"""Request-local authoritative identity; never sourced from browser identity fields."""
from contextvars import ContextVar

authenticated_actor: ContextVar[str | None] = ContextVar("procurement_authenticated_actor", default=None)


def trusted_actor(fallback: str = "system") -> str:
    return authenticated_actor.get() or fallback
