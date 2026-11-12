"""Synthetic validation vectors using isolated current Python helpers; no app/DB import."""
import ast
import contextvars
import hashlib
import json
import math
import re
import sys
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pandas as pd
from fastapi import HTTPException


def main():
    target=Path(sys.argv[1]).resolve()
    if target.exists() and any(target.iterdir()): raise RuntimeError("Use a fresh fixture directory")
    target.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[2]
    names={"_ledger_decimal","json_safe_cell_value","_workbook_input_errors","get_critical_field_columns",
           "find_critical_blank_reasons","_parse_date_text","_cached_date_text","_cacheable_date_text",
           "parse_date_flexible","classify_date_cell","excel_serial_to_datetime","_parse_date_cell"}
    source=ast.parse((root/"routers/import_export.py").read_text(encoding="utf-8"))
    nodes=[node for node in source.body if isinstance(node,ast.FunctionDef) and node.name in names]
    if len(nodes)!=len(names): raise AssertionError("Python helper inventory changed")
    env=dict(pd=pd,openpyxl=openpyxl,math=math,re=re,Decimal=Decimal,InvalidOperation=InvalidOperation,
             datetime=datetime,time=time,lru_cache=lru_cache,HTTPException=HTTPException,
             _ledger_context=contextvars.ContextVar("isolated_ledger",default=None),DEFAULT_DATE_DELIMITER="/",DEFAULT_DATE_ORDER="DMY",
             CRITICAL_FIELD_LABELS={"document_id":"Document ID","date":"Date","amount":"Amount"},
             footing_columns=lambda columns:[c for c in columns if c.get("reconciliation_role")=="footing"],
             resolve_document_column=lambda columns:next((c for c in columns if c.get("reconciliation_role")=="document_id"),columns[0]),
             resolve_document_date_column=lambda columns:next((c for c in columns if c.get("reconciliation_role")=="document_date"),None))
    exec(compile(ast.Module(body=nodes,type_ignores=[]),"isolated_validation_helpers","exec"),env)
    vectors=[]
    values=[None,"",0,-125,125.25,True,False,"125.25","-0",".5","1.","1e3","1e-9999","1e9999",
            "1_000.5","1__0","1_e2","١٢٣.٥","𝟙𝟚.𝟝","\u00a0 125.25 \u00a0","\x1c125\x1f","1,234.50",
            "12,34","NaN","nan","+NaN","Infinity","-inf","NULL","#REF!","9007199254740993",
            "0.1000000000000000000000000000000001","-0.00","DR 125","125 CR","(125) CR","(125) DR","-125 DR","-125 CR","CR125"]
    for ledger,balance in [(False,False),(True,False),(True,True)]:
        for value in values:
            kind="Blank" if value is None else "Boolean" if isinstance(value,bool) else "Number" if isinstance(value,(int,float)) else "Text"
            vector={"kind":kind,"value":value if kind=="Boolean" else None if value is None else str(value),"ledger":ledger,"balance":balance}
            try:
                if value is None or isinstance(value,str) and not value.strip(): valid=True;number=None
                elif ledger: number=env["_ledger_decimal"](value,balance=balance);valid=True
                else: number=float(value);valid=math.isfinite(number)
                vector["valid"]=valid
                if valid and number is not None: vector.update(negative=number<0,zero=number==0)
            except (ValueError,TypeError,OverflowError,HTTPException,InvalidOperation): vector["valid"]=False
            vectors.append(vector)
    book=openpyxl.Workbook();book.remove(book.active)
    columns=[dict(id=i,column_name=name,data_type=kind,reconciliation_role=role) for i,(name,kind,role) in enumerate([
        ("Voucher","Text","document_id"),("Date","Date","document_date"),("Account","Text",None),
        ("Debit","Numeric",None),("Credit","Numeric",None)],1)]
    cases=[]
    for name,column,value,literal,footing in [
        ("valid",4,125,False,False),("zero",4,0,False,False),("negative",4,-125,False,False),
        ("nonfinite",4,"Infinity",False,False),("nan-number",4,"NaN",True,False),("overflow",4,"1e9999",False,False),
        ("invalid-number",4,"125x",False,False),("comma-number",4,"1,250",False,False),
        ("excel-error",3,"#REF!",False,False),("literal-error",3,"#REF!",True,False),
        ("text-nan",3,"NaN",True,False),("blank-voucher",1,None,False,False),
        ("blank-date",2,None,False,False),("blank-amount",4,None,False,False),
        ("invalid-date",2,"31/04/2026",False,False),("native-date",2,datetime(2026,4,1),False,False),
        ("serial-date",2,46113,False,False),("time-date",2,time(12,30),False,False),
        ("precision",4,"0.1000000000000000000000000000000001",True,False),
        ("formula-missing",4,"=1+2",False,True)]:
        sheet=book.create_sheet(name);sheet.append([c["column_name"] for c in columns]);sheet.append(["00123","01/04/2026","Cash",125,0])
        sheet.cell(2,column,value)
        if value is None: sheet.cell(2,column).value=None
        if literal: sheet.cell(2,column).data_type="s"
        if name=="native-date": sheet.cell(2,column).number_format="yyyy-mm-dd"
        cases.append(dict(sheet=name,footing=footing))
    sheet=book.create_sheet("Rows");sheet.append([c["column_name"] for c in columns]+["Extra"])
    for row in [["V9","01/04/2026","Cash",10,0,None],[None]*6,["V1","02/04/2026","Cash",20,0,None],
                ["TOTAL",None,None,30,0,None],["TOTAL","03/04/2026","Cash",5,0,"evidence"],
                ["V1","02/04/2026","Cash",20,0,None]]: sheet.append(row)
    sheet=book.create_sheet("Many");sheet.append([c["column_name"] for c in columns])
    for number in range(202): sheet.append([f"V{number}","01/04/2026","Cash","bad",0])
    sheet=book.create_sheet("Ledger");sheet.append(["Voucher","Date","Description","Debit","Credit","Balance"])
    for row in [[None,None,"Opening Balance",None,None,"DR 100"],["V1","01/04/2026","Cash",10,0,"110 DR"],
                [None,None,"Supporting narration",None,None,None],[None]*6,[None,None,"Orphan narration",None,None,None],
                ["V2","02/04/2026","Closing balance",0,5,"105 DR"],[None,None,"Closing Balance",None,None,"105 DR"]]: sheet.append(row)
    path=target/"validation.xlsx";book.save(path);book.close();before=hashlib.sha256(path.read_bytes()).hexdigest()
    for case in cases:
        template=[dict(c) for c in columns]
        if case["footing"]: template[3]["reconciliation_role"]="footing"
        frame=pd.read_excel(path,sheet_name=case["sheet"])
        mapping={c["column_name"]:c["column_name"] for c in columns}
        request=SimpleNamespace(sheet_name=case["sheet"],excluded_rows=[],comparison_mode="correction")
        errors=env["_workbook_input_errors"](path,request,template,mapping,frame)
        row=frame.to_dict("records")[0];missing=env["find_critical_blank_reasons"](row,env["get_critical_field_columns"](template),
            {c["id"]:SimpleNamespace(excel_column_name=c["column_name"]) for c in columns})
        # The existing source-input-failure response precedes critical blanks.
        if errors: missing=[]
        ordinary=[]
        if not errors:
            for c in template:
                value=row[c["column_name"]]
                if pd.isna(value) or isinstance(value,str) and not value.strip(): continue
                try:
                    if c["data_type"]=="Numeric": float(value)
                    elif c["data_type"]=="Date": env["_parse_date_cell"](value,"/","DMY")
                except (ValueError,TypeError,OverflowError): ordinary.append(c["id"])
        case.update(error_columns=[next(c["id"] for c in columns if c["column_name"]==e["column"]) for r in errors for e in r["errors"]]+ordinary,
                    missing_columns=[next(c["id"] for c in columns if c["column_name"]==reason["column"]) for reason in missing])
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    (target/"oracle.json").write_text(json.dumps(dict(numbers=vectors,cases=cases,sha256=before),ensure_ascii=False),encoding="utf-8")


if __name__=="__main__": main()
