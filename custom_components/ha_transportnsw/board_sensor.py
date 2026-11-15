"""Departure board sensors for the Transport NSW Mk II integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import UnitOfTime
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SUBENTRY_TYPE_BOARD, TFNSW_ATTRIBUTION
from .departure_board import HIGH_PRIORITIES, TransportNSWBoardCoordinator


@dataclass(frozen=True, kw_only=True)
class BoardSensorEntityDescription(SensorEntityDescription):
    """Describes a departure board sensor."""

    # Which upcoming departure this sensor describes - 0 is the next one
    index: int = 0
    value_fn: Callable[[dict[str, Any]], datetime | int | str | None]


def get_due(departure: dict[str, Any]) -> int:
    """Return how many minutes away a departure is."""
    seconds = (departure["departure_time"] - dt_util.utcnow()).total_seconds()
    return max(0, round(seconds / 60))


def get_occupancy(departure: dict[str, Any]) -> str | None:
    """Return the occupancy in a friendly format, eg 'Many seats'."""
    occupancy = departure["occupancy"]
    return occupancy.replace("_", " ").capitalize() if occupancy else None


BOARD_SENSORS: tuple[BoardSensorEntityDescription, ...] = (
    BoardSensorEntityDescription(
        key="due",
        name="Due",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=get_due,
    ),
    BoardSensorEntityDescription(
        key="departure_time",
        name="Departure time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda departure: departure["departure_time"],
    ),
    BoardSensorEntityDescription(
        key="departure_time_2",
        name="Following departure time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        index=1,
        value_fn=lambda departure: departure["departure_time"],
    ),
    BoardSensorEntityDescription(
        key="departure_time_3",
        name="Third departure time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        index=2,
        value_fn=lambda departure: departure["departure_time"],
    ),
    BoardSensorEntityDescription(
        key="line_name_short",
        name="Line",
        icon="mdi:routes",
        value_fn=lambda departure: departure["line"],
    ),
    BoardSensorEntityDescription(
        key="destination_name",
        name="Destination",
        icon="mdi:map-marker-right",
        value_fn=lambda departure: departure["destination"],
    ),
    BoardSensorEntityDescription(
        key="platform",
        name="Platform",
        icon="mdi:sign-direction",
        value_fn=lambda departure: departure["platform"],
    ),
    BoardSensorEntityDescription(
        key="delay",
        name="Delay",
        icon="mdi:clock-alert-outline",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda departure: departure["delay"],
    ),
    BoardSensorEntityDescription(
        key="occupancy",
        name="Occupancy",
        icon="mdi:seat-passenger",
        value_fn=get_occupancy,
    ),
)


def setup_board_sensors(
    config_entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create the sensors for each departure board subentry."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_BOARD:
            continue

        coordinator = config_entry.runtime_data.board_coordinators.get(
            subentry.subentry_id
        )
        if coordinator is None:
            continue

        async_add_entities(
            [
                *(
                    TransportNSWBoardSensor(coordinator, description, subentry)
                    for description in BOARD_SENSORS
                ),
                TransportNSWBoardAlertsSensor(coordinator, subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )


class TransportNSWBoardEntity(CoordinatorEntity[TransportNSWBoardCoordinator]):
    """Base class for departure board entities."""

    _attr_has_entity_name = True
    _attr_attribution = TFNSW_ATTRIBUTION

    def __init__(
        self,
        coordinator: TransportNSWBoardCoordinator,
        subentry: ConfigSubentry,
        key: str,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self.subentry = subentry
        self._attr_unique_id = f"{subentry.subentry_id}_board_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{subentry.subentry_id}_board")},
            name=subentry.title,
            manufacturer="Transport for NSW",
            entry_type=DeviceEntryType.SERVICE,
        )


class TransportNSWBoardSensor(TransportNSWBoardEntity, SensorEntity):
    """A sensor describing one of the next departures from a stop."""

    entity_description: BoardSensorEntityDescription

    def __init__(
        self,
        coordinator: TransportNSWBoardCoordinator,
        description: BoardSensorEntityDescription,
        subentry: ConfigSubentry,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, subentry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> datetime | int | str | None:
        """Return the value of the sensor."""
        departures = self.coordinator.data
        if len(departures) <= self.entity_description.index:
            return None

        return self.entity_description.value_fn(
            departures[self.entity_description.index]
        )


class TransportNSWBoardAlertsSensor(TransportNSWBoardEntity, SensorEntity):
    """The number of alerts across the departures on a board."""

    _attr_name = "Alerts"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(
        self, coordinator: TransportNSWBoardCoordinator, subentry: ConfigSubentry
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, subentry, "alerts")

    @property
    def _alerts(self) -> list[dict[str, Any]]:
        alerts: dict[str, dict[str, Any]] = {}
        for departure in self.coordinator.data:
            for alert in departure["alerts"]:
                alerts.setdefault(alert["title"], alert)

        return list(alerts.values())

    @property
    def native_value(self) -> int:
        """Return the number of distinct alerts."""
        return len(self._alerts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the alerts themselves."""
        alerts = self._alerts
        return {
            "alerts": alerts,
            "high_priority": any(
                alert["priority"] in HIGH_PRIORITIES for alert in alerts
            ),
        }

