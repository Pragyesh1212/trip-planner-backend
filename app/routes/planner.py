from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Any

from app.dependencies.authentication import get_current_user_id
from app.database import db
from app.schemas.planner_schema import PlannerMessageRequest
from app.controllers.planner_controller import planner_message_controller
from app.tools.hotel_tool import search_hotels
from app.tools.hotel_prebook_tool import prebook_hotel_offer
from app.tools.hotel_booking_tool import book_hotel


class HotelSearchRequest(BaseModel):
    destination: str
    checkin: str
    checkout: str
    adults: int = 1
    currency: str = "INR"
    guest_nationality: str = "IN"
    limit: int = 8


class HotelPrebookRequest(BaseModel):
    offer_id: str


class HotelBookingRequest(BaseModel):
    prebook_id: str
    first_name: str
    last_name: str
    email: str
    phone: str


router = APIRouter(prefix="/planner", tags=["Planner"])
conversation_collection = db["planner_conversations"]
hotel_bookings_collection = db["hotel_bookings"]


class ConversationSaveRequest(BaseModel):
    conversation_id: str
    title: str = "New Trip"
    messages: list[dict[str, Any]] = Field(default_factory=list)


def _owned_conversation(user_id: str, conversation_id: str):
    return conversation_collection.find_one({
        "user_id": str(user_id),
        "conversation_id": conversation_id,
    })


@router.post("/message")
def planner_message(
    request: PlannerMessageRequest,
    user_id: str = Depends(get_current_user_id),
):
    # Scope the LangGraph thread to the authenticated owner. A user cannot
    # access another user's state by guessing their conversation_id.
    scoped_request = request.model_copy(
        update={"conversation_id": f"{user_id}:{request.conversation_id}"}
    )
    return planner_message_controller(scoped_request, str(user_id))


@router.get("/history")
def list_conversations(user_id: str = Depends(get_current_user_id)):
    docs = conversation_collection.find(
        {"user_id": str(user_id)},
        {"_id": 0, "conversation_id": 1, "title": 1, "updated_at": 1, "messages": 1},
    ).sort("updated_at", -1)
    result = []
    for doc in docs:
        result.append({
            "conversation_id": doc["conversation_id"],
            "title": doc.get("title", "New Trip"),
            "updated_at": doc.get("updated_at"),
            "messages": doc.get("messages", []),
        })
    return {"conversations": result}


@router.get("/history/{conversation_id}")
def get_conversation(
    conversation_id: str,
    user_id: str = Depends(get_current_user_id),
):
    doc = _owned_conversation(str(user_id), conversation_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "conversation_id": doc["conversation_id"],
        "title": doc.get("title", "New Trip"),
        "messages": doc.get("messages", []),
    }


