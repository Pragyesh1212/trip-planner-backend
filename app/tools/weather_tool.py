import requests
from datetime import date, timedelta


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


# Representative locations for Indian states/UTs.
# Used when a user enters a state instead of a city.
INDIAN_STATE_LOCATIONS = {
    "andhra pradesh": {
        "name": "Andhra Pradesh",
        "latitude": 16.5062,
        "longitude": 80.6480,
        "admin1": "Andhra Pradesh"
    },
    "arunachal pradesh": {
        "name": "Arunachal Pradesh",
        "latitude": 27.0844,
        "longitude": 93.6053,
        "admin1": "Arunachal Pradesh"
    },
    "assam": {
        "name": "Assam",
        "latitude": 26.1445,
        "longitude": 91.7362,
        "admin1": "Assam"
    },
    "bihar": {
        "name": "Bihar",
        "latitude": 25.0961,
        "longitude": 85.3131,
        "admin1": "Bihar"
    },
    "chhattisgarh": {
        "name": "Chhattisgarh",
        "latitude": 21.2787,
        "longitude": 81.8661,
        "admin1": "Chhattisgarh"
    },
    "goa": {
        "name": "Goa",
        "latitude": 15.4909,
        "longitude": 73.8278,
        "admin1": "Goa"
    },
    "gujarat": {
        "name": "Gujarat",
        "latitude": 22.2587,
        "longitude": 71.1924,
        "admin1": "Gujarat"
    },
    "haryana": {
        "name": "Haryana",
        "latitude": 29.0588,
        "longitude": 76.0856,
        "admin1": "Haryana"
    },
    "himachal pradesh": {
        "name": "Himachal Pradesh",
        "latitude": 31.1048,
        "longitude": 77.1734,
        "admin1": "Himachal Pradesh"
    },
    "jharkhand": {
        "name": "Jharkhand",
        "latitude": 23.6102,
        "longitude": 85.2799,
        "admin1": "Jharkhand"
    },
    "karnataka": {
        "name": "Karnataka",
        "latitude": 15.3173,
        "longitude": 75.7139,
        "admin1": "Karnataka"
    },
    "kerala": {
        "name": "Kerala",
        "latitude": 10.8505,
        "longitude": 76.2711,
        "admin1": "Kerala"
    },
    "madhya pradesh": {
        "name": "Madhya Pradesh",
        "latitude": 22.9734,
        "longitude": 78.6569,
        "admin1": "Madhya Pradesh"
    },
    "maharashtra": {
        "name": "Maharashtra",
        "latitude": 19.7515,
        "longitude": 75.7139,
        "admin1": "Maharashtra"
    },
    "manipur": {
        "name": "Manipur",
        "latitude": 24.6637,
        "longitude": 93.9063,
        "admin1": "Manipur"
    },
    "meghalaya": {
        "name": "Meghalaya",
        "latitude": 25.4670,
        "longitude": 91.3662,
        "admin1": "Meghalaya"
    },
    "mizoram": {
        "name": "Mizoram",
        "latitude": 23.1645,
        "longitude": 92.9376,
        "admin1": "Mizoram"
    },
    "nagaland": {
        "name": "Nagaland",
        "latitude": 26.1584,
        "longitude": 94.5624,
        "admin1": "Nagaland"
    },
    "odisha": {
        "name": "Odisha",
        "latitude": 20.9517,
        "longitude": 85.0985,
        "admin1": "Odisha"
    },
    "punjab": {
        "name": "Punjab",
        "latitude": 31.1471,
        "longitude": 75.3412,
        "admin1": "Punjab"
    },
    "rajasthan": {
        "name": "Rajasthan",
        "latitude": 27.0238,
        "longitude": 74.2179,
        "admin1": "Rajasthan"
    },
    "sikkim": {
        "name": "Sikkim",
        "latitude": 27.5330,
        "longitude": 88.5122,
        "admin1": "Sikkim"
    },
    "tamil nadu": {
        "name": "Tamil Nadu",
        "latitude": 11.1271,
        "longitude": 78.6569,
        "admin1": "Tamil Nadu"
    },
    "telangana": {
        "name": "Telangana",
        "latitude": 18.1124,
        "longitude": 79.0193,
        "admin1": "Telangana"
    },
    "tripura": {
        "name": "Tripura",
        "latitude": 23.9408,
        "longitude": 91.9882,
        "admin1": "Tripura"
    },
    "uttar pradesh": {
        "name": "Uttar Pradesh",
        "latitude": 26.8467,
        "longitude": 80.9462,
        "admin1": "Uttar Pradesh"
    },
    "uttarakhand": {
        "name": "Uttarakhand",
        "latitude": 30.0668,
        "longitude": 79.0193,
        "admin1": "Uttarakhand"
    },
    "west bengal": {
        "name": "West Bengal",
        "latitude": 22.9868,
        "longitude": 87.8550,
        "admin1": "West Bengal"
    },
    "delhi": {
        "name": "Delhi",
        "latitude": 28.6139,
        "longitude": 77.2090,
        "admin1": "Delhi"
    },
    "jammu and kashmir": {
        "name": "Jammu and Kashmir",
        "latitude": 33.7782,
        "longitude": 76.5762,
        "admin1": "Jammu and Kashmir"
    },
    "ladakh": {
        "name": "Ladakh",
        "latitude": 34.1526,
        "longitude": 77.5771,
        "admin1": "Ladakh"
    },
    "puducherry": {
        "name": "Puducherry",
        "latitude": 11.9416,
        "longitude": 79.8083,
        "admin1": "Puducherry"
    },
    "chandigarh": {
        "name": "Chandigarh",
        "latitude": 30.7333,
        "longitude": 76.7794,
        "admin1": "Chandigarh"
    },
}


