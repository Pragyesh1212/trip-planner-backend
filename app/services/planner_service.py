from app.ai.graph import (
    trip_graph,
    classify_flight_selection,
    classify_chat_intent,
    classify_weather_confirmation,
    _run_central_tool_agent,
    _run_central_message_agent,
)
from app.tools.flight_tool import flight_tool, create_duffel_order
from app.tools.hotel_tool import search_hotels
from app.tools.hotel_prebook_tool import prebook_hotel_offer
from app.tools.hotel_booking_tool import book_hotel
import json
import re
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
    # Gemini may rephrase ordinary conversational responses, but flight
    # result messages must remain backend-authored. The backend has the
    # verified Duffel offers and prices; the wording agent does not receive
    # those offers and must not invent airline names, fares, or availability.
    draft_message = response or state.get("assistant_response") or ""
    print(
        "[FLIGHT RESPONSE CHECK]",
        "next_step =", repr(next_step),
        "gemini_allowed =", next_step != "outbound_results",
        flush=True
    )
    if draft_message.strip() and next_step not in ("outbound_results", "hotel_booking_complete","trip_review"):
        print("[CENTRAL GEMINI RESPONSE] Attempting wording generation.", flush=True)
        try:
            generated = _run_central_message_agent(
                draft_message=draft_message,
                trip_context={
                    "conversation_stage": state.get("conversation_stage"),
                    "next_step": next_step or state.get("next_step"),
                    "origin": state.get("origin"),
                    "destination": state.get("destination"),
                    "start_date": state.get("start_date"),
                    "end_date": state.get("end_date"),
                    "budget_inr": state.get("budget"),
                    "flight_direction": state.get("flight_direction"),
                    "selected_flight": state.get("selected_flight"),
                    "booking_status": state.get("booking_status"),
                    "booking_payment_status": state.get("booking_payment_status"),
                    "hotel_booking_skipped": state.get("hotel_booking_skipped"),
                    "assistant_response": state.get("assistant_response"),
                    "recent_messages": (state.get("messages") or [])[-8:],
                    "adults": state.get("adults"),
                    "children": state.get("children"),
                    "child_ages": state.get("child_ages"),
                    "passenger_count_confirmed": state.get("passenger_count_confirmed"),
                    "traveller_count": state.get("traveller_count"),
                    "remaining_budget": state.get("remaining_budget"),
                    "hotel_results": state.get("hotel_results"),
                    "selected_hotel": state.get("selected_hotel"),
                    "booking_order": state.get("booking_order"),
                    "hotel_booking": state.get("hotel_booking"),
                    "next_action": state.get("next_step"),
                },
            )
            generated_message = generated.get("message")
            used_gemini_message = (
                generated.get("status") == "completed"
                and isinstance(generated_message, str)
                and bool(generated_message.strip())
            )
            print(
                "[CENTRAL GEMINI RESPONSE] "
                f"status={generated.get('status')}; "
                f"used_generated_message={used_gemini_message}; "
                f"iterations={generated.get('iterations')}",
                flush=True,
            )
            if used_gemini_message:
                response = generated_message.strip()
            else:
                print(
                    "[CENTRAL GEMINI RESPONSE] Using backend draft fallback.",
                    flush=True,
                )
        except Exception as exc:
            # Preserve service availability if Gemini is temporarily unavailable.
            print(f"[CENTRAL GEMINI] Response generation failed: {exc}", flush=True)
            response = draft_message
    else:
        response = draft_message
    # Always communicate the child age restriction.
    # This is enforced by the backend, not left to Gemini's wording.
    if next_step == "passenger_count":
        age_rule = (
            " Children must be between 0 and 17 years old on your "
            "departure date. Anyone who will be 18 or older on that "
            "date must be counted as an adult."
        )

        if age_rule.strip() not in response:
            response = response.rstrip() + age_rule
    print(
        "[PASSENGER AGE RULE DEBUG]",
        "next_step =", repr(next_step),
        "response =", repr(response),
        flush=True
    )

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
        "hotel_booking": state.get("hotel_booking"),

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


def _passenger_count_is_valid(state: dict) -> bool:
    adults = state.get("adults")
    children = state.get("children")
    child_ages = state.get("child_ages")

    if adults is None or children is None:
        return False

    try:
        adults = int(adults)
        children = int(children)
    except (TypeError, ValueError):
        return False

    if adults < 1 or children < 0:
        return False

    # An adults-only trip does not require child ages.
    if children == 0 and child_ages is None:
        child_ages = []

    if not isinstance(child_ages, list):
        return False

    if len(child_ages) != children:
        return False

    # Children must be younger than 18 on the departure date. The age is
    # collected before flight search and will be checked again against DOB
    # during passenger-details validation.
    for age in child_ages:
        if (
            isinstance(age, bool)
            or not isinstance(age, int)
            or age < 0
            or age > 17
        ):
            return False

    return True



