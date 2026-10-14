import json
import re
from datetime import date, timedelta

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.mongodb import MongoDBSaver
from app.database import client, DATABASE_NAME

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool

from app.ai.state import TripState
from app.config import GEMINI_API_KEY

from app.tools.weather_tool import weather_tool
from app.tools.flight_tool import flight_tool


# ============================================================
# GEMINI
# ============================================================

llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    google_api_key=GEMINI_API_KEY
)


# ============================================================
# CONVERSATION INTENT CLASSIFIER
# ============================================================

def classify_chat_intent(message: str, state: dict | None = None) -> dict:
    """Classify each user message while leaving workflow validation deterministic."""
    state = state or {}
    context = {
        "conversation_stage": state.get("conversation_stage"),
        "next_step": state.get("next_step"),
        "assistant_question": state.get("assistant_response"),
        "origin": state.get("origin"),
        "destination": state.get("destination"),
        "start_date": state.get("start_date"),
        "end_date": state.get("end_date"),
        "budget": state.get("budget"),
        "flight_direction": state.get("flight_direction"),
        "available_flight_count": len(
            state.get("outbound_flights") or state.get("flights") or []
        ),
    }
    prompt = f"""
You classify the intent of the latest user message in a travel-planning chat.
Use context to understand short replies such as "yes", "continue", "50000",
"option 2", "Delhi", and "Goa".

CONTEXT JSON:
{json.dumps(context, ensure_ascii=False)}

LATEST USER MESSAGE:
{message}

Choose exactly one intent from:
start_trip, confirm_travel, decline_travel, provide_origin,
provide_destination, provide_dates, provide_budget, update_budget,
confirm_weather, change_dates, continue_despite_budget,
select_outbound_flight, select_return_flight, ask_hotel,
booking_question, general_question, unclear.

Extract only values supported by the message and context. A bare numeric
message is update_budget only when the current stage is budget_confirmation
or the assistant is asking for a new budget. Do not treat a flight option
number as a budget. Do not claim a booking has occurred.

Return ONLY valid JSON:
{{
  "intent": "one allowed intent",
  "confidence": 0.0,
  "origin": null,
  "destination": null,
  "start_date": null,
  "end_date": null,
  "budget": null,
  "flight_option": null
}}
"""
    try:
        response = llm.invoke(prompt)
        content = get_response_text(response)
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
        parsed = json.loads(content)
        allowed = {
            "start_trip", "confirm_travel", "decline_travel",
            "provide_origin", "provide_destination", "provide_dates",
            "provide_budget", "update_budget", "confirm_weather",
            "change_dates", "continue_despite_budget",
            "select_outbound_flight", "select_return_flight", "ask_hotel",
            "booking_question", "general_question", "unclear",
        }
        if parsed.get("intent") not in allowed:
            parsed["intent"] = "unclear"
        try:
            confidence = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        parsed["confidence"] = max(0.0, min(1.0, confidence))
        return parsed
    except Exception as exc:
        # Keep the current deterministic flow available if Gemini fails.
        print(f"Intent classification fallback: {exc}")
        return {"intent": "unclear", "confidence": 0.0}


# ============================================================
# WEATHER TOOL
# ============================================================

@tool
def get_weather(
    location: str,
    start_date: str | None = None,
    end_date: str | None = None
) -> dict:

    """
    Get weather forecast for a destination
    between the specified travel dates.
    """

    return weather_tool(
        location=location,
        start_date=start_date,
        end_date=end_date
    )


# ============================================================
# FLIGHT TOOL
# ============================================================

@tool
def get_flights(
    origin: str,
    destination: str,
    start_date: str,
    end_date: str | None = None
) -> dict:

    """
    Search available flights between an
    origin and destination.
    """

    return flight_tool(
        origin=origin,
        destination=destination,
        start_date=start_date,
        end_date=end_date
    )


# ============================================================
# RESPONSE TEXT
# ============================================================

def get_response_text(response) -> str:

    content = response.content

    if isinstance(content, str):
        return content

    if isinstance(content, list):

        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("text")
        )

    return str(content)


# ============================================================
# DATE VALIDATION
# ============================================================

def validate_date(
    value: str | None
) -> str | None:

    if not value:
        return None

    try:

        date.fromisoformat(value)

        return value

    except ValueError:

        return None

# ============================================================
# ALTERNATIVE TRAVEL DATE SUGGESTIONS
# ============================================================

