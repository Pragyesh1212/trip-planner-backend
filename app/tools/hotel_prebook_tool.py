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
        return {"success": False, "message": "A valid hotel offer ID is required.", "error": "Missing offer_id."}
    # Log metadata only; offer IDs are opaque provider tokens and should not be dumped.
    print("LiteAPI prebook offer metadata:", {
        "type": type(offer_id).__name__,
        "length": len(offer_id),
        "prefix": offer_id[:12],
    })

    api_key = os.getenv("LITEAPI_API_KEY", "").strip()
    if not api_key:
        return {"success": False, "message": "Hotel prebooking is not configured.", "error": "LITEAPI_API_KEY is missing."}

    payload = {"offerId": offer_id, "usePaymentSdk": False}
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
        if not response.ok:
            print("LiteAPI prebook HTTP status:", response.status_code)
            print("LiteAPI prebook error body:", response.text[:3000])
            try:
                body = response.json()
            except ValueError:
                body = {}
            err = body.get("error", {}) if isinstance(body, dict) else {}
            provider_message = (
                err.get("description") or err.get("message")
                if isinstance(err, dict) else str(err or "")
            )
            provider_code = err.get("code") if isinstance(err, dict) else None

            # LiteAPI error 2001 / HTTP 409 means the provider can no longer
            # honor the locked selling price for this offer. Do not retry the
            # same offerId: the user must fetch fresh rates and select again.
            normalized_provider_message = str(provider_message or "").lower()
            price_or_availability_conflict = (
                response.status_code == 409
                or str(provider_code) == "2001"
                or "no availability found" in normalized_provider_message
                or "provider price exceeds locked selling price" in normalized_provider_message
            )
            if price_or_availability_conflict:
                message = (
                    "This hotel rate is no longer available at its displayed price. "
                    "Please click ‘Find hotels for this trip’ to fetch fresh rates, "
                    "then select a room again. No hotel booking or payment was made."
                )
                return {
                    "success": False,
                    "message": message,
                    "error": message,
                    "provider_error_code": provider_code,
                    "requires_new_search": True,
                }

            message = provider_message or f"LiteAPI prebooking failed (HTTP {response.status_code})."
            return {"success": False, "message": str(message), "error": str(message)}

        body = response.json()

        # Some provider/proxy responses may carry an error object even with HTTP 200.
        if isinstance(body, dict) and body.get("error"):
            err = body.get("error")
            if isinstance(err, dict):
                message = err.get("description") or err.get("message") or "LiteAPI rejected this hotel offer."
            else:
                message = str(err)
            print("LiteAPI prebook returned provider error:", message[:500])
            return {"success": False, "message": message, "error": message}
    except requests.Timeout:
        return {"success": False, "message": "LiteAPI prebooking timed out. Please retry.", "error": "Request timed out."}
    except requests.RequestException as exc:
        return {"success": False, "message": "Could not contact LiteAPI to revalidate this rate.", "error": str(exc)}
    except ValueError:
        return {"success": False, "message": "LiteAPI returned an unreadable prebooking response.", "error": "Invalid JSON."}

    data = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}

    prebook_id = data.get("prebookId") or data.get("prebook_id")
    if not prebook_id and isinstance(body, dict):
        prebook_id = body.get("prebookId") or body.get("prebook_id")
    price = None
    currency = "INR"
    # LiteAPI response variants can put the total in different nested objects.
    for container in (data.get("price"), data.get("retailRate"), data.get("offerRetailRate"), data.get("finalPrice")):
        if isinstance(container, dict):
            amount = container.get("amount") or container.get("total")
            if isinstance(amount, (int, float, str)):
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
        print("LiteAPI prebook response missing prebookId:", str(body)[:3000])
        return {"success": False, "message": "LiteAPI did not return a prebooking reference for this rate. Please choose another room.", "error": "Missing prebookId."}

    return {
        "success": True,
        "message": "Room rate revalidated successfully. No reservation or payment has been made.",
        "prebook_id": str(prebook_id),
        "price": price,
        "currency": currency,
        "cancellation_policies": data.get("cancellationPolicies") or data.get("cancelPolicy"),
    }
