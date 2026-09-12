from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    # GL-02 fills in the lifespan body
    yield


def create_app(settings=None) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    return app


app = create_app()
