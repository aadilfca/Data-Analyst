from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from database import init_db
from routers import dashboard, verification, import_export, templates, clients, data

# ✅ CREATE the app instance FIRST
app = FastAPI(title="System Analyst Dashboard", version="1.0.0")

# Then add middleware and static files
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ✅ THEN include routers
app.include_router(dashboard.router)
app.include_router(verification.router)
app.include_router(import_export.router)
app.include_router(templates.router)
app.include_router(clients.router)
app.include_router(data.router)

# Startup event and run block...
@app.on_event("startup")
def startup():
    print("🚀 Starting System Analyst App...")
    init_db()
    print("✅ App ready!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)