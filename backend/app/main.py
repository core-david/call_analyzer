from fastapi import FastAPI

app = FastAPI(title="Call Analyzer")

@app.get("/health")
async def health():
    return {"status": "ok"}

