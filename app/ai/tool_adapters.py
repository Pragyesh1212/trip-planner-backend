"""Adapters exposing existing backend tools to the Gemini agent."""

from typing import Any

from app.tools.flight_tool import flight_tool
from app.tools.weather_tool import weather_tool


def get_weather(
    destination: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Look up weather for a destination and optional travel-date range."""
    return weather_tool(
        location=destination,
        start_date=start_date,
        end_date=end_date,
    )


def get_flights(
    origin: str,
    destination: str,
    journey_type: str = "one_way",
    start_date: str | None = None,
    end_date: str | None = None,
    adults: int = 1,
    children: int = 0,
    cabin_class: str = "economy",
    max_results: int = 5,
    multi_city_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Search Duffel for one-way, return, or multi-city flight offers.

    For multi-city searches, each segment must contain origin, destination,
    and departure_date. The first segment's date is supplied as start_date
    for compatibility with the existing flight_tool function signature.
    """
    if journey_type == "multi_city":
        if not multi_city_segments:
            return {
                "success": False,
                "error": (
                    "multi_city_segments is required for a multi-city search."
                ),
            }
        first_segment = multi_city_segments[0]
        start_date = start_date or first_segment.get("departure_date") or first_segment.get("date")

    if not start_date:
        return {
            "success": False,
            "error": "start_date is required for flight search.",
        }

    return flight_tool(
        origin=origin,
        destination=destination,
        start_date=start_date,
        end_date=end_date,
        cabin_class=cabin_class,
        max_results=max_results,
        journey_type=journey_type,
        adults=adults,
        children=children,
        multi_city_segments=multi_city_segments,
    )
