import json
import re
from datetime import date, timedelta

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.mongodb import MongoDBSaver
from app.database import client, DATABASE_NAME

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.ai.state import TripState
from app.config import (
    GEMINI_API_KEY,
    GEMINI_PRIMARY_MODEL,
    GEMINI_FALLBACK_MODEL,
)
from google import genai
from app.ai.agent import GeminiAgent
from app.ai.tool_adapters import get_weather as central_get_weather, get_flights as central_get_flights
from app.ai.tool_registry import build_tool_registry

from app.tools.weather_tool import weather_tool
from app.tools.flight_tool import flight_tool


# ============================================================
# GEMINI
# ============================================================

llm = ChatGoogleGenerativeAI(
    model=GEMINI_PRIMARY_MODEL,
    google_api_key=GEMINI_API_KEY
)

fallback_llm = ChatGoogleGenerativeAI(
    model=GEMINI_FALLBACK_MODEL,
    google_api_key=GEMINI_API_KEY
)

def invoke_with_gemini_fallback(primary_model, fallback_model, prompt):
    try:
        return primary_model.invoke(prompt)

    except Exception as exc:
        status_code = (
            getattr(exc, "code", None)
            or getattr(exc, "status_code", None)
        )

        response = getattr(exc, "response", None)

        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        try:
            is_503 = int(status_code) == 503
        except (TypeError, ValueError):
            is_503 = False

        if not is_503:
            raise

        print(
            f"Primary Gemini model failed with HTTP 503. "
            f"Trying fallback model: {GEMINI_FALLBACK_MODEL}",
            flush=True,
        )

        return fallback_model.invoke(prompt)

# Shared Gemini function-calling client. The graph still controls workflow stages;
# only the stage-authorized read/search tool is exposed to each agent invocation.
_gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
_central_tools = build_tool_registry(central_get_weather, central_get_flights)


def _run_central_tool_agent(
    *,
    user_message: str,
    trip_context: dict,
    tool_name: str,
) -> dict:
    """Run the central Gemini tool loop with only the current stage's tool."""
    if _gemini_client is None:
        return {
            "status": "error",
            "message": "Gemini API key is not configured.",
            "tool_calls": [],
            "tool_results": [],
            "iterations": 0,
        }

    allowed_tools = {tool_name: _central_tools[tool_name]}
    print(f"[CENTRAL GEMINI] Starting agent; allowed_tool={tool_name}", flush=True)
    agent = GeminiAgent(
        client=_gemini_client,
        model=GEMINI_PRIMARY_MODEL,
        fallback_model=GEMINI_FALLBACK_MODEL,
        tools=allowed_tools,
        max_iterations=4,
    )
    result = agent.run(
        user_message=user_message,
        trip_context=trip_context,
    )
    print(
        "[CENTRAL GEMINI] Finished agent; "
        f"status={result.get('status')}; "
        f"tool_calls={[call.get('name') for call in result.get('tool_calls', [])]}; "
        f"iterations={result.get('iterations')}",
        flush=True,
    )
    return result



