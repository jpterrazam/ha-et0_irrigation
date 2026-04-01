"""Programmatic irrigation automation generator for ET₀ Irrigation."""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Any

import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BLOCKS,
    CONF_BLOCK_ZONES,
    CONF_IRRIGATION_TIME,
    CONF_MIN_DEFICIT,
    CONF_ZONE_COMPANION_POOL,
    CONF_ZONE_APPLICATION_RATE,
    CONF_ZONE_MAX_MINUTES,
    CONF_ZONE_MIN_MINUTES,
    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION,
    CONF_ZONE_NAME,
    CONF_ZONE_REQUIRES_COMPANION,
    CONF_ZONE_SWITCH,
    CONF_ZONE_TYPE,
    CONF_ZONES,
    DEFAULT_APPLICATION_RATE,
    DOMAIN,
)
from .irrigation_rules import (
    DEFAULT_ZONE_MAX_MINUTES,
    build_zone_duration_template,
    max_duration_template,
)

_LOGGER = logging.getLogger(__name__)

ZONE_TYPE_ET0 = "et0"
_MANAGED_AUTOMATION_ALIAS = "ET₀ Irrigation — Irrigação automática"
_MANAGED_AUTOMATION_DESC_MARKER = "Gerado automaticamente pelo componente ET₀ Irrigation."
_MANAGED_AUTOMATION_ENTITY_ID_PREFIX = "automation.et0_irrigation_irrigacao_automatica"


def _normalize_text(value: Any) -> str:
    """Normalize text for robust legacy matching (accents/dashes/case)."""
    if not isinstance(value, str):
        return ""
    normalized = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return normalized


def _looks_like_et0_managed_automation_text(*values: Any) -> bool:
    """Return True for legacy/current ET0 managed automation identifiers."""
    joined = " ".join(_normalize_text(v) for v in values if isinstance(v, str))
    if not joined:
        return False

    # Current/legacy patterns seen in generated IDs, aliases and entity_ids.
    patterns = (
        "et0_irrigation",
        "et0 irrigation",
        "irrigacao automatica",
    )
    return all(p in joined for p in ("et0", "irrig")) or any(p in joined for p in patterns)


def _managed_automation_id(entry: ConfigEntry) -> str:
    """Return a stable automation id for this config entry."""
    return f"et0_irrigation_{entry.entry_id}"


def _normalize_time_string(value: str) -> str:
    """Normalize time strings to HH:MM:SS for automation triggers."""
    parts = [part.strip() for part in str(value).split(":")]
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        hour, minute = parts
        return f"{int(hour):02d}:{int(minute):02d}:00"
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        hour, minute, second = parts
        return f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
    return "02:00:00"


