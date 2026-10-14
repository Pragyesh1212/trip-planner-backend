from app.ai.graph import (
    trip_graph,
    classify_flight_selection,
    classify_chat_intent,
)
from app.tools.flight_tool import flight_tool, create_duffel_order
from app.tools.hotel_tool import search_hotels
from app.database import db
from datetime import datetime, timezone
from pymongo.errors import PyMongoError


def _get_config(conversation_id: str) -> dict:
    return {
        "configurable": {
            "thread_id": conversation_id,
        }
    }


def _get_state(config: dict) -> dict:
    snapshot = trip_graph.get_state(config)

    if snapshot and snapshot.values:
        return dict(snapshot.values)

    return {}


def _save_state(config: dict, updates: dict) -> dict:
    trip_graph.update_state(config, updates)
    return _get_state(config)


def _date_change_requested(message: str) -> bool:
    text = message.lower().strip()

    phrases = (
        "change date",
        "change dates",
        "different date",
        "different dates",
        "new dates",
        "change my travel dates",
        "change my dates",
        "change the dates",
    )

    return any(phrase in text for phrase in phrases)


def _response(
    response: str,
    next_step: str | None,
    state: dict,
    weather: dict | None = None,
    hotels: list | None = None,
) -> dict:
    flights = state.get("flights") or []
    outbound_flights = state.get("outbound_flights") or []
    return_flights = state.get("return_flights") or []

    selected_outbound = state.get("selected_outbound_flight")
    selected_return = state.get("selected_return_flight")

    return {
        "response": response,
        "next_step": next_step,
        "weather": weather,
        "hotels": hotels if hotels is not None else state.get("hotel_results") or [],
        "flights": flights,
        "outbound_flights": outbound_flights,
        "return_flights": return_flights,
        "selected_flight": state.get("selected_flight"),
        "selected_outbound_flight": selected_outbound,
        "selected_return_flight": selected_return,
        "booking_order": state.get("booking_order"),

        # Alternative travel date suggestions
        "suggested_dates": state.get("suggested_dates") or [],
    }

def _search_one_way(
    origin: str,
    destination: str,
    travel_date: str,
) -> dict:
    try:
        result = flight_tool(
            origin=origin,
            destination=destination,
            start_date=travel_date,
            end_date=None,
        )

        if not isinstance(result, dict):
            return {
                "success": False,
                "offers": [],
            }

        offers = result.get("offers", [])

        return {
            **result,
            "offers": offers if isinstance(offers, list) else [],
        }

    except Exception:
        return {
            "success": False,
            "offers": [],
        }


def _minimum_fare(offers: list) -> float | None:
    """Return the lowest usable INR price from flight offers."""
    prices = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        value = offer.get("price_inr")
        if value is None:
            value = offer.get("total_amount")
        try:
            price = float(value)
            if price >= 0:
                prices.append(price)
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else None


def _is_continue(message: str) -> bool:
    text = message.strip().lower().strip(".! ")
    return text in {
        "continue", "yes continue", "continue anyway", "proceed",
        "show flights", "show me the flights", "yes, continue",
    }

def _extract_budget_change(
    message: str,
    *,
    allow_plain_amount: bool = False,
) -> float | None:
    """Extract a new budget; bare numbers are accepted only when prompted."""
    import re

    text = message.strip().lower()
    amount_pattern = r"(?:₹|rs\.?\s*)?([0-9][0-9,]*(?:\.[0-9]{1,2})?)"

    if allow_plain_amount:
        match = re.fullmatch(amount_pattern, text)
        if match:
            try:
                amount = float(match.group(1).replace(",", ""))
                return amount if amount > 0 else None
            except ValueError:
                return None

    if not any(term in text for term in ("budget", "spend", "afford")):
        return None
    if not any(term in text for term in (
        "change", "update", "increase", "decrease", "set", "new"
    )):
        return None

    match = re.search(amount_pattern, text)
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
        return amount if amount > 0 else None
    except ValueError:
        return None


