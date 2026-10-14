
from datetime import date
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


class PassengerDetails(BaseModel):
    given_name: str = Field(min_length=1, max_length=100)
    family_name: str = Field(min_length=1, max_length=100)
    born_on: date
    title: Literal["mr", "mrs", "ms", "miss", "dr"]
    gender: Literal["m", "f"]
    email: EmailStr
    phone_number: str = Field(min_length=5, max_length=30)

    @field_validator("born_on")
    @classmethod
    def validate_birth_date(cls, value: date) -> date:
        if value >= date.today():
            raise ValueError("Birth date must be in the past.")
        return value


class FlightBookingRequest(BaseModel):
    offer_id: str = Field(min_length=1)
    passengers: list[PassengerDetails] = Field(
        min_length=1,
        max_length=9
    )
    booking_type: Literal["hold"] = "hold"
