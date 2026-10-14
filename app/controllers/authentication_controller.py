from fastapi import HTTPException, status

from app.schemas.user_schema import (
    UserSignup,
    UserLogin,
    AuthenticationResponse,
    ForgotPasswordRequest,
    ResetPasswordRequest
)

from app.services.authentication_service import (
    signup_user,
    login_user,
    get_current_user,
    check_email_for_password_reset,
    reset_user_password
)


def signup_controller(
    user: UserSignup
):
    try:
        user_id = signup_user(
            user.name,
            user.email,
            user.password
        )

        return {
            "message": "User registered successfully",
            "user_id": user_id
        }

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error)
        )


def login_controller(
    user: UserLogin
):
    try:
        token = login_user(
            user.email,
            user.password
        )

        return AuthenticationResponse(
            message="Login successful",
            access_token=token,
            token_type="bearer"
        )

    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error)
        )

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error)
        )


def current_user_controller(
    user_id: str
):
    try:
        return get_current_user(
            user_id
        )

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error)
        )


def forgot_password_controller(
    user: ForgotPasswordRequest
):
    try:
        check_email_for_password_reset(
            user.email
        )

        return {
            "message": "Email verified successfully"
        }

    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error)
        )


def reset_password_controller(
    user: ResetPasswordRequest
):
    try:
        reset_user_password(
            user.email,
            user.new_password
        )

        return {
            "message": "Password updated successfully"
        }

    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error)
        )

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error)
        )