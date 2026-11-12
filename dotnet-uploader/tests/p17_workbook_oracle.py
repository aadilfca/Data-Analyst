"""Synthetic golden workbooks and independent source/Python observations; no app or DB import."""
import ast
import base64
import hashlib
import json
import math
import struct
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

import openpyxl
import pandas as pd
import xlrd
from openpyxl.utils.datetime import CALENDAR_MAC_1904


def normalize(value):
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NaT:
        return {"kind": "Blank"}
    if isinstance(value, bool):
        return {"kind": "Boolean", "value": value}
    if isinstance(value, datetime):
        return {"kind": "DateTime", "value": value.isoformat(timespec="milliseconds")}
    if isinstance(value, time):
        return {"kind": "Time", "seconds": value.hour*3600+value.minute*60+value.second+value.microsecond/1e6}
    if isinstance(value, timedelta):
        return {"kind": "Duration", "seconds": value.total_seconds()}
    if isinstance(value, (int, float)):
        return {"kind": "Number", "value": repr(value)}
    return {"kind": "Text", "value": str(value)}


def xlsx(path, epoch1904):
    book = openpyxl.Workbook()
    if epoch1904:
        book.epoch = CALENDAR_MAC_1904
    sheet = book.active
    sheet.title = "Data"
    sheet.append([f"Column{i}" for i in range(1,17)])
    values = ["00123", 1234.56789012345, "123456789012345678901234567890.1234500", None, 0,
              True, datetime(2026,4,1), time(12,34,56), timedelta(hours=49), "=1+2", "=1+2",
              "#REF!", "#REF!", "NaN", 1, ""]
    for number in (2,3):
        sheet.append(values)
        sheet.cell(number,13).data_type = "s"
        sheet.cell(number,7).number_format = "yyyy-mm-dd"
        sheet.cell(number,8).number_format = "hh:mm:ss"
        sheet.cell(number,9).number_format = "[h]:mm:ss"
    sheet.cell(5,1,"Unicode اردو café")
    sheet.cell(5,2,"Merged")
    sheet.merge_cells("B5:C5")
    sheet.cell(7,1,"last")
    epochs = book.create_sheet("Epoch")
    epochs.append(["serial"])
    for serial in (0,1,59,60,61):
        epochs.append([serial])
        epochs.cell(epochs.max_row,1).number_format = "yyyy-mm-dd"
    book.create_sheet("Empty")
    hidden=book.create_sheet("Hidden")
    hidden.sheet_state="hidden"
    hidden.append(["Header"])
    hidden.append(["value"])
    precision=book.create_sheet("Precision")
    precision.append(["Value"])
    for _ in range(6): precision.append([1])
    trailing=book.create_sheet("Trailing")
    trailing.append(["Header"])
    trailing.cell(10,1).number_format="0.00"
    formulas=book.create_sheet("Formulas")
    formulas.append(["Number","Text","Boolean","Empty","Error","Missing","Shared"])
    for column in range(1,8): formulas.cell(2,column,"=1+2")
    formulas.cell(3,7,"=1+2")
    invalid=book.create_sheet("InvalidNumbers")
    invalid.append(["Value"])
    for _ in range(3): invalid.append([1])
    book.save(path)
    book.close()
    ns={"s":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as archive:
        entries={name:archive.read(name) for name in archive.namelist()}
    root=ET.fromstring(entries["xl/worksheets/sheet1.xml"])
    for cell in root.findall(".//s:c",ns):
        address=cell.attrib["r"]
        if address in ("J2","J3"):
            cell.find("s:v",ns).text="3"
        if address in ("O2","O3"):
            cell.find("s:v",ns).text="123456789012345678901234567890.1234500"
        if address=="L3":
            cell.attrib["t"]="e"
            formula=ET.Element("{"+ns["s"]+"}f"); formula.text="1/0"; cell.insert(0,formula)
            cell.find("s:v",ns).text="#DIV/0!"
    entries["xl/worksheets/sheet1.xml"]=ET.tostring(root,encoding="utf-8",xml_declaration=True)
    precision=ET.fromstring(entries["xl/worksheets/sheet5.xml"])
    tokens=("9007199254740993","0.1000000000000000000000000000000001","1e-300","1.2300","-0","1e+100")
    for cell,token in zip(precision.findall(".//s:row",ns)[1:],tokens):
        cell.find("s:c/s:v",ns).text=token
    entries["xl/worksheets/sheet5.xml"]=ET.tostring(precision,encoding="utf-8",xml_declaration=True)
    formulas=ET.fromstring(entries["xl/worksheets/sheet7.xml"])
    caches={"A2":("n","3"),"B2":("str","cached text"),"C2":("b","1"),"D2":("str",""),"E2":("e","#DIV/0!"),"F2":("n",None),"G2":("n","4"),"G3":("n","4")}
    for cell in formulas.findall(".//s:c",ns):
        if cell.attrib["r"] not in caches: continue
        kind,value=caches[cell.attrib["r"]]; cell.attrib["t"]=kind
        original=cell.find("s:v",ns)
        if original is not None: cell.remove(original)
        if value is not None: ET.SubElement(cell,"{"+ns["s"]+"}v").text=value
        if cell.attrib["r"] in ("G2","G3"):
            formula=cell.find("s:f",ns); formula.attrib.update({"t":"shared","si":"0"})
            if cell.attrib["r"]=="G2": formula.attrib["ref"]="G2:G3"
            else: formula.text=None
    entries["xl/worksheets/sheet7.xml"]=ET.tostring(formulas,encoding="utf-8",xml_declaration=True)
    invalid=ET.fromstring(entries["xl/worksheets/sheet8.xml"])
    for row,value in zip(invalid.findall(".//s:row",ns)[1:],("1e9999","NaN","INF")):
        row.find("s:c/s:v",ns).text=value
    entries["xl/worksheets/sheet8.xml"]=ET.tostring(invalid,encoding="utf-8",xml_declaration=True)
    with ZipFile(path,"w",ZIP_DEFLATED) as archive:
        for name,value in entries.items():
            archive.writestr(name,value)


def biff_xls(path, epoch1904):
    """Small original BIFF8/CFB fixture, no external workbook or writer dependency."""
    def rec(code,data=b""):
        return struct.pack("<HH",code,len(data))+data
    def bof(kind):
        return rec(0x0809,struct.pack("<HHHHII",0x0600,kind,0x0DBB,1997,0x41,6))
    def label(row,col,value):
        return rec(0x0204,struct.pack("<HHHHB",row,col,0,len(value),1)+value.encode("utf-16le"))
    def number(row,col,value,style=0):
        return rec(0x0203,struct.pack("<HHHd",row,col,style,value))
    def formula(row,col,value):
        result=struct.pack("<d",value) if isinstance(value,(int,float)) else value
        return rec(0x0006,struct.pack("<HHH",row,col,0)+result+struct.pack("<HIH",0,0,3)+b"\x1e\x03\x00")
    data=bof(0x10)+rec(0x0200,struct.pack("<IIHHH",0,7,0,16,0))
    for col in range(16):
        data+=label(0,col,f"Column{col+1}")
    serial=44651 if epoch1904 else 46113
    for row in (1,2):
        data+=label(row,0,"00123")+number(row,1,1234.56789012345)
        data+=label(row,2,"123456789012345678901234567890.1234500")
        data+=rec(0x0201,struct.pack("<HHH",row,3,0))+number(row,4,0)
        data+=rec(0x0205,struct.pack("<HHHBB",row,5,0,1,0))
        data+=number(row,6,serial,1)+number(row,7,(12*3600+34*60+56)/86400,2)+number(row,8,49/24,3)
        data+=formula(row,9,3)+formula(row,10,b"\x03\x00\x00\x00\x00\x00\xff\xff")
        data+=(rec(0x0205,struct.pack("<HHHBB",row,11,0,23,1)) if row==1 else formula(row,11,b"\x02\x00\x07\x00\x00\x00\xff\xff"))+label(row,12,"#REF!")
        data+=label(row,13,"NaN")+number(row,14,123456789012345)+label(row,15,"")
    data+=label(4,0,"Unicode اردو café")+label(4,1,"Merged")+label(6,0,"last")
    data+=rec(0x00E5,struct.pack("<HHHHH",1,4,4,1,2))+rec(0x000A)
    epochs=bof(0x10)+rec(0x0200,struct.pack("<IIHHH",0,6,0,1,0))+label(0,0,"serial")
    for row,value in enumerate((0,1,59,60,61),1):
        epochs+=number(row,0,value,1)
    epochs+=rec(0x000A)
    global_records=bof(5)+rec(0x0042,struct.pack("<H",1200))+rec(0x0022,struct.pack("<H",int(epoch1904)))
    for index,fmt in enumerate(("yyyy-mm-dd","hh:mm:ss","[h]:mm:ss"),164):
        global_records+=rec(0x041E,struct.pack("<HHB",index,len(fmt),1)+fmt.encode("utf-16le"))
    for fmt in (0,164,165,166):
        global_records+=rec(0x00E0,struct.pack("<HHH",0,fmt,0xfff0)+bytes(14))
    def bound(name,offset):
        return rec(0x0085,struct.pack("<IBBBB",offset,0,0,len(name),1)+name.encode("utf-16le"))
    start=len(global_records)+len(bound("Data",0))+len(bound("Epoch",0))+4
    payload=global_records+bound("Data",start)+bound("Epoch",start+len(data))+rec(0x000A)+data+epochs
    size=max(4096,((len(payload)+511)//512)*512)
    payload=payload.ljust(size,b"\0")
    count=size//512
    header=bytearray(512)
    header[:8]=bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH",header,24,0x3e,3,0xfffe,9,6)
    struct.pack_into("<IIIIIIIII",header,40,0,1,1,0,4096,0xfffffffe,0,0xfffffffe,0)
    struct.pack_into("<109I",header,76,0,*([0xffffffff]*108))
    fat=[0xfffffffd,0xfffffffe]+list(range(3,2+count))+[0xfffffffe]
    fat+= [0xffffffff]*(128-len(fat))
    def directory(name,kind,start,length,child=0xffffffff):
        entry=bytearray(128); encoded=(name+"\0").encode("utf-16le"); entry[:len(encoded)]=encoded
        struct.pack_into("<HBBIII",entry,64,len(encoded),kind,1,0xffffffff,0xffffffff,child)
        struct.pack_into("<IQ",entry,116,start,length)
        return entry
    entries=directory("Root Entry",5,0xfffffffe,0,1)+directory("Workbook",2,2,size)+bytes(256)
    path.write_bytes(bytes(header)+struct.pack("<128I",*fat)+entries+payload)


def source_view(path):
    result={}
    if path.suffix==".xlsx":
        book=openpyxl.load_workbook(path,data_only=True,read_only=True)
        formulas=openpyxl.load_workbook(path,data_only=False,read_only=True)
        try:
            for sheet in book:
                rows=[]
                try:
                    for row,originals in zip(sheet.iter_rows(),formulas[sheet.title].iter_rows()):
                        values=[]
                        for cell,original in zip(row,originals):
                            value=normalize(cell.value)
                            if cell.data_type=="e": value={"kind":"Error","value":cell.value}
                            value["formula"]=original.data_type=="f"
                            values.append(value)
                        rows.append(values)
                    result[sheet.title]=rows
                except ValueError as error:
                    result[sheet.title]={"failure":type(error).__name__}
        finally:
            book.close(); formulas.close()
    else:
        book=xlrd.open_workbook(path,on_demand=True)
        try:
            for sheet in book.sheets():
                rows=[]
                for r in range(sheet.nrows):
                    row=[]
                    for c in range(sheet.ncols):
                        cell=sheet.cell(r,c)
                        if cell.ctype in (xlrd.XL_CELL_EMPTY,xlrd.XL_CELL_BLANK): value={"kind":"Blank"}
                        elif cell.ctype==xlrd.XL_CELL_ERROR: value={"kind":"Error","value":xlrd.error_text_from_code[cell.value]}
                        elif cell.ctype==xlrd.XL_CELL_BOOLEAN: value=normalize(bool(cell.value))
                        elif cell.ctype==xlrd.XL_CELL_DATE: value=normalize(xlrd.xldate.xldate_as_datetime(cell.value,book.datemode))
                        else: value=normalize(cell.value)
                        row.append(value)
                    rows.append(row)
                result[sheet.name]=rows
        finally:
            book.release_resources()
    return result


def main():
    target=Path(sys.argv[1]).resolve()
    if target.exists() and any(target.iterdir()): raise RuntimeError("Use a fresh empty fixture directory")
    target.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[2]
    module=ast.parse((root/"routers"/"import_export.py").read_text(encoding="utf-8"))
    functions=[node for node in module.body if isinstance(node,ast.FunctionDef) and node.name=="json_safe_cell_value"]
    helpers={"pd":pd,"datetime":datetime,"time":time}
    exec(compile(ast.Module(body=functions,type_ignores=[]),"isolated_cell_helper","exec"),helpers)
    report={}
    for old_epoch in (False,True):
        for extension,maker in (("xlsx",xlsx),("xls",biff_xls)):
            path=target/f"golden_{1904 if old_epoch else 1900}.{extension}"
            maker(path,old_epoch)
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            legacy,failures={},{}
            with pd.ExcelFile(path) as excel:
                for name in excel.sheet_names:
                    try:
                        frame=pd.read_excel(excel,sheet_name=name)
                        legacy[name]=[[{"kind":"Blank"} if pd.isna(v) else normalize(helpers["json_safe_cell_value"](v)) for v in row]
                                      for row in frame.itertuples(index=False,name=None)]
                    except (OverflowError,ValueError) as error:
                        failures[name]=type(error).__name__
            report[path.name]={"source":source_view(path),"legacy":legacy,"legacyFailures":failures,"sha256":before}
            if hashlib.sha256(path.read_bytes()).hexdigest()!=before: raise AssertionError("Oracle modified fixture")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route("**/*",lambda route:route.abort())
            page.set_content("<html><body></body></html>")
            page.add_script_tag(path=str(root/"static"/"vendor"/"xlsx.full.min.js"))
            for name,observations in report.items():
                encoded=base64.b64encode((target/name).read_bytes()).decode("ascii")
                observations["browser"]=page.evaluate("""encoded => {
                    const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
                    const book=XLSX.read(bytes,{type:'array',cellNF:true,cellFormula:true});
                    const output={date1904:!!book.Workbook?.WBProps?.date1904,sheets:{}};
                    for(const name of book.SheetNames) {
                        const sheet=book.Sheets[name], rows=[];
                        if(!sheet['!ref']) {output.sheets[name]=rows;continue;}
                        const range=XLSX.utils.decode_range(sheet['!ref']);
                        for(let r=0;r<=range.e.r;r++) {
                            const row=[];
                            for(let c=0;c<=range.e.c;c++) {
                                const cell=sheet[XLSX.utils.encode_cell({r,c})];
                                const result={kind:!cell?'Blank':({s:'Text',str:'Text',n:'Number',b:'Boolean',e:'Error',d:'DateTime'}[cell.t]||'Blank')};
                                if(cell && cell.v!==undefined) result.value=cell.t==='n'?String(cell.v):cell.v;
                                result.formula=!!cell?.f;
                                if(cell?.t==='n' && XLSX.SSF.is_date(cell.z||'')) {
                                    const date=XLSX.SSF.parse_date_code(cell.v,{date1904:output.date1904});
                                    if(date) result.datePreview=[date.y,date.m,date.d,date.H,date.M,date.S];
                                }
                                row.push(result);
                            }
                            rows.push(row);
                        }
                        output.sheets[name]=rows;
                    }
                    return output;
                }""",encoded)
        finally:
            browser.close()
    (target/"oracle.json").write_text(json.dumps(report,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    print("Four synthetic workbooks and independent source/Python observations created.")


if __name__=="__main__":
    main()
