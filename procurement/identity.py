"""Request-local authoritative identity; never sourced from browser identity fields."""
from contextvars import ContextVar

authenticated_actor: ContextVar[str | None] = ContextVar("procurement_authenticated_actor", default=None)
authenticated_role: ContextVar[str] = ContextVar("procurement_authenticated_role", default="staff")


def trusted_actor(fallback: str = "system") -> str:
    return authenticated_actor.get() or fallback


def trusted_role() -> str:
    return authenticated_role.get()
