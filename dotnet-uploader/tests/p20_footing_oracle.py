"""Synthetic Footing/precision oracle; no application import or live database access."""
import ast
import hashlib
import json
import math
import re
import sys
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

import openpyxl
import pandas as pd


def main():
    target=Path(sys.argv[1]).resolve()
    if target.exists() and any(target.iterdir()): raise RuntimeError("Use a fresh fixture directory")
    target.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[2]
    source=ast.parse((root/"services"/"upload_reconciliation.py").read_text(encoding="utf-8"))
    functions={"blank","amount","footing_columns","selected_control_row","summarize_rows","reconcile_source"}
    constants={"NUMBER_PATTERN","NUMBER","FOOTING_AUTO_ACCEPT_BELOW","FOOTING_CONFIRMATION_LIMIT","SOURCE_WHITESPACE"}
    nodes=[n for n in source.body if isinstance(n,ast.FunctionDef) and n.name in functions
           or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in constants for t in n.targets)]
    env=dict(Decimal=Decimal,InvalidOperation=InvalidOperation,localcontext=localcontext,math=math,re=re,
             resolve_document_column=lambda columns:next((c for c in columns if c.get("reconciliation_role")=="document_id"),columns[0]))
    exec(compile(ast.Module(body=nodes,type_ignores=[]),"isolated_footing_helpers","exec"),env)
    parse=[]
    for text in ["0","-0.00","0e1000","0e-1000","0001.20",".20","1.","+1e3","1e1000","1e-1000",
                 "9"*1000,"1e1001","1e-1001","9"*1001,"NaN","Infinity","1,000","1_000","١٢٣","=SUM(A1:A2)"]:
        case={"text":text}
        try: case.update(valid=True,formatted=format(env["amount"](text,required=True),"f"))
        except ValueError: case["valid"]=False
        parse.append(case)
    arithmetic=[]
    for values in [["0.10","0.20"],["1000000000000000000000000000000.01","0.02"],
                   ["1e-1000","1e-1000"],["1e1000","1"+"0"*998+"1e-1999"],
                   ["1e40","-1e40","0.0000000000000000000000000000001"], ["-0.00","0"]]:
        with localcontext() as context:
            context.prec=10000
            arithmetic.append(dict(values=values,total=format(sum((Decimal(v) for v in values),Decimal(0)),"f")))
    columns=[dict(id=i,column_name=name,data_type=kind,reconciliation_role=role) for i,(name,kind,role) in enumerate([
        ("Reference","Text",None),("Document","Text","document_id"),("Date","Date","document_date"),
        ("Debit","Numeric","footing"),("Credit","Numeric","footing")],1)]
    mapping={c["column_name"]:c["column_name"] for c in columns}
    thresholds=[]
    for difference in ["0","0.009","-0.009","0.01","-0.01","1.00","-1.00","1.0001","-1.0001"]:
        detail=[dict(Reference="PO",Document="A",Date="01/01/2026",Debit="100",Credit="0")]
        footer=dict(Debit=format(Decimal(100)-Decimal(difference),"f"),Credit="0")
        proof=env["reconcile_source"](footer,detail,columns,mapping,3,lambda row,fallback:fallback)
        thresholds.append(dict(difference=difference,footer=footer["Debit"],status=proof["status"],check=proof["checks"][0]))
    book=openpyxl.Workbook();book.remove(book.active)
    headers=list(mapping)
    data=[["PO1","A","01/01/2026","0.10",0],["PO1","A","01/01/2026","0.20",0],
          ["PO1","B","01/01/2026",0,"0.30"],["TOTAL",None,None,"0.30","0.30"]]
    for name in ["Full","Middle","Unlabelled","BadDetail","BooleanTotal","MissingCache","Cached", "Precision"]:
        sheet=book.create_sheet(name);sheet.append(headers)
        rows=[list(row) for row in data]
        if name=="Middle": rows.insert(1,rows.pop());rows[1][:3]=["ref","neighbouring document","not a date"]
        if name=="Unlabelled": rows[-1][0]=""
        if name=="BadDetail": rows[0][3]="Infinity"
        if name=="BooleanTotal": rows[-1][3]=True
        if name in ("MissingCache","Cached"): rows[-1][3]="=SUM(D2:D4)"
        if name=="Precision": rows[0][3]="0.1000000000000000000000000000000001";rows[-1][3]="0.3000000000000000000000000000000001"
        for row in rows: sheet.append(row)
    for number,item in enumerate(thresholds):
        sheet=book.create_sheet(f"Threshold{number}");sheet.append(headers)
        sheet.append(["PO","A","01/01/2026","100","0"]);sheet.append(["TOTAL",None,None,item["footer"],"0"])
    sheet=book.create_sheet("Mixed");sheet.append(headers)
    sheet.append(["PO","A","01/01/2026","100","100"]);sheet.append(["TOTAL",None,None,"99.99","98"])
    sheet=book.create_sheet("Repeated");sheet.append(headers)
    for row in [["PO1","A","01/01/2026","0.15",0],["PO1","A","01/01/2026","0.15",0],data[2],data[3]]: sheet.append(row)
    for name in ["Ledger","LedgerSelected"]:
        sheet=book.create_sheet(name);sheet.append(["Document","Date","Description","Debit","Credit","Balance"])
        for row in [[None,None,"Opening Balance",None,None,"100 DR"],["A","01/01/2026","Purchase","0.10",0,"100.10 DR"],
                    [None,None,"Narration",None,None,None],["A","01/01/2026","Purchase","0.20",0,"100.30 DR"],
                    ["B","01/01/2026","Payment",0,"0.30","100 DR"],
                    [None,None,"Closing Balance","0.30" if name=="LedgerSelected" else None,"0.30" if name=="LedgerSelected" else None,"100 DR"],
                    ["TOTAL",None,"Total","0.30","0.30",None]]: sheet.append(row)
    path=target/"footing.xlsx";book.save(path);book.close()
    with ZipFile(path) as original: entries=[(info,original.read(info.filename)) for info in original.infolist()]
    with ZipFile(path,"w",ZIP_DEFLATED) as result:
        for info,content in entries:
            if info.filename=="xl/worksheets/sheet7.xml":
                xml=ET.fromstring(content);ns={"x":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                xml.find(".//x:c[@r='D5']/x:v",ns).text="0.30";content=ET.tostring(xml)
            result.writestr(info,content)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    # Record a concrete whole-reader coercion discrepancy as review evidence.
    frame=pd.read_excel(path,sheet_name="Precision")
    precision=dict(raw="0.1000000000000000000000000000000001",legacy_first=str(frame.iloc[0]["Debit"]),legacy_footer=str(frame.iloc[-1]["Debit"]))
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    (target/"oracle.json").write_text(json.dumps(dict(parse=parse,arithmetic=arithmetic,thresholds=thresholds,
        precision=precision,sha256=before),ensure_ascii=False),encoding="utf-8")


if __name__=="__main__": main()
