"""Synthetic legacy request-binding invariants and an independent HMAC vector."""
import ast
import hashlib
import hmac
import json
import sys
from pathlib import Path

from openpyxl import Workbook

root=Path(__file__).resolve().parents[2]
target=Path(sys.argv[1]).resolve()
if (root/'dotnet-uploader/.tools').resolve() not in target.parents or target.exists():
    raise RuntimeError('Fresh owned fixture directory required')
target.mkdir(parents=True)
tree=ast.parse((root/'routers/import_export.py').read_text(encoding='utf-8-sig'))
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_validation_request_signature')
ns=dict(json=json,hashlib=hashlib)
exec(compile(ast.Module([node],[]),'legacy-request-signature','exec'),ns)
class Request:
    def __init__(self,values): self.values=values
    def model_dump(self,exclude,exclude_none): return {k:v for k,v in self.values.items() if k not in exclude}
base=dict(file_id=1,client_id=10,template_id=1,sheet_name='Base',comparison_mode='replace_range',
    coverage_start='2026-04-01',coverage_end='2026-04-30',control_total_row=5,control_total_confirmed=True,
    confirm_footing_variance=False,excluded_rows=[],mappings=[dict(column_id=1,position=1)],validation_plan_id=None,skip_validation=False)
signature=ns['_validation_request_signature'](Request(base))
vectors=[]
for field,value in dict(file_id=2,client_id=20,template_id=2,sheet_name='Changed',coverage_end='2026-04-29',
    control_total_row=4,control_total_confirmed=False,confirm_footing_variance=True,excluded_rows=[2],
    mappings=[dict(column_id=1,position=2)],validation_plan_id='ignored',skip_validation=True).items():
    changed=ns['_validation_request_signature'](Request({**base,field:value}))!=signature
    assert changed==(field not in {'validation_plan_id','skip_validation'})
    vectors.append(dict(field=field,changed=changed))
counts=dict(Incoming=1,Existing=1,Unchanged=0,Changed=1,Added=0,Removed=0,Retained=0,InformationalUpdates=0,SourceOrderUpdates=0)
envelope=dict(Format='dotnet-upload-plan-v1',RequestId='00000000-0000-0000-0000-000000000001',PlanId='1'*32,
    Tenant=101,User=1,Client=10,Template=1,File=1,RequestHash='a'*64,PayloadHash='b'*64,CorrectionKey='c'*64,
    ConfigurationKey='d'*64,MappingRevision=0,FileHash='e'*64,CreatedAt='2026-11-12T00:00:00+00:00',
    ExpiresAt='2026-11-12T00:30:00+00:00',NoOp=False,Counts=counts)
signed=json.dumps(envelope,separators=(',',':'))
key=bytes(range(32))
book=Workbook();book.remove(book.active)
base_rows=[['V1','03/04/2026',100,'Office  supplies','100 DR'],['V1','03/04/2026',100,'Office  supplies','100 DR'],['V2','08/04/2026',50,'Other','50 DR']]
for name,rows in [('Base',base_rows+[['TOTAL',None,250]]),('Changed',[[*base_rows[0][:2],107,*base_rows[0][3:]],base_rows[1],base_rows[2],['TOTAL',None,257]]),
    ('Large',[['V1','03/04/2026',107,'m'*6000,'100 DR'],['V1','03/04/2026',100,'m'*6000,'100 DR'],base_rows[2],['TOTAL',None,257]]),
    ('Unchecked',[[*base_rows[0][:4],'80 DR'],base_rows[1],base_rows[2],['TOTAL',None,250]]),
    ('Ledger',[[None,None,None,'Opening Balance',None,'100 DR'],['V1','03/04/2026',10,'Payment',0,'110 DR'],
        [None,None,None,'Invoice # 1'],[None,None,None,'Closing Balance',None,'110 DR'],['TOTAL',None,10]]),
    ('BindingReview',[base_rows[0],base_rows[1],['V2','08/04/2026',50,'Changed detail','50 DR'],['TOTAL',None,250]])]:
    sheet=book.create_sheet(name);sheet.append(['Document','Date','Amount','Memo','Credit','Balance'] if name=='Ledger' else ['Document','Date','Amount','Memo','Balance','Extra'])
    for row in rows: sheet.append(row)
book.save(target/'plans.xlsx')
(target/'oracle.json').write_text(json.dumps(dict(vectors=vectors,envelope=envelope,
    signature=hmac.new(key,signed.encode(),hashlib.sha256).hexdigest(),sha256=hashlib.sha256((target/'plans.xlsx').read_bytes()).hexdigest())),encoding='utf-8')
