from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from database import init_db
from routers import dashboard, verification, import_export, templates

# Create the FastAPI app
app = FastAPI(title="System Analyst Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # In production, restrict this
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard.router)
app.include_router(verification.router)
app.include_router(import_export.router)
app.include_router(templates.router)

@app.on_event("startup")
def startup():
    print("🚀 Starting System Analyst App...")
    init_db()
    print("✅ App ready!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)