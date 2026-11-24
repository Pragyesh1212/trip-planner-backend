import requests
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from app.config import DUFFEL_API_KEY


DUFFEL_API_URL = "https://api.duffel.com"
EXCHANGE_RATE_API_URL = "https://api.frankfurter.dev/v2"


def get_headers() -> dict:

    return {
        "Authorization": f"Bearer {DUFFEL_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Duffel-Version": "v2"
    }


def get_exchange_rate(
    from_currency: str,
    to_currency: str = "INR"
) -> dict | None:

    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:

        return {
            "rate": Decimal("1"),
            "date": date.today().isoformat()
        }

    try:

        response = requests.get(
            f"{EXCHANGE_RATE_API_URL}/rate/"
            f"{from_currency}/{to_currency}",
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        return {
            "rate": Decimal(str(data["rate"])),
            "date": data.get("date")
        }

    except (
        requests.RequestException,
        KeyError,
        ValueError
    ):

        return None


def convert_to_inr(
    amount: str,
    currency: str
) -> dict | None:

    currency = currency.upper()

    amount_decimal = Decimal(str(amount))

    if currency == "INR":

        return {
            "amount_inr": amount_decimal.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            ),
            "rate": Decimal("1"),
            "rate_date": date.today().isoformat()
        }

    exchange = get_exchange_rate(
        currency,
        "INR"
    )

    if not exchange:

        return None

    amount_inr = (
        amount_decimal * exchange["rate"]
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    return {
        "amount_inr": amount_inr,
        "rate": exchange["rate"],
        "rate_date": exchange["date"]
    }


def resolve_place(location: str) -> dict | None:

    location = location.strip()
    normalized_location = location.lower()

    response = requests.get(
        f"{DUFFEL_API_URL}/places/suggestions",
        headers=get_headers(),
        params={
            "query": location
        },
        timeout=15
    )

    response.raise_for_status()

    places = response.json().get(
        "data",
        []
    )

    if not places:
        return None

    # 1. Exact city name in India
    for place in places:

        if (
            place.get("type") == "city"
            and place.get("name", "").strip().lower()
            == normalized_location
            and place.get("iata_country_code") == "IN"
        ):
            return place

    # 2. Exact place name in India
    for place in places:

        if (
            place.get("name", "").strip().lower()
            == normalized_location
            and place.get("iata_country_code") == "IN"
        ):
            return place

    # 3. Exact city name regardless of country
    for place in places:

        if (
            place.get("type") == "city"
            and place.get("name", "").strip().lower()
            == normalized_location
        ):
            return place

    # 4. Exact airport name regardless of country
    for place in places:

        if (
            place.get("type") == "airport"
            and place.get("name", "").strip().lower()
            == normalized_location
        ):
            return place

    # 5. Exact city_name match in India
    for place in places:

        if (
            place.get("city_name", "").strip().lower()
            == normalized_location
            and place.get("iata_country_code") == "IN"
        ):
            return place

    # 6. Last resort
    return places[0]


def extract_segment(segment: dict) -> dict:

    operating_carrier = (
        segment.get("operating_carrier")
        or {}
    )

    marketing_carrier = (
        segment.get("marketing_carrier")
        or {}
    )

    departure = segment.get(
        "departing_at"
    )

    arrival = segment.get(
        "arriving_at"
    )

    origin = (
        segment.get("origin")
        or {}
    )

    destination = (
        segment.get("destination")
        or {}
    )

    return {
        "flight_number": segment.get(
            "marketing_carrier_flight_number"
        ),
        "operating_carrier": operating_carrier.get(
            "name"
        ),
        "operating_carrier_iata": operating_carrier.get(
            "iata_code"
        ),
        "marketing_carrier": marketing_carrier.get(
            "name"
        ),
        "origin": {
            "name": origin.get("name"),
            "iata_code": origin.get("iata_code")
        },
        "destination": {
            "name": destination.get("name"),
            "iata_code": destination.get("iata_code")
        },
        "departure": departure,
        "arrival": arrival,
        "duration": segment.get(
            "duration"
        )
    }


def extract_offer(
    offer: dict,
    exchange_rate: Decimal | None = None,
    exchange_rate_date: str | None = None
) -> dict:

    slices = offer.get(
        "slices",
        []
    )

    journeys = []

    for trip_slice in slices:

        segments = trip_slice.get(
            "segments",
            []
        )

        journeys.append({
            "duration": trip_slice.get(
                "duration"
            ),
            "segments": [
                extract_segment(segment)
                for segment in segments
            ]
        })

    owner = (
        offer.get("owner")
        or {}
    )

    original_amount = offer.get(
        "total_amount"
    )

    original_currency = offer.get(
        "total_currency"
    )

    result = {
        "offer_id": offer.get(
            "id"
        ),
        "total_amount": original_amount,
        "total_currency": original_currency,
        "expires_at": offer.get(
            "expires_at"
        ),
        "airline": owner.get(
            "name"
        ),
        "airline_iata": owner.get(
            "iata_code"
        ),
        "live_mode": offer.get(
            "live_mode"
        ),
        "journeys": journeys
    }

    # Add INR display price
    if (
        exchange_rate is not None
        and original_amount is not None
    ):

        price_inr = (
            Decimal(str(original_amount))
            * exchange_rate
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

        result["price_inr"] = float(
            price_inr
        )

        result["price_currency"] = "INR"

        result["exchange_rate"] = float(
            exchange_rate
        )

        result["exchange_rate_date"] = (
            exchange_rate_date
        )

    return result


def flight_tool(
    origin: str,
    destination: str,
    start_date: str,
    end_date: str | None = None,
    cabin_class: str = "economy",
    max_results: int = 5,
    journey_type: str | None = None,
    adults: int = 1,
    children: int = 0,
    multi_city_segments: list[dict] | None = None
) -> dict:

    if not DUFFEL_API_KEY:

        return {
            "success": False,
            "error": (
                "DUFFEL_API_KEY is not configured."
            )
        }

    # Validate passenger counts and search options.
    if not isinstance(adults, int) or adults < 1:
        return {
            "success": False,
            "error": "At least one adult passenger is required."
        }

    if not isinstance(children, int) or children < 0:
        return {
            "success": False,
            "error": "Children count must be zero or greater."
        }

    if adults + children > 9:
        return {
            "success": False,
            "error": "A maximum of 9 passengers can be searched in one request."
        }

    supported_cabin_classes = {
        "economy",
        "premium_economy",
        "business",
        "first"
    }
    cabin_class = cabin_class.strip().lower().replace(" ", "_")
    if cabin_class not in supported_cabin_classes:
        return {
            "success": False,
            "error": (
                "Unsupported cabin class. Use economy, premium_economy, "
                "business, or first."
            )
        }

    if not isinstance(max_results, int) or max_results < 1:
        return {
            "success": False,
            "error": "max_results must be a positive integer."
        }

    journey_type = (journey_type or ("return" if end_date else "one_way")).strip().lower()
    if journey_type in {"one-way", "one way"}:
        journey_type = "one_way"
    elif journey_type in {"round_trip", "round-trip", "round trip"}:
        journey_type = "return"
    elif journey_type in {"multi-city", "multi city", "multicity"}:
        journey_type = "multi_city"

    if journey_type not in {"one_way", "return", "multi_city"}:
        return {
            "success": False,
            "error": "journey_type must be one_way, return, or multi_city."
        }

    # Duffel expects one passenger object per traveller. Preserve the separate
    # adult/child counts in the request rather than searching for one adult only.
    passengers = ([{"type": "adult"} for _ in range(adults)] +
                  [{"type": "child"} for _ in range(children)])

    # Build and validate the journey slices. Each slice is resolved to an IATA
    # code using the existing Duffel place lookup.
    slices = []
    resolved_segment_places = []

    if journey_type == "multi_city":
        if not isinstance(multi_city_segments, list) or len(multi_city_segments) < 2:
            return {
                "success": False,
                "error": (
                    "For a multi-city search, provide at least two "
                    "multi_city_segments. Each segment needs origin, destination, "
                    "and departure_date."
                )
            }

        raw_segments = multi_city_segments
    else:
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date) if end_date else None
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": "Invalid date format. Use YYYY-MM-DD."
            }

        if end and end < start:
            return {
                "success": False,
                "error": "Return date cannot be before the departure date."
            }

        if journey_type == "return" and not end_date:
            return {
                "success": False,
                "error": "A return date is required for a return journey."
            }

        raw_segments = [{
            "origin": origin,
            "destination": destination,
            "departure_date": start_date
        }]

        if journey_type == "return":
            raw_segments.append({
                "origin": destination,
                "destination": origin,
                "departure_date": end_date
            })

    for index, segment in enumerate(raw_segments, start=1):
        if not isinstance(segment, dict):
            return {
                "success": False,
                "error": f"Multi-city segment {index} must be an object."
            }

        segment_origin = str(segment.get("origin") or "").strip()
        segment_destination = str(segment.get("destination") or "").strip()
        segment_date = segment.get("departure_date") or segment.get("date")

        if not segment_origin or not segment_destination or not segment_date:
            return {
                "success": False,
                "error": (
                    f"Journey segment {index} requires origin, destination, "
                    "and departure_date (YYYY-MM-DD)."
                )
            }

        try:
            date.fromisoformat(str(segment_date))
        except ValueError:
            return {
                "success": False,
                "error": f"Invalid departure date in journey segment {index}. Use YYYY-MM-DD."
            }

        try:
            origin_place = resolve_place(segment_origin)
            destination_place = resolve_place(segment_destination)
        except requests.RequestException as error:
            return {
                "success": False,
                "error": (
                    f"Duffel place lookup failed for journey segment {index}: "
                    f"{str(error)}"
                )
            }

        if not origin_place:
            return {
                "success": False,
                "error": f"Origin '{segment_origin}' in journey segment {index} could not be found."
            }

        if not destination_place:
            return {
                "success": False,
                "error": f"Destination '{segment_destination}' in journey segment {index} could not be found."
            }

        origin_code = origin_place.get("iata_code")
        destination_code = destination_place.get("iata_code")

        if not origin_code or not destination_code:
            return {
                "success": False,
                "error": (
                    f"Duffel did not provide an IATA code for journey segment {index}."
                )
            }

        slices.append({
            "origin": origin_code,
            "destination": destination_code,
            "departure_date": str(segment_date)
        })
        resolved_segment_places.append((origin_place, destination_place))

    # Keep the top-level route summary aligned with the first and final slices.
    origin_place = resolved_segment_places[0][0]
    destination_place = resolved_segment_places[-1][1]
    origin_code = slices[0]["origin"]
    destination_code = slices[-1]["destination"]

    request_body = {
        "data": {
            "slices": slices,
            "passengers": passengers,
            "cabin_class": cabin_class,
            "max_connections": 1
        }
    }

    # Search flights
    try:

        response = requests.post(
            f"{DUFFEL_API_URL}/air/offer_requests",
            headers=get_headers(),
            params={
                "return_offers": "true",
                "supplier_timeout": 10000
            },
            json=request_body,
            timeout=20
        )

    except requests.RequestException as error:

        return {
            "success": False,
            "error": (
                "Duffel flight search failed: "
                f"{str(error)}"
            )
        }

    if not response.ok:

        return {
            "success": False,
            "error": (
                f"Duffel API returned "
                f"{response.status_code}: "
                f"{response.text}"
            )
        }

    data = response.json().get(
        "data",
        {}
    )

    offers = data.get(
        "offers",
        []
    )

    # Sort by original price
    offers = sorted(
        offers,
        key=lambda offer: float(
            offer.get(
                "total_amount",
                "999999999"
            )
        )
    )

    # Limit results
    offers = offers[:max_results]

    # Determine currencies
    currencies = {
        offer.get("total_currency")
        for offer in offers
        if offer.get("total_currency")
    }

    exchange_rates = {}

    for currency in currencies:

        conversion = convert_to_inr(
            "1",
            currency
        )

        if conversion:

            exchange_rates[currency] = {
                "rate": conversion["rate"],
                "date": conversion["rate_date"]
            }

    converted_offers = []

    for offer in offers:

        currency = offer.get(
            "total_currency"
        )

        conversion = exchange_rates.get(
            currency
        )

        if conversion:

            converted_offers.append(
                extract_offer(
                    offer,
                    conversion["rate"],
                    conversion["date"]
                )
            )

        else:

            converted_offers.append(
                extract_offer(offer)
            )

    return {
        "success": True,
        "origin": {
            "name": origin_place.get(
                "name"
            ),
            "iata_code": origin_code
        },
        "destination": {
            "name": destination_place.get(
                "name"
            ),
            "iata_code": destination_code
        },
        "start_date": start_date,
        "end_date": end_date,
        "journey_type": journey_type,
        "passenger_counts": {
            "adults": adults,
            "children": children,
            "total": adults + children
        },
        "journey_segments": slices,
        "cabin_class": cabin_class,
        "offer_request_id": data.get(
            "id"
        ),
        "live_mode": data.get(
            "live_mode"
        ),
        "offers": converted_offers,
        "offer_count": len(
            converted_offers
        )
    }

