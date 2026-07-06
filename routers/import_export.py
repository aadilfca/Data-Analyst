from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import StreamingResponse
import os, json, openpyxl, pandas as pd
from io import BytesIO
from database import execute_write, log_audit

router = APIRouter()

@router.post("/api/import-excel")
async def import_excel(file: UploadFile = File(...)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx and .xls files are supported")
    contents = await file.read()
    df = pd.read_excel(BytesIO(contents))
    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, file.filename)
    with open(file_path, "wb") as f:
        f.write(contents)
    file_id = execute_write(
        "INSERT INTO uploaded_files (file_name, file_path, file_type, record_count, status) VALUES (?, ?, ?, ?, ?)",
        (file.filename, file_path, 'excel', len(df), 'Pending')
    )
    log_audit("Analyst", "Upload", "Import", f"Uploaded file: {file.filename} ({len(df)} rows)")
    return {"message": f"File '{file.filename}' uploaded successfully ({len(df)} rows)", "file_id": file_id, "record_count": len(df)}

@router.get("/api/download-template")
def download_template():
    # Similar to original – uses the first template's columns or defaults
    # (imports from database)
    ...