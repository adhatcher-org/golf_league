"""Main application entry point."""

from fastapi import FastAPI
from .database import init_database
from .config import settings

def create_app(settings=None):
    """Create and configure the FastAPI application."""
    # This function constructs app without import-time I/O
    app = FastAPI(
        title="Golf League",
        version="0.1.0",
        description="Golf League Management Application"
    )
    
    # Initialize database and migrations
    init_database()
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "healthy"}
    
    return app

# For direct execution
if __name__ == "__main__":
    import uvicorn
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8000)