def _passenger_count_prompt(state: dict) -> str:
    adults = state.get("adults")
    children = state.get("children")
    child_ages = state.get("child_ages") or []

    age_rule = (
        "IMPORTANT CHILD AGE RULE: Every child must be between 0 and 17 "
        "years old on the departure date. A passenger aged 18 or above "
        "must be counted as an adult, not a child. Always communicate "
        "this rule when asking about passenger counts or child ages."
    )

    if adults is None and children is None:
        return (
            "Ask the user how many adults and children will be travelling. "
            "If there are children, ask for each child's age. "
            f"{age_rule}"
        )

    if adults is None:
        return (
            "Ask the user how many adults are travelling. "
            f"The number of children already provided is {children}. "
            f"Their ages are {child_ages}. "
            f"{age_rule}"
        )

    if children is None:
        return (
            f"The user has provided {adults} adult passenger(s). "
            "Ask how many children are travelling and, if applicable, "
            "their ages. "
            f"{age_rule}"
        )

    if children > 0 and len(child_ages) != children:
        return (
            f"The user has provided {adults} adult(s) and {children} child(ren). "
            f"The child ages provided so far are {child_ages}. "
            "Ask for the age of every child. "
            f"{age_rule}"
        )

    return (
        "Ask the user to provide a valid passenger composition: "
        "at least one adult, a non-negative number of children, "
        "and the age of every child. "
        f"{age_rule}"
    )


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
    if not _passenger_count_is_valid(state):
        updated = _save_state(
            config,
            {
                "conversation_stage": "passenger_count",
                "next_step": "passenger_count",
                "passenger_count_confirmed": False,
                "assistant_response": _passenger_count_prompt(state),
            },
        )
        return _response(
            updated.get("assistant_response", ""),
            "passenger_count",
            updated,
        )

    # Use the central Gemini tool-calling agent for the live flight search.
    # The saved trip values are passed as context; the agent is instructed to
    # use these exact values and is only given the flight-search tool.
    try:
        tool_run = _run_central_tool_agent(
            tool_name="get_flights",
            user_message=(
                "Search for complete round-trip flight offers using the exact "
                "origin, destination, departure date, and return date in the "
                "supplied trip context. Call get_flights with journey_type "
                "'return'. Use the supplied values without changing or inventing "
                "route, dates, passenger count, or cabin class. Search only; do "
                "not book or pay. Do not call any other tool."
            ),
            trip_context={
                "origin": origin,
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                "journey_type": "return",
                "adults": int(state.get("adults") or 1),
                "children": int(state.get("children") or 0),
                "child_ages": state.get("child_ages") or [],
                "cabin_class": state.get("cabin_class") or "economy",
                "conversation_stage": state.get("conversation_stage"),
            },
        )
        result = next(
            (
                item.get("result")
                for item in reversed(tool_run.get("tool_results", []))
                if item.get("tool") == "get_flights"
                and isinstance(item.get("result"), dict)
            ),
            None,
        )
        if not isinstance(result, dict):
            result = {
                "success": False,
                "offers": [],
                "error": tool_run.get("message") or "Gemini did not execute the flight-search tool.",
            }
    except Exception as exc:
        print("Central Gemini round-trip flight search failed:", exc)
        result = {"success": False, "offers": [], "error": str(exc)}

    if not isinstance(result, dict) or not result.get("success"):
        print("Round-trip flight search returned no success:", result.get("error") if isinstance(result, dict) else "invalid result")
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
    ):
        warning = (
            f"The cheapest complete round-trip flight offer is "
            f"₹{minimum_round_trip:,.2f}, which exceeds your total trip budget "
            f"of ₹{budget:,.2f} by ₹{minimum_round_trip - budget:,.2f}. "
            "Please increase your budget or choose different travel dates. "
            "Flight options cannot be displayed until the budget is sufficient."
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
                "pending_outbound_flights": [],
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

    # Hotel selection is a deterministic stage. Handle it before Gemini intent
    # classification or the planning graph so a room selection cannot restart
    # weather/flight planning. LiteAPI's offerId is stored as rate_id by search_hotels.
    if stage == "hotel_results":
        hotel_options = existing_state.get("hotel_results") or []
        normalized = " ".join((message or "").strip().lower().split())

        option_match = re.search(
            r"\b(?:option|hotel|room|rate|select|choose)\s*#?\s*(\d+)\b",
            normalized,
        )
        if option_match:
            option_number = int(option_match.group(1))
        else:
            ordinal_numbers = {
                "first": 1, "1st": 1, "second": 2, "2nd": 2,
                "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
                "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
                "seventh": 7, "7th": 7, "eighth": 8, "8th": 8,
            }
            option_number = next(
                (number for word, number in ordinal_numbers.items()
                 if re.search(rf"\b{word}\b", normalized)),
                None,
            )

        if option_number is None:
            prompt = (
                "Please select a hotel room by its option number, for example "
                "'option 1'. No hotel has been prebooked or reserved yet."
            )
            updated = _save_state(config, {
                "conversation_stage": "hotel_results",
                "next_step": "hotel_search_results",
                "assistant_response": prompt,
            })
            return _response(prompt, "hotel_search_results", updated, hotels=hotel_options)

        if not (1 <= option_number <= len(hotel_options)):
            prompt = (
                f"Please choose an option between 1 and {len(hotel_options)}. "
                "No hotel has been prebooked or reserved."
            )
            updated = _save_state(config, {
                "conversation_stage": "hotel_results",
                "next_step": "hotel_search_results",
                "assistant_response": prompt,
            })
            return _response(prompt, "hotel_search_results", updated, hotels=hotel_options)

        selected_hotel = hotel_options[option_number - 1]
        offer_id = (
            selected_hotel.get("rate_id")
            or selected_hotel.get("offer_id")
        ) if isinstance(selected_hotel, dict) else None
        if not offer_id:
            prompt = (
                "This hotel option does not contain a valid LiteAPI offer ID. "
                "Please choose another displayed option or search hotels again."
            )
            updated = _save_state(config, {
                "conversation_stage": "hotel_results",
                "next_step": "hotel_search_results",
                "assistant_response": prompt,
            })
            return _response(prompt, "hotel_search_results", updated, hotels=hotel_options)

        # Prebooking only revalidates the rate; it does not create a reservation
        # or charge the guest. Do not call the final booking tool at this stage.
        prebook_result = prebook_hotel_offer(str(offer_id))
        if not prebook_result.get("success"):
            prebook_message = prebook_result.get(
                "message", "LiteAPI could not revalidate this hotel rate. Please try again."
            )
            if prebook_result.get("requires_new_search"):
                updated = _save_state(config, {
                    "hotel_results": [],
                    "selected_hotel": None,
                    "hotel_prebook": None,
                    "conversation_stage": "hotel_confirmation",
                    "next_step": "hotel_search_error",
                    "assistant_response": prebook_message,
                })
                return _response(prebook_message, "hotel_search_error", updated, hotels=[])

            updated = _save_state(config, {
                "selected_hotel": None,
                "hotel_prebook": None,
                "conversation_stage": "hotel_results",
                "next_step": "hotel_search_results",
                "assistant_response": prebook_message,
            })
            return _response(prebook_message, "hotel_search_results", updated, hotels=hotel_options)

        selected_hotel = {**selected_hotel, "selected_option": option_number}
        updated = _save_state(config, {
            "selected_hotel": selected_hotel,
            "hotel_prebook": prebook_result,
            "conversation_stage": "hotel_guest_details",
            "next_step": "hotel_guest_details",
            "assistant_response": (
                "The selected hotel rate has been revalidated successfully. "
                "No reservation or payment has been made yet. Please provide "
                "the lead guest's first name, last name, email address, and phone number."
            ),
        })
        return _response(
            updated.get("assistant_response", ""),
            "hotel_guest_details",
            updated,
            hotels=hotel_options,
        )

    # Hotel guest details are a deterministic booking stage. Handle this before
    # weather/general intent classification so the graph cannot restart trip
    # planning while a LiteAPI prebooking reference is waiting to be booked.
    if stage == "hotel_guest_details":
        # If the booking was already saved, never submit the same prebook again.
        saved_hotel_booking = existing_state.get("hotel_booking")
        if isinstance(saved_hotel_booking, dict) and saved_hotel_booking.get("success"):
            completed_text = (
                "Your hotel booking is already confirmed. "
                f"Booking ID: {saved_hotel_booking.get('booking_id') or 'not provided'}. "
                f"Confirmation code: {saved_hotel_booking.get('hotel_confirmation_code') or 'not provided'}."
            )
            return _response(
                completed_text,
                "hotel_booking_complete",
                existing_state,
            )

        # Never submit the same hotel prebook again when a previous attempt is
        # still running or its provider outcome has not been verified. This
        # also protects against a process crash after LiteAPI accepted a request
        # but before the confirmation was saved to the checkpoint.
        attempt_status = existing_state.get("hotel_booking_attempt_status")
        if attempt_status in {"processing", "failed_or_unconfirmed"}:
            safety_message = (
                "A hotel booking request has already been started, but its final "
                "status has not been safely confirmed in this conversation. "
                "To prevent a duplicate reservation, I have not sent another "
                "request to LiteAPI. Please verify the booking status in your "
                "LiteAPI account/support before any retry."
            )
            return _response(
                safety_message,
                "hotel_booking_status_check",
                existing_state,
            )

        try:
            guest_details = message if isinstance(message, dict) else json.loads(message or "")
        except (TypeError, json.JSONDecodeError):
            guest_details = None

        if not isinstance(guest_details, dict):
            prompt = (
                "Please submit the lead guest details as JSON with these fields: "
                "first_name, last_name, email, and phone. "
                'Example: {"first_name":"Test","last_name":"Guest",'
                '"email":"test@example.com","phone":"9999999999"}. '
                "Use test details when running in LiteAPI sandbox."
            )
            updated = _save_state(config, {
                "conversation_stage": "hotel_guest_details",
                "next_step": "hotel_guest_details",
                "assistant_response": prompt,
            })
            return _response(prompt, "hotel_guest_details", updated)

        first_name = str(guest_details.get("first_name") or "").strip()
        last_name = str(guest_details.get("last_name") or "").strip()
        email = str(guest_details.get("email") or "").strip()
        phone = str(guest_details.get("phone") or "").strip()

        email_is_valid = bool(
            re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
        )
        if not all((first_name, last_name, phone)) or not email_is_valid:
            prompt = (
                "The guest details are incomplete or invalid. Please provide "
                "first_name, last_name, a valid email address, and phone "
                "number in the JSON format requested."
            )
            updated = _save_state(config, {
                "conversation_stage": "hotel_guest_details",
                "next_step": "hotel_guest_details",
                "assistant_response": prompt,
            })
            return _response(prompt, "hotel_guest_details", updated)

        prebook = existing_state.get("hotel_prebook") or {}
        prebook_id = prebook.get("prebook_id") if isinstance(prebook, dict) else None
        if not prebook_id:
            prompt = (
                "I couldn't find the saved LiteAPI prebooking reference, so I "
                "cannot safely create the hotel booking. Please search hotels "
                "again and select a room to revalidate its rate."
            )
            updated = _save_state(config, {
                "hotel_prebook": None,
                "selected_hotel": None,
                "conversation_stage": "hotel_confirmation",
                "next_step": "hotel_search_error",
                "assistant_response": prompt,
            })
            return _response(prompt, "hotel_search_error", updated, hotels=[])

        # Persist the in-flight marker BEFORE contacting LiteAPI. If the
        # process stops or the response is lost, the next request will be
        # blocked by the guard above instead of creating a possible duplicate.
        attempt_state = _save_state(config, {
            "hotel_booking_attempt_status": "processing",
            "conversation_stage": "hotel_guest_details",
            "next_step": "hotel_booking_processing",
        })
        if attempt_state.get("hotel_booking_attempt_status") != "processing":
            safety_message = (
                "I couldn't safely record the hotel booking attempt, so I did "
                "not send a booking request to LiteAPI. Please try again later."
            )
            return _response(
                safety_message,
                "hotel_guest_details",
                attempt_state or existing_state,
            )

        # This is the only point where the final LiteAPI booking endpoint is
        # called. The prebook step above only revalidates the rate.
        import traceback

        try:
            print("[HOTEL BOOKING DEBUG] Calling LiteAPI...")
            print("[HOTEL BOOKING DEBUG] Prebook ID:", prebook_id)

            booking_result = book_hotel(
                prebook_id=str(prebook_id),
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
            )

            print("[HOTEL BOOKING DEBUG] Result:", booking_result)

        except Exception as exc:
            print("[HOTEL BOOKING EXCEPTION]", repr(exc))
            traceback.print_exc()

            _save_state(config, {
                "hotel_booking_attempt_status": "failed_or_unconfirmed"
            })

            return {
                "response": "An unexpected error occurred during hotel booking. Please check the backend logs before retrying.",
                "next_step": "hotel_booking_status_check"
            }

        if not isinstance(booking_result, dict) or not booking_result.get("success"):
            error_message = (
                booking_result.get("message")
                if isinstance(booking_result, dict)
                else None
            ) or "LiteAPI could not complete the hotel booking. Please check the provider status before retrying."
            # Keep the stage and prebook reference so recoverable validation or
            # provider errors can be handled without restarting the trip flow.
            # For timeouts/missing booking IDs, the tool explicitly advises
            # checking LiteAPI before another attempt to avoid duplicates.
            updated = _save_state(config, {
                "hotel_booking_attempt_status": "failed_or_unconfirmed",
                "conversation_stage": "hotel_guest_details",
                "next_step": "hotel_guest_details",
                "assistant_response": str(error_message),
            })
            return _response(str(error_message), "hotel_guest_details", updated)

        hotel_name = (
            booking_result.get("hotel_name")
            or (existing_state.get("selected_hotel") or {}).get("name")
            or "your selected hotel"
        )
        confirmation_text = (
            f"Your hotel booking for {hotel_name} was confirmed by LiteAPI. "
            f"Booking ID: {booking_result.get('booking_id') or 'not provided'}. "
            f"Confirmation code: {booking_result.get('hotel_confirmation_code') or 'not provided'}. "
            f"Status: {booking_result.get('status') or 'confirmed'}. "
        )
        if booking_result.get("price") is not None:
            confirmation_text += (
                f"Total: {booking_result.get('currency') or 'INR'} "
                f"{booking_result.get('price')}. "
            )
        confirmation_text += "Your trip booking process is complete."

        updated = _save_state(config, {
            "hotel_booking": booking_result,
            "hotel_booking_status": booking_result.get("status") or "confirmed",
            "hotel_booking_attempt_status": "succeeded",
            "hotel_guest_details": {
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": phone,
            },
            "conversation_stage": "trip_complete",
            "next_step": "hotel_booking_complete",
            "assistant_response": confirmation_text,
        })
        return _response(
            confirmation_text,
            "hotel_booking_complete",
            updated,
        )

    # Weather confirmation has its own isolated Gemini classifier. Handle it
    # before general trip-edit extraction so a confirmation or date-change
    # reply cannot be mistaken for a passenger/trip-field update.
    if stage == "weather_confirmation":
        weather_decision = classify_weather_confirmation(message, existing_state)
        print(
            "[WEATHER CONFIRMATION] decision =",
            repr(weather_decision),
            flush=True,
        )

        if weather_decision is True:
            updated = _save_state(config, {
                "messages": existing_state.get("messages", []) + [message],
                "weather_confirmed": True,
                "conversation_stage": "flight_search",
                "next_step": "flight_search",
                "assistant_response": None,
            })
            # This existing helper enforces passenger-count validation before
            # it can call Duffel. No other workflow stage is bypassed.
            return _start_outbound_search(config, updated)

        if weather_decision is False:
            updated = _save_state(config, {
                "messages": existing_state.get("messages", []) + [message],
                "start_date": None,
                "end_date": None,
                "weather": None,
                "weather_confirmed": False,
                "suggested_dates": [],
                "flights": [],
                "outbound_flights": [],
                "return_flights": [],
                "pending_outbound_flights": [],
                "selected_flight": None,
                "selected_outbound_flight": None,
                "selected_return_flight": None,
                "flight_direction": None,
                "conversation_stage": "collecting_info",
                "next_step": "dates",
                "assistant_response": "Sure. What new departure and return dates would you like?",
            })
            return _response(
                updated.get("assistant_response", ""),
                "dates",
                updated,
                weather=None,
            )

        clarification = (
            "I couldn't determine whether you want to keep these dates. "
            "Would you like to continue with the current dates or choose different dates?"
        )
        updated = _save_state(config, {
            "messages": existing_state.get("messages", []) + [message],
            "conversation_stage": "weather_confirmation",
            "next_step": "weather_confirmation",
            "assistant_response": clarification,
        })
        return _response(
            clarification,
            "weather_confirmation",
            updated,
            weather=existing_state.get("weather"),
        )

    # Gemini classifies other user messages. Existing workflow stages remain
    # authoritative so intent classification cannot skip required checks.
    chat_intent = classify_chat_intent(message, existing_state)
    print("\n========== PASSENGER EXTRACTION DEBUG ==========")
    print("Chat intent:", repr(chat_intent))
    print("Adults:", repr(chat_intent.get("adults")))
    print("Children:", repr(chat_intent.get("children")))
    print("Child ages:", repr(chat_intent.get("child_ages")))
    print("================================================\n", flush=True)
    intent_name = chat_intent.get("intent", "unclear")
    print(
        "DEBUG chat intent:",
        intent_name,
        "confidence:",
        chat_intent.get("confidence", 0),
    )
    # A completed trip must not re-enter the planning graph.
    if stage == "trip_complete":
        print("DEBUG checking completed-trip guard:", stage, flush=True)
        normalized = " ".join((message or "").strip().lower().split())

        new_trip_phrases = (
            "plan another trip",
            "start a new trip",
            "new trip",
            "plan a different trip",
            "start planning a new trip",
        )

        if not any(phrase in normalized for phrase in new_trip_phrases):
            completion_text = (
                "Your previous trip plan is already complete. "
                "Your Duffel test flight order was created, and you chose "
                "to skip hotel booking. No hotel reservation was made. "
                "You can start a new trip whenever you are ready."
            )

            return _response(
                completion_text,
                "trip_complete",
                existing_state,
            )
    print("DEBUG conversation stage:", stage)
    print("DEBUG next step:", existing_state.get("next_step"))
    print("DEBUG booking order exists:", bool(existing_state.get("booking_order")))
    print("DEBUG booking status:", existing_state.get("booking_status"))
    print("DEBUG selected flight:", bool(existing_state.get("selected_flight")))
    print("DEBUG hotel skipped:", existing_state.get("hotel_booking_skipped"))

    # High-priority guard: once a flight order exists, a hotel-skip request
    # must complete the trip even if a stale/unexpected graph stage is loaded.
    # Handle this before trip-edit extraction or graph invocation so it cannot
    # restart flight search. Keep this guarded by an existing order/status.
    normalized_message = " ".join((message or "").strip().lower().split())
    skip_hotel_phrases = (
        "skip hotels", "skip hotel", "no hotel", "no hotels",
        "without hotel", "without hotels", "finish my trip plan",
        "finish trip", "finish my trip", "no accommodation",
        "skip accommodation", "finish without hotels",
    )
    order_exists = bool(
        existing_state.get("booking_order")
        or existing_state.get("booking_status") in {"created", "confirmed", "paid"}
    )
    if order_exists and (
        intent_name == "skip_hotel"
        or any(phrase in normalized_message for phrase in skip_hotel_phrases)
    ):
        completion_text = (
            "Your trip plan is complete. Your Duffel test flight order was created, "
            "and you chose to skip hotel booking. No hotel reservation has been made. "
            "Your flight booking reference and order ID are in the flight confirmation."
        )
        updated = _save_state(config, {
            "messages": existing_state.get("messages", []) + [message],
            "conversation_stage": "trip_complete",
            "next_step": "trip_complete",
            "hotel_booking_skipped": True,
            "assistant_response": completion_text,
        })
        return _response(completion_text, "trip_complete", updated, hotels=[])

    # Handle natural-language edits to trip details before stage-specific
    # handlers can mistake them for a flight selection or a confirmation.
    # Gemini extracts all changed fields; the backend owns state mutation,
    # stale-result invalidation, and whether a new search may run.
    editable_stages = {
        "collecting_info", "dates", "weather_confirmation", "flight_search",
        "outbound_results", "return_search", "return_results", "trip_review",
        "budget_confirmation",
    }
    extracted_edits = {}
    field_map = {
        "origin": "origin",
        "destination": "destination",
        "start_date": "start_date",
        "end_date": "end_date",
        "budget": "budget",
        "traveller_count": "traveller_count",
        "adults": "adults",
        "children": "children",
        "child_ages": "child_ages",
        "cabin_class": "cabin_class",
        "journey_type": "journey_type",
    }
    for extracted_key, state_key in field_map.items():
        value = chat_intent.get(extracted_key)
        if value is None or value == "":
            continue
        old_value = existing_state.get(state_key)
        if state_key == "budget":
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

        elif state_key in {"traveller_count", "adults", "children"}:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue

        elif state_key == "child_ages":
            if not isinstance(value, list):
                continue

            try:
                value = [int(age) for age in value]
            except (TypeError, ValueError):
                continue
        if str(value).strip().lower() != str(old_value).strip().lower():
            extracted_edits[state_key] = value

    # Only reinterpret as a trip edit while a trip is still being planned.
    # Once an order has been created, changing dates/routes must not silently
    # mutate or imply cancellation of that existing booking.
    booking_already_created = bool(
        existing_state.get("booking_order")
        or existing_state.get("booking_status") in {"created", "confirmed", "paid"}
    )
    if extracted_edits and stage in editable_stages and not booking_already_created:
        route_changed = any(k in extracted_edits for k in ("origin", "destination"))
        dates_changed = any(k in extracted_edits for k in ("start_date", "end_date"))
        budget_changed = "budget" in extracted_edits
        passenger_or_preference_changed = any(
            k in extracted_edits
            for k in (
                "traveller_count",
                "adults",
                "children",
                "child_ages",
                "cabin_class",
                "journey_type",
            )
        )

        updates = {
            **extracted_edits,
            "messages": existing_state.get("messages", []) + [message],
            "flights": [],
            "outbound_flights": [],
            "return_flights": [],
            "pending_outbound_flights": [],
            "selected_flight": None,
            "selected_outbound_flight": None,
            "selected_return_flight": None,
            "flight_direction": None,
            "budget_override_pending": False,
            "budget_override_confirmed": False,
            "minimum_outbound_fare": None,
            "minimum_return_fare": None,
            "minimum_round_trip_fare": None,
            "next_step": None,
        }
        # Destination/date changes require a fresh weather decision; origin,
        # budget, passenger and cabin changes do not change destination weather.
        if "destination" in extracted_edits or dates_changed:
            updates.update({
                "weather": None,
                "weather_confirmed": None,
                "suggested_dates": [],
                "conversation_stage": "collecting_info",
            })
        elif route_changed or budget_changed or passenger_or_preference_changed:
            # Do not turn a pending weather confirmation into an implicit yes.
            updates.update({
                "conversation_stage": (
                    "weather_confirmation"
                    if stage == "weather_confirmation"
                    else "flight_search"
                ),
                "weather_confirmed": existing_state.get("weather_confirmed"),
            })

        updated = _save_state(config, updates)
        print("[CENTRAL GEMINI] Applied trip edits:", sorted(extracted_edits), flush=True)

        # For a route/budget/passenger/preference edit, immediately search with
        # the updated authoritative values if the required trip details exist.
        # Destination/date edits re-enter the graph so weather is re-evaluated.
        if (
            (route_changed or budget_changed or passenger_or_preference_changed)
            and stage != "weather_confirmation"
            and "destination" not in extracted_edits
            and not dates_changed
            and updated.get("origin") and updated.get("destination")
            and updated.get("start_date") and updated.get("end_date")
        ):
            return _start_outbound_search(config, updated)

        existing_state = updated
        stage = existing_state.get("conversation_stage")
        print("DEBUG saved stage:", stage, flush=True)
        print("DEBUG saved next_step:", existing_state.get("next_step"), flush=True)
        print("DEBUG booking order exists:", bool(existing_state.get("booking_order")), flush=True)
        print("DEBUG booking status:", existing_state.get("booking_status"), flush=True)
        print("DEBUG selected flight exists:", bool(existing_state.get("selected_flight")), flush=True)

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
            return _response(
                "The cheapest flight option exceeds your current trip budget. "
                "Please increase your budget or choose different travel dates "
                "before I can show flight options.",
                "budget_confirmation",
                existing_state,
            )

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
        # Collect and validate passenger counts before flight search.
    if stage == "passenger_count":
        passenger_updates = {
            "messages": existing_state.get("messages", []) + [message],
        }

        for key in ("adults", "children", "child_ages"):
            value = chat_intent.get(key)

            if value is not None:
                passenger_updates[key] = value

        updated = _save_state(config, passenger_updates)

        # Keep the legacy total in sync for existing code that still reads it.
        if (
            updated.get("adults") is not None
            and updated.get("children") is not None
        ):
            updated = _save_state(
                config,
                {
                    "traveller_count": (
                        int(updated["adults"]) + int(updated["children"])
                    ),
                },
            )

        if not _passenger_count_is_valid(updated):
            updated = _save_state(
                config,
                {
                    "passenger_count_confirmed": False,
                    "conversation_stage": "passenger_count",
                    "next_step": "passenger_count",
                    "assistant_response": _passenger_count_prompt(updated),
                },
            )
            return _response(
                updated.get("assistant_response", ""),
                "passenger_count",
                updated,
            )

        updated = _save_state(
            config,
            {
                "passenger_count_confirmed": True,
                "conversation_stage": "flight_search",
                "next_step": "flight_search",
            },
        )

        return _start_outbound_search(config, updated)
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

    
    if stage == "child_age_mismatch_confirmation":
        normalized = " ".join((message or "").strip().lower().split())
        yes_answers = {"yes", "yes update", "update age", "change age", "update it", "yes please"}
        no_answers = {"no", "no correct dob", "correct dob", "keep age", "no i'll correct dob", "no i will correct dob"}
        mismatch = existing_state.get("child_age_mismatch") or {}
        if normalized in yes_answers or normalized.startswith("yes"):
            child_index = mismatch.get("child_index")
            calculated_age = mismatch.get("calculated_age")
            child_ages = list(existing_state.get("child_ages") or [])
            if (
                not isinstance(child_index, int)
                or not isinstance(calculated_age, int)
                or child_index < 0
                or child_index >= len(child_ages)
                or calculated_age < 0
                or calculated_age > 17
            ):
                return _response(
                    "I couldn't safely update the child's age. Please provide the passenger count and child ages again.",
                    "passenger_count",
                    existing_state,
                )
            child_ages[child_index] = calculated_age
            updated = _save_state(config, {
                "child_ages": child_ages,
                "passenger_count_confirmed": True,
                "child_age_mismatch": None,
                "pending_passenger_details": None,
                "flights": [],
                "outbound_flights": [],
                "return_flights": [],
                "selected_flight": None,
                "selected_outbound_flight": None,
                "selected_return_flight": None,
                "conversation_stage": "flight_search",
                "next_step": "flight_search",
                "messages": existing_state.get("messages", []) + [message],
                "assistant_response": (
                    f"Updated the child's age to {calculated_age} based on the date of birth. "
                    "I'm searching for flights again with the corrected passenger information. "
                    "After selecting a new flight, please submit the passenger details again."
                ),
            })
            return _start_outbound_search(config, updated)

        if normalized in no_answers or normalized.startswith("no"):
            updated = _save_state(config, {
                "conversation_stage": "flight_passenger_details",
                "next_step": "flight_passenger_details",
                "messages": existing_state.get("messages", []) + [message],
                "assistant_response": (
                    "No problem. Please correct the date of birth so that the child's age "
                    "matches the age entered earlier on the departure date, then submit all "
                    "passenger details again."
                ),
            })
            return _response(updated["assistant_response"], "flight_passenger_details", updated)

        prompt = (
            f"The child's previously entered age is {mismatch.get('entered_age')} years, "
            f"but the date of birth indicates {mismatch.get('calculated_age')} years "
            f"on the departure date ({existing_state.get('start_date')}). "
            f"Would you like to update the child's age to {mismatch.get('calculated_age')} "
            "based on the date of birth? Reply Yes to update the age and search flights again, "
            "or No to correct the date of birth."
        )
        return _response(prompt, "child_age_mismatch_confirmation", existing_state)

    if stage == "flight_passenger_details":                
        from datetime import date

        updated = _save_state(config, {
            "messages": existing_state.get("messages", []) + [message],
        })
        selected_offer = updated.get("selected_flight") or updated.get("selected_outbound_flight")
        if not selected_offer or not selected_offer.get("offer_id"):
            return _response(
                "I couldn't find the selected Duffel offer. Please select your flight again.",
                "outbound_results",
                updated,
            )
        
        print("\n========== PASSENGER RAW MESSAGE DEBUG ==========")
        print("Raw message:", repr(message))
        print("Message type:", type(message).__name__)

        try:
            debug_payload = json.loads(message)
            print("JSON parsing: SUCCESS")
            print("Parsed type:", type(debug_payload).__name__)
        except (json.JSONDecodeError, TypeError) as exc:
            print("JSON parsing: FAILED")
            print("Error:", str(exc))

        print("=================================================\n", flush=True)

        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("passengers"), list):
            passenger_details = payload["passengers"]
        elif isinstance(payload, list):
            passenger_details = payload
        elif isinstance(payload, dict):
            # Backward-compatible single-passenger format.
            passenger_details = [payload]
        else:
            passenger_details = None

        if passenger_details is None:
            return _response(
                'Please submit passenger details as JSON using this structure: '
                '{"passengers":[{"given_name":"Asha","family_name":"Kumar","title":"ms",'
                '"gender":"f","born_on":"1998-05-20","email":"asha@example.com",'
                '"phone_number":"+919876543210"}]}. Include one object for every passenger.',
                "flight_passenger_details",
                updated,
            )

        adults = int(updated.get("adults") or 1)
        children = int(updated.get("children") or 0)
        expected_count = adults + children
        if len(passenger_details) != expected_count:
            return _response(
                f"You entered {len(passenger_details)} passenger detail record(s), but your trip "
                f"has {adults} adult(s) and {children} child(ren), for {expected_count} passenger(s) "
                "in total. Please submit exactly one details record for each passenger.",
                "flight_passenger_details",
                updated,
            )

        required = (
            "given_name", "family_name", "title", "gender",
            "born_on", "email", "phone_number",
        )
        aliases = {
            "given_name": ("givenname", "first_name", "firstname"),
            "family_name": ("familyname", "last_name", "lastname"),
            "born_on": ("bornon", "date_of_birth", "dateofbirth"),
            "phone_number": ("phonenumber", "phone", "telephone"),
        }
        validated_passengers = []
        departure_date = date.fromisoformat(str(updated.get("start_date")))
        for index, details in enumerate(passenger_details):
            if not isinstance(details, dict):
                return _response(
                    f"Passenger {index + 1}: please provide the details as a JSON object.",
                    "flight_passenger_details",
                    updated,
                )
            details = dict(details)
            for canonical, alternate_names in aliases.items():
                if not details.get(canonical):
                    for alias in alternate_names:
                        if details.get(alias):
                            details[canonical] = details[alias]
                            break

            missing = [
                key for key in required
                if not isinstance(details.get(key), str) or not details[key].strip()
            ]
            if missing:
                return _response(
                    f"Passenger {index + 1} is missing: {', '.join(missing)}. "
                    "Please provide all required details for every passenger.",
                    "flight_passenger_details",
                    updated,
                )
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", details["email"].strip()):
                return _response(
                    f"Please provide a valid email address for passenger {index + 1}.",
                    "flight_passenger_details",
                    updated,
                )
            try:
                birth_date = date.fromisoformat(details["born_on"].strip())
            except ValueError:
                return _response(
                    f"Passenger {index + 1}: date of birth must use YYYY-MM-DD format.",
                    "flight_passenger_details",
                    updated,
                )
            if birth_date > departure_date:
                return _response(
                    f"Passenger {index + 1}: date of birth cannot be after the departure date.",
                    "flight_passenger_details",
                    updated,
                )

            passenger = {key: details[key].strip() for key in required}
            passenger["title"] = passenger["title"].lower()
            passenger["gender"] = passenger["gender"].lower()
            validated_passengers.append(passenger)

        # Passenger order follows the Duffel search order: adults first, then children.
        child_ages = list(updated.get("child_ages") or [])
        for child_index in range(children):
            passenger_index = adults + child_index
            birth_date = date.fromisoformat(validated_passengers[passenger_index]["born_on"])
            age = departure_date.year - birth_date.year
            if (departure_date.month, departure_date.day) < (birth_date.month, birth_date.day):
                age -= 1

            if age < 0 or age > 17:
                return _response(
                    f"Passenger {passenger_index + 1}: the date of birth indicates an age of {age} "
                    "on the departure date. A child passenger must be between 0 and 17 years old. "
                    "If the passenger will be 18 or older, please change the passenger count and "
                    "add them as an adult, then search for flights again.",
                    "flight_passenger_details",
                    updated,
                )

            entered_age = child_ages[child_index]
            if age != entered_age:
                mismatch = {
                    "child_index": child_index,
                    "passenger_index": passenger_index,
                    "entered_age": entered_age,
                    "calculated_age": age,
                    "born_on": validated_passengers[passenger_index]["born_on"],
                }
                prompt = (
                    f"The child's age and date of birth don't match. You previously entered "
                    f"{entered_age} years, but this date of birth indicates {age} years on your "
                    f"departure date ({departure_date.isoformat()}). Would you like to update "
                    f"the child's age to {age} based on the date of birth? Reply Yes to update "
                    "the age and search flights again, or No to correct the date of birth."
                )
                updated = _save_state(config, {
                    "child_age_mismatch": mismatch,
                    "pending_passenger_details": validated_passengers,
                    "conversation_stage": "child_age_mismatch_confirmation",
                    "next_step": "child_age_mismatch_confirmation",
                    "assistant_response": prompt,
                })
                return _response(prompt, "child_age_mismatch_confirmation", updated)

        amount = selected_offer.get("total_amount")
        currency = selected_offer.get("total_currency")
        if amount is None or not currency:
            return _response(
                "The selected offer is missing its original price/currency. Please search and select a fresh flight offer.",
                "outbound_results",
                updated,
            )

        order_result = create_duffel_order(
            offer_id=selected_offer["offer_id"],
            passengers=validated_passengers,
            amount=str(amount),
            currency=str(currency),
        )
        if not order_result.get("success"):
            return _response(
                "Duffel could not confirm the booking: "
                + order_result.get("error", "Unknown error")
                + " Please check the Duffel dashboard before retrying, especially if the request timed out.",
                "flight_passenger_details",
                updated,
            )
        order = order_result["order"]

        # Persist the successful Duffel instant order against the authenticated user.
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
                        "passenger_details": validated_passengers,
                        "passengers": order.get("passengers") or [],
                        "slices": order.get("slices") or [],
                        "provider_created_at": order.get("created_at"),
                        "created_at": existing_booking.get("created_at", now) if existing_booking else now,
                        "updated_at": now,
                    }
                    booking_collection.update_one(
                        {"provider_order_id": provider_order_id, "user_id": str(user_id)},
                        {
                            "$set": {key: value for key, value in booking_record.items() if key != "created_at"},
                            "$setOnInsert": {"created_at": booking_record["created_at"]},
                        },
                        upsert=True,
                    )
            except PyMongoError as exc:
                persistence_error = str(exc)

        display_price_inr = selected_offer.get("price_inr")
        if display_price_inr is not None:
            try:
                display_total = f"₹{float(display_price_inr):,.2f}"
            except (TypeError, ValueError):
                display_total = f"{order.get('total_amount') or amount} {order.get('total_currency') or currency}"
        else:
            display_total = f"{order.get('total_amount') or amount} {order.get('total_currency') or currency}"

        confirmation = (
            "Your Duffel test flight order was created successfully. "
            f"Booking reference: {order.get('booking_reference') or 'not provided'}. "
            f"Order ID: {order.get('id')}. Status: {order.get('status') or 'unknown'}. "
            f"Payment status: {order.get('payment_status') or 'unknown'}. "
            f"Total for {len(validated_passengers)} passenger(s): {display_total}. "
        )
        if persistence_error:
            confirmation += (
                "WARNING: Duffel returned an order, but saving it to your Trip Planner booking history failed. "
                "Do not retry the booking yet; check the Duffel dashboard and server logs first. "
            )
        confirmation += "Would you like to search for hotels, or skip hotels and finish your trip plan?"
        updated = _save_state(config, {
            "user_id": str(user_id) if user_id is not None else "",
            "passenger_details": validated_passengers,
            "booking_order": order,
            "booking_status": order.get("status") or "unknown",
            "booking_payment_status": order.get("payment_status") or "unknown",
            "booking_persistence_error": bool(persistence_error),
            "child_age_mismatch": None,
            "pending_passenger_details": None,
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

        if intent_name == "skip_hotel" or any(phrase in normalized for phrase in skip_phrases):
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

        if intent_name == "ask_hotel" or any(phrase in normalized for phrase in hotel_phrases):
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
                adults=int(existing_state.get("adults") or 1),
                child_ages=existing_state.get("child_ages") or [],
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
    
        # Preserve passenger information extracted from the current message.
    for key in ("adults", "children", "child_ages"):
        value = chat_intent.get(key)

        if value is not None:
            input_state[key] = value
    
    try:
        result = trip_graph.invoke(
            input_state,
            config=config,
        )
    except Exception as exc:
        # Keep the API responsive when a transient LLM/provider failure
        # interrupts graph execution. Log the technical exception for
        # debugging, but return a safe message to the frontend. Do not
        # modify the saved trip state or run any booking/search operation.
        import traceback

        print(
            "[PLANNER GRAPH ERROR] Graph execution failed; "
            f"conversation_id={conversation_id}; error={exc}",
            flush=True,
        )
        traceback.print_exc()

        current_state = existing_state or {}
        return {
            "response": (
                "I'm having trouble processing your request right now. "
                "Please try again in a moment."
            ),
            "next_step": current_state.get("next_step"),
            "weather": current_state.get("weather"),
            "hotels": current_state.get("hotel_results") or [],
            "flights": current_state.get("flights") or [],
            "outbound_flights": current_state.get("outbound_flights") or [],
            "return_flights": current_state.get("return_flights") or [],
            "selected_flight": current_state.get("selected_flight"),
            "selected_outbound_flight": current_state.get("selected_outbound_flight"),
            "selected_return_flight": current_state.get("selected_return_flight"),
            "booking_order": current_state.get("booking_order"),
            "suggested_dates": current_state.get("suggested_dates") or [],
        }

    print("DEBUG graph suggested_dates:", result.get("suggested_dates"))
    print("DEBUG graph stage:", result.get("conversation_stage"))

    response = result.get("assistant_response")
    next_step = result.get("next_step")
    
    print("\n========== TRIP INFORMATION DEBUG ==========")
    print("Origin:", repr(result.get("origin")))
    print("Destination:", repr(result.get("destination")))
    print("Start date:", repr(result.get("start_date")))
    print("End date:", repr(result.get("end_date")))
    print("Travel intent:", repr(result.get("travel_intent")))
    print("Conversation stage:", repr(result.get("conversation_stage")))
    print("Graph next step:", repr(result.get("next_step")))
    print("Service next step:", repr(next_step))
    print("============================================\n", flush=True)

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

    if not isinstance(weather, dict):
        weather = None
    print("\n========== PASSENGER VALIDATION DEBUG ==========")
    print("Next step:", repr(next_step))
    print("Adults:", repr(result.get("adults")))
    print("Children:", repr(result.get("children")))
    print("Child ages:", repr(result.get("child_ages")))
    print("Traveller count:", repr(result.get("traveller_count")))
    print(
        "Passenger count confirmed:",
        repr(result.get("passenger_count_confirmed"))
    )
    print("Validation:", _passenger_count_is_valid(result))
    print("================================================\n")
    # Collect passenger counts after weather confirmation
    # and before starting the flight search.
    if (
        next_step == "flight_search"
        and result.get("origin")
        and result.get("destination")
        and result.get("start_date")
        and result.get("end_date")
        and not _passenger_count_is_valid(result)
    ):
        updated = _save_state(
            config,
            {
                "adults": result.get("adults"),
                "children": result.get("children"),
                "child_ages": result.get("child_ages"),
                "conversation_stage": "passenger_count",
                "next_step": "passenger_count",
                "passenger_count_confirmed": False,
                "assistant_response": _passenger_count_prompt(result),
            },
        )

        return _response(
            updated.get("assistant_response", ""),
            "passenger_count",
            updated,
        )
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
