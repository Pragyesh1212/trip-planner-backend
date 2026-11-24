
from pydantic import BaseModel


class PlannerMessageRequest(BaseModel):
    message: str
    conversation_id: str = "default"
    start_date: str | None = None
    end_date: str | None = None


class SuggestedDateRange(BaseModel):
    start_date: str
    end_date: str


class PlannerMessageResponse(BaseModel):
    message: str
    response: str
    next_step: str | None = None
    weather: dict | None = None
    hotels: list[dict] | None = None
    flights: list[dict] | None = None
    outbound_flights: list[dict] | None = None
    return_flights: list[dict] | None = None
    flight_direction: str | None = None
    selected_outbound_flight: dict | None = None
    selected_return_flight: dict | None = None
    suggested_dates: list[SuggestedDateRange] = []
