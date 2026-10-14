from bson import ObjectId

from app.database import users_collection


def find_user_by_email(email: str):
    return users_collection.find_one({
        "email": email
    })


def create_user(user_data: dict):
    return users_collection.insert_one(
        user_data
    )


def find_user_by_id(user_id: str):
    return users_collection.find_one({
        "_id": ObjectId(user_id)
    })


def update_user_password(
    email: str,
    password_hash: str
):
    return users_collection.update_one(
        {
            "email": email
        },
        {
            "$set": {
                "password_hash": password_hash
            }
        }
    )