def resolve_location(location: str) -> dict | None:

    location = location.strip()
    normalized_location = location.lower()

    # ---------------------------------------------------------
    # 1. Check Indian states/UTs first.
    # ---------------------------------------------------------

    if normalized_location in INDIAN_STATE_LOCATIONS:

        state = INDIAN_STATE_LOCATIONS[normalized_location]

        return {
            "name": state["name"],
            "latitude": state["latitude"],
            "longitude": state["longitude"],
            "country": "India",
            "country_code": "IN",
            "admin1": state["admin1"],
            "timezone": "Asia/Kolkata"
        }

    # ---------------------------------------------------------
    # 2. Search Open-Meteo specifically within India.
    # ---------------------------------------------------------

    response = requests.get(
        GEOCODING_URL,
        params={
            "name": location,
            "count": 10,
            "language": "en",
            "format": "json",
            "countryCode": "IN"
        },
        timeout=10
    )

    response.raise_for_status()

    results = response.json().get("results", [])

    if not results:
        return None

    # ---------------------------------------------------------
    # 3. Prefer exact name match.
    # ---------------------------------------------------------

    for result in results:

        if (
            result.get("name", "").strip().lower()
            == normalized_location
            and result.get("country_code") == "IN"
        ):
            return result

    # ---------------------------------------------------------
    # 4. Otherwise use the first Indian result.
    # ---------------------------------------------------------

    return results[0]


def weather_tool(
    location: str,
    start_date: str | None = None,
    end_date: str | None = None
) -> dict:

    today = date.today()

    # Resolve location
    try:
        place = resolve_location(location)
    except requests.RequestException as error:
        return {
            "success": False,
            "error": f"Location service failed: {str(error)}"
        }

    if not place:
        return {
            "success": False,
            "error": f"Location '{location}' could not be found."
        }

    latitude = place["latitude"]
    longitude = place["longitude"]

    resolved_location = place["name"]
    country = place.get("country")
    country_code = place.get("country_code")
    admin1 = place.get("admin1")

    # ---------------------------------------------------------
    # Default dates
    # ---------------------------------------------------------

    if not start_date:
        start_date = today.isoformat()

    if not end_date:
        end_date = (
            date.fromisoformat(start_date)
            + timedelta(days=1)
        ).isoformat()

    # ---------------------------------------------------------
    # Validate dates
    # ---------------------------------------------------------

    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return {
            "success": False,
            "error": "Invalid date format. Use YYYY-MM-DD."
        }

    if end < start:
        return {
            "success": False,
            "error": "End date cannot be before start date."
        }

    # ---------------------------------------------------------
    # Open-Meteo forecast window
    # ---------------------------------------------------------

    if (start - today).days > 15:
        return {
            "success": False,
            "forecast_available": False,
            "error": (
                "Weather forecasts are currently available "
                "only for approximately the next 16 days."
            ),
            "requested_start_date": start_date,
            "requested_end_date": end_date
        }

    if (end - today).days > 15:
        return {
            "success": False,
            "forecast_available": False,
            "error": (
                "The requested trip extends beyond the "
                "available 16-day weather forecast window."
            ),
            "requested_start_date": start_date,
            "requested_end_date": end_date
        }

    # ---------------------------------------------------------
    # Request weather forecast
    # ---------------------------------------------------------

    try:
        weather_response = requests.get(
            WEATHER_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "daily": ",".join([
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                    "wind_speed_10m_max"
                ]),
                "timezone": "auto",
                "start_date": start_date,
                "end_date": end_date
            },
            timeout=10
        )

    except requests.RequestException as error:
        return {
            "success": False,
            "error": f"Weather service failed: {str(error)}"
        }

    if not weather_response.ok:
        return {
            "success": False,
            "error": (
                f"Weather API returned "
                f"{weather_response.status_code}: "
                f"{weather_response.text}"
            )
        }

    weather_data = weather_response.json()
    daily = weather_data.get("daily", {})

    return {
        "success": True,
        "forecast_available": True,
        "location": resolved_location,
        "admin1": admin1,
        "country": country,
        "country_code": country_code,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": weather_data.get("timezone"),
        "dates": daily.get("time", []),
        "temperature_max": daily.get(
            "temperature_2m_max", []
        ),
        "temperature_min": daily.get(
            "temperature_2m_min", []
        ),
        "precipitation_probability": daily.get(
            "precipitation_probability_max", []
        ),
        "wind_speed": daily.get(
            "wind_speed_10m_max", []
        ),
        "weather_code": daily.get(
            "weather_code", []
        )
    }