def suggest_alternative_date_ranges(
    start_date: str | None,
    end_date: str | None,
    limit: int = 3
) -> list[dict[str, str]]:
    """
    Suggest nearby travel date ranges while preserving
    the original trip duration.

    These are date suggestions only. Their weather has
    not been checked.
    """

    valid_start = validate_date(start_date)
    valid_end = validate_date(end_date)

    if not valid_start or not valid_end:
        return []

    try:
        start = date.fromisoformat(valid_start)
        end = date.fromisoformat(valid_end)
    except ValueError:
        return []

    if end < start:
        return []

    # Keep the original trip duration.
    trip_duration = end - start
    today = date.today()

    # Try nearby dates first, then later dates.
    offsets = (-3, 3, 7, 14, -7)

    suggestions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for offset in offsets:
        candidate_start = start + timedelta(days=offset)
        candidate_end = candidate_start + trip_duration

        # Never suggest a trip starting in the past.
        if candidate_start < today:
            continue

        candidate = (
            candidate_start.isoformat(),
            candidate_end.isoformat()
        )

        # Avoid duplicate dates and the original date range.
        if candidate in seen or candidate_start == start:
            continue

        seen.add(candidate)

        suggestions.append({
            "start_date": candidate[0],
            "end_date": candidate[1]
        })

        if len(suggestions) >= limit:
            break

    return suggestions
# ============================================================
# WEATHER CONFIRMATION CLASSIFIER
# ============================================================

def classify_weather_confirmation(
    message: str
) -> bool | None:
    """Classify the user's meaning in the weather-confirmation stage.

    Explicit negations are handled first so phrases such as
    "I don't want to change dates" cannot be mistaken for a date-change
    request. Ambiguous wording is then reviewed by Gemini.
    """
    normalized = re.sub(r"\s+", " ", message.lower()).strip()

    # Deterministic guardrails for common, unambiguous replies.
    keep_date_phrases = (
        "i don't want to change dates",
        "i do not want to change dates",
        "i don't want to change the dates",
        "i do not want to change the dates",
        "i don't want to change my dates",
        "i do not want to change my dates",
        "don't change dates",
        "do not change dates",
        "keep the dates",
        "keep these dates",
        "keep my dates",
        "no change",
        "no need to change",
        "leave the dates as they are",
        "leave dates as they are",
    )
    if any(phrase in normalized for phrase in keep_date_phrases):
        return True

    change_date_phrases = (
        "change dates",
        "change the dates",
        "change my dates",
        "change travel dates",
        "different dates",
        "new dates",
        "i want to change",
        "i would like to change",
        "i want different dates",
    )
    if any(phrase in normalized for phrase in change_date_phrases):
        return False

    prompt = f"""
The user was shown weather information for their planned trip.
Classify the meaning of their latest reply in this context.

- true: they want to keep the current dates and continue, including
  "I don't want to change dates", "no, keep these dates", or "continue".
- false: they explicitly want to choose different travel dates.
- null: their intent is unclear or they ask something unrelated.

Important: Do not interpret the word "change" alone as a request to
change dates when the sentence explicitly says they do NOT want a change.

User message: {message}

Return ONLY valid JSON, with confirmed set to true, false, or null:
{{"confirmed": true}}
{{"confirmed": false}}
{{"confirmed": null}}
"""

    try:
        response = llm.invoke(prompt)
        content = get_response_text(response).strip()
        # Tolerate fenced JSON returned by the model.
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
        result = json.loads(content)
        value = result.get("confirmed")
        if value is True:
            return True
        if value is False:
            return False
        return None
    except Exception:
        # If the LLM is unavailable or returns malformed output, do not
        # guess that an ambiguous message means the user confirmed.
        return None


# ============================================================
# FLIGHT DATE CHANGE CLASSIFIER
# ============================================================

def classify_date_change(
    message: str
) -> bool:

    prompt = f"""
Determine whether this user message means
that they want to change their travel dates.

User message:

{message}

Examples meaning TRUE:

"change dates"
"I want different dates"
"I want to travel on different dates"
"change my travel dates"
"I want new dates"

Examples meaning FALSE:

"show me those flights"
"which flight is cheapest"
"book the first one"

Return ONLY valid JSON:

{{"change_dates": true}}

or

{{"change_dates": false}}
"""

    response = llm.invoke(prompt)

    content = get_response_text(
        response
    ).strip()

    try:

        result = json.loads(content)

        return bool(
            result.get(
                "change_dates",
                False
            )
        )

    except json.JSONDecodeError:

        normalized = message.lower()

        phrases = [
            "change date",
            "change dates",
            "different date",
            "different dates",
            "new dates",
            "change my travel dates"
        ]

        return any(
            phrase in normalized
            for phrase in phrases
        )


