from typing import TypedDict


class TripState(TypedDict, total=False):
    # Identity and conversation
    user_id: str
    conversation_id: str
    messages: list

    # Trip requirements
    travel_intent: bool | None
    origin: str | None
    destination: str | None
    start_date: str | None
    end_date: str | None
    budget: float | None
    traveller_count: int | None
    
    # Passenger information
    adults: int | None
    children: int | None
    child_ages: list[int] | None
    passenger_count_confirmed: bool | None

    # Weather
    weather: dict | None
    weather_confirmed: bool | None
    suggested_dates: list | None

    # Flights
    flights: list | None
    selected_flight: dict | None
    outbound_flights: list | None
    return_flights: list | None
    selected_outbound_flight: dict | None
    selected_return_flight: dict | None
    flight_direction: str | None

    # Flight booking
    passenger_details: dict | None
    booking_order: dict | None
    booking_status: str | None
    booking_payment_status: str | None
    booking_persistence_error: bool | None

    
    # Hotels
    hotel_results: list | None
    hotel_search: dict | None
    hotel_adults: int | None
    selected_hotel: dict | None
    hotel_prebook: dict | None
    hotel_booking: dict | None
    hotel_booking_skipped: bool | None
    hotel_booking_attempt_status: str | None


    # Budget accounting and explicit approvals
    estimated_expenses: dict | None
    committed_expenses: dict | None
    budget_override_pending: bool | None
    budget_override_confirmed: bool | None
    pending_confirmation: dict | None

    # Orchestration and response
    conversation_stage: str | None
    next_step: str | None
    extracted_info: str | None
    assistant_response: str | None
    action: dict | None
    

