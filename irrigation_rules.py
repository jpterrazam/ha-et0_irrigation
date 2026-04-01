"""Core irrigation rule helpers shared by automation generation and validation."""

from __future__ import annotations

from typing import Iterable


DEFAULT_ZONE_MAX_MINUTES = 30


def clamp_minutes(value: int, minimum: int = 0, maximum: int = 240) -> int:
    """Clamp minute values used by irrigation configuration."""
    return max(minimum, min(maximum, int(value)))


def build_zone_duration_template(
    *,
    deficit_sensor: str,
    min_deficit: float,
    application_rate: float,
    min_minutes: int,
    max_minutes: int,
    max_days_without_irrigation: int,
) -> str:
    """
    Build a Jinja template for zone irrigation duration in seconds.

        Rule:
            - If deficit < min_deficit and max-days rule is not reached => 0 sec
            - Else duration = ceil(clamp(max(deficit / rate, min_minutes), 0, max_minutes) * 60)
            - If max-days rule is reached, minimum duration is at least min_minutes
    """
    safe_rate = float(application_rate) if application_rate > 0 else 0.473
    safe_min = clamp_minutes(min_minutes, minimum=0, maximum=240)
    safe_max = clamp_minutes(max_minutes, minimum=1, maximum=240)
    safe_max_days = clamp_minutes(max_days_without_irrigation, minimum=0, maximum=5)
    if safe_max < safe_min:
        safe_max = safe_min

    deficit_expr = f"states('{deficit_sensor}') | float(0)"
    force_by_days_expr = (
        "(state_attr("
        f"'{deficit_sensor}', 'days_without_irrigation'"
        ") | int(0) >= "
        f"{safe_max_days})"
    )
    should_irrigate_expr = f"(({deficit_expr} >= {min_deficit}) or {force_by_days_expr})"

    return (
        "{{ (((("
        f"[{deficit_expr} / {safe_rate}, {safe_min}] | max"
        f") | min({safe_max})) * 60) | round(0, 'ceil') | int) "
        f"if {should_irrigate_expr} else 0 }}}}"
    )


def max_duration_template(var_names: Iterable[str]) -> str:
    """Return a Jinja template with the max duration from variable names."""
    names = list(var_names)
    if not names:
        return "0"
    joined = ", ".join(f"({name} | float(0))" for name in names)
    return f"{{{{ [{joined}] | max }}}}"
