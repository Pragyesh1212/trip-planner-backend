
from app.schemas.planner_schema import (
    PlannerMessageRequest,
    PlannerMessageResponse
)

from app.services.planner_service import (
    process_planner_message
)


def planner_message_controller(
    request: PlannerMessageRequest,
    user_id: str,
):
    result = process_planner_message(
        message=request.message,
        conversation_id=request.conversation_id,
        start_date=request.start_date,
        end_date=request.end_date,
        user_id=str(user_id)
    )

    return PlannerMessageResponse(
        message="Planner request processed successfully",
        response=result["response"],
        next_step=result["next_step"],
        weather=result.get("weather"),
        hotels=result.get("hotels", []),
        flights=result.get("flights"),
        outbound_flights=result.get("outbound_flights"),
        return_flights=result.get("return_flights"),
        selected_flight=result.get("selected_flight"),
        selected_outbound_flight=result.get(
            "selected_outbound_flight"
        ),
        selected_return_flight=result.get(
            "selected_return_flight"
        ),
        suggested_dates=result.get("suggested_dates", [])
    )

from app.schemas.planner_schema import (
    PlannerMessageRequest,
    PlannerMessageResponse
)

from app.services.planner_service import (
    process_planner_message
)


def planner_message_controller(
    request: PlannerMessageRequest,
    user_id: str,
):
    result = process_planner_message(
        message=request.message,
        conversation_id=request.conversation_id,
        start_date=request.start_date,
        end_date=request.end_date,
        user_id=str(user_id)
    )

    return PlannerMessageResponse(
        message="Planner request processed successfully",
        response=result["response"],
        next_step=result["next_step"],
        weather=result.get("weather"),
        hotels=result.get("hotels", []),
        flights=result.get("flights"),
        outbound_flights=result.get("outbound_flights"),
        return_flights=result.get("return_flights"),
        selected_flight=result.get("selected_flight"),
        selected_outbound_flight=result.get(
            "selected_outbound_flight"
        ),
        selected_return_flight=result.get(
            "selected_return_flight"
        ),
        suggested_dates=result.get("suggested_dates", [])
    )
