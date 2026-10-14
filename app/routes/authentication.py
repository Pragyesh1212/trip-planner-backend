from fastapi import APIRouter, Depends

from app.schemas.user_schema import (
    UserSignup,
    UserLogin,
    ForgotPasswordRequest,
    ResetPasswordRequest
)

from app.controllers.authentication_controller import (
    signup_controller,
    login_controller,
    current_user_controller,
    forgot_password_controller,
    reset_password_controller
)

from app.dependencies.authentication import (
    get_current_user_id
)


router = APIRouter(
    prefix="/authentication",
    tags=["Authentication"]
)


@router.post("/signup")
def signup(
    user: UserSignup
):
    return signup_controller(
        user
    )


@router.post("/login")
def login(
    user: UserLogin
):
    return login_controller(
        user
    )


@router.get("/me")
def get_current_user(
    user_id: str = Depends(
        get_current_user_id
    )
):
    return current_user_controller(
        user_id
    )


@router.post("/forgot-password")
def forgot_password(
    user: ForgotPasswordRequest
):
    return forgot_password_controller(
        user
    )


@router.post("/reset-password")
def reset_password(
    user: ResetPasswordRequest
):
    return reset_password_controller(
        user
    )