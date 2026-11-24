"""Tool registry and Gemini function schemas."""

from typing import Callable


def build_tool_registry(
    get_weather: Callable,
    get_flights: Callable,
) -> dict[str, Callable]:
    """Attach Gemini-compatible descriptions/schemas and return the registry."""

    get_weather.tool_description = (
        "Check weather for a destination over an optional date range. "
        "Use this for weather-only requests without requiring flight details."
    )
    get_weather.parameters_schema = {
        "type": "OBJECT",
        "properties": {
            "destination": {
                "type": "STRING",
                "description": "Destination city or place to check weather for.",
            },
            "start_date": {
                "type": "STRING",
                "description": "Optional start date in YYYY-MM-DD format.",
            },
            "end_date": {
                "type": "STRING",
                "description": "Optional end date in YYYY-MM-DD format.",
            },
        },
        "required": ["destination"],
    }

    get_flights.tool_description = (
        "Search available flight offers. Supports one-way, return, and "
        "multi-city journeys, passenger counts, and cabin class. Search only; "
        "this tool does not book or pay for flights."
    )
    get_flights.parameters_schema = {
        "type": "OBJECT",
        "properties": {
            "origin": {
                "type": "STRING",
                "description": "Departure city or airport.",
            },
            "destination": {
                "type": "STRING",
                "description": "Final destination city or airport.",
            },
            "journey_type": {
                "type": "STRING",
                "enum": ["one_way", "return", "multi_city"],
                "description": "Type of journey.",
            },
            "start_date": {
                "type": "STRING",
                "description": (
                    "Departure date in YYYY-MM-DD format. For multi-city, "
                    "may be the first segment departure date."
                ),
            },
            "end_date": {
                "type": "STRING",
                "description": (
                    "Return date in YYYY-MM-DD format; required for return "
                    "journeys and omitted for one-way journeys."
                ),
            },
            "adults": {
                "type": "INTEGER",
                "description": "Number of adult passengers; at least 1.",
            },
            "children": {
                "type": "INTEGER",
                "description": "Number of child passengers; zero or greater.",
            },
            "cabin_class": {
                "type": "STRING",
                "enum": ["economy", "premium_economy", "business", "first"],
                "description": "Requested cabin class.",
            },
            "max_results": {
                "type": "INTEGER",
                "description": "Maximum number of offers to return.",
            },
            "multi_city_segments": {
                "type": "ARRAY",
                "description": (
                    "Required for multi_city. Ordered journey segments, each "
                    "with origin, destination, and departure_date."
                ),
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "origin": {
                            "type": "STRING",
                            "description": "Origin city or airport for this segment.",
                        },
                        "destination": {
                            "type": "STRING",
                            "description": "Destination city or airport for this segment.",
                        },
                        "departure_date": {
                            "type": "STRING",
                            "description": "Segment departure date in YYYY-MM-DD format.",
                        },
                    },
                    "required": ["origin", "destination", "departure_date"],
                },
            },
        },
        "required": ["origin", "destination", "journey_type", "start_date"],
    }

    return {
        "get_weather": get_weather,
        "get_flights": get_flights,
    }
