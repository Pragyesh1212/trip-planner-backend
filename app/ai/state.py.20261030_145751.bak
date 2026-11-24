from typing import TypedDict


class TripState(TypedDict):
    user_id: str
    conversation_id: str
    messages: list

    travel_intent: bool | None

    origin: str | None
    destination: str | None

    start_date: str | None
    end_date: str | None

    budget: float | None

    weather: dict | None
    weather_confirmed: bool | None

    suggested_dates: list | None

    # Existing flight fields
    flights: list | None
    selected_flight: dict | None

    # Separate outbound and return flight state
    outbound_flights: list | None
    return_flights: list | None
    selected_outbound_flight: dict | None
    selected_return_flight: dict | None

    # Duffel test booking state
    passenger_details: dict | None
    booking_order: dict | None
    booking_status: str | None

    conversation_stage: str | None
    flight_direction: str | None

    next_step: str | None
    extracted_info: str | None

    assistant_response: str | None