# ============================================================
# FLIGHT SELECTION CLASSIFIER
# ============================================================

def classify_flight_selection(
    message: str,
    flights: list
):

    if not flights:
        return None

    normalized = message.lower().strip()

    # --------------------------------------------------------
    # EXACT DUFFEL OFFER ID
    # --------------------------------------------------------

    for flight in flights:

        offer_id = str(
            flight.get(
                "offer_id",
                ""
            )
        ).lower()

        if (
            offer_id
            and offer_id in normalized
        ):

            return {
                "index":
                    flights.index(flight),

                "offer_id":
                    flight.get(
                        "offer_id"
                    )
            }

    # --------------------------------------------------------
    # ORDINAL WORDS
    # --------------------------------------------------------

    ordinal_map = {

        "first": 0,
        "1st": 0,
        "one": 0,

        "second": 1,
        "2nd": 1,
        "two": 1,

        "third": 2,
        "3rd": 2,
        "three": 2,

        "fourth": 3,
        "4th": 3,
        "four": 3,

        "fifth": 4,
        "5th": 4,
        "five": 4
    }

    # --------------------------------------------------------
    # OPTION / FLIGHT / CHOICE NUMBER
    # --------------------------------------------------------

    numeric_match = re.search(
        r"\b(?:option|flight|choice)\s*"
        r"(?:number\s*)?(\d+)\b",
        normalized
    )

    if numeric_match:

        index = (
            int(
                numeric_match.group(1)
            ) - 1
        )

        if 0 <= index < len(flights):

            return {
                "index": index,

                "offer_id":
                    flights[index].get(
                        "offer_id"
                    )
            }

    # --------------------------------------------------------
    # FIRST / SECOND / THIRD
    # --------------------------------------------------------

    for word, index in ordinal_map.items():

        if re.search(
            rf"\b{re.escape(word)}\b",
            normalized
        ):

            if index < len(flights):

                return {
                    "index": index,

                    "offer_id":
                        flights[index].get(
                            "offer_id"
                        )
                }

    # --------------------------------------------------------
    # GEMINI FALLBACK
    # --------------------------------------------------------

    flight_options = []

    for index, flight in enumerate(
        flights,
        start=1
    ):

        journeys = flight.get(
            "journeys",
            []
        )

        first_journey = (
            journeys[0]
            if journeys
            else {}
        )

        segments = first_journey.get(
            "segments",
            []
        )

        first_segment = (
            segments[0]
            if segments
            else {}
        )

        flight_options.append({

            "option":
                index,

            "offer_id":
                flight.get(
                    "offer_id"
                ),

            "airline":
                flight.get(
                    "airline"
                ),

            "price_inr":
                flight.get(
                    "price_inr"
                ),

            "flight_number":
                first_segment.get(
                    "flight_number"
                )
        })

    prompt = f"""
The user is selecting one flight from
the exact flight options returned by
the travel system.

Flight options:

{json.dumps(
    flight_options,
    indent=2
)}

User message:

{message}

Identify which option the user selected.

Rules:

- Return only an option number from the list.
- Do not invent an option.
- If the message does not clearly select
  one option, return null.

Return ONLY valid JSON:

{{"option": 1}}

or

{{"option": null}}
"""

    response = llm.invoke(
        prompt
    )

    content = get_response_text(
        response
    ).strip()

    try:

        result = json.loads(
            content
        )

        option = result.get(
            "option"
        )

        if isinstance(
            option,
            int
        ):

            index = option - 1

            if 0 <= index < len(flights):

                return {
                    "index":
                        index,

                    "offer_id":
                        flights[index].get(
                            "offer_id"
                        )
                }

    except json.JSONDecodeError:

        pass

    return None


# ============================================================
# EXTRACT TRIP INFORMATION
# ============================================================

