from pydantic import BaseModel, Field


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

    # Existing response fields
    weather: dict | None = None
    hotels: list[dict] = Field(default_factory=list)
    flights: list[dict] | None = None
    outbound_flights: list[dict] | None = None
    return_flights: list[dict] | None = None
    flight_direction: str | None = None
    selected_flight: dict | None = None
    selected_outbound_flight: dict | None = None
    selected_return_flight: dict | None = None
    suggested_dates: list[SuggestedDateRange] = Field(default_factory=list)

    # Structured fields for the next frontend integration
    action: dict | None = None
    trip_summary: dict | None = None
    budget_breakdown: dict | None = None
    pending_confirmation: dict | None = None
