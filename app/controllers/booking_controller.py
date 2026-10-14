
from fastapi import HTTPException

from app.schemas.booking_schema import FlightBookingRequest
from app.services.booking_service import create_flight_booking


def booking_controller(
    request: FlightBookingRequest,
    user_id: str,
) -> dict:
    passengers = [
        passenger.model_dump(mode="json")
        for passenger in request.passengers
    ]

    return create_flight_booking(
        user_id=user_id,
        offer_id=request.offer_id,
        passengers=passengers,
    )
