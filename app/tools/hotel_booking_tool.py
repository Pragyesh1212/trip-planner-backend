"""Create a hotel booking using LiteAPI's booking endpoint.

This endpoint is intended for the project's LiteAPI sandbox credentials.
The ACC_CREDIT_CARD method simulates the sandbox payment path; do not use
this implementation with production credentials.
"""
from __future__ import annotations

import os
from typing import Any

import requests

LITEAPI_BOOK_URL = "https://book.liteapi.travel/v3.0"


def book_hotel(prebook_id: str, first_name: str, last_name: str, email: str, phone: str) -> dict[str, Any]:
    prebook_id = (prebook_id or "").strip()
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    email = (email or "").strip()
    phone = (phone or "").strip()
    if not all((prebook_id, first_name, last_name, email, phone)):
        return {"success": False, "message": "All guest details and a valid prebook ID are required.", "error": "Missing required booking fields."}

    api_key = os.getenv("LITEAPI_API_KEY", "").strip()
    if not api_key:
        return {"success": False, "message": "Hotel booking is not configured.", "error": "LITEAPI_API_KEY is missing."}

    payload = {
        "prebookId": prebook_id,
        "holder": {"firstName": first_name, "lastName": last_name, "email": email, "phone": phone},
        "guests": [{"occupancyNumber": 1, "firstName": first_name, "lastName": last_name, "email": email, "phone": phone}],
        "payment": {"method": "ACC_CREDIT_CARD"},
    }
    try:
        response = requests.post(
            f"{LITEAPI_BOOK_URL}/rates/book",
            headers={"X-Api-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.ok or (isinstance(body, dict) and body.get("error")):
            err = body.get("error", {}) if isinstance(body, dict) else {}
            message = (err.get("description") or err.get("message") if isinstance(err, dict) else str(err)) or f"LiteAPI booking failed (HTTP {response.status_code})."
            print("LiteAPI hotel booking failed:", {"status": response.status_code, "message": str(message)[:500]})
            return {"success": False, "message": str(message), "error": str(message)}
    except requests.Timeout:
        return {"success": False, "message": "LiteAPI booking timed out. Check LiteAPI before retrying to avoid duplicate bookings.", "error": "Request timed out."}
    except requests.RequestException as exc:
        return {"success": False, "message": "Could not contact LiteAPI to complete the booking.", "error": str(exc)}

    data = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}
    booking_id = data.get("bookingId") or data.get("booking_id")
    if not booking_id:
        return {"success": False, "message": "LiteAPI did not return a booking ID. Check the provider response before retrying.", "error": "Missing bookingId in response."}

    hotel = data.get("hotel") if isinstance(data.get("hotel"), dict) else {}
    price = data.get("price")
    currency = data.get("currency") or "INR"
    if isinstance(price, dict):
        currency = price.get("currency") or currency
        price = price.get("amount") or price.get("total")
    return {
        "success": True,
        "message": "LiteAPI returned a hotel booking confirmation.",
        "booking_id": str(booking_id),
        "hotel_confirmation_code": data.get("hotelConfirmationCode") or data.get("confirmationCode"),
        "status": data.get("status"),
        "price": price,
        "currency": currency,
        "checkin": data.get("checkin"),
        "checkout": data.get("checkout"),
        "hotel_name": hotel.get("name") or data.get("hotelName"),
    }
