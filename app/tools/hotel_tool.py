"""LiteAPI hotel search tool for Trip Planner.

Uses LiteAPI's rates endpoint so returned prices are tied to the requested stay.
Configure LITEAPI_API_KEY in the backend environment before calling this tool.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import requests

LITEAPI_BASE_URL = "https://api.liteapi.travel/v3.0"


def search_hotels(
    destination: str,
    checkin: str,
    checkout: str,
    adults: int = 1,
    currency: str = "INR",
    guest_nationality: str = "IN",
    limit: int = 8,
) -> dict[str, Any]:
    """Search live/sandbox hotel rates for a destination and date range."""
    api_key = os.getenv("LITEAPI_API_KEY", "").strip()
    if not api_key:
        return {
            "success": False,
            "error": "LITEAPI_API_KEY is not configured. Add your LiteAPI sandbox key to the backend environment.",
            "hotels": [],
        }

    destination = (destination or "").strip()
    if not destination:
        return {"success": False, "error": "A destination is required for hotel search.", "hotels": []}

    try:
        checkin_date = date.fromisoformat(checkin)
        checkout_date = date.fromisoformat(checkout)
    except (TypeError, ValueError):
        return {"success": False, "error": "Hotel check-in and check-out dates must use YYYY-MM-DD format.", "hotels": []}

    if checkout_date <= checkin_date:
        return {"success": False, "error": "Hotel check-out must be after check-in.", "hotels": []}
    if adults < 1:
        return {"success": False, "error": "At least one adult guest is required.", "hotels": []}

    
    payload = {
        "checkin": checkin_date.isoformat(),
        "checkout": checkout_date.isoformat(),
        "currency": (currency or "INR").upper(),
        "guestNationality": (guest_nationality or "IN").upper(),
        "occupancies": [
            {
                "adults": int(adults),
                "children": []
            }
        ],
        "countryCode": "IN",
        "cityName": destination,
        "limit": max(1, min(int(limit), 20))
    }


    try:
        response = requests.post(
            f"{LITEAPI_BASE_URL}/hotels/rates",
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )
        if response.status_code in (401, 403):
            return {"success": False, "error": "LiteAPI rejected the API key. Check that LITEAPI_API_KEY is a valid sandbox key.", "hotels": []}
        
        if not response.ok:
            print("LiteAPI hotel search HTTP status:", response.status_code)
            print("LiteAPI hotel search response:", response.text[:3000])
            response.raise_for_status()

        body = response.json()

    except requests.Timeout:
        return {"success": False, "error": "Hotel search timed out. Please try again.", "hotels": []}
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return {"success": False, "error": f"LiteAPI hotel search failed{f' (HTTP {status})' if status else ''}. Check the backend log and try again.", "hotels": []}
    except ValueError:
        return {"success": False, "error": "LiteAPI returned an unreadable response.", "hotels": []}

    # The observed LiteAPI sandbox response is shaped as:
    # data: { rates: [ ... ], hotels: [hotel metadata, ... ] }
    # Each entry in data.rates has offerId, hotelId, offerRetailRate and
    # a nested rates[] list containing room name and detailed retailRate.
    data = body.get("data", {}) if isinstance(body, dict) else {}
    if isinstance(data, list):
        # Keep compatibility with responses that return hotel objects directly.
        hotels_data = data
        offers_data = []
    elif isinstance(data, dict):
        hotels_data = data.get("hotels", []) or []
        offers_data = data.get("rates", []) or []
        if not offers_data and isinstance(data.get("data"), list):
            hotels_data = data["data"]
    else:
        hotels_data, offers_data = [], []

    if not isinstance(hotels_data, list):
        hotels_data = []
    if not isinstance(offers_data, list):
        offers_data = []

    hotel_by_id = {}
    for hotel in hotels_data:
        if isinstance(hotel, dict):
            hotel_id = str(hotel.get("id") or hotel.get("hotelId") or "")
            if hotel_id:
                hotel_by_id[hotel_id] = hotel

    normalized: list[dict[str, Any]] = []

    # Primary shape: each top-level rate is a bookable offer for a hotel.
    for offer in offers_data:
        if not isinstance(offer, dict):
            continue

        hotel_id = str(offer.get("hotelId") or offer.get("hotel_id") or "")
        hotel = hotel_by_id.get(hotel_id, {})
        detail_rates = offer.get("rates") or []
        detail = next((r for r in detail_rates if isinstance(r, dict)), {}) if isinstance(detail_rates, list) else {}

        
        offer_id = offer.get("offerId")

        if offer_id is not None:
            offer_id = str(offer_id).strip()

        price_obj = offer.get("offerRetailRate") or offer.get("retailRate") or {}
        if not isinstance(price_obj, dict):
            price_obj = {}
        amount = price_obj.get("amount")
        currency_value = price_obj.get("currency") or payload["currency"]

        # Some LiteAPI responses put the amount under rates[].retailRate.total[].
        if amount is None:
            retail = detail.get("retailRate") or {}
            totals = retail.get("total", []) if isinstance(retail, dict) else []
            if isinstance(totals, list) and totals and isinstance(totals[0], dict):
                amount = totals[0].get("amount")
                currency_value = totals[0].get("currency") or currency_value
            elif isinstance(retail, dict):
                amount = retail.get("amount")
                currency_value = retail.get("currency") or currency_value

        # Do not show unpriced or non-bookable options.
        if not offer_id or amount is None:
            continue

        cancellation = detail.get("cancellationPolicies") or offer.get("cancellationPolicies") or {}
        if isinstance(cancellation, dict):
            refundable_tag = cancellation.get("refundableTag")
            policy_infos = cancellation.get("cancelPolicyInfos") or []
            cancel_text = "; ".join(
                str(item.get("cancelTime") or item.get("type") or item.get("description"))
                for item in policy_infos if isinstance(item, dict)
                and (item.get("cancelTime") or item.get("type") or item.get("description"))
            ) or None
            refundable = refundable_tag not in ("NRFN", "NON_REFUNDABLE", "non_refundable") if refundable_tag else None
        else:
            cancel_text, refundable = None, None

        print("LiteAPI parsed bookable offer:", {"has_offerId": bool(offer_id), "offerId_length": len(str(offer_id)) if offer_id else 0, "offerId_prefix": str(offer_id)[:12] if offer_id else None})
        normalized.append({
            "hotel_id": hotel_id,
            "name": hotel.get("name") or offer.get("hotelName") or offer.get("name") or "Hotel option",
            "address": hotel.get("address"),
            "city": hotel.get("city_name") or hotel.get("cityName") or destination,
            "rating": hotel.get("rating"),
            "stars": hotel.get("stars"),
            "image_url": hotel.get("main_photo") or hotel.get("thumbnail") or hotel.get("image"),
            "room_name": detail.get("name") or offer.get("roomName") or offer.get("name") or "Room",
            "price": amount,
            "currency": currency_value,
            "rate_id": str(offer_id),
            "offer_id": str(offer_id),
            "refundable": refundable,
            "cancellation_policy": cancel_text,
        })

    # Official LiteAPI response shape:
    # body["data"] is a list of hotels; each hotel contains roomTypes.
    # The bookable offerId belongs to the roomType, while each entry in
    # roomType["rates"] contains the rate name and retail pricing.
    if not normalized:
        # LiteAPI may return hotel/roomTypes records directly in data (list),
        # or wrap those records in data.data / data.hotels (dict response).
        # Use the already-normalized hotel list as a fallback for both shapes.
        fallback_hotels = data if isinstance(data, list) else hotels_data
        for hotel_item in fallback_hotels:
            if not isinstance(hotel_item, dict):
                continue

            hotel_id = str(hotel_item.get("hotelId") or hotel_item.get("id") or "")
            room_types = hotel_item.get("roomTypes") or hotel_item.get("rooms") or []

            for room in room_types:
                if not isinstance(room, dict):
                    continue

                # Only LiteAPI's explicit offerId is valid for prebooking.
                # Do not substitute rateId or a generic room id.
                raw_offer_id = room.get("offerId")
                offer_id = str(raw_offer_id).strip() if raw_offer_id is not None else ""
                if not offer_id:
                    print("LiteAPI room skipped: missing explicit offerId", {"hotel_id": hotel_id})
                    continue

                rates = room.get("rates") or room.get("offers") or []
                if not isinstance(rates, list) or not rates:
                    rates = [room]

                for rate in rates:
                    if not isinstance(rate, dict):
                        continue

                    retail = (
                        rate.get("retailRate")
                        or rate.get("offerRetailRate")
                        or rate.get("price")
                        or room.get("retailRate")
                        or room.get("offerRetailRate")
                        or {}
                    )
                    if not isinstance(retail, dict):
                        retail = {}

                    amount = retail.get("amount")
                    currency_value = retail.get("currency") or payload["currency"]

                    if amount is None:
                        total = retail.get("total")
                        if isinstance(total, list) and total and isinstance(total[0], dict):
                            amount = total[0].get("amount")
                            currency_value = total[0].get("currency") or currency_value

                    if amount is None:
                        continue

                    cancellation = (
                        rate.get("cancellationPolicies")
                        or room.get("cancellationPolicies")
                        or {}
                    )
                    if isinstance(cancellation, dict):
                        refundable_tag = cancellation.get("refundableTag")
                        policy_infos = cancellation.get("cancelPolicyInfos") or []
                        cancel_text = "; ".join(
                            str(item.get("cancelTime") or item.get("type") or item.get("description"))
                            for item in policy_infos
                            if isinstance(item, dict)
                            and (item.get("cancelTime") or item.get("type") or item.get("description"))
                        ) or None
                        refundable = (
                            refundable_tag not in ("NRFN", "NON_REFUNDABLE", "non_refundable")
                            if refundable_tag else None
                        )
                    else:
                        cancel_text, refundable = None, None

                    print(
                        "LiteAPI parsed room offer:",
                        {
                            "has_offerId": bool(offer_id),
                            "offerId_length": len(offer_id),
                            "offerId_prefix": offer_id[:12] if offer_id else None,
                            "hotel_id": hotel_id,
                        },
                    )

                    normalized.append({
                        "hotel_id": hotel_id,
                        "name": hotel_item.get("name") or hotel_item.get("hotelName") or "Hotel option",
                        "address": hotel_item.get("address"),
                        "city": hotel_item.get("cityName") or hotel_item.get("city_name") or destination,
                        "rating": hotel_item.get("rating"),
                        "stars": hotel_item.get("stars"),
                        "image_url": hotel_item.get("main_photo") or hotel_item.get("thumbnail") or hotel_item.get("image"),
                        "room_name": rate.get("name") or rate.get("roomName") or room.get("name") or room.get("roomName") or "Room",
                        "price": amount,
                        "currency": currency_value,
                        "rate_id": offer_id,
                        "offer_id": offer_id,
                        "refundable": refundable,
                        "cancellation_policy": cancel_text,
                    })

    if not normalized:
        return {
            "success": True,
            "hotels": [],
            "message": "LiteAPI returned a response, but no priced bookable offers could be extracted. Check the response data structure and dates.",
            "raw_count": len(offers_data),
        }

    return {"success": True, "hotels": normalized[:limit], "count": len(normalized), "checkin": checkin_date.isoformat(), "checkout": checkout_date.isoformat(), "currency": payload["currency"], "sandbox": True}
