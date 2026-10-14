import os

import certifi

from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()


MONGODB_URL = os.getenv("MONGODB_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME")


client = MongoClient(
    MONGODB_URL,
    tls=True,
    tlsCAFile=certifi.where()
)


db = client[DATABASE_NAME]


users_collection = db["users"]