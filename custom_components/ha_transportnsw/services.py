"""Actions for the Transport NSW Mk II integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    SERVICE_GET_DEPARTURES,
    SERVICE_GET_JOURNEYS,
    SUBENTRY_TYPE_BOARD,
    SUBENTRY_TYPE_JOURNEY,
)
from .departure_board import departure_as_response

SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id})


def _find_subentry(hass: HomeAssistant, call: ServiceCall, subentry_type: str):
    """Return the config entry and subentry that an entity belongs to."""
    entity_id = call.data[ATTR_ENTITY_ID]
    entity = er.async_get(hass).async_get(entity_id)

    if entity is None or entity.platform != DOMAIN:
        raise ServiceValidationError(
            f"{entity_id} isn't a Transport NSW Mk II entity"
        )

    config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    if config_entry is None or config_entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"The integration for {entity_id} isn't loaded")

    subentry = config_entry.subentries.get(entity.config_subentry_id)
    if subentry is None or subentry.subentry_type != subentry_type:
        wanted = (
            "a departure board" if subentry_type == SUBENTRY_TYPE_BOARD else "a journey"
        )
        raise ServiceValidationError(f"{entity_id} doesn't belong to {wanted}")

    return config_entry, subentry


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions."""

    async def get_departures(call: ServiceCall) -> ServiceResponse:
        """Return every upcoming departure on a departure board."""
        config_entry, subentry = _find_subentry(hass, call, SUBENTRY_TYPE_BOARD)
        coordinator = config_entry.runtime_data.board_coordinators[subentry.subentry_id]

        return {
            "stop": subentry.title,
            "departures": [
                departure_as_response(departure)
                for departure in coordinator.data or []
            ],
        }

    async def get_journeys(call: ServiceCall) -> ServiceResponse:
        """Return the journeys that are being tracked, with all of their detail."""
        config_entry, subentry = _find_subentry(hass, call, SUBENTRY_TYPE_JOURNEY)
        data = config_entry.runtime_data.coordinator.data or {}

        return {
            "journey": subentry.title,
            "journeys": data.get(subentry.subentry_id, []),
        }

    for name, handler in (
        (SERVICE_GET_DEPARTURES, get_departures),
        (SERVICE_GET_JOURNEYS, get_journeys),
    ):
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
