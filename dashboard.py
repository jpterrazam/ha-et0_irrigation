"""Automatic Lovelace dashboard generator for ET₀ Irrigation."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import frontend

from .const import (
    CONF_BLOCKS,
    CONF_BLOCK_ZONES,
    CONF_MIN_DEFICIT,
    CONF_SENSOR_RAIN_TODAY,
    CONF_ZONE_APPLICATION_RATE,
    CONF_ZONE_FACTOR,
    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION,
    CONF_ZONE_MIN_MINUTES,
    CONF_ZONE_NAME,
    CONF_ZONE_REQUIRES_COMPANION,
    CONF_ZONE_SWITCH,
    CONF_ZONES,
    DEFAULT_APPLICATION_RATE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DASHBOARD_URL_PATH = "et0-irrigation"
DASHBOARD_TITLE = "ET₀ Irrigação"
DASHBOARD_ICON = "mdi:sprinkler-variant"

# Key stored in entry options to track the dashboard url_path for removal
DASHBOARD_URL_KEY = "managed_dashboard_url"

# Lovelace domain key
_LOVELACE = "lovelace"


def _zone_label(zone: dict) -> str:
    """Return a friendly label for the zone."""
    friendly = zone.get("switch_friendly_name")
    if isinstance(friendly, str) and friendly.strip():
        return friendly.strip()
    return str(zone.get(CONF_ZONE_SWITCH, "zona"))


def _zone_deficit_entity(zone: dict) -> str:
    """Return the zone deficit sensor entity_id."""
    import re
    import unicodedata

    switch = str(zone.get(CONF_ZONE_SWITCH, ""))
    ascii_name = (
        unicodedata.normalize("NFKD", switch)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")
    return f"sensor.et0_irrigation_zone_deficit_{slug}"


def _icon_color_template(entity_id: str) -> str:
    """Return a Mushroom icon_color template based on deficit value."""
    return (
        f"{{% set v = states('{entity_id}') | float(0) %}}"
        f"{{% if v <= 0.1 %}}blue"
        f"{{% elif v < 4 %}}green"
        f"{{% elif v < 8 %}}orange"
        f"{{% else %}}red{{% endif %}}"
    )


def _build_dashboard_config(config: dict) -> dict[str, Any]:
    """Build the full Lovelace dashboard config dict.

    Layout:
      1. ET₀ vs Chuva 7d (apexcharts)
      2. ET₀ hoje + Chuva hoje + Próxima Rega + Reset (grid compacto)
      3. Zonas — déficit + válvula em grid 2x2 compacto
      4. Histórico déficit 24h (apexcharts)
    """
    zones: list[dict] = [
        z for z in config.get(CONF_ZONES, [])
        if isinstance(z, dict) and z.get(CONF_ZONE_SWITCH)
    ]
    sensor_rain = config.get(CONF_SENSOR_RAIN_TODAY, "sensor.rain_today")
    et0_today   = "sensor.et0_irrigation_et0_today"
    irrigation_time = config.get("irrigation_time", "02:00")

    palette = ["#4ade80", "#f87171", "#fb923c", "#fbbf24", "#c084fc", "#34d399", "#f472b6"]
    cards: list[dict] = []

    # ── 1. ET₀ vs Chuva 7 dias ───────────────────────────────────────────────
    cards.append({
        "type": "custom:apexcharts-card",
        "header": {
            "show": True,
            "title": "ET₀ vs Chuva · 7 dias",
            "show_states": True,
            "colorize_states": True,
        },
        "graph_span": "7d",
        "span": {"start": "day", "offset": "-6d"},
        "update_interval": "1h",
        "series": [
            {
                "entity": et0_today,
                "name": "ET₀",
                "color": "#f59e0b",
                "type": "column",
                "group_by": {"func": "last", "duration": "1d"},
            },
            {
                "entity": sensor_rain,
                "name": "Chuva",
                "color": "#60a5fa",
                "type": "column",
                "group_by": {"func": "max", "duration": "1d", "fill": "last"},
            },
        ],
        "apex_config": {
            "chart": {"type": "bar", "height": 220},
            "plotOptions": {"bar": {"borderRadius": 3, "columnWidth": "60%"}},
            "yaxis": {"decimalsInFloat": 1, "title": {"text": "mm"}},
            "xaxis": {"type": "datetime"},
            "legend": {"show": True, "position": "bottom"},
            "tooltip": {"x": {"format": "dd/MM"}},
        },
    })

    # ── 2. ET₀ hoje + Chuva hoje + Próxima Rega + Reset ─────────────────────
    cards.append({
        "type": "grid",
        "columns": 2,
        "square": False,
        "cards": [
            {
                "type": "custom:mushroom-template-card",
                "primary": "ET₀ Hoje",
                "secondary": f"{{{{ states('{et0_today}') | float(0) | round(1) }}}} mm",
                "icon": "mdi:weather-sunny",
                "icon_color": "amber",
                "vertical": True,
            },
            {
                "type": "custom:mushroom-template-card",
                "primary": "Chuva Hoje",
                "secondary": f"{{{{ states('{sensor_rain}') | float(0) | round(1) }}}} mm",
                "icon": "mdi:weather-rainy",
                "icon_color": "blue",
                "vertical": True,
            },
            {
                "type": "custom:mushroom-template-card",
                "primary": "Próxima Rega",
                "secondary": f"Programada · {irrigation_time}",
                "icon": "mdi:clock-check-outline",
                "icon_color": "green",
                "vertical": True,
            },
            {
                "type": "custom:mushroom-entity-card",
                "entity": "button.et0_irrigation_reset_zone_deficits",
                "name": "Reset Déficits",
                "icon": "mdi:restore",
                "vertical": True,
                "tap_action": {
                    "action": "perform-action",
                    "perform_action": "button.press",
                    "target": {"entity_id": "button.et0_irrigation_reset_zone_deficits"},
                },
            },
        ],
    })

    # ── 3. Zonas — grid 2x2 compacto ─────────────────────────────────────────
    zone_grid_cards: list[dict] = []
    for zone in zones:
        label   = _zone_label(zone)
        deficit = _zone_deficit_entity(zone)
        switch  = str(zone[CONF_ZONE_SWITCH])
        factor  = zone.get("factor", 1.0)
        min_min = zone.get("min_minutes", 0)
        suffix  = f"fator {factor}"
        if min_min:
            suffix += f" · mín {min_min} min"

        zone_grid_cards.append({
            "type": "custom:mushroom-template-card",
            "primary": label,
            "secondary": (
                f"{{{{ states('{deficit}') | float(0) | round(2) }}}} mm · {suffix}"
            ),
            "icon": "mdi:water-alert",
            "icon_color": (
                f"{{% if is_state('{switch}', 'on') %}}blue"
                f"{{% else %}}{{% set v = states('{deficit}') | float(0) %}}"
                "{% if v <= 0.1 %}blue{% elif v < 4 %}green{% elif v < 8 %}orange{% else %}red{% endif %}"
                "{% endif %}"
            ),
        })

        zone_grid_cards.append({
            "type": "custom:mushroom-template-card",
            "entity": switch,
            "primary": "Válvula",
            "secondary": (
                f"{{% if is_state('{switch}', 'on') %}}Ligado agora"
                f"{{% else %}}Última: {{{{ relative_time(states['{switch}'].last_changed) }}}}"
                "{% endif %}"
            ),
            "icon": (
                f"{{% if is_state('{switch}', 'on') %}}mdi:sprinkler-variant"
                "{% else %}mdi:valve{% endif %}"
            ),
            "icon_color": (
                f"{{% if is_state('{switch}', 'on') %}}blue"
                "{% else %}grey{% endif %}"
            ),
            "tap_action": {"action": "toggle"},
        })

    if zone_grid_cards:
        cards.append({
            "type": "grid",
            "columns": 2,
            "square": False,
            "cards": zone_grid_cards,
        })

    # ── 4. Histórico déficit 24h ──────────────────────────────────────────────
    zone_series = [
        {"entity": "sensor.water_deficit_1d", "name": "Global 1d", "color": "#60a5fa", "stroke_width": 2},
    ]
    for idx, zone in enumerate(zones):
        zone_series.append({
            "entity": _zone_deficit_entity(zone),
            "name": _zone_label(zone),
            "color": palette[idx % len(palette)],
            "stroke_width": 1,
        })

    cards.append({
        "type": "custom:apexcharts-card",
        "header": {
            "show": True,
            "title": "Histórico de déficit · 24h",
            "show_states": True,
            "colorize_states": True,
        },
        "graph_span": "24h",
        "series": zone_series,
        "apex_config": {
            "chart": {"type": "line", "height": 260},
            "stroke": {"curve": "stepline", "width": 1.5},
            "yaxis": {"decimalsInFloat": 1, "title": {"text": "mm"}},
            "xaxis": {"type": "datetime"},
            "legend": {"show": True, "position": "bottom"},
            "tooltip": {"x": {"format": "dd/MM HH:mm"}},
        },
    })

    # ── 5. Configuração das zonas (card por zona, editável via serviço) ────────
    # Card markdown com resumo geral + um card por zona com parâmetros atuais
    # e ação de edição via et0_irrigation.set_zone_parameter.

    blocks: list[dict] = config.get(CONF_BLOCKS, [])
    min_deficit = config.get(CONF_MIN_DEFICIT, 2.0)

    # Resumo geral e grupos
    block_lines = []
    for i, block in enumerate(blocks, 1):
        block_zones = ", ".join(block.get(CONF_BLOCK_ZONES, []))
        block_lines.append(f"**Grupo {i}:** {block_zones}")

    summary_lines = [
        f"**Déficit mínimo para irrigar:** {min_deficit} mm",
        "",
        "### Grupos de irrigação",
        "",
    ] + block_lines

    cards.append({
        "type": "markdown",
        "content": "\n".join(summary_lines),
    })

    # Um card por zona com parâmetros atuais + botão de edição
    for zone in zones:
        name     = zone.get(CONF_ZONE_NAME, _zone_label(zone))
        switch   = str(zone.get(CONF_ZONE_SWITCH, ""))
        factor   = zone.get(CONF_ZONE_FACTOR, 1.0)
        rate     = zone.get(CONF_ZONE_APPLICATION_RATE, DEFAULT_APPLICATION_RATE)
        min_min  = zone.get(CONF_ZONE_MIN_MINUTES, 0) or 0
        max_days = zone.get(CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION, 0) or 0
        companion = "Sim" if zone.get(CONF_ZONE_REQUIRES_COMPANION) else "Não"

        zone_md = "\n".join([
            f"### {name}",
            f"**Switch:** `{switch}`",
            f"**Fator ET₀:** {factor}",
            f"**Taxa de aplicação:** {rate} mm/min",
            f"**Tempo mínimo:** {min_min} min",
            f"**Máx. dias sem rega:** {max_days}",
            f"**Requer zona companion:** {companion}",
        ])

        cards.append({
            "type": "vertical-stack",
            "cards": [
                {
                    "type": "markdown",
                    "content": zone_md,
                },
                {
                    "type": "custom:mushroom-template-card",
                    "primary": "Editar parâmetros",
                    "secondary": f"Chama et0_irrigation.set_zone_parameter para {name}",
                    "icon": "mdi:pencil",
                    "icon_color": "grey",
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "et0_irrigation.set_zone_parameter",
                        "data": {"zone_switch": switch},
                    },
                },
            ],
        })

    return {
        "title": DASHBOARD_TITLE,
        "views": [
            {
                "title": DASHBOARD_TITLE,
                "icon": DASHBOARD_ICON,
                "path": "default",
                "type": "masonry",
                "max_columns": 1,
                "cards": cards,
            }
        ],
    }


async def async_create_dashboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create or recreate the ET₀ Irrigation Lovelace dashboard.

    In HA 2026.2+ hass.data["lovelace"] is a LovelaceData @dataclass with
    fields: resource_mode, dashboards, resources.
    We bypass the DashboardsCollection listener pattern and directly:
      1. Create a LovelaceStorage object for our url_path.
      2. Register it in lovelace.dashboards.
      3. Register the sidebar panel via frontend.async_register_built_in_panel.
      4. Save the card config via LovelaceStorage.async_save.
    This mirrors exactly what the HA core's storage_dashboard_changed listener
    does when a dashboard is created via the UI.
    """
    from homeassistant.components.lovelace.dashboard import LovelaceStorage
    from homeassistant.components.lovelace.const import MODE_STORAGE

    config = {**entry.data, **entry.options}

    lovelace = hass.data.get(_LOVELACE)
    if lovelace is None:
        _LOGGER.warning("ET₀ Irrigation: Lovelace not available, skipping dashboard creation")
        return

    # Remove existing managed dashboard first
    await async_remove_dashboard(hass, entry)

    # Build a minimal item dict matching what DashboardsCollection would store
    item = {
        "id": DASHBOARD_URL_PATH,
        "url_path": DASHBOARD_URL_PATH,
        "title": DASHBOARD_TITLE,
        "icon": DASHBOARD_ICON,
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": MODE_STORAGE,
    }

    # Create the LovelaceStorage object and register it in lovelace.dashboards
    dashboard_store = LovelaceStorage(hass, item)
    lovelace.dashboards[DASHBOARD_URL_PATH] = dashboard_store

    # Register the sidebar panel (mirrors _register_panel in lovelace/__init__.py)
    try:
        frontend.async_register_built_in_panel(
            hass,
            "lovelace",
            sidebar_title=DASHBOARD_TITLE,
            sidebar_icon=DASHBOARD_ICON,
            frontend_url_path=DASHBOARD_URL_PATH,
            config={"mode": MODE_STORAGE},
            require_admin=False,
            update=False,
        )
    except ValueError:
        # Panel already registered — update instead
        try:
            frontend.async_register_built_in_panel(
                hass,
                "lovelace",
                sidebar_title=DASHBOARD_TITLE,
                sidebar_icon=DASHBOARD_ICON,
                frontend_url_path=DASHBOARD_URL_PATH,
                config={"mode": MODE_STORAGE},
                require_admin=False,
                update=True,
            )
        except Exception as err:
            _LOGGER.error("ET₀ Irrigation: failed to register dashboard panel: %s", err)
            return

    # Save the card config
    dashboard_config = _build_dashboard_config(config)
    try:
        await dashboard_store.async_save(dashboard_config)
    except Exception as err:
        _LOGGER.error("ET₀ Irrigation: failed to save dashboard config: %s", err)
        return

    _LOGGER.info(
        "ET₀ Irrigation: dashboard created at /%s with %d zone(s)",
        DASHBOARD_URL_PATH,
        len([z for z in config.get(CONF_ZONES, []) if isinstance(z, dict) and z.get(CONF_ZONE_SWITCH)]),
    )


async def async_remove_dashboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the managed ET₀ Irrigation dashboard if it exists."""
    lovelace = hass.data.get(_LOVELACE)
    if lovelace is None:
        return

    if DASHBOARD_URL_PATH not in lovelace.dashboards:
        return

    # Delete the stored card config
    dashboard_store = lovelace.dashboards.pop(DASHBOARD_URL_PATH, None)
    if dashboard_store is not None:
        try:
            await dashboard_store.async_delete()
        except Exception as err:
            _LOGGER.warning("ET₀ Irrigation: error deleting dashboard store: %s", err)

    # Unregister the sidebar panel
    try:
        frontend.async_remove_panel(hass, DASHBOARD_URL_PATH)
    except Exception as err:
        _LOGGER.warning("ET₀ Irrigation: error removing dashboard panel: %s", err)

    _LOGGER.info("ET₀ Irrigation: dashboard removed (url_path=%s)", DASHBOARD_URL_PATH)
