"""Generate synthetic preparation cases; execute isolated existing Python helpers only."""
import ast
import hashlib
import json
import re
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pandas as pd


def main():
    target=Path(sys.argv[1]).resolve()
    if target.exists() and any(target.iterdir()):
        raise RuntimeError("Use a fresh empty fixture directory")
    target.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[2]
    names={"_parse_date_text","_cached_date_text","_cacheable_date_text","parse_date_flexible",
           "classify_date_cell","excel_serial_to_datetime","auto_map_headers"}
    source=ast.parse((root/"routers/import_export.py").read_text(encoding="utf-8"))
    functions=[node for node in source.body if isinstance(node,ast.FunctionDef) and node.name in names]
    if len(functions)!=len(names): raise AssertionError("Existing Python helper changed")
    # Route registration is not part of the pure algorithm under comparison.
    for node in functions:
        if node.name=="auto_map_headers": node.decorator_list=[]
    env={"datetime":datetime,"pd":pd,"re":re,"lru_cache":lru_cache,"AutoMapRequest":SimpleNamespace,
         "DEFAULT_DATE_DELIMITER":"/","DEFAULT_DATE_ORDER":"DMY"}
    exec(compile(ast.Module(body=functions,type_ignores=[]),"isolated_preparation_helpers","exec"),env)
    cases=[]
    for text,delimiter,order in [
        ("29/02/2024","/","DMY"),("29/02/2026","/","DMY"),("31/04/2026","/","DMY"),
        ("04/05/2026","/","DMY"),("04/05/2026","/","MDY"),("2026-11-09","-","YMD"),
        ("09112026","","DMY"),("20261109","","YMD"),(" 11.09.2026 ",".","MDY"),
        ("9/1/2026","/","DMY"),("11-02-2026","/","DMY"),("","/","DMY"),
        ("   ","/","DMY"),("1"*100,"/","DMY"),("00010101","","YMD"),
    ]:
        case={"text":text,"delimiter":delimiter,"order":order}
        try:
            value=None if env["classify_date_cell"](text)=="empty" else env["parse_date_flexible"](text,delimiter,order)
            case["parsed"]=None if value is None else value.date().isoformat()
        except (ValueError,OverflowError): case["error"]=True
        cases.append(case)
    mappings=[]
    for index,(headers,columns) in enumerate([
        (["GP #","Date","Unit","Qty"],[("GP no","document_id"),("Transaction Date","document_date"),("Factory Unit",None),("Unit of measurement",None),("Quantity",None)]),
        (["Bill #","Date","Supplier","Sup Bill #","GRN #","PO #","Qty","Price","Sales Tax","Total"],[("Bill no","document_id"),("Transaction Date","document_date"),("Vendor",None),("vendor Bill no",None),("GRN no",None),("PO no",None),("Receive Quantity",None),("Rate",None),("Tax Amount",None),("Amount","footing")]),
        (["Quantity","Qty","Other"],[("Receive Quantity",None),("Quantity",None)]),
        (["One","Two","Three"],[("Unrelated",None)]),
    ]):
        book=openpyxl.Workbook();sheet=book.active;sheet.title="Data";sheet.append(headers)
        path=target/f"mapping-{index}.xlsx";book.save(path);book.close()
        template=[dict(column_id=i+1,column_name=name,data_type="Text",reconciliation_role=role,sort_order=i)
                  for i,(name,role) in enumerate(columns)]
        def query(sql,params=None):
            if "FROM template_column_mappings" in sql: return template
            if "FROM uploaded_files" in sql: return [{"file_path":str(path)}]
            raise AssertionError("Unexpected database request")
        env["execute_query_pg"]=query
        result=env["auto_map_headers"](SimpleNamespace(file_id=1,template_id=1,sheet_name="Data"))
        mappings.append({"headers":headers,"columns":template,"expected":result["mappings"]})
    book=openpyxl.Workbook();sheet=book.active;sheet.title="Data"
    sheet.append(["Voucher","Date","Amount","Alternative","Duplicate","Duplicate",None])
    sheet.append(["00123","04/05/2026",0,"13/04/2026","one","two",None])
    sheet.append(["subtotal","05/05/2026",12,"14/04/2026",None,None,None])
    sheet.append([None,"06/05/2026",13,"15/04/2026",None,None,None])
    sheet.append(["V4","bad date",14,"16/04/2026",None,None,None])
    sheet.append(["V5",datetime(2026,6,7),15,"17/04/2026",None,None,None]);sheet["B6"].number_format="yyyy-mm-dd"
    sheet.append(["V6",46064,16,"18/04/2026",None,None,None])
    sheet2=book.create_sheet("Other");sheet2.append(["Different"]);sheet2.append(["x"])
    large=book.create_sheet("LargeDates");large.append(["Voucher","Date","Amount"])
    large.append(["V1","x"*5000,1])
    book.save(target/"preparation.xlsx");book.close()
    data={"dates":cases,"mappings":mappings,"sha256":hashlib.sha256((target/"preparation.xlsx").read_bytes()).hexdigest()}
    (target/"oracle.json").write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")


if __name__=="__main__": main()
