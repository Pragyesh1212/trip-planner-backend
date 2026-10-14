
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.utils.token_utils import decode_access_token
from app.schemas.booking_schema import FlightBookingRequest
from app.controllers.booking_controller import booking_controller


router = APIRouter(
    prefix="/bookings",
    tags=["Flight Bookings"],
)

security = HTTPBearer()


def get_booking_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = payload.get("sub")

        if not user_id:
            raise ValueError("Token has no subject")

        return str(user_id)

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
        )


@router.post("/flights")
def create_booking(
    request: FlightBookingRequest,
    user_id: str = Depends(get_booking_user_id),
):
    return booking_controller(
        request=request,
        user_id=user_id,
    )
