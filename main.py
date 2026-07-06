from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
from fastapi.responses import FileResponse

app = FastAPI()

# Allow connections
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Database
DB_NAME = "test.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT NOT NULL,
            qty INTEGER NOT NULL,
            rate REAL NOT NULL,
            amount REAL NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def root():
    return {"message": "System Analyst Dashboard"}

# Simple test endpoint
@app.get("/api/test")
def test():
    return {"status": "Server is running!"}