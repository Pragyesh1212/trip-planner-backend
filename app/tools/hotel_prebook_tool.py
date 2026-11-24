
"""LiteAPI hotel rate prebooking (revalidation) tool.

This does not create a hotel reservation or take payment.
"""
from __future__ import annotations

import os
from typing import Any

import requests

LITEAPI_BOOK_URL = "https://book.liteapi.travel/v3.0"


def prebook_hotel_offer(offer_id: str) -> dict[str, Any]:
    offer_id = (offer_id or "").strip()

    if not offer_id:
        return {
            "success": False,
            "message": "A valid hotel offer ID is required.",
            "error": "Missing offer_id.",
        }

    print("LiteAPI prebook offer metadata:", {
        "type": type(offer_id).__name__,
        "length": len(offer_id),
    })

    api_key = os.getenv("LITEAPI_API_KEY", "").strip()

    if not api_key:
        return {
            "success": False,
            "message": "Hotel prebooking is not configured.",
            "error": "LITEAPI_API_KEY is missing.",
        }

    # Occupancy is already associated with the selected offerId.
    payload = {
        "offerId": offer_id,
        "usePaymentSdk": False,
    }

    try:
        response = requests.post(
            f"{LITEAPI_BOOK_URL}/rates/prebook",
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )

        try:
            body = response.json()
        except ValueError:
            body = {}

        if not response.ok:
            err = body.get("error", {}) if isinstance(body, dict) else {}

            provider_message = (
                err.get("description")
                or err.get("message")
                if isinstance(err, dict)
                else str(err or "")
            )

            provider_code = (
                err.get("code")
                if isinstance(err, dict)
                else None
            )

            print("LiteAPI prebook HTTP status:", response.status_code)
            print("LiteAPI prebook provider code:", provider_code)

            normalized_message = str(provider_message or "").lower()

            price_or_availability_conflict = (
                response.status_code == 409
                or str(provider_code) == "2001"
                or "no availability found" in normalized_message
                or "provider price exceeds locked selling price" in normalized_message
            )

            if price_or_availability_conflict:
                message = (
                    "This hotel rate is no longer available at its displayed price. "
                    "Please fetch fresh hotel rates and select a room again. "
                    "No hotel booking or payment was made."
                )

                return {
                    "success": False,
                    "message": message,
                    "error": message,
                    "provider_error_code": provider_code,
                    "requires_new_search": True,
                }

            message = (
                provider_message
                or f"LiteAPI prebooking failed (HTTP {response.status_code})."
            )

            return {
                "success": False,
                "message": str(message),
                "error": str(message),
            }

        if isinstance(body, dict) and body.get("error"):
            err = body["error"]

            if isinstance(err, dict):
                message = (
                    err.get("description")
                    or err.get("message")
                    or "LiteAPI rejected this hotel offer."
                )
            else:
                message = str(err)

            return {
                "success": False,
                "message": message,
                "error": message,
            }

    except requests.Timeout:
        return {
            "success": False,
            "message": "LiteAPI prebooking timed out. Please retry.",
            "error": "Request timed out.",
        }

    except requests.RequestException as exc:
        return {
            "success": False,
            "message": "Could not contact LiteAPI to revalidate this rate.",
            "error": str(exc),
        }

    data = body.get("data", body) if isinstance(body, dict) else {}

    if not isinstance(data, dict):
        data = {}

    prebook_id = (
        data.get("prebookId")
        or data.get("prebook_id")
        or (body.get("prebookId") if isinstance(body, dict) else None)
        or (body.get("prebook_id") if isinstance(body, dict) else None)
    )

    # Log only occupancy-related metadata if LiteAPI returns it.
    print("LiteAPI prebook response metadata:", {
        "has_prebook_id": bool(prebook_id),
        "response_keys": list(data.keys()),
        "adult_count": data.get("adultCount"),
        "child_count": data.get("childCount"),
        "children_ages": data.get("childrenAges"),
        "occupancy_number": data.get("occupancyNumber"),
    })

    price = None
    currency = "INR"

    for container in (
        data.get("price"),
        data.get("retailRate"),
        data.get("offerRetailRate"),
        data.get("finalPrice"),
    ):
        if not isinstance(container, dict):
            continue

        amount = container.get("amount")

        if amount is None:
            amount = container.get("total")

        if amount is not None and not isinstance(amount, list):
            price = amount
            currency = container.get("currency") or currency
            break

        if isinstance(amount, list):
            for item in amount:
                if isinstance(item, dict) and item.get("amount") is not None:
                    price = item["amount"]
                    currency = item.get("currency") or currency
                    break

        if price is not None:
            break

    if not prebook_id:
        print(
            "LiteAPI prebook response missing prebookId. "
            "Inspect the provider response securely."
        )

        return {
            "success": False,
            "message": (
                "LiteAPI did not return a prebooking reference "
                "for this rate. Please choose another room."
            ),
            "error": "Missing prebookId.",
        }

    return {
        "success": True,
        "message": (
            "Room rate revalidated successfully. "
            "No reservation or payment has been made."
        ),
        "prebook_id": str(prebook_id),
        "price": price,
        "currency": currency,
        "cancellation_policies": (
            data.get("cancellationPolicies")
            or data.get("cancelPolicy")
        ),
    }
