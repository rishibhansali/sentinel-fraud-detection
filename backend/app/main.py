from fastapi import FastAPI

app = FastAPI(title="Sentinel")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