def extract_trip_info(
    state: TripState
):

    message = state[
        "messages"
    ][-1].strip()

    today = date.today()

    today_string = today.isoformat()

    current_year = today.year

    origin = state.get(
        "origin"
    )

    destination = state.get(
        "destination"
    )

    start_date = state.get(
        "start_date"
    )

    end_date = state.get(
        "end_date"
    )

    budget = state.get(
        "budget"
    )

    travel_intent = state.get(
        "travel_intent"
    )

    conversation_stage = state.get(
        "conversation_stage"
    )

    # ========================================================
    # DETERMINISTIC HANDLING OF REQUIRED TRIP DETAILS
    # ========================================================
    # Use the previous next_step to interpret short replies such as
    # "yes", "Delhi", and "Goa" in the context of the question asked.
    # This prevents the LLM from repeating a question when it fails to
    # extract a short answer. All downstream weather/flight logic remains
    # unchanged.
    previous_next_step = state.get("next_step")
    normalized_message = message.strip()
    normalized_lower = normalized_message.lower()

    if previous_next_step == "travel_confirmation":
        yes_answers = {
            "yes", "yes please", "yeah", "yep", "y", "sure",
            "of course", "i want to travel", "let's go", "lets go"
        }
        no_answers = {
            "no", "no thanks", "no thank you", "nope", "n",
            "i don't want to travel", "i do not want to travel"
        }

        if normalized_lower in yes_answers:
            return {
                "travel_intent": True,
                "next_step": None,
                "assistant_response": None,
                "conversation_stage": "collecting_info",
                "extracted_info": json.dumps({"travel_intent": True}),
            }

        if normalized_lower in no_answers:
            return {
                "travel_intent": False,
                "next_step": "no_travel",
                "assistant_response": "Okay. Let me know whenever you'd like to plan a trip.",
                "conversation_stage": "complete",
                "extracted_info": json.dumps({"travel_intent": False}),
            }

    if previous_next_step == "origin" and not origin and normalized_message:
        return {
            "travel_intent": True,
            "origin": normalized_message,
            "destination": destination,
            "start_date": start_date,
            "end_date": end_date,
            "budget": budget,
            "next_step": None,
            "assistant_response": None,
            "conversation_stage": "collecting_info",
            "extracted_info": json.dumps({"origin": normalized_message}),
        }

    if (
        previous_next_step == "destination"
        and origin
        and not destination
        and normalized_message
    ):
        return {
            "travel_intent": True,
            "origin": origin,
            "destination": normalized_message,
            "start_date": start_date,
            "end_date": end_date,
            "budget": budget,
            "next_step": None,
            "assistant_response": None,
            "conversation_stage": "collecting_info",
            "extracted_info": json.dumps({"destination": normalized_message}),
        }

    # ========================================================
    # WEATHER CONFIRMATION
    # ========================================================

    if conversation_stage == "weather_confirmation":

        confirmation = (
            classify_weather_confirmation(
                message
            )
        )

        if confirmation is True:

            return {

                "weather_confirmed":
                    True,

                "conversation_stage":
                    "flight_search",

                "next_step":
                    "flight_search",

                "assistant_response":
                    None
            }

        if confirmation is False:

            return {

                "weather":
                    None,

                "weather_confirmed":
                    False,

                "suggested_dates":
                    [],

                "flights":
                    [],

                "conversation_stage":
                    "collecting_info",

                "next_step":
                    "dates",

                "assistant_response":
                    "What dates would you like to travel?"
            }

        return {

            "conversation_stage":
                "weather_confirmation",

            "next_step":
                "weather_confirmation",

            "assistant_response":
                (
                    "Please tell me whether "
                    "you want to continue "
                    "with these dates or "
                    "change your travel dates."
                )
        }

    # ========================================================
    # FLIGHT RESULTS
    # ========================================================

    if conversation_stage == "flight_results":

        flights = state.get(
            "flights",
            []
        )

        selection = (
            classify_flight_selection(
                message,
                flights
            )
        )

        if selection is not None:

            selected_flight = flights[
                selection["index"]
            ]

            return {

                "selected_flight":
                    selected_flight,

                "conversation_stage":
                    "booking",

                "next_step":
                    "booking",

                "assistant_response":
                    (
                        "Great. I have selected "
                        f"flight option "
                        f"{selection['index'] + 1}. "
                        "We can proceed to booking."
                    )
            }

        change_dates = (
            classify_date_change(
                message
            )
        )

        if change_dates:

            return {

                "weather":
                    None,

                "weather_confirmed":
                    None,

                "suggested_dates":
                    [],

                "flights":
                    [],

                "conversation_stage":
                    "collecting_info",

                "next_step":
                    "dates",

                "assistant_response":
                    "What dates would you like to travel?"
            }

    # ========================================================
    # NORMAL EXTRACTION
    # ========================================================

    prompt = f"""
You are the information extraction system
for an AI travel planner.

Today's date is:

{today_string}

Current year:

{current_year}

Current trip information:

Travel intent:
{travel_intent}

Origin:
{origin}

Destination:
{destination}

Start date:
{start_date}

End date:
{end_date}

Budget:
{budget}

Latest user message:

{message}

Extract travel information explicitly
provided by the user.

DATE RULES:

1. Today's date is {today_string}.

2. Current year is {current_year}.

3. If a date is given without a year,
   use {current_year}.

4. Convert dates to YYYY-MM-DD.

5. Do not invent dates.

OTHER RULES:

- Do not invent information.
- Keep existing information.
- Extract origin.
- Extract destination.
- Extract budget.
- Extract dates.
- If the user clearly wants to travel,
  travel_intent = true.
- If the user clearly does not want
  to travel, travel_intent = false.

Return ONLY valid JSON:

{{
    "travel_intent": null,
    "origin": null,
    "destination": null,
    "start_date": null,
    "end_date": null,
    "budget": null
}}
"""

    response = llm.invoke(
        prompt
    )

    content = get_response_text(
        response
    )

    try:

        extracted = json.loads(
            content
        )

    except json.JSONDecodeError:

        extracted = {}

    extracted_start_date = (
        validate_date(
            extracted.get(
                "start_date"
            )
        )
    )

    extracted_end_date = (
        validate_date(
            extracted.get(
                "end_date"
            )
        )
    )

    final_start_date = (
        extracted_start_date
        or start_date
    )

    final_end_date = (
        extracted_end_date
        or end_date
    )

    return {

        "travel_intent":
            (
                extracted.get(
                    "travel_intent"
                )
                if extracted.get(
                    "travel_intent"
                ) is not None
                else travel_intent
            ),

        "origin":
            extracted.get(
                "origin"
            ) or origin,

        "destination":
            extracted.get(
                "destination"
            ) or destination,

        "start_date":
            final_start_date,

        "end_date":
            final_end_date,

        "budget":
            (
                extracted.get(
                    "budget"
                )
                if extracted.get(
                    "budget"
                ) is not None
                else budget
            ),

        "weather":
            None,

        "weather_confirmed":
            None,

        "suggested_dates":
            [],

        "flights":
            [],

        "selected_flight":
            state.get(
                "selected_flight"
            ),

        "conversation_stage":
            "collecting_info",

        "next_step":
            None,

        "assistant_response":
            None,

        "extracted_info":
            content
    }