def _zone_var_name(zone_name: str, used_names: set[str]) -> str:
    """Create a valid, unique variable name from a zone label."""
    ascii_name = (
        unicodedata.normalize("NFKD", zone_name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    base = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")
    if not base:
        base = "zone"

    var_name = f"t_{base}"
    suffix = 2
    while var_name in used_names:
        var_name = f"t_{base}_{suffix}"
        suffix += 1

    used_names.add(var_name)
    return var_name


def _zone_label(zone: dict) -> str:
    """Return a readable label for zone actions and aliases."""
    switch = str(zone.get(CONF_ZONE_SWITCH, ""))
    friendly = zone.get("switch_friendly_name")
    if isinstance(friendly, str) and friendly.strip():
        return friendly.strip()
    return switch or str(zone.get(CONF_ZONE_NAME, "zona"))


def _zone_deficit_sensor(zone: dict) -> str:
    """Return per-zone effective deficit sensor (rain + prior irrigation discounted)."""
    switch = str(zone.get(CONF_ZONE_SWITCH, ""))
    ascii_name = (
        unicodedata.normalize("NFKD", switch)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")
    return f"sensor.et0_irrigation_zone_deficit_{slug}"


def _zone_time_template(zone: dict, min_deficit: float) -> str:
    """
    Return a Jinja2 template string (in seconds) for the given zone's
    irrigation duration.

    All zones are ET0-based.

    duration_sec = ceil(max(zone_deficit_mm / application_rate, min_minutes) * 60)

    The et0_factor is NOT applied here — ZoneWaterDeficitSensor already
    accumulates deficits multiplied by the factor, so the sensor value
    already represents the zone-specific water depth to replenish.
    """
    zone_deficit_sensor = _zone_deficit_sensor(zone)

    try:
        application_rate = float(zone.get(CONF_ZONE_APPLICATION_RATE, DEFAULT_APPLICATION_RATE))
    except (TypeError, ValueError):
        application_rate = DEFAULT_APPLICATION_RATE
    if application_rate <= 0:
        application_rate = DEFAULT_APPLICATION_RATE

    try:
        min_minutes = max(0, int(zone.get(CONF_ZONE_MIN_MINUTES, 0) or 0))
    except (TypeError, ValueError):
        min_minutes = 0

    try:
        max_minutes = int(
            zone.get(CONF_ZONE_MAX_MINUTES, DEFAULT_ZONE_MAX_MINUTES)
            or DEFAULT_ZONE_MAX_MINUTES
        )
    except (TypeError, ValueError):
        max_minutes = DEFAULT_ZONE_MAX_MINUTES

    try:
        max_days_without_irrigation = int(
            zone.get(CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION, 0) or 0
        )
    except (TypeError, ValueError):
        max_days_without_irrigation = 0

    return build_zone_duration_template(
        deficit_sensor=zone_deficit_sensor,
        min_deficit=min_deficit,
        application_rate=application_rate,
        min_minutes=min_minutes,
        max_minutes=max_minutes,
        max_days_without_irrigation=max_days_without_irrigation,
    )


def _pick_companion(
    dependent_zone_name: str,
    active_zone_names: list[str],
    companion_pool: list[str],
) -> str | None:
    """Pick one companion zone that can stay on with the dependent zone."""
    candidates = [z for z in active_zone_names if z != dependent_zone_name]
    if not candidates:
        return None

    if companion_pool:
        pooled = [z for z in candidates if z in companion_pool]
        if pooled:
            return pooled[0]

    return candidates[0]


def _build_automation(config: dict, automation_id: str) -> dict[str, Any]:
    """
    Build the full automation dict to be written to automations.yaml.

        For each block:
            1. Turn ON zones not already ON
            2. Turn OFF ending zones in parallel at each zone's own duration
            3. Wait for the longest ending-zone duration in that block
                 (zones appearing in later blocks stay ON)
    After all blocks, turn OFF any remaining zones (safety).
    """
    zones: list[dict] = config[CONF_ZONES]
    blocks: list[dict] = config[CONF_BLOCKS]
    irrigation_time: str = _normalize_time_string(
        str(config.get(CONF_IRRIGATION_TIME, "02:00"))
    )
    min_deficit: float = float(config.get(CONF_MIN_DEFICIT, 2.0))

    zone_map = {z[CONF_ZONE_NAME]: z for z in zones}
    zone_label_map = {z[CONF_ZONE_NAME]: _zone_label(z) for z in zones}

    # Last block index each zone appears in
    last_block_of: dict[str, int] = {}
    for i, block in enumerate(blocks):
        for zname in block[CONF_BLOCK_ZONES]:
            last_block_of[zname] = i

    actions: list[dict] = []

    # Variables: pre-compute all zone durations
    variables: dict[str, str] = {}
    zone_var_map: dict[str, str] = {}
    used_var_names: set[str] = set()
    for zone in zones:
        var_name = _zone_var_name(zone[CONF_ZONE_NAME], used_var_names)
        zone_var_map[zone[CONF_ZONE_NAME]] = var_name
        variables[var_name] = _zone_time_template(zone, min_deficit)
    actions.append({"variables": variables})

    # Simple companion strategy: raise companion duration to at least the
    # dependent zone duration, i.e. max(t_companion, t_dependent).
    companion_requirements: dict[str, list[str]] = {}
    dependent_companion_var: dict[str, str] = {}
    for zone in zones:
        if not zone.get(CONF_ZONE_REQUIRES_COMPANION, False):
            continue

        dependent_name = zone[CONF_ZONE_NAME]
        dependent_var = zone_var_map[dependent_name]
        companion_pool = zone.get(CONF_ZONE_COMPANION_POOL, []) or []
        companion_candidates = [
            c for c in companion_pool if c in zone_var_map and c != dependent_name
        ]
        if not companion_candidates:
            continue

        companion_name = companion_candidates[0]
        companion_var = zone_var_map[companion_name]
        dependent_companion_var[dependent_name] = companion_var
        companion_requirements.setdefault(companion_var, [companion_var]).append(dependent_var)

    if companion_requirements:
        companion_overrides = {
            cvar: max_duration_template(dep_vars)
            for cvar, dep_vars in companion_requirements.items()
        }
        actions.append({"variables": companion_overrides})

    zones_on: set[str] = set()

    for block_idx, block in enumerate(blocks):
        block_zone_names: list[str] = block[CONF_BLOCK_ZONES]

        # Turn ON zones not already ON
        for zname in block_zone_names:
            if zname not in zones_on:
                switch = zone_map[zname][CONF_ZONE_SWITCH]
                zlabel = zone_label_map[zname]
                zvar = zone_var_map[zname]
                condition_templates = [
                    {
                        "condition": "template",
                        "value_template": f"{{{{ {zvar} | float(0) > 0 }}}}",
                    }
                ]
                companion_var = dependent_companion_var.get(zname)
                if companion_var:
                    condition_templates.append(
                        {
                            "condition": "template",
                            "value_template": f"{{{{ {companion_var} | float(0) > 0 }}}}",
                        }
                    )
                actions.append(
                    {
                        "alias": f"Liga {zlabel} se houver necessidade",
                        "choose": [
                            {
                                "conditions": condition_templates,
                                "sequence": [
                                    {
                                        "action": "switch.turn_on",
                                        "target": {"entity_id": switch},
                                        "alias": f"Liga {zlabel}",
                                    }
                                ],
                            }
                        ],
                    }
                )
                zones_on.add(zname)

        # Turn OFF zones that end in this block using each zone's own duration.
        ending_zone_names = [z for z in block_zone_names if last_block_of[z] == block_idx]
        if ending_zone_names:
            # Base off-delay requirement for each ending zone is its own duration.
            off_requirements: dict[str, list[str]] = {
                zname: [zone_var_map[zname]] for zname in ending_zone_names
            }

            # Enforce "full-duration companion": zones marked as dependent
            # must have at least one companion active for the whole duration.
            active_zone_names = list(zones_on)
            for zname in ending_zone_names:
                zone_cfg = zone_map[zname]
                if not zone_cfg.get(CONF_ZONE_REQUIRES_COMPANION, False):
                    continue

                companion_pool = zone_cfg.get(CONF_ZONE_COMPANION_POOL, []) or []
                companion = _pick_companion(zname, active_zone_names, companion_pool)
                if companion is None:
                    _LOGGER.warning(
                        "ET₀ Irrigation: zone %s requires companion but no active companion is available in block %d",
                        zname,
                        block_idx + 1,
                    )
                    continue

                # Force companion to stay on at least until dependent zone ends.
                if companion in off_requirements:
                    off_requirements[companion].append(zone_var_map[zname])

            parallel_sequences: list[dict[str, Any]] = []
            for zname in ending_zone_names:
                switch = zone_map[zname][CONF_ZONE_SWITCH]
                zlabel = zone_label_map[zname]
                off_delay = max_duration_template(off_requirements.get(zname, [zone_var_map[zname]]))
                parallel_sequences.append(
                    {
                        "sequence": [
                            {
                                "choose": [
                                    {
                                        "conditions": [
                                            {
                                                "condition": "template",
                                                "value_template": f"{{{{ {zone_var_map[zname]} | float(0) > 0 }}}}",
                                            }
                                        ],
                                        "sequence": [
                                            {
                                                "delay": {
                                                    "seconds": off_delay
                                                },
                                                "alias": f"Aguarda {zlabel} (bloco {block_idx + 1})",
                                            },
                                            {
                                                "action": "switch.turn_off",
                                                "target": {"entity_id": switch},
                                                "alias": f"Desliga {zlabel}",
                                            },
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                )
                zones_on.discard(zname)

            actions.append({"parallel": parallel_sequences})

        # Only add an explicit inter-block wait when some zones continue into
        # the next block. The parallel sequences above already handle turn-off
        # timing internally via their own delays, so adding another wait here
        # when ALL zones end would double the duration.
        continuing_zones = [z for z in block_zone_names if last_block_of[z] != block_idx]
        if continuing_zones and ending_zone_names:
            ending_vars = [zone_var_map[z] for z in ending_zone_names]
            block_wait = max_duration_template(ending_vars)
            actions.append({
                "delay": {"seconds": block_wait},
                "alias": f"Aguarda fim do bloco {block_idx + 1} antes do próximo",
            })

    # Safety: turn off anything still ON
    for zname in list(zones_on):
        switch = zone_map[zname][CONF_ZONE_SWITCH]
        zlabel = zone_label_map[zname]
        actions.append({
            "action": "switch.turn_off",
            "target": {"entity_id": switch},
            "alias": f"Desliga {zlabel} (segurança)",
        })

    return {
        "id": automation_id,
        "alias": "ET₀ Irrigation — Irrigação automática",
        "description": (
            f"Gerado automaticamente pelo componente ET₀ Irrigation. "
            f"Déficit mínimo por zona: {min_deficit}mm."
        ),
        "triggers": [{"trigger": "time", "at": irrigation_time}],
        "actions": actions,
        "mode": "single",
    }


def _automations_yaml_path(hass: HomeAssistant) -> str:
    return hass.config.path("automations.yaml")


def _is_managed_automation_record(item: Any) -> bool:
    """Return True when an automation YAML record is managed by this integration."""
    if not isinstance(item, dict):
        return False
    aid = item.get("id")
    alias = item.get("alias")
    desc = item.get("description")
    if isinstance(aid, str) and aid.startswith("et0_irrigation_"):
        return True
    if alias == _MANAGED_AUTOMATION_ALIAS and isinstance(desc, str) and _MANAGED_AUTOMATION_DESC_MARKER in desc:
        return True
    if _looks_like_et0_managed_automation_text(aid, alias, desc):
        return True
    return False


async def _async_cleanup_stale_automation_registry_entries(
    hass: HomeAssistant,
    keep_automation_id: str | None,
) -> None:
    """Remove stale automation entity-registry entries created by this integration."""
    registry = er.async_get(hass)
    for entry in list(registry.entities.values()):
        if entry.domain != "automation":
            continue

        entry_entity_id = (entry.entity_id or "").lower()
        entry_unique_id = entry.unique_id if isinstance(entry.unique_id, str) else ""
        entry_name = entry.name if isinstance(entry.name, str) else ""
        entry_original_name = (
            entry.original_name if isinstance(entry.original_name, str) else ""
        )

        looks_managed = (
            entry_unique_id.startswith("et0_irrigation_")
            or entry_entity_id.startswith(_MANAGED_AUTOMATION_ENTITY_ID_PREFIX)
            or entry_name == _MANAGED_AUTOMATION_ALIAS
            or entry_original_name == _MANAGED_AUTOMATION_ALIAS
            or _looks_like_et0_managed_automation_text(
                entry_entity_id,
                entry_unique_id,
                entry_name,
                entry_original_name,
            )
        )
        if not looks_managed:
            continue

        # Keep exactly the current managed automation entity when possible.
        if keep_automation_id and entry_unique_id == keep_automation_id:
            continue

        registry.async_remove(entry.entity_id)


def _cleanup_stale_automation_states(
    hass: HomeAssistant,
    keep_automation_id: str | None,
) -> None:
    """Remove stale ET0 automation entities from HA runtime state machine."""
    for state in list(hass.states.async_all("automation")):
        entity_id = (state.entity_id or "").lower()
        if not (
            entity_id.startswith(_MANAGED_AUTOMATION_ENTITY_ID_PREFIX)
            or _looks_like_et0_managed_automation_text(
                entity_id,
                state.name,
                state.attributes.get("friendly_name"),
                state.attributes.get("id"),
            )
        ):
            continue

        state_automation_id = state.attributes.get("id")
        if keep_automation_id and state_automation_id == keep_automation_id:
            continue

        hass.states.async_remove(state.entity_id)


async def async_cleanup_automation_ghosts(hass: HomeAssistant) -> None:
    """Force cleanup of stale ET0 automation entities/registry entries."""
    await _async_cleanup_stale_automation_registry_entries(hass, None)
    _cleanup_stale_automation_states(hass, None)
    await hass.services.async_call("automation", "reload", blocking=True)


async def async_create_automation(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Write the irrigation automation to automations.yaml and reload."""
    config = {**entry.data, **entry.options}
    automation_id = _managed_automation_id(entry)
    automation = _build_automation(config, automation_id)

    yaml_path = _automations_yaml_path(hass)

    def _write():
        # Load existing automations
        if os.path.exists(yaml_path):
            with open(yaml_path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or []
        else:
            existing = []

        if not isinstance(existing, list):
            _LOGGER.error(
                "ET₀ Irrigation: automations.yaml is not a list — cannot append automation"
            )
            return False

        # Keep only non-managed records, then write exactly one managed automation.
        existing = [a for a in existing if not _is_managed_automation_record(a)]

        existing.append(automation)

        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        return True

    success = await hass.async_add_executor_job(_write)
    if not success:
        return

    # Reload automations so HA picks up the new entry.
    # This must not bring down the integration if reload fails.
    try:
        await hass.services.async_call("automation", "reload", blocking=True)
        await _async_cleanup_stale_automation_registry_entries(hass, automation_id)
        _cleanup_stale_automation_states(hass, automation_id)
        # Reload again after cleanup so HA keeps only one managed entity in selectors.
        await hass.services.async_call("automation", "reload", blocking=True)
    except Exception:
        _LOGGER.exception("ET₀ Irrigation: failed to reload automations after write")
        return

    _LOGGER.info(
        "ET₀ Irrigation: automation written to automations.yaml (id=%s) at %s, "
        "min deficit per zone: %smm",
        automation_id,
        config.get(CONF_IRRIGATION_TIME, "02:00"),
        config.get(CONF_MIN_DEFICIT, 2.0),
    )


async def async_remove_automation(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the managed automation from automations.yaml."""
    automation_id = _managed_automation_id(entry)

    yaml_path = _automations_yaml_path(hass)

    def _remove():
        if not os.path.exists(yaml_path):
            return
        with open(yaml_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or []
        if not isinstance(existing, list):
            return
        existing = [a for a in existing if not _is_managed_automation_record(a)]
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    await hass.async_add_executor_job(_remove)
    try:
        await hass.services.async_call("automation", "reload", blocking=True)
        await _async_cleanup_stale_automation_registry_entries(hass, None)
        _cleanup_stale_automation_states(hass, None)
    except Exception:
        _LOGGER.exception("ET₀ Irrigation: failed to reload automations after removal")
        return

    _LOGGER.info("ET₀ Irrigation: automation removed from automations.yaml (id=%s)", automation_id)