@router.put("/history")
def save_conversation(
    request: ConversationSaveRequest,
    user_id: str = Depends(get_current_user_id),
):
    messages = request.messages[-500:]
    title = (request.title or "New Trip").strip()[:100] or "New Trip"
    conversation_collection.update_one(
        {"user_id": str(user_id), "conversation_id": request.conversation_id},
        {"$set": {
            "user_id": str(user_id),
            "conversation_id": request.conversation_id,
            "title": title,
            "messages": messages,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {"success": True}


@router.post("/hotels/search")
def search_hotel_offers(request: HotelSearchRequest):
    """Search LiteAPI for available hotel offers and return their offer IDs."""
    result = search_hotels(
        destination=request.destination,
        checkin=request.checkin,
        checkout=request.checkout,
        adults=request.adults,
        currency=request.currency,
        guest_nationality=request.guest_nationality,
        limit=request.limit,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail={
                "success": False,
                "message": result.get("error", "Hotel search failed."),
            },
        )
    return result


@router.post("/hotels/prebook")
def prebook_hotel(request: HotelPrebookRequest):
    result = prebook_hotel_offer(request.offer_id)
    if not result.get("success"):
        # Make failures visible to Angular's error path instead of returning HTTP 200.
        # This prevents a provider rejection from looking like a successful HTTP call.
        raise HTTPException(
            status_code=502,
            detail={
                "message": result.get("message", "Hotel rate could not be revalidated."),
                "error": result.get("error"),
                "success": False,
            },
        )
    return result


@router.post("/hotels/book")
def confirm_hotel_booking(
    request: HotelBookingRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Confirm a hotel booking and save a user-scoped record after provider success."""
    result = book_hotel(
        prebook_id=request.prebook_id,
        first_name=request.first_name,
        last_name=request.last_name,
        email=request.email,
        phone=request.phone,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail={
                "message": result.get("message", "Hotel booking could not be completed."),
                "error": result.get("error"),
                "success": False,
            },
        )

    # Accept known provider field variants, but never fabricate a booking ID.
    provider_booking_id = (
        result.get("booking_id")
        or result.get("bookingId")
        or result.get("reservation_id")
        or result.get("reservationId")
        or result.get("hotel_booking_id")
    )
    if not provider_booking_id:
        raise HTTPException(
            status_code=502,
            detail={
                "success": False,
                "message": (
                    "The provider reported success but returned no booking ID. "
                    "Verify the booking with LiteAPI before retrying."
                ),
            },
        )

    now = datetime.now(timezone.utc).isoformat()
    booking_record = {
        "user_id": str(user_id),
        "booking_id": str(provider_booking_id),
        "provider": "liteapi",
        "status": (
            result.get("status")
            or result.get("booking_status")
            or result.get("bookingStatus")
            or "unknown"
        ),
        "hotel_name": result.get("hotel_name") or result.get("hotelName") or result.get("name"),
        "hotel_id": result.get("hotel_id") or result.get("hotelId"),
        "confirmation_code": (
            result.get("confirmation_code")
            or result.get("confirmationCode")
            or result.get("hotel_confirmation_code")
        ),
        "checkin": result.get("checkin") or result.get("check_in") or result.get("checkIn"),
        "checkout": result.get("checkout") or result.get("check_out") or result.get("checkOut"),
        "price": result.get("price") or result.get("total_price") or result.get("totalPrice"),
        "currency": (
            result.get("currency")
            or result.get("price_currency")
            or result.get("priceCurrency")
            or "INR"
        ),
        "guest": {
            "first_name": request.first_name,
            "last_name": request.last_name,
            "email": request.email,
            "phone": request.phone,
        },
        "prebook_id": request.prebook_id,
        "created_at": now,
        "updated_at": now,
        "provider_response": result,
    }

    hotel_bookings_collection.update_one(
        {"user_id": str(user_id), "booking_id": str(provider_booking_id)},
        {"$setOnInsert": booking_record},
        upsert=True,
    )

    return result


@router.get("/hotel-bookings")
def get_hotel_bookings(user_id: str = Depends(get_current_user_id)):
    """List only hotel bookings belonging to the authenticated user."""
    bookings = list(
        hotel_bookings_collection.find(
            {"user_id": str(user_id)},
            {"_id": 0, "user_id": 0, "provider_response": 0},
        ).sort("created_at", -1)
    )
    return {"bookings": bookings}


@router.get("/hotel-bookings/{booking_id}")
def get_hotel_booking_details(
    booking_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Return booking details only if the authenticated user owns the booking."""
    booking = hotel_bookings_collection.find_one(
        {"user_id": str(user_id), "booking_id": booking_id},
        {"_id": 0, "user_id": 0},
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Hotel booking not found.")
    return {"booking": booking}


@router.get("/flight-bookings")
def get_flight_bookings(
    user_id: str = Depends(get_current_user_id),
):
    bookings_collection = db["flight_bookings"]

    bookings = list(
        bookings_collection.find(
            {"user_id": str(user_id)}
        ).sort("created_at", -1)
    )

    result = []

    for booking in bookings:
        slices = booking.get("slices") or []

        # The first slice represents the outbound journey.
        outbound = slices[0] if slices else {}
        outbound_segments = outbound.get("segments") or []

        # The last segment handles itineraries with connections.
        first_segment = outbound_segments[0] if outbound_segments else {}
        last_segment = outbound_segments[-1] if outbound_segments else {}

        origin = (
            outbound.get("origin")
            or first_segment.get("origin")
        )
        destination = (
            outbound.get("destination")
            or last_segment.get("destination")
        )

        departure_date = (
            outbound.get("departing_at")
            or first_segment.get("departing_at")
        )

        # The second slice represents the return journey, if present.
        inbound = slices[1] if len(slices) > 1 else {}
        inbound_segments = inbound.get("segments") or []
        inbound_first_segment = (
            inbound_segments[0] if inbound_segments else {}
        )

        return_date = (
            inbound.get("departing_at")
            or inbound_first_segment.get("departing_at")
        )

        result.append({
            "booking_id": str(booking.get("_id")),
            "provider_order_id": booking.get("provider_order_id"),
            "booking_reference": booking.get("booking_reference"),
            "status": booking.get("status", "unknown"),
            "booking_type": booking.get("booking_type"),
            "created_at": booking.get("created_at"),
            "updated_at": booking.get("updated_at"),
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "return_date": return_date,
            "price": booking.get("total_amount"),
            "currency": booking.get("total_currency"),
            "provider": booking.get("provider"),
        })

    return {"bookings": result}