def create_duffel_order(offer_id: str, passengers: list[dict], amount: str, currency: str) -> dict:
    """Create a Duffel Test Mode instant order using account-balance payment.

    Duffel requires each order passenger to reference the passenger ID returned
    by the selected offer. Never guess or generate these IDs.
    """
    if not DUFFEL_API_KEY:
        return {"success": False, "error": "DUFFEL_API_KEY is not configured."}
    if not DUFFEL_API_KEY.startswith("duffel_test_"):
        return {
            "success": False,
            "error": (
                "Booking is blocked because DUFFEL_API_KEY does not look like a "
                "Duffel Test Mode token. Configure a duffel_test_ token before testing."
            ),
        }
    if not offer_id or not passengers:
        return {"success": False, "error": "A selected offer and at least one passenger are required."}

    headers = get_headers()
    try:
        # Get the canonical passenger IDs from the selected offer. Duffel's order
        # passenger objects must refer to these IDs; an empty/made-up ID causes
        # the provider error: Field 'id' can't be blank.
        offer_response = requests.get(
            f"{DUFFEL_API_URL}/air/offers/{offer_id}",
            headers=headers,
            timeout=20,
        )
        try:
            offer_body = offer_response.json()
        except ValueError:
            offer_body = {}

        if not offer_response.ok:
            errors = offer_body.get("errors") or []
            message = "; ".join(
                str(item.get("message") or item.get("title") or item)
                for item in errors
            ) or f"Duffel offer lookup failed (HTTP {offer_response.status_code})."
            return {"success": False, "error": message, "status_code": offer_response.status_code}

        offer = offer_body.get("data") or {}
        if offer.get("live_mode") is True:
            return {"success": False, "error": "Live offers are blocked; this booking flow only supports Duffel Test Mode."}

        offer_passengers = offer.get("passengers") or []
        if len(offer_passengers) != len(passengers):
            return {
                "success": False,
                "error": (
                    f"The selected offer contains {len(offer_passengers)} passenger(s), "
                    f"but {len(passengers)} passenger detail record(s) were supplied. "
                    "The passenger details must match the selected offer exactly. "
                    "Search for a fresh offer with the correct passenger count."
                ),
            }

        order_passengers = []
        for index, passenger in enumerate(passengers):
            passenger_id = offer_passengers[index].get("id")
            if not passenger_id:
                return {
                    "success": False,
                    "error": (
                        "Duffel did not return a passenger ID for the selected offer. "
                        "Search for a fresh offer before retrying."
                    ),
                }
            # Use the offer's passenger ID and the validated details from the caller.
            order_passengers.append({**passenger, "id": passenger_id})

        # Use the verified offer's original price/currency, not a converted display
        # price. This also avoids stale or mismatched client-side totals.
        verified_amount = offer.get("total_amount")
        verified_currency = offer.get("total_currency")
        if verified_amount is None or not verified_currency:
            return {"success": False, "error": "Duffel's selected offer is missing its price or currency. Search again."}

        payload = {
            "data": {
                "type": "instant",
                "selected_offers": [offer_id],
                "passengers": order_passengers,
                "payments": [{
                    "type": "balance",
                    "amount": str(verified_amount),
                    "currency": str(verified_currency).upper(),
                }],
            }
        }

        response = requests.post(
            f"{DUFFEL_API_URL}/air/orders",
            headers=headers,
            json=payload,
            timeout=140,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.ok:
            errors = body.get("errors") or []
            message = "; ".join(
                str(item.get("message") or item.get("title") or item)
                for item in errors
            ) or f"Duffel order creation failed (HTTP {response.status_code})."
            return {"success": False, "error": message, "status_code": response.status_code}

        order = body.get("data") or {}
        if not order.get("id"):
            return {
                "success": False,
                "error": "Duffel returned no order ID. Check the Duffel dashboard before retrying.",
            }
        return {
            "success": True,
            "order": {
                "id": order.get("id"),
                "booking_reference": order.get("booking_reference"),
                "status": order.get("status"),
                "payment_status": order.get("payment_status"),
                "total_amount": order.get("total_amount"),
                "total_currency": order.get("total_currency"),
                "created_at": order.get("created_at"),
                "passengers": order.get("passengers", []),
                "slices": order.get("slices", []),
            },
        }
    except requests.Timeout:
        return {
            "success": False,
            "error": (
                "Duffel is taking longer than expected to create the order. "
                "Check the Duffel dashboard before retrying to avoid duplicate bookings."
            ),
        }
    except requests.RequestException as exc:
        return {"success": False, "error": f"Could not reach Duffel: {exc}"}