def _run_central_message_agent(
    *,
    draft_message: str,
    trip_context: dict | None = None,
) -> dict:
    """
    Generate natural user-facing planner messages using Gemini.

    Gemini handles wording only. The backend remains responsible for
    validation, workflow decisions, state transitions, and tool execution.
    """
    if _gemini_client is None:
        return {
            "status": "error",
            "message": "",
            "tool_calls": [],
            "tool_results": [],
            "iterations": 0,
        }

    context = dict(trip_context or {})
    context["backend_outcome_draft"] = draft_message or ""

    agent = GeminiAgent(
        client=_gemini_client,
        model=GEMINI_PRIMARY_MODEL,
        fallback_model=GEMINI_FALLBACK_MODEL,
        tools={},
        max_iterations=1,
    )
    return agent.run(
        user_message=(
            "You are the conversational assistant for a travel-planning "
            "application.\n\n"

            "Your responsibility is to communicate the backend's verified "
            "outcome naturally and clearly.\n\n"

            "RULES:\n"
            "1. Use the supplied conversation state and backend outcome "
            "as the only source of truth.\n"
            "2. Generate friendly, natural and concise responses.\n"
            "3. If information is missing, ask only for the information "
            "needed for the current workflow stage.\n"
            "4. If the backend outcome contains a question, preserve its "
            "meaning but phrase it naturally.\n"
            "5. Preserve important prices, dates, locations, booking "
            "references, errors and warnings accurately.\n"
            "6. Never invent passenger details, prices, availability, "
            "booking status or payment status.\n"
            "7. Never claim that a search, booking or payment succeeded "
            "unless the backend outcome confirms it.\n"
            "8. Do not ask questions that have already been answered "
            "in the supplied conversation state.\n"
            "9. Do not change the next action or workflow stage.\n"
            "10. Do not expose internal variable names, tool names, "
            "debug information or implementation details.\n"
            "11. If the backend outcome is an error, explain it clearly "
            "without hiding the error.\n"
            "12. Return only the final message shown to the user. "
            "Do not return JSON or explanations about your reasoning.\n\n"

            "BACKEND OUTCOME:\n"
            f"{draft_message or ''}"
        ),
        trip_context=context,
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
        "traveller_count": state.get("traveller_count"),
        "adults": state.get("adults"),
        "children": state.get("children"),
        "child_ages": state.get("child_ages"),
        "passenger_count_confirmed": state.get("passenger_count_confirmed"),
        "cabin_class": state.get("cabin_class"),
        "journey_type": state.get("journey_type"),
        "hotel_booking_skipped": state.get("hotel_booking_skipped"),
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

Choose the intent that best describes what the user wants NOW. Users may
change their mind at any stage, including while viewing flights or hotels.
Recognize natural phrasing, corrections, and combined requests, not only exact
keywords. Examples include changing the route, dates, budget, traveller count,
trip type or cabin; asking a question; requesting hotel search; skipping hotels;
starting over; selecting an offer; or confirming a pending step.

Choose exactly one primary intent from:
start_trip, confirm_travel, decline_travel, provide_origin,
provide_destination, provide_dates, provide_budget, update_trip_details,
update_budget, confirm_weather, change_dates, continue_despite_budget,
select_outbound_flight, select_return_flight, ask_hotel, skip_hotel,
restart_trip, booking_question, general_question, unclear.

Extract EVERY trip field explicitly supplied or corrected by the user, even when
only one field changes. Preserve other values from context; do not invent them.
If the user says to keep existing dates/route, leave those extracted fields null
unless the message explicitly repeats the values.

A user may change origin, destination, dates, budget, passenger counts,
child ages, cabin class, or journey type in one message.

Extract passenger information when explicitly provided:
- "2 adults and 1 child aged 8" means adults=2, children=1, child_ages=[8].
- "3 adults" means adults=3, children=0, child_ages=[].
- "2 adults and 2 children aged 5 and 10" means
  adults=2, children=2, child_ages=[5, 10].

Do not assume passenger counts or child ages. If a value is not explicitly
provided, return null. If children are explicitly zero, return children=0
and child_ages=[].

Distinguish changing trip details from selecting a flight option. A bare
number is update_budget only when the current stage is budget_confirmation
or the assistant is asking for a new budget. Never claim a booking has occurred.

Return ONLY valid JSON:
{{
  "intent": "one allowed intent",
  "confidence": 0.0,
  "origin": null,
  "destination": null,
  "start_date": null,
  "end_date": null,
  "budget": null,
  "traveller_count": null,
  "adults": null,
  "children": null,
  "child_ages": null,
  "cabin_class": null,
  "journey_type": null,
  "flight_option": null
}}
"""
    try:
        response = invoke_with_gemini_fallback(
            llm,
            fallback_llm,
            prompt
        )
        content = get_response_text(response)
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
        parsed = json.loads(content)
        allowed = {
            "start_trip", "confirm_travel", "decline_travel",
            "provide_origin", "provide_destination", "provide_dates",
            "provide_budget", "update_budget", "confirm_weather",
            "change_dates", "continue_despite_budget",
            "update_trip_details", "select_outbound_flight", "select_return_flight",
            "ask_hotel", "skip_hotel", "restart_trip", "booking_question",
            "general_question", "unclear",
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
# MODEL-DIRECTED TOOL CALLING
# ============================================================

def run_tool_calling_loop(
    system_prompt: str,
    user_prompt: str,
    tools: list,
    max_iterations: int = 6,
) -> dict:
    """
    Let the model choose registered tools, execute its actual tool calls,
    return tool results to the model, and continue until it answers.

    This helper is for read/search tools. Consequential actions such as
    booking and payment must use separate backend validation and explicit
    confirmation; they must not be registered here without those controls.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")

    tool_map = {registered_tool.name: registered_tool for registered_tool in tools}
    model = llm.bind_tools(tools)
    fallback_bound_model = fallback_llm.bind_tools(tools)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    tool_results = []

    for iteration in range(max_iterations):
        response = invoke_with_gemini_fallback(
            model,
            fallback_bound_model,
            messages
        )
        messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            return {
                "response": get_response_text(response),
                "tool_results": tool_results,
                "iterations": iteration + 1,
                "limit_reached": False,
            }

        for index, tool_call in enumerate(tool_calls):
            tool_name = tool_call.get("name")
            tool_args = tool_call.get("args") or {}
            tool_call_id = tool_call.get("id") or f"tool-call-{iteration}-{index}"
            registered_tool = tool_map.get(tool_name)

            if registered_tool is None:
                result = {
                    "success": False,
                    "error": f"Tool '{tool_name}' is not registered.",
                }
            else:
                try:
                    result = registered_tool.invoke(tool_args)
                except Exception as exc:
                    # Keep internal exception details out of the conversation.
                    result = {
                        "success": False,
                        "error": f"Tool execution failed ({type(exc).__name__}).",
                    }

            tool_results.append({
                "tool": tool_name,
                "result": result,
            })
            messages.append(
                ToolMessage(
                    content=json.dumps(result, ensure_ascii=False, default=str),
                    tool_call_id=tool_call_id,
                )
            )

    return {
        "response": (
            "I couldn't complete the request within the tool-call limit. "
            "Please try again."
        ),
        "tool_results": tool_results,
        "iterations": max_iterations,
        "limit_reached": True,
    }


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
    message: str,
    state: dict | None = None,
) -> bool | None:
    """Use Gemini to classify a reply to the current weather/date question.

    Returns:
        True: user wants to keep the current dates and continue.
        False: user wants to choose different dates.
        None: intent is unclear, or Gemini output is unavailable/invalid.

    This classifier is intentionally called only while the conversation is
    in the weather_confirmation stage. It does not mutate workflow state.
    """
    state = state or {}
    context = {
        "conversation_stage": state.get("conversation_stage"),
        "next_step": state.get("next_step"),
        "assistant_question": state.get("assistant_response"),
        "origin": state.get("origin"),
        "destination": state.get("destination"),
        "start_date": state.get("start_date"),
        "end_date": state.get("end_date"),
        "weather_available": isinstance(state.get("weather"), dict),
        "weather_confirmed": state.get("weather_confirmed"),
    }

    prompt = f"""
You are classifying the user's latest reply to a travel planner's question
about whether to keep the current travel dates or choose different dates.

CONVERSATION CONTEXT:
{json.dumps(context, ensure_ascii=False)}

LATEST USER MESSAGE:
{message}

Choose exactly one decision:

- "confirm_dates": The user wants to keep the currently selected dates and
  continue. This includes natural replies such as agreeing, approving,
  proceeding, saying the dates are fine, or asking to go ahead.
- "change_dates": The user wants to choose different travel dates, reschedule,
  or replace the current dates.
- "unclear": The user asks an unrelated question, gives information that does
  not answer the date question, expresses uncertainty, or the meaning is
  ambiguous.

Important rules:
- Interpret the complete meaning, not isolated keywords.
- A sentence such as "I don't want to change the dates" means confirm_dates.
- A sentence such as "I want to change the dates" means change_dates.
- Do not treat passenger counts, budget updates, route updates, or unrelated
  questions as confirmation.
- Do not invent dates or change any trip information.
- Return only valid JSON. Do not include Markdown or explanations.

Required JSON format:
{{
  "decision": "confirm_dates | change_dates | unclear",
  "confidence": 0.0
}}
"""

    try:
        response = invoke_with_gemini_fallback(
            llm,
            fallback_llm,
            prompt
        )
        content = get_response_text(response).strip()
        content = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            content,
            flags=re.I,
        )
        result = json.loads(content)

        decision = result.get("decision")
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        if confidence < 0.65:
            return None
        if decision == "confirm_dates":
            return True
        if decision == "change_dates":
            return False
        return None

    except Exception as exc:
        print(f"[WEATHER CONFIRMATION] Gemini classification failed: {exc}", flush=True)
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

    response = invoke_with_gemini_fallback(
        llm,
        fallback_llm,
        prompt
    )

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

    response = invoke_with_gemini_fallback(
        llm,
        fallback_llm,
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

        confirmation = classify_weather_confirmation(
            message,
            state,
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

    response = invoke_with_gemini_fallback(
        llm,
        fallback_llm,
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

def weather_agent(state: TripState):
    destination = state.get("destination")
    start_date = state.get("start_date")
    end_date = state.get("end_date")

    # This node is reached only after the graph has collected required trip
    # information. The central agent receives the current values, not a sample query.
    tool_run = _run_central_tool_agent(
        tool_name="get_weather",
        user_message=(
            "Retrieve the actual weather forecast for the destination and date "
            "range in the supplied trip context. Call get_weather using those "
            "values only. Do not search flights in this stage."
        ),
        trip_context={
            "destination": destination,
            "start_date": start_date,
            "end_date": end_date,
            "conversation_stage": state.get("conversation_stage"),
        },
    )

    weather_result = next(
        (
            item["result"]
            for item in reversed(tool_run.get("tool_results", []))
            if item.get("tool") == "get_weather"
            and isinstance(item.get("result"), dict)
        ),
        None,
    )
    if weather_result is None:
        weather_result = {
            "forecast_available": False,
            "error": tool_run.get("message") or "The weather tool was not successfully executed.",
        }

    if not weather_result.get("forecast_available", False):
        return {
            "weather": weather_result,
            "weather_confirmed": None,
            "suggested_dates": suggest_alternative_date_ranges(start_date, end_date),
            "conversation_stage": "weather_confirmation",
            "next_step": "weather_confirmation",
            "assistant_response": (
                "I couldn't retrieve a weather forecast for those dates. You can "
                "still continue with these dates or choose another date range. "
                "Would you like to continue or change your travel dates?"
            ),
        }

    unfavorable, weather_warnings = _weather_is_unfavorable(weather_result)
    if unfavorable:
        warning_text = " ".join(weather_warnings)
        return {
            "weather": weather_result,
            "weather_confirmed": None,
            "suggested_dates": suggest_alternative_date_ranges(start_date, end_date),
            "conversation_stage": "weather_confirmation",
            "next_step": "weather_confirmation",
            "assistant_response": (
                f"Weather warning for {destination} from {start_date} to {end_date}. "
                f"{warning_text} I recommend checking the alternative dates below. "
                "Choose an alternative range to check its forecast, or reply "
                "'continue' if you want to keep these dates."
            ),
        }

    return {
        "weather": weather_result,
        "weather_confirmed": True,
        "suggested_dates": [],
        "conversation_stage": "flight_search",
        "next_step": "flight_search",
        "assistant_response": (
            f"The weather forecast for {destination} from {start_date} to {end_date} "
            "looks suitable based on the available forecast. I'll show you outbound "
            "flight options now."
        ),
    }


# ============================================================
# FLIGHT AGENT
# ============================================================

def flight_agent(state: TripState):
    origin = state.get("origin")
    destination = state.get("destination")
    start_date = state.get("start_date")
    end_date = state.get("end_date")
    traveller_count = state.get("traveller_count") or 1
    journey_type = "return" if end_date else "one_way"

    # This node is only reached after the graph's weather/budget gates permit
    # flight search. The agent can call flight search only, never booking/payment.
    tool_run = _run_central_tool_agent(
        tool_name="get_flights",
        user_message=(
            "Search live flight offers using the exact route, dates, and passenger "
            "count in the supplied trip context. Call get_flights with those values. "
            "Do not invent offers and do not book or pay. Do not search weather in this stage."
        ),
        trip_context={
            "origin": origin,
            "destination": destination,
            "start_date": start_date,
            "end_date": end_date,
            "journey_type": journey_type,
            "adults": traveller_count,
            "children": 0,
            "cabin_class": state.get("cabin_class") or "economy",
            "conversation_stage": state.get("conversation_stage"),
        },
    )

    flight_result = next(
        (
            item["result"]
            for item in reversed(tool_run.get("tool_results", []))
            if item.get("tool") == "get_flights"
            and isinstance(item.get("result"), dict)
        ),
        None,
    )
    if flight_result is None:
        flight_result = {
            "success": False,
            "offers": [],
            "error": tool_run.get("message") or "The flight search tool was not successfully executed.",
        }

    if not flight_result.get("success"):
        return {
            "flights": [],
            "conversation_stage": "flight_results",
            "next_step": "flight_error",
            "assistant_response": "I couldn't find suitable flights for those dates.",
        }

    # Keep existing graph state contract: the service formats and presents offers.
    offers = flight_result.get("offers") or flight_result.get("flights") or []
    return {
        "flights": offers,
        "outbound_flights": offers,
        "conversation_stage": "flight_results",
        "next_step": "flight_results",
        "assistant_response": "I found flight options for your trip. Please review the available options.",
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