def _start_outbound_search(
    config: dict,
    state: dict,
) -> dict:
    """Search complete round-trip offers in one Duffel request."""
    origin = state.get("origin")
    destination = state.get("destination")
    start_date = state.get("start_date")
    end_date = state.get("end_date")

    if not origin or not destination or not start_date or not end_date:
        return _response(
            "I need your departure city, destination, departure date, and "
            "return date before searching for round-trip flights.",
            "dates",
            state,
        )

    try:
        result = flight_tool(
            origin=origin,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        print("Round-trip flight search failed:", exc)
        result = {"success": False, "offers": []}

    if not isinstance(result, dict) or not result.get("success"):
        result = {"success": False, "offers": []}

    offers = result.get("offers", [])
    if not isinstance(offers, list):
        offers = []

    if not offers:
        updated = _save_state(
            config,
            {
                "flights": [],
                "outbound_flights": [],
                "return_flights": [],
                "selected_flight": None,
                "selected_outbound_flight": None,
                "selected_return_flight": None,
                "pending_outbound_flights": [],
                "conversation_stage": "outbound_results",
                "flight_direction": "round_trip",
                "next_step": "outbound_results",
                "assistant_response": (
                    f"I couldn't find complete round-trip offers from "
                    f"{origin} to {destination}, departing {start_date} "
                    f"and returning {end_date}. Please try different dates."
                ),
            },
        )
        return _response(updated.get("assistant_response", ""), "outbound_results", updated)

    budget_value = state.get("budget")
    try:
        budget = float(budget_value) if budget_value is not None else None
    except (TypeError, ValueError):
        budget = None

    # Each Duffel offer already represents the complete itinerary, so compare
    # its total price directly; do not add separately searched leg prices.
    minimum_round_trip = _minimum_fare(offers)
    if (
        budget is not None
        and budget >= 0
        and minimum_round_trip is not None
        and minimum_round_trip > budget
        and not state.get("budget_override_confirmed", False)
    ):
        warning = (
            f"The cheapest complete round-trip flight offer I found is "
            f"₹{minimum_round_trip:,.2f}, which is "
            f"₹{minimum_round_trip - budget:,.2f} over your budget of "
            f"₹{budget:,.2f}. Reply 'continue' to view the available "
            "round-trip options, or change your dates or budget. "
            "No flight options are shown until you choose to continue."
        )
        updated = _save_state(
            config,
            {
                "flights": [],
                "outbound_flights": [],
                "return_flights": [],
                "selected_flight": None,
                "selected_outbound_flight": None,
                "selected_return_flight": None,
                # Kept under the existing key for compatibility with the
                # current budget-confirmation handler.
                "pending_outbound_flights": offers,
                "minimum_outbound_fare": minimum_round_trip,
                "minimum_return_fare": None,
                "minimum_round_trip_fare": minimum_round_trip,
                "budget_override_confirmed": False,
                "budget_override_pending": True,
                "conversation_stage": "budget_confirmation",
                "flight_direction": None,
                "next_step": "budget_confirmation",
                "assistant_response": warning,
            },
        )
        return _response(warning, "budget_confirmation", updated)

    updated = _save_state(
        config,
        {
            "flights": offers,
            # Retain the legacy field for existing frontend consumers, but it
            # now contains complete round-trip offers, not outbound-only legs.
            "outbound_flights": offers,
            "return_flights": [],
            "selected_flight": None,
            "selected_outbound_flight": None,
            "selected_return_flight": None,
            "pending_outbound_flights": [],
            "conversation_stage": "outbound_results",
            "flight_direction": "round_trip",
            "next_step": "outbound_results",
            "assistant_response": (
                f"I found {len(offers)} complete round-trip option(s) from "
                f"{origin} to {destination}, departing {start_date} and "
                f"returning {end_date}. Select one option to review the "
                "full itinerary."
            ),
        },
    )
    return _response(updated.get("assistant_response", ""), "outbound_results", updated)


def _handle_outbound_selection(
    message: str,
    config: dict,
    state: dict,
) -> dict:
    """Select one complete round-trip offer and move directly to review."""
    flights = state.get("flights") or state.get("outbound_flights") or []

    if not flights:
        updated = _save_state(
            config,
            {
                "flights": [],
                "outbound_flights": [],
                "return_flights": [],
                "selected_flight": None,
                "selected_outbound_flight": None,
                "selected_return_flight": None,
                "weather": None,
                "weather_confirmed": None,
                "suggested_dates": [],
                "conversation_stage": "collecting_info",
                "flight_direction": None,
                "next_step": "dates",
                "assistant_response": (
                    "There are no round-trip flight options available for "
                    "the selected dates. Please enter different travel dates."
                ),
            },
        )
        return _response(updated.get("assistant_response", ""), "dates", updated)

    selection = classify_flight_selection(message, flights)
    if selection is None:
        return _response(
            "Please select a round-trip option by saying, for example, "
            "'first flight' or 'option 2'.",
            "outbound_results",
            state,
        )

    index = selection.get("index")
    if not isinstance(index, int) or not (0 <= index < len(flights)):
        return _response(
            "I couldn't identify that round-trip option. Please select one "
            "of the displayed options.",
            "outbound_results",
            state,
        )

    selected = flights[index]
    updated = _save_state(
        config,
        {
            "selected_flight": selected,
            # Keep these legacy fields populated consistently for the current
            # response/frontend, while the canonical selected offer is
            # selected_flight and represents both directions.
            "selected_outbound_flight": selected,
            # The selected Duffel offer is the canonical complete itinerary.
            # It contains both journeys under one offer_id for order creation.
            "selected_return_flight": None,
            "conversation_stage": "trip_review",
            "flight_direction": "complete",
            "next_step": "trip_review",
        },
    )

    price = selected.get("price_inr")
    if price is None:
        price = selected.get("total_amount")
    try:
        price_text = f" The displayed total for the complete itinerary is ₹{float(price):,.2f}."
    except (TypeError, ValueError):
        price_text = ""

    response_text = (
        "Your round-trip itinerary is selected. Please review the outbound "
        "and return journeys in this offer. Booking and payment have not "
        "been completed."
        + price_text
    )
    updated = _save_state(
        config,
        {
            "assistant_response": response_text,
            "next_step": "trip_review",
        },
    )
    return _response(response_text, "trip_review", updated)


def _handle_return_selection(
    message: str,
    config: dict,
    state: dict,
) -> dict:
    flights = state.get("return_flights") or []

    selection = classify_flight_selection(message, flights)

    if selection is None:
        return _response(
            "Please select a return flight by saying, for example, "
            "'first flight' or 'option 2'.",
            "return_results",
            state,
        )

    index = selection.get("index")

    if not isinstance(index, int) or not (0 <= index < len(flights)):
        return _response(
            "I couldn't identify that return flight. "
            "Please select one of the displayed options.",
            "return_results",
            state,
        )

    selected_return = flights[index]

    updated = _save_state(
        config,
        {
            "selected_return_flight": selected_return,
            "conversation_stage": "trip_review",
            "flight_direction": "complete",
            "next_step": "trip_review",
        },
    )

    outbound = updated.get("selected_outbound_flight") or {}
    outbound_price = outbound.get("price_inr")
    return_price = selected_return.get("price_inr")

    total_price = None

    try:
        if outbound_price is not None and return_price is not None:
            total_price = float(outbound_price) + float(return_price)
    except (TypeError, ValueError):
        total_price = None

    message_text = (
        "Both flights are selected. Please review your outbound "
        "and return flight cards below."
    )

    if total_price is not None:
        message_text += f" Their combined displayed price is ₹{total_price:,.2f}."

    updated = _save_state(
        config,
        {
            "assistant_response": message_text,
            "next_step": "trip_review",
        },
    )

    return _response(
        message_text,
        "trip_review",
        updated,
    )


def process_planner_message(
    message: str,
    conversation_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    user_id: str | None = None,
):
    config = _get_config(conversation_id)
    existing_state = _get_state(config)
    stage = existing_state.get("conversation_stage")

    # Gemini classifies every user message. Existing workflow stages remain
    # authoritative so intent classification cannot skip required checks.
    chat_intent = classify_chat_intent(message, existing_state)
    intent_name = chat_intent.get("intent", "unclear")
    print(
        "DEBUG chat intent:",
        intent_name,
        "confidence:",
        chat_intent.get("confidence", 0),
    )

    # Keep the existing date-change workflow available during flight selection.
    if stage in {
        "outbound_results",
        "return_search",
        "return_results",
        "trip_review",
    } and (
        _date_change_requested(message)
        or intent_name == "change_dates"
    ):
        trip_graph.update_state(
            config,
            {
                "conversation_stage": "collecting_info",
                "flight_direction": None,
                "flights": [],
                "outbound_flights": [],
                "return_flights": [],
                "selected_flight": None,
                "selected_outbound_flight": None,
                "selected_return_flight": None,
                "pending_outbound_flights": [],
                "budget_override_pending": False,
                "budget_override_confirmed": False,
                "weather_confirmed": None,
                "next_step": None,
            },
        )

        existing_state = _get_state(config)
        stage = existing_state.get("conversation_stage")

    # Handle budget confirmation before the graph can interpret "continue"
    # as an unrelated intent.
    pending_offers = existing_state.get("pending_outbound_flights") or []
    budget_pending = (
        stage == "budget_confirmation"
        or bool(existing_state.get("budget_override_pending"))
        or (
            bool(pending_offers)
            and existing_state.get("minimum_round_trip_fare") is not None
            and not existing_state.get("budget_override_confirmed", False)
        )
    )
    if budget_pending:
        changed_budget = _extract_budget_change(
            message,
            allow_plain_amount=True,
        )
        if changed_budget is not None:
            updated = _save_state(
                config,
                {
                    "messages": existing_state.get("messages", []) + [message],
                    "budget": changed_budget,
                    "pending_outbound_flights": [],
                    "budget_override_pending": False,
                    "budget_override_confirmed": False,
                    "conversation_stage": "flight_search",
                    "next_step": "flight_search",
                },
            )
            return _start_outbound_search(config, updated)

        if _is_continue(message):
            if pending_offers:
                total = float(existing_state.get("minimum_round_trip_fare") or 0)
                response_text = (
                    "Understood. Here are the available complete round-trip offers. "
                    f"Please note that the cheapest round-trip fare we found was ₹{total:,.2f}, "
                    "which exceeds your stated budget."
                )
                updated = _save_state(
                    config,
                    {
                        "messages": existing_state.get("messages", []) + [message],
                        "flights": pending_offers,
                        "outbound_flights": pending_offers,
                        "return_flights": [],
                        "pending_outbound_flights": [],
                        "budget_override_confirmed": True,
                        "budget_override_pending": False,
                        "conversation_stage": "outbound_results",
                        "flight_direction": "outbound",
                        "next_step": "outbound_results",
                        "assistant_response": response_text,
                    },
                )
                return _response(response_text, "outbound_results", updated)
            # If pending offers were lost, retry once with the override recorded.
            resumed = _save_state(
                config,
                {
                    "messages": existing_state.get("messages", []) + [message],
                    "budget_override_confirmed": True,
                    "budget_override_pending": False,
                    "conversation_stage": "flight_search",
                    "next_step": "flight_search",
                },
            )
            return _start_outbound_search(config, resumed)

        if _date_change_requested(message):
            updated = _save_state(
                config,
                {
                    "messages": existing_state.get("messages", []) + [message],
                    "conversation_stage": "collecting_info",
                    "flight_direction": None,
                    "flights": [],
                    "outbound_flights": [],
                    "return_flights": [],
                    "pending_outbound_flights": [],
                    "budget_override_pending": False,
                    "budget_override_confirmed": False,
                    "weather_confirmed": None,
                    "next_step": None,
                },
            )
            return _response("Sure. What new travel dates would you like?", "dates", updated)

        return _response(
            "Please reply 'continue' to view the available flights despite the budget warning, or ask to change your dates or budget.",
            "budget_confirmation",
            existing_state,
        )

    # Handle the user's selection without sending flight-selection messages
    # through the general trip-information extraction prompt.
    if stage == "outbound_results":
        updated = _save_state(
            config,
            {
                "messages": existing_state.get("messages", []) + [message],
            },
        )

        return _handle_outbound_selection(
            message,
            config,
            updated,
        )

    if stage == "return_results":
        updated = _save_state(
            config,
            {
                "messages": existing_state.get("messages", []) + [message],
            },
        )

        return _handle_return_selection(
            message,
            config,
            updated,
        )

    
    if stage == "flight_passenger_details":
        import json
        import re
        updated = _save_state(config, {
            "messages": existing_state.get("messages", []) + [message],
        })
        selected_offer = updated.get("selected_flight") or updated.get("selected_outbound_flight")
        if not selected_offer or not selected_offer.get("offer_id"):
            return _response("I couldn't find the selected Duffel offer. Please select your flight again.", "outbound_results", updated)
        # Passenger details are submitted as JSON so the API payload is unambiguous.
        try:
            details = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return _response(
                'Please send passenger details as JSON, for example: {"given_name":"Asha","family_name":"Kumar","title":"ms","gender":"f","born_on":"1998-05-20","email":"asha@example.com","phone_number":"+919876543210"}.',
                "flight_passenger_details", updated)
        # Accept older no-underscore spellings as well as the canonical form
        # so a previously typed JSON payload does not fail due to key naming.
        if isinstance(details, dict):
            aliases = {
                "given_name": ("givenname", "first_name", "firstname"),
                "family_name": ("familyname", "last_name", "lastname"),
                "born_on": ("bornon", "date_of_birth", "dateofbirth"),
                "phone_number": ("phonenumber", "phone", "telephone"),
            }
            for canonical, alternate_names in aliases.items():
                if not details.get(canonical):
                    for alias in alternate_names:
                        if details.get(alias):
                            details[canonical] = details[alias]
                            break

        required = ("given_name", "family_name", "title", "gender", "born_on", "email", "phone_number")
        missing = [key for key in required if not isinstance(details.get(key), str) or not details[key].strip()]
        if missing:
            return _response("Missing required passenger fields: " + ", ".join(missing) + ". Please send all required fields as JSON.", "flight_passenger_details", updated)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", details["email"].strip()):
            return _response("Please provide a valid passenger email address.", "flight_passenger_details", updated)
        try:
            from datetime import date
            date.fromisoformat(details["born_on"])
        except ValueError:
            return _response("born_on must use YYYY-MM-DD format.", "flight_passenger_details", updated)
        passenger = {key: details[key].strip() for key in required}
        # Duffel requires title and gender values accepted by its API.
        passenger["title"] = passenger["title"].lower()
        passenger["gender"] = passenger["gender"].lower()
        amount = selected_offer.get("total_amount")
        currency = selected_offer.get("total_currency")
        if amount is None or not currency:
            return _response("The selected offer is missing its original price/currency. Please search and select a fresh flight offer.", "outbound_results", updated)
        order_result = create_duffel_order(
            offer_id=selected_offer["offer_id"],
            passengers=[passenger],
            amount=str(amount),
            currency=str(currency),
        )
        if not order_result.get("success"):
            return _response(
                "Duffel could not confirm the booking: " + order_result.get("error", "Unknown error") + " Please check the Duffel dashboard before retrying, especially if the request timed out.",
                "flight_passenger_details", updated)
        order = order_result["order"]

        # Persist the successful Duffel instant order against the authenticated
        # user. The provider order ID is the idempotency key: if the same order
        # is encountered again, do not create a second MongoDB booking record.
        persistence_error = None
        if not user_id:
            persistence_error = "The authenticated user ID was not provided to the planner service."
        else:
            booking_collection = db["flight_bookings"]
            now = datetime.now(timezone.utc)
            provider_order_id = order.get("id")
            try:
                existing_booking = booking_collection.find_one({"provider_order_id": provider_order_id})
                if existing_booking and str(existing_booking.get("user_id")) != str(user_id):
                    persistence_error = "This provider order ID is already associated with another user."
                else:
                    booking_record = {
                        "user_id": str(user_id),
                        "conversation_id": conversation_id,
                        "offer_id": selected_offer.get("offer_id"),
                        "provider": "duffel",
                        "provider_order_id": provider_order_id,
                        "booking_reference": order.get("booking_reference"),
                        "status": order.get("status") or "unknown",
                        "payment_status": order.get("payment_status") or "unknown",
                        "booking_type": "instant",
                        "total_amount": order.get("total_amount") or amount,
                        "total_currency": order.get("total_currency") or currency,
                        "passenger_details": passenger,
                        "passengers": order.get("passengers") or [],
                        "slices": order.get("slices") or [],
                        "provider_created_at": order.get("created_at"),
                        "created_at": existing_booking.get("created_at", now) if existing_booking else now,
                        "updated_at": now,
                    }
                    booking_collection.update_one(
                        {"provider_order_id": provider_order_id, "user_id": str(user_id)},
                        {"$set": {key: value for key, value in booking_record.items() if key not in {"created_at"}},
                         "$setOnInsert": {"created_at": booking_record["created_at"]}},
                        upsert=True,
                    )
            except PyMongoError as exc:
                persistence_error = str(exc)

        # Keep Duffel's original amount/currency for the actual order request,
        # but show the converted INR price to the user when it is available.
        display_price_inr = selected_offer.get("price_inr")
        if display_price_inr is not None:
            try:
                display_total = f"₹{float(display_price_inr):,.2f}"
            except (TypeError, ValueError):
                display_total = (
                    f"{order.get('total_amount') or amount} "
                    f"{order.get('total_currency') or currency}"
                )
        else:
            display_total = (
                f"{order.get('total_amount') or amount} "
                f"{order.get('total_currency') or currency}"
            )

        confirmation = (
            "Your Duffel test flight order was created successfully. "
            f"Booking reference: {order.get('booking_reference') or 'not provided'}. "
            f"Order ID: {order.get('id')}. Status: {order.get('status') or 'unknown'}. "
            f"Payment status: {order.get('payment_status') or 'unknown'}. "
            f"Total: {display_total}. "
        )
        if persistence_error:
            confirmation += (
                "WARNING: Duffel returned an order, but saving it to your Trip Planner booking history failed. "
                "Do not retry the booking yet; check the Duffel dashboard and server logs first. "
            )
        confirmation += "Would you like to search for hotels, or skip hotels and finish your trip plan?"
        updated = _save_state(config, {
            "user_id": str(user_id) if user_id is not None else "",
            "passenger_details": passenger,
            "booking_order": order,
            "booking_status": order.get("status") or "unknown",
            "booking_payment_status": order.get("payment_status") or "unknown",
            "booking_persistence_error": bool(persistence_error),
            "conversation_stage": "hotel_confirmation",
            "next_step": "hotel_confirmation",
            "assistant_response": confirmation,
        })
        return _response(confirmation, "hotel_confirmation", updated)

    # After a successful flight order, handle hotel decisions explicitly.
    # Do this before invoking the LLM/graph so a "skip hotels" reply cannot
    # accidentally restart flight search or be treated as a new trip request.
    if stage == "hotel_confirmation":
        normalized = " ".join((message or "").strip().lower().split())
        skip_phrases = (
            "skip hotels", "skip hotel", "no hotel", "no hotels",
            "without hotel", "without hotels", "finish my trip plan",
            "finish trip", "finish my trip", "no accommodation",
            "skip accommodation", "finish without hotels",
        )
        hotel_phrases = (
            "find hotels", "search hotels", "search for hotels",
            "hotel search", "book a hotel", "book hotels",
            "need a hotel", "need hotels", "accommodation",
        )

        if any(phrase in normalized for phrase in skip_phrases):
            completion_text = (
                "Your trip plan is complete. Your Duffel test flight booking "
                "has been created, and you chose to skip hotel booking. "
                "No hotel reservation has been made. You can find your flight "
                "booking reference and order ID in the confirmation above."
            )
            updated = _save_state(config, {
                "conversation_stage": "trip_complete",
                "next_step": "trip_complete",
                "hotel_booking_skipped": True,
                "assistant_response": completion_text,
            })
            return _response(completion_text, "trip_complete", updated)

        if any(phrase in normalized for phrase in hotel_phrases):
            destination = existing_state.get("destination")
            checkin = existing_state.get("start_date")
            checkout = existing_state.get("end_date")
            if not destination or not checkin or not checkout:
                hotel_text = (
                    "Your flight booking is saved, but I need the destination, "
                    "check-in date, and check-out date to search hotel rates. "
                    "Please provide the missing stay details."
                )
                updated = _save_state(config, {
                    "conversation_stage": "hotel_confirmation",
                    "next_step": "hotel_confirmation",
                    "assistant_response": hotel_text,
                })
                return _response(hotel_text, "hotel_confirmation", updated)

            hotel_result = search_hotels(
                destination=destination,
                checkin=checkin,
                checkout=checkout,
                adults=int(existing_state.get("hotel_adults") or 1),
                currency="INR",
                guest_nationality="IN",
                limit=8,
            )
            if not hotel_result.get("success"):
                hotel_text = (
                    "Your flight booking is saved, but I couldn't search hotel rates. "
                    + hotel_result.get("error", "Please try again.")
                )
                updated = _save_state(config, {
                    "conversation_stage": "hotel_confirmation",
                    "next_step": "hotel_search_error",
                    "hotel_results": [],
                    "assistant_response": hotel_text,
                })
                return _response(hotel_text, "hotel_search_error", updated, hotels=[])

            hotels = hotel_result.get("hotels") or []
            if not hotels:
                hotel_text = hotel_result.get(
                    "message",
                    "I couldn't find priced hotel rooms for those dates. Try another date range."
                )
                updated = _save_state(config, {
                    "conversation_stage": "hotel_confirmation",
                    "next_step": "hotel_search_results",
                    "hotel_results": [],
                    "assistant_response": hotel_text,
                })
                return _response(hotel_text, "hotel_search_results", updated, hotels=[])

            hotel_text = (
                f"I found {len(hotels)} priced hotel option(s) in {destination} "
                f"for {checkin} to {checkout}. Prices are from LiteAPI sandbox and "
                "must be revalidated before booking. Choose a room to continue. "
                "No hotel has been booked yet."
            )
            updated = _save_state(config, {
                "conversation_stage": "hotel_results",
                "next_step": "hotel_search_results",
                "hotel_results": hotels,
                "hotel_search": {
                    "destination": destination,
                    "checkin": checkin,
                    "checkout": checkout,
                    "currency": hotel_result.get("currency", "INR"),
                },
                "assistant_response": hotel_text,
            })
            return _response(hotel_text, "hotel_search_results", updated, hotels=hotels)

        # Keep the conversation at the hotel decision rather than falling back
        # into the flight-planning graph for an unclear reply.
        prompt = "Would you like to find hotels, or skip hotels and finish your trip plan?"
        updated = _save_state(config, {
            "conversation_stage": "hotel_confirmation",
            "next_step": "hotel_confirmation",
            "assistant_response": prompt,
        })
        return _response(prompt, "hotel_confirmation", updated)

    if stage == "trip_review":
        updated = _save_state(
            config,
            {
                "messages": existing_state.get("messages", []) + [message],
            },
        )

        text = message.lower().strip()

        confirm_terms = (
            "confirm", "confirmed", "continue", "proceed",
            "yes", "okay", "ok", "go ahead",
        )
        hotel_terms = (
            "hotel", "hotels", "accommodation", "stay", "lodging",
        )

        is_confirmation = any(term in text for term in confirm_terms)
        wants_hotels = any(term in text for term in hotel_terms)

        selected_offer = (
            updated.get("selected_flight")
            or updated.get("selected_outbound_flight")
        )

        if not selected_offer:
            return _response(
                "Please select a flight option first.",
                "outbound_results",
                updated,
            )

        if is_confirmation:
            response_text = (
                "Please complete the passenger details form to continue with your Duffel test booking. "
                "Required details are first name, last name, title, gender, date of birth, email, and phone number. "
                "The request will use Duffel Test Mode."
            )
            updated = _save_state(config, {
                "selected_flight": selected_offer,
                "conversation_stage": "flight_passenger_details",
                "next_step": "flight_passenger_details",
                "assistant_response": response_text,
            })
            return _response(response_text, "flight_passenger_details", updated)

        return _response(
            "Your selected round-trip itinerary is ready for review. "
            "Use the Passenger details button to continue with the test booking, "
            "or tell me if you want to skip hotel booking later.",
            "trip_review",
            updated,
        )


    # Preserve the existing graph flow for collecting trip details,
    # checking weather, and handling weather/date confirmation.
    if existing_state:
        input_state = {
            "messages": existing_state.get("messages", []) + [message],
        }

        if start_date:
            input_state["start_date"] = start_date

        if end_date:
            input_state["end_date"] = end_date

    else:
        input_state = {
            "user_id": str(user_id) if user_id is not None else "",
            "conversation_id": conversation_id,
            "messages": [message],
            "travel_intent": None,
            "origin": None,
            "destination": None,
            "start_date": start_date,
            "end_date": end_date,
            "budget": None,
            "weather": None,
            "weather_confirmed": None,
            "suggested_dates": [],
            "flights": [],
            "selected_flight": None,
            "outbound_flights": [],
            "return_flights": [],
            "pending_outbound_flights": [],
            "budget_override_pending": False,
            "budget_override_confirmed": False,
            "selected_outbound_flight": None,
            "selected_return_flight": None,
            "conversation_stage": None,
            "flight_direction": None,
            "next_step": None,
            "extracted_info": None,
            "assistant_response": None,
        }
    
    result = trip_graph.invoke(
        input_state,
        config=config,
    )
    print("DEBUG graph suggested_dates:", result.get("suggested_dates"))
    print("DEBUG graph stage:", result.get("conversation_stage"))

    response = result.get("assistant_response")
    next_step = result.get("next_step")

    if response is None:
        prompts = {
            "dates": "What dates would you like to travel?",
            "origin": "What city are you travelling from?",
            "destination": "Where would you like to travel?",
            "budget": "What is your budget for the trip?",
            "travel_confirmation": "Are you planning to travel?",
            "weather_confirmation": (
                "Would you like to continue with these dates "
                "or change your travel dates?"
            ),
            "booking": (
                "Your flight has been selected. "
                "We can proceed to booking."
            ),
        }

        response = prompts.get(next_step, "")

    weather = result.get("weather")

    if next_step != "weather_confirmation":
        weather = None

    if not isinstance(weather, dict):
        weather = None

    # Start outbound-only search when the graph routes a confirmed weather
    # check to flight_search. The graph intentionally ends at this stage;
    # requiring conversation_stage == "flight_results" here skipped the
    # search and could leave the assistant bubble blank.
    if (
        next_step == "flight_search"
        and result.get("origin")
        and result.get("destination")
        and result.get("start_date")
        and result.get("end_date")
    ):
        return _start_outbound_search(
            config,
            result,
        )

    return _response(
        response,
        next_step,
        result,
        weather,
    )
