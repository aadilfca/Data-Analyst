# Add this model at the top of the file (after other imports)
from pydantic import BaseModel

class AutoMapRequest(BaseModel):
    file_id: int
    template_id: int
    sheet_name: str = "Sheet1"

# Then replace the endpoint function:
@router.post("/api/upload/auto-map")
def auto_map_headers(request: AutoMapRequest):
    file_id = request.file_id
    template_id = request.template_id
    sheet_name = request.sheet_name

    # Fetch template columns
    template_cols = execute_query(
        """
        SELECT m.id AS column_id, m.column_name, m.data_type, p.sort_order
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    file_info = execute_query("SELECT file_path FROM uploaded_files WHERE id = ?", (file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")

    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=sheet_name)
        excel_headers = df.columns.tolist()
    except Exception as e:
        # Fallback to first sheet if the specified sheet doesn't exist
        try:
            df = pd.read_excel(file_info[0]['file_path'])
            excel_headers = df.columns.tolist()
        except Exception as e2:
            raise HTTPException(400, f"Error reading Excel file: {str(e2)}")

    mappings = []
    used = set()
    for t_col in template_cols:
        t_name_clean = t_col['column_name'].strip().lower().replace(" ", "")
        best_match = None
        best_score = 0
        for h in excel_headers:
            if h in used:
                continue
            h_clean = str(h).strip().lower().replace(" ", "")
            if h_clean == t_name_clean:
                best_match = h
                best_score = 1.0
                break
            if t_name_clean in h_clean or h_clean in t_name_clean:
                score = min(len(t_name_clean), len(h_clean)) / max(len(t_name_clean), len(h_clean))
                if score > best_score:
                    best_score = score
                    best_match = h
        if best_match is None and len(excel_headers) > len(template_cols):
            pos = template_cols.index(t_col)
            if pos < len(excel_headers) and excel_headers[pos] not in used:
                best_match = excel_headers[pos]
                best_score = 0.3

        mappings.append({
            "column_id": t_col['column_id'],
            "column_name": t_col['column_name'],
            "data_type": t_col['data_type'],
            "excel_column": best_match,
            "is_matched": best_match is not None,
            "match_score": best_score,
            "sort_order": t_col['sort_order']
        })
        if best_match:
            used.add(best_match)

    return {
        "file_id": file_id,
        "template_id": template_id,
        "excel_headers": excel_headers,
        "mappings": mappings,
        "auto_mapped_count": len([m for m in mappings if m['is_matched']]),
        "total_columns": len(mappings)
    }