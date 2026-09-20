"""Service-name normalization, done once so everything downstream agrees.

PRISM splits a column on its *first* underscore, so a service name containing an underscore would
be read as a shorter service with a property that does not classify. Every name is therefore
reduced to lowercase with `-` as the only separator.
"""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[_\s.]+")


def normalize_service(raw: str) -> str:
    """`Cart_Service` / `cart service` / `shop/cartservice` -> `cart-service` / `cartservice`.

    A tag value (`kube_deployment:cartservice`) or an OTel `service.name` both go through here,
    which is what lets container metrics and spanmetrics land on the same component.
    """
    name = raw.strip().lower().rsplit("/", 1)[-1]
    name = _SEPARATORS.sub("-", name)
    return name.strip("-")


def column(service: str, family: str) -> str:
    """Build a PRISM column name: `{service}_{family}`, service free of underscores."""
    return f"{normalize_service(service)}_{family}"


def service_of(column_name: str) -> str:
    """Inverse of `column`, using PRISM's own rule (text before the first underscore)."""
    return column_name.partition("_")[0]


def family_of(column_name: str) -> str:
    return column_name.partition("_")[2]
