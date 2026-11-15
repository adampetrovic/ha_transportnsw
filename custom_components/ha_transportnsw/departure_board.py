"""Departure boards for the Transport NSW Mk II integration.

A departure board is everything leaving one stop, where a journey is the next
trip between two stops.  The trip planner's departure_mon call isn't part of
the TransportNSWv2 library, so it's called from here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOARD_LINES,
    CONF_BOARD_STOP_ID,
    CONF_TRIP_WAIT_TIME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DEPARTURES_URL = "https://api.transport.nsw.gov.au/v1/tp/departure_mon"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
HIGH_PRIORITIES = {"high", "veryHigh"}


class DepartureBoardError(Exception):
    """The departure board couldn't be retrieved."""


class DepartureBoardAuthError(DepartureBoardError):
    """The API key was rejected."""


class DepartureBoardRateLimitError(DepartureBoardError):
    """The API rate limit was hit."""


def _parse_time(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


def _parse_alerts(infos: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    alerts = []
    for info in infos or []:
        properties = info.get("properties", {})
        links = info.get("infoLinks") or [{}]
        title = properties.get("smsText") or links[0].get("urlText")
        if title:
            alerts.append(
                {
                    "title": title,
                    "priority": info.get("priority", "normal"),
                    "url": links[0].get("url"),
                }
            )
    return alerts


def parse_departure(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one stop event from the API into a departure."""
    planned = _parse_time(event.get("departureTimePlanned"))
    if planned is None:
        return None

    estimated = _parse_time(event.get("departureTimeEstimated"))
    location = event.get("location", {})
    location_properties = location.get("properties", {})
    transport = event.get("transportation", {})
    line = transport.get("disassembledName") or transport.get("number") or ""
    occupancy = location_properties.get("occupancy")

    return {
        "departure_time": estimated or planned,
        "planned_time": planned,
        "delay": (
            None
            if estimated is None
            else round((estimated - planned).total_seconds() / 60)
        ),
        "line": line,
        "line_name": transport.get("number") or line,
        "destination": transport.get("destination", {}).get("name", ""),
        "transport_type": transport.get("product", {}).get("class", 0),
        "platform": location_properties.get("platformName"),
        "realtime": estimated is not None,
        "cancelled": bool(event.get("isCancelled"))
        or any("CANCEL" in status for status in event.get("realtimeStatus") or []),
        "occupancy": occupancy.upper() if occupancy else None,
        "alerts": _parse_alerts(event.get("infos")),
    }


async def get_departures(
    session: aiohttp.ClientSession, api_key: str, stop_id: str, when: datetime
) -> list[dict[str, Any]]:
    """Return the departures from a stop at or after a time, soonest first.

    The API returns at most 40 services.
    """
    # Ask from slightly in the past so a service that's boarding is included
    local = when.astimezone(SYDNEY_TZ) - timedelta(minutes=1)
    params = {
        "outputFormat": "rapidJSON",
        "coordOutputFormat": "EPSG:4326",
        "version": "10.2.1.42",
        "mode": "direct",
        "type_dm": "stop",
        "name_dm": stop_id,
        "depArrMacro": "dep",
        "TfNSWDM": "true",
        "itdDate": local.strftime("%Y%m%d"),
        "itdTime": local.strftime("%H%M"),
    }

    try:
        async with session.get(
            DEPARTURES_URL,
            params=params,
            headers={"Authorization": f"apikey {api_key}"},
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status in (401, 403):
                raise DepartureBoardAuthError("Invalid API key")
            if response.status == 429:
                raise DepartureBoardRateLimitError("API rate limit exceeded")
            response.raise_for_status()
            data = await response.json(content_type=None)

    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise DepartureBoardError(str(err) or type(err).__name__) from err

    if not isinstance(data, dict):
        raise DepartureBoardError("Unexpected response")

    departures = [
        departure
        for event in data.get("stopEvents", [])
        if (departure := parse_departure(event)) is not None
    ]
    departures.sort(key=lambda departure: departure["departure_time"])

    return departures


def departure_as_response(departure: dict[str, Any]) -> dict[str, Any]:
    """Return a departure in a form that can be sent as an action response."""
    return departure | {
        "departure_time": departure["departure_time"].isoformat(),
        "planned_time": departure["planned_time"].isoformat(),
    }


class TransportNSWBoardCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Coordinator for the departures from one stop."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        """Initialise the coordinator."""
        self.subentry = subentry
        self.journey_coordinator = config_entry.runtime_data.coordinator

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} ({subentry.title})",
            update_interval=self.journey_coordinator.update_interval,
        )

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Fetch the departure board from the TfNSW API endpoint."""
        # Follow the journey coordinator's interval, which may be throttled to stay within the daily API limit
        self.update_interval = self.journey_coordinator.update_interval

        earliest = dt_util.utcnow() + timedelta(
            minutes=self.subentry.data.get(CONF_TRIP_WAIT_TIME, 0)
        )

        try:
            departures = await get_departures(
                async_get_clientsession(self.hass),
                self.config_entry.data[CONF_API_KEY],
                self.subentry.data[CONF_BOARD_STOP_ID],
                earliest,
            )

        except DepartureBoardError as ex:
            raise UpdateFailed(
                f"Error communicating with API for departure board {self.subentry.title}: {ex}"
            ) from ex

        finally:
            # Each board poll is one API call - make sure it's counted against the daily limit
            self.journey_coordinator.daily_api_calls += 1
            self.journey_coordinator.board_api_calls += 1

        lines = self.subentry.data.get(CONF_BOARD_LINES, [])

        return [
            departure
            for departure in departures
            if not departure["cancelled"]
            and departure["departure_time"] >= earliest
            and (not lines or departure["line"] in lines)
        ]
