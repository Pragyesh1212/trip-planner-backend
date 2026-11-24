import os
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_PRIMARY_MODEL = os.getenv(
    "GEMINI_PRIMARY_MODEL",
    "gemini-3.1-flash-lite"
)

GEMINI_FALLBACK_MODEL = os.getenv(
    "GEMINI_FALLBACK_MODEL",
    "gemini-3.5-flash-lite"
)
DUFFEL_API_KEY = os.getenv("DUFFEL_API_KEY")