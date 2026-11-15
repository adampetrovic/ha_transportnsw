"""The bundled list of stations, wharves and light rail stops.

The trip planner API can search for a stop by name but can't list the stops
a line serves, so that list ships with the integration.  It's generated from
the Transport for NSW GTFS timetables and only drives the choices offered when
adding a journey or a departure board - bus stops aren't in it.

network.json holds one [stop ID, name, transport type, lines] entry per stop.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

TRAIN, METRO = 1, 2
MODE_NAMES = {1: "Train", 2: "Metro", 4: "Light rail", 9: "Ferry"}

# Train and metro stations are one network as far as a traveller is concerned
_RAIL = frozenset({TRAIN, METRO})


@dataclass(frozen=True, slots=True)
class Station:
    """A station, wharf or light rail stop and the lines that serve it."""

    id: str
    name: str
    mode: int
    lines: frozenset[str]

    @property
    def label(self) -> str:
        """Return the name with its transport type, eg 'Circular Quay (Ferry)'."""
        return f"{self.name} ({MODE_NAMES[self.mode]})"

    @property
    def modes(self) -> frozenset[int]:
        """Return the transport types that share a network with this stop."""
        return _RAIL if self.mode in _RAIL else frozenset({self.mode})


class Network:
    """Lookups over the bundled stations."""

    def __init__(self, stations: Iterable[Station]) -> None:
        """Initialise the network."""
        self.stations = tuple(stations)

    def named(
        self, text: str, stations: Iterable[Station] | None = None
    ) -> Station | None:
        """Return the one station with this name or label, if there is exactly one."""
        wanted = text.strip().casefold()
        matches = [
            station
            for station in (self.stations if stations is None else stations)
            if wanted in (station.name.casefold(), station.label.casefold())
        ]
        return matches[0] if len(matches) == 1 else None

    def destinations(self, origin: Station) -> list[Station]:
        """Return where you could go from a station without changing network.

        Stations that share a line with the origin are listed first.
        """
        return sorted(
            (
                station
                for station in self.stations
                if station.mode in origin.modes and station.id != origin.id
            ),
            key=lambda station: (not station.lines & origin.lines, station.name),
        )


@cache
def load_network() -> Network:
    """Read the bundled stations.  This reads a file, so call it in an executor."""
    data = json.loads((Path(__file__).parent / "network.json").read_text("utf-8"))
    return Network(
        Station(id=stop_id, name=name, mode=mode, lines=frozenset(lines))
        for stop_id, name, mode, lines in data["stations"]
    )
