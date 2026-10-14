from app.repositories.user_repository import (
    find_user_by_email,
    create_user,
    find_user_by_id,
    update_user_password
)

from app.utils.password_utils import (
    hash_password,
    verify_password
)

from app.utils.token_utils import create_access_token


def signup_user(
    name: str,
    email: str,
    password: str
):
    existing_user = find_user_by_email(email)

    if existing_user:
        raise ValueError(
            "Email already registered"
        )

    hashed_password = hash_password(
        password
    )

    user_data = {
        "name": name,
        "email": email,
        "password_hash": hashed_password
    }

    result = create_user(
        user_data
    )

    return str(
        result.inserted_id
    )


def login_user(
    email: str,
    password: str
):
    existing_user = find_user_by_email(
        email
    )

    if not existing_user:
        raise LookupError(
            "User not registered"
        )

    password_valid = verify_password(
        password,
        existing_user["password_hash"]
    )

    if not password_valid:
        raise ValueError(
            "Invalid password"
        )

    token = create_access_token(
        str(existing_user["_id"])
    )

    return token


def get_current_user(
    user_id: str
):
    user = find_user_by_id(
        user_id
    )

    if not user:
        raise ValueError(
            "User not found"
        )

    return {
        "id": str(user["_id"]),
        "name": user["name"],
        "email": user["email"]
    }


def check_email_for_password_reset(
    email: str
):
    existing_user = find_user_by_email(
        email
    )

    if not existing_user:
        raise LookupError(
            "Email is not registered"
        )

    return True


def reset_user_password(
    email: str,
    new_password: str
):
    existing_user = find_user_by_email(
        email
    )

    if not existing_user:
        raise LookupError(
            "Email is not registered"
        )

    hashed_password = hash_password(
        new_password
    )

    result = update_user_password(
        email,
        hashed_password
    )

    if result.modified_count == 0:
        raise ValueError(
            "Password could not be updated"
        )

    return True