# ============================================================
# CHECK MISSING INFORMATION
# ============================================================

def check_missing_info(
    state: TripState
):

    if state.get(
        "next_step"
    ) == "dates":

        return {
            "next_step":
                "dates"
        }

    if state.get(
        "next_step"
    ) == "weather_confirmation":

        return {
            "next_step":
                "weather_confirmation"
        }

    if state.get(
        "next_step"
    ) == "flight_search":

        return {
            "next_step":
                "flight_search"
        }

    if state.get(
        "next_step"
    ) == "booking":

        return {
            "next_step":
                "booking"
        }

    if state.get(
        "travel_intent"
    ) is None:

        return {
            "next_step":
                "travel_confirmation"
        }

    if state.get(
        "travel_intent"
    ) is False:

        return {
            "next_step":
                "no_travel"
        }

    if not state.get(
        "origin"
    ):

        return {
            "next_step":
                "origin"
        }

    if not state.get(
        "destination"
    ):

        return {
            "next_step":
                "destination"
        }

    if (
        not state.get(
            "start_date"
        )
        or not state.get(
            "end_date"
        )
    ):

        return {
            "next_step":
                "dates"
        }

    if state.get(
        "budget"
    ) is None:

        return {
            "next_step":
                "budget"
        }

    if state.get(
        "weather_confirmed"
    ) is True:

        return {
            "next_step":
                "flight_search"
        }

    return {
        "next_step":
            "weather_check"
    }


# ============================================================
# WEATHER QUALITY ASSESSMENT
# ============================================================

