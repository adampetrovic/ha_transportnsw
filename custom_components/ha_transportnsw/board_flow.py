"""Departure board subentry flow for Transport NSW Mk II integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from TransportNSWv2 import APIRateLimitExceeded, InvalidAPIKey

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectOptionDict, selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOARD_LINES,
    CONF_BOARD_STOP,
    CONF_BOARD_STOP_ID,
    CONF_BOARD_STOP_NAME,
    CONF_TRIP_WAIT_TIME,
    DEFAULT_BOARD_WAIT_TIME,
    MAX_TRIP_WAIT_TIME,
    SUBENTRY_TYPE_BOARD,
)
from .departure_board import (
    DepartureBoardAuthError,
    DepartureBoardError,
    DepartureBoardRateLimitError,
    get_departures,
)
from .helpers import check_stops
from .network import load_network

_LOGGER = logging.getLogger(__name__)


class DepartureBoardSubEntryFlowHandler(ConfigSubentryFlow):
    """Handle a departure board subentry flow for Transport NSW MK II"""

    _board_data: dict[str, Any]

    async def _find_stop(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        """Work out which stop the user means - return its details, or an error."""
        network = await self.hass.async_add_executor_job(load_network)
        station = network.named(text)

        if station is not None:
            # Only mention the transport type if there's another stop with the same name
            shared_name = [
                other for other in network.stations if other.name == station.name
            ]
            return {
                CONF_BOARD_STOP_ID: station.id,
                CONF_BOARD_STOP_NAME: (
                    station.label if len(shared_name) > 1 else station.name
                ),
            }, None

        # It's not in the bundled list, so let the API try and find it
        try:
            stop_data = await self.hass.async_add_executor_job(
                check_stops, self._get_entry().data[CONF_API_KEY], [text]
            )

        except InvalidAPIKey:
            return None, "invalidapikey"

        except APIRateLimitExceeded:
            return None, "apiratelimitexceeded"

        except Exception:
            return None, "stoperror"

        stop = stop_data["stop_list"][0]
        if not stop_data.get("all_stops_valid") or not stop.get("valid"):
            return None, "stoperror"

        return {
            CONF_BOARD_STOP_ID: stop["stop_id"],
            CONF_BOARD_STOP_NAME: stop["stop_detail"]["disassembledName"],
        }, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose the stop."""
        errors: dict[str, str] = {}

        if user_input is not None:
            stop, error = await self._find_stop(user_input[CONF_BOARD_STOP])

            if error is not None:
                errors["base"] = error
            elif any(
                subentry.unique_id == f"board_{stop[CONF_BOARD_STOP_ID]}"
                for subentry in self._get_entry().subentries.values()
            ):
                errors["base"] = "already_configured"
            else:
                self._board_data = stop
                return await self.async_step_filter()

        network = await self.hass.async_add_executor_job(load_network)

        BOARD_STOP_SCHEMA = vol.Schema(
            {
                vol.Required(
                    CONF_BOARD_STOP,
                    default=(user_input or {}).get(CONF_BOARD_STOP, ""),
                ): selector(
                    {
                        "select": {
                            "options": [station.label for station in network.stations],
                            "mode": "dropdown",
                            "custom_value": True,
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=BOARD_STOP_SCHEMA,
            errors=errors,
            last_step=False,
        )

    async def async_step_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose which lines to show and how soon a departure can be."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._board_data.update(
                {
                    CONF_BOARD_LINES: user_input.get(CONF_BOARD_LINES, []),
                    CONF_TRIP_WAIT_TIME: user_input[CONF_TRIP_WAIT_TIME],
                }
            )
            config_entry = self._get_entry()

            if self.source == SOURCE_RECONFIGURE:
                return self.async_update_reload_and_abort(
                    config_entry,
                    self._get_reconfigure_subentry(),
                    data=self._board_data,
                )

            self.hass.config_entries.async_add_subentry(
                config_entry,
                ConfigSubentry(
                    data=self._board_data,
                    subentry_type=SUBENTRY_TYPE_BOARD,
                    title=self._board_data[CONF_BOARD_STOP_NAME],
                    unique_id=f"board_{self._board_data[CONF_BOARD_STOP_ID]}",
                ),
            )

            # There's no update listener in place, so force a reload to create the sensors
            await self.hass.config_entries.async_reload(config_entry.entry_id)

            return self.async_abort(
                reason="board_created",
                description_placeholders={
                    "stop_name": self._board_data[CONF_BOARD_STOP_NAME]
                },
            )

        # Offer the lines that are running from the stop at the moment
        lines: dict[str, str] = {}
        try:
            departures = await get_departures(
                async_get_clientsession(self.hass),
                self._get_entry().data[CONF_API_KEY],
                self._board_data[CONF_BOARD_STOP_ID],
                dt_util.utcnow(),
            )
            for departure in departures:
                lines.setdefault(departure["line"], departure["line_name"])

        except DepartureBoardAuthError:
            errors["base"] = "invalidapikey"

        except DepartureBoardRateLimitError:
            errors["base"] = "apiratelimitexceeded"

        except DepartureBoardError as ex:
            # Not fatal - line names can still be typed in
            _LOGGER.warning(f"Error {ex} retrieving the lines for a departure board")

        # Keep any lines that were chosen previously, even if they aren't running right now
        saved_lines = self._board_data.get(CONF_BOARD_LINES, [])
        for line in saved_lines:
            lines.setdefault(line, line)

        BOARD_FILTER_SCHEMA = vol.Schema(
            {
                vol.Optional(CONF_BOARD_LINES, default=saved_lines): selector(
                    {
                        "select": {
                            "options": [
                                SelectOptionDict(value=line, label=line_name)
                                for line, line_name in sorted(lines.items())
                                if line
                            ],
                            "mode": "dropdown",
                            "custom_value": True,
                            "multiple": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_TRIP_WAIT_TIME,
                    default=self._board_data.get(
                        CONF_TRIP_WAIT_TIME, DEFAULT_BOARD_WAIT_TIME
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_TRIP_WAIT_TIME)),
            }
        )

        return self.async_show_form(
            step_id="filter",
            data_schema=BOARD_FILTER_SCHEMA,
            description_placeholders={
                "stop_name": self._board_data[CONF_BOARD_STOP_NAME]
            },
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the lines and wait time of an existing departure board."""
        self._board_data = dict(self._get_reconfigure_subentry().data)

        return await self.async_step_filter()
