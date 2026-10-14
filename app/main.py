from app.app import create_app


app = create_app()


@app.get("/")
def root():
    return {
        "message": "Trip Planner API is running"
    }