def _weather_is_unfavorable(weather_result: dict) -> tuple[bool, list[str]]:
    """Assess forecast risk using actual weather-tool fields only.

    Conservative rules: thunderstorms/heavy rain are warnings; rain codes
    or a high precipitation probability also trigger a warning. Unknown
    fields alone do not mark the weather as bad.
    """
    warnings: list[str] = []
    severe_codes = {65, 67, 82, 95, 96, 99}
    rainy_codes = {51, 53, 55, 56, 57, 61, 63, 66, 80, 81}

    def numbers(value):
        if isinstance(value, list):
            return [v for v in value if isinstance(v, (int, float))]
        if isinstance(value, (int, float)):
            return [value]
        return []

    codes = numbers(
        weather_result.get("weather_code")
        or weather_result.get("weather_codes")
        or weather_result.get("weathercode")
    )
    rain_chances = numbers(
        weather_result.get("precipitation_probability")
        or weather_result.get("precipitation_probability_max")
        or weather_result.get("rain_probability")
    )

    if any(int(code) in {95, 96, 99} for code in codes):
        warnings.append("Thunderstorms are forecast on one or more travel days.")
    elif any(int(code) in severe_codes for code in codes):
        warnings.append("Heavy rain or severe weather is forecast on one or more travel days.")
    elif any(int(code) in rainy_codes for code in codes):
        warnings.append("Rain or showers are forecast on one or more travel days.")

    if rain_chances and max(rain_chances) >= 60:
        warnings.append(
            f"The chance of precipitation reaches {int(max(rain_chances))}% on at least one travel day."
        )

    return bool(warnings), warnings


# ============================================================
# WEATHER AGENT
# ============================================================

def weather_agent(
    state: TripState
):

    destination = state.get(
        "destination"
    )

    start_date = state.get(
        "start_date"
    )

    end_date = state.get(
        "end_date"
    )

    prompt = f"""
You are the weather agent for an
AI travel planner.

Destination:
{destination}

Start date:
{start_date}

End date:
{end_date}

You MUST call the get_weather tool.

Use exactly these values:

location = "{destination}"
start_date = "{start_date}"
end_date = "{end_date}"
"""

    weather_llm = llm.bind_tools(
        [get_weather]
    )

    weather_llm.invoke(
        prompt
    )

    weather_result = (
        get_weather.invoke(
            {
                "location":
                    destination,

                "start_date":
                    start_date,

                "end_date":
                    end_date
            }
        )
    )

    # ========================================================
    # WEATHER NOT AVAILABLE
    # ========================================================

    if not weather_result.get(
        "forecast_available",
        False
    ):

        return {

            "weather":
                weather_result,

            # A missing forecast must not count as user confirmation.
            # Keep the user in the weather-confirmation stage so they
            # can explicitly continue or choose different dates.
            "weather_confirmed":
                None,

            "suggested_dates": suggest_alternative_date_ranges(
                start_date,
                end_date
            ),

            "conversation_stage":
                "weather_confirmation",

            "next_step":
                "weather_confirmation",

            "assistant_response":
                (
                    "I couldn't retrieve a weather forecast for "
                    "those dates. You can still continue with these "
                    "dates or choose another date range. Would you "
                    "like to continue or change your travel dates?"
                )
        }

    # ========================================================
    # WEATHER AVAILABLE: classify forecast and route accordingly
    # ========================================================

    unfavorable, weather_warnings = _weather_is_unfavorable(weather_result)

    if unfavorable:
        warning_text = " ".join(weather_warnings)
        return {
            "weather": weather_result,
            "weather_confirmed": None,
            "suggested_dates": suggest_alternative_date_ranges(
                start_date,
                end_date
            ),
            "conversation_stage": "weather_confirmation",
            "next_step": "weather_confirmation",
            "assistant_response": (
                f"⚠️ Weather warning for {destination} from "
                f"{start_date} to {end_date}. {warning_text} "
                "I recommend checking the alternative dates below. "
                "Choose an alternative range to check its forecast, or "
                "reply 'continue' if you want to keep these dates."
            ),
        }

    # Good/acceptable forecast: continue directly to flight search.
    # The service returns the forecast alongside outbound flight results.
    return {
        "weather": weather_result,
        "weather_confirmed": True,
        "suggested_dates": [],
        "conversation_stage": "flight_search",
        "next_step": "flight_search",
        "assistant_response": (
            f"✅ The weather forecast for {destination} from "
            f"{start_date} to {end_date} looks suitable based on the "
            "available forecast. I'll show you outbound flight options now."
        ),
    }


