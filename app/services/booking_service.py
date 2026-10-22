from datetime import datetime, timezone

import requests
from fastapi import HTTPException
from pymongo.errors import PyMongoError

from app.config import DUFFEL_API_KEY
from app.database import db


DUFFEL_API_URL = "https://api.duffel.com"


def get_duffel_headers() -> dict:
    if not DUFFEL_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Duffel API key is not configured."
        )

    return {
        "Authorization": f"Bearer {DUFFEL_API_KEY}",
        "Duffel-Version": "v2",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _airport_summary(airport: dict | None) -> dict | None:
    """Return a JSON-friendly airport summary from Duffel data."""
    if not isinstance(airport, dict):
        return None

    return {
        "id": airport.get("id"),
        "name": airport.get("name"),
        "iata_code": airport.get("iata_code"),
        "iata_city_code": airport.get("iata_city_code"),
        "city_name": airport.get("city_name"),
        "iata_country_code": airport.get("iata_country_code"),
        "time_zone": airport.get("time_zone"),
    }



def _extract_trip_details(slices: list) -> dict:
    """
    Extract outbound and optional return details from Duffel slices.
    Preserve the field names expected by the flight-bookings endpoint,
    while retaining the existing departure_date and arrival_date fields.
    """
    if not isinstance(slices, list) or not slices:
        return {
            "origin": None,
            "destination": None,
            "departure_date": None,
            "return_date": None,
            "slices": [],
        }

    normalized_slices = []

    for flight_slice in slices:
        if not isinstance(flight_slice, dict):
            continue

        segments = flight_slice.get("segments") or []
        first_segment = segments[0] if segments else {}
        last_segment = segments[-1] if segments else {}

        origin = (
            flight_slice.get("origin")
            or first_segment.get("origin")
        )

        destination = (
            flight_slice.get("destination")
            or last_segment.get("destination")
        )

        departure = (
            flight_slice.get("departing_at")
            or flight_slice.get("departure_date")
            or first_segment.get("departing_at")
            or first_segment.get("departure_date")
        )

        arrival = (
            flight_slice.get("arriving_at")
            or flight_slice.get("arrival_date")
            or last_segment.get("arriving_at")
            or last_segment.get("arrival_date")
        )

        normalized_slices.append({
            "origin": _airport_summary(origin),
            "destination": _airport_summary(destination),

            # Expected by the existing /flight-bookings endpoint
            "departing_at": departure,
            "arriving_at": arrival,

            # Retain the existing field names for compatibility
            "departure_date": departure,
            "arrival_date": arrival,

            # Preserve flight segments for expanded booking details
            "segments": segments,
        })

    if not normalized_slices:
        return {
            "origin": None,
            "destination": None,
            "departure_date": None,
            "return_date": None,
            "slices": [],
        }

    outbound = normalized_slices[0]
    inbound = (
        normalized_slices[1]
        if len(normalized_slices) > 1
        else None
    )

    return {
        "origin": outbound.get("origin"),
        "destination": outbound.get("destination"),
        "departure_date": outbound.get("departing_at"),
        "return_date": (
            inbound.get("departing_at")
            if inbound
            else None
        ),
        "slices": normalized_slices,
    }



def create_flight_booking(
    user_id: str,
    offer_id: str,
    passengers: list[dict],
) -> dict:
    headers = get_duffel_headers()

    # Step 1: Verify that the selected offer still exists.
    try:
        offer_response = requests.get(
            f"{DUFFEL_API_URL}/air/offers/{offer_id}",
            headers=headers,
            timeout=20,
        )
    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to the flight booking provider."
        )

    if offer_response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="The selected flight offer was not found."
        )

    if not offer_response.ok:
        raise HTTPException(
            status_code=502,
            detail="Could not verify the selected flight offer."
        )

    offer = offer_response.json().get("data", {})

    if offer.get("live_mode") is True:
        raise HTTPException(
            status_code=400,
            detail=(
                "This endpoint is for sandbox testing only. "
                "Live flight orders are not supported."
            ),
        )

    expires_at = offer.get("expires_at")

    if expires_at:
        try:
            expiry = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            )
            if expiry <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=400,
                    detail="This flight offer has expired. Search again."
                )
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail="The provider returned an invalid offer expiry."
            )

    requirements = offer.get("payment_requirements") or {}

    if requirements.get("requires_instant_payment") is True:
        raise HTTPException(
            status_code=400,
            detail=(
                "This offer requires immediate payment. "
                "The hold-booking endpoint cannot process it."
            ),
        )

    # Duffel requires each order passenger to reference the passenger ID
    # attached to the selected offer. Do not send blank or invented IDs.
    offer_passengers = offer.get("passengers") or []
    if len(offer_passengers) < len(passengers):
        raise HTTPException(
            status_code=400,
            detail=(
                f"The selected offer has {len(offer_passengers)} passenger slot(s), "
                f"but {len(passengers)} passenger record(s) were supplied. "
                "Search for a fresh offer with the correct passenger count."
            ),
        )

    order_passengers = []
    for index, passenger in enumerate(passengers):
        passenger_id = offer_passengers[index].get("id")
        if not passenger_id:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Duffel did not return a passenger ID for this offer. "
                    "Search for a fresh offer before retrying."
                ),
            )
        order_passengers.append({**passenger, "id": passenger_id})

    # Step 2: Request a hold order. A hold is not a completed paid booking.
    payload = {
        "data": {
            "selected_offers": [offer_id],
            "passengers": order_passengers,
            "type": "hold",
            "metadata": {
                "trip_planner_user_id": str(user_id)
            },
        }
    }

    try:
        order_response = requests.post(
            f"{DUFFEL_API_URL}/air/orders",
            headers=headers,
            json=payload,
            timeout=30,
        )
    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail=(
                "The booking provider could not be reached. "
                "Check the provider dashboard before retrying."
            ),
        )

    if not order_response.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "The provider could not create the hold order.",
                "provider_status": order_response.status_code,
                "provider_error": order_response.json()
                if order_response.headers.get("content-type", "").startswith(
                    "application/json"
                )
                else None,
            },
        )

    order = order_response.json().get("data", {})
    now = datetime.now(timezone.utc)

    # Prefer details returned on the created order, falling back to the
    # verified offer when the order response omits slices or price fields.
    trip_details = _extract_trip_details(
        order.get("slices") or offer.get("slices") or []
    )
    total_amount = order.get("total_amount")
    if total_amount is None:
        total_amount = offer.get("total_amount")

    total_currency = order.get("total_currency")
    if total_currency is None:
        total_currency = offer.get("total_currency")

    booking_record = {
    "user_id": str(user_id),
    "offer_id": offer_id,
    "provider_order_id": order.get("id"),
    "booking_reference": order.get("booking_reference"),
    "status": order.get("status", "unknown"),
    "booking_type": "hold",

    "origin": trip_details["origin"],
    "destination": trip_details["destination"],
    "departure_date": trip_details["departure_date"],
    "return_date": trip_details["return_date"],
    "slices": trip_details["slices"],

    "total_amount": total_amount,
    "total_currency": total_currency,
    "provider": "duffel",

    # Save passenger information in MongoDB
    "passenger_details": passengers,

    "created_at": now,
    "updated_at": now,
}

    # Step 3: Save the provider order reference and flight details in MongoDB.
    try:
        result = db["flight_bookings"].insert_one(booking_record)
    except PyMongoError:
        # The provider order may already exist; do not blindly recreate it.
        raise HTTPException(
            status_code=500,
            detail=(
                "The provider returned an order, but saving it failed. "
                "Check the provider dashboard before retrying."
            ),
        )

    return {
        "success": True,
        "message": "Flight hold order created.",
        "booking_id": str(result.inserted_id),
        "provider_order_id": order.get("id"),
        "booking_reference": order.get("booking_reference"),
        "status": order.get("status", "unknown"),
        "booking_type": "hold",
    }
