
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.routes.authentication import router as authentication_router
from app.routes.planner import router as planner_router
from app.routes.booking import router as booking_router
from app.utils.token_utils import decode_access_token


# Routes that must remain accessible without a JWT.
PUBLIC_PATHS = {
    "/",
    "/authentication/signup",
    "/authentication/login",
    "/authentication/forgot-password",
    "/authentication/reset-password",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
}


class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"

        # Preserve CORS preflight requests.
        if request.method == "OPTIONS":
            return await call_next(request)

        # Preserve existing public routes.
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Validate the Bearer token.
        authorization = request.headers.get("Authorization", "")
        parts = authorization.split()

        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Authentication required. Provide a valid Bearer token."
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            payload = decode_access_token(parts[1])
            user_id = payload.get("sub")

            if not user_id:
                raise ValueError("JWT subject is missing")

            # Make the authenticated identity available to route handlers.
            request.state.user_id = str(user_id)

        except Exception:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Invalid or expired authentication token"
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


def create_app():
    app = FastAPI(
        title="Trip Planner API",
        description="AI-powered travel planning backend",
        version="1.0.0",
    )

    # Register JWT middleware first.
    app.add_middleware(JWTAuthMiddleware)

    # Keep CORS outside JWT middleware so browser requests,
    # including authentication error responses, retain CORS handling.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:4200",
            "http://127.0.0.1:4200",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Keep all existing routers.
    app.include_router(authentication_router)
    app.include_router(planner_router)
    app.include_router(booking_router)

    return app