# ============================================================
# FLIGHT AGENT
# ============================================================

def flight_agent(
    state: TripState
):

    origin = state.get(
        "origin"
    )

    destination = state.get(
        "destination"
    )

    start_date = state.get(
        "start_date"
    )

    end_date = state.get(
        "end_date"
    )

    prompt = f"""
You are the flight search agent.

Search flights for:

Origin:
{origin}

Destination:
{destination}

Start date:
{start_date}

End date:
{end_date}

You MUST call the get_flights tool.

Use exactly the supplied values.
"""

    flight_llm = llm.bind_tools(
        [get_flights]
    )

    flight_llm.invoke(
        prompt
    )

    flight_result = (
        get_flights.invoke(
            {
                "origin":
                    origin,

                "destination":
                    destination,

                "start_date":
                    start_date,

                "end_date":
                    end_date
            }
        )
    )

    if not flight_result.get(
        "success"
    ):

        return {

            "flights":
                [],

            "conversation_stage":
                "flight_results",

            "next_step":
                "flight_error",

            "assistant_response":
                (
                    "I couldn't find "
                    "suitable flights "
                    "for those dates."
                )
        }

    final_prompt = f"""
You are an AI travel planner.

Trip:

Origin:
{origin}

Destination:
{destination}

Travel dates:
{start_date} to {end_date}

Budget:
₹{state.get("budget")}

Flight search results:

{json.dumps(
    flight_result,
    indent=2
)}

Present the best flight options.

Rules:

- Use price_inr.
- Display prices using ₹.
- Do not perform currency conversion.
- Mention airline.
- Mention flight number.
- Mention departure.
- Mention arrival.
- Mention duration.
- Separate outbound and return journeys.
- Prioritize options within the user's budget.
- Do not expose API, tool, or JSON details.
"""

    final_response = llm.invoke(
        final_prompt
    )

    return {

        "flights":
            flight_result.get(
                "offers",
                []
            ),

        "selected_flight":
            None,

        "conversation_stage":
            "flight_results",

        "next_step":
            "complete",

        "assistant_response":
            get_response_text(
                final_response
            )
    }


# ============================================================
# GRAPH
# ============================================================

graph = StateGraph(
    TripState
)


graph.add_node(
    "extract_trip_info",
    extract_trip_info
)

graph.add_node(
    "check_missing_info",
    check_missing_info
)

graph.add_node(
    "weather_agent",
    weather_agent
)

graph.add_node(
    "flight_agent",
    flight_agent
)


graph.set_entry_point(
    "extract_trip_info"
)


graph.add_edge(
    "extract_trip_info",
    "check_missing_info"
)


# ============================================================
# ROUTE AFTER MISSING INFO
# ============================================================

def route_after_missing_info(
    state: TripState
):

    next_step = state.get(
        "next_step"
    )

    if next_step == "weather_check":

        return "weather_agent"

    # The planner service owns the dedicated outbound-only search.
    # End here so the legacy flight_agent cannot generate a combined
    # round-trip summary before the service intercepts flight_search.
    if next_step == "flight_search":
        return END

    return END


graph.add_conditional_edges(
    "check_missing_info",
    route_after_missing_info
)


# ============================================================
# ROUTE AFTER WEATHER
# ============================================================

def route_after_weather(
    state: TripState
):

    next_step = state.get(
        "next_step"
    )

    # Never let the general round-trip flight agent run directly after
    # weather assessment. The planner service starts an outbound-only
    # search and preserves this weather assessment in the response.
    # This applies to acceptable weather and unavailable forecasts; bad
    # weather stays at weather_confirmation and does not search flights.
    if next_step in {"flight_search", "weather_confirmation"}:
        return END

    return END


graph.add_conditional_edges(
    "weather_agent",
    route_after_weather
)


# ============================================================
# FLIGHT AGENT END
# ============================================================

graph.add_edge(
    "flight_agent",
    END
)


# ============================================================
# MEMORY
# ============================================================

checkpointer = MongoDBSaver(
    client,
    db_name=DATABASE_NAME,
)

trip_graph = graph.compile(
    checkpointer=checkpointer
)