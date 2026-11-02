"""Small adversarial ledger sources, account isolation and document repairs."""
import hashlib
import json
from pathlib import Path
import run as h


def main():
    h.RESULTS.extend(json.loads((h.HERE/'results.json').read_text(encoding='utf-8')))
    cid=h.client('Ledger fault cases')
    tid,cols=h.template('Ledger fault cases',[('Voucher ID','Text'),('Transaction Date','Date'),('Voucher Detail','Text'),('Debit Amount','Numeric'),('Credit Amount','Numeric'),('Balance','Text')],True)
    tb=h.write("INSERT INTO trial_balance_uploads(client_id,as_of_date,file_name,status,period_end) VALUES (%s,'2026-12-31','Synthetic accounts','parsed','2026-12-31') RETURNING id",(cid,))
    for code in ['A','B']:
        h.write("INSERT INTO trial_balance_accounts(upload_id,account_code,account_name,classification) VALUES (%s,%s,%s,'leaf')",(tb,code,'Synthetic '+code))
    mapping=dict(zip([c['column_name'] for c in cols],['Voucher ID','Date','Description','Debit','Credit','Balance']))
    original=next((h.ROOT/'uploads').glob('*Daska ledger.xlsx'))
    def trim(s):
        s.delete_rows(6,s.max_row)
        s.append([None,None,'Closing Balance',None,None,s.cell(5,6).value])
    base=h.variant(original,'ledger-small',trim)
    def payload(path=base,account='A'):
        return h.stage(path,cid,tid,cols,mapping,'2026-10-01','2026-10-01',account)
    def isolation():
        for code in ['A','B']:
            p=payload(account=code);h.apply(p,h.validate(p))
        records=h.stored(cid,tid)
        assert len(records)==6 and len({r['source_identity'] for r in records})==6
        counts=h.query('SELECT u.ledger_account_code,count(*) AS n FROM client_data cd JOIN uploaded_files u ON u.id=cd.upload_id WHERE cd.client_id=%s GROUP BY u.ledger_account_code',(cid,))
        assert sorted(r['n'] for r in counts)==[3,3],counts
        return {'rows_per_account':counts,'unique_identities':6}
    h.check('Same ledger source stays separate under two posting accounts',isolation)
    faults=[('negative amount',4,-1),('nonnumeric amount',4,'abc'),('nonfinite amount',4,'Infinity'),('invalid date',2,'31/02/2026'),('missing date',2,None),('invalid balance',6,'not a balance')]
    for name,col,value in faults:
        def fault(name=name,col=col,value=value):
            path=h.variant(base,'ledger-'+name.replace(' ','-'),lambda s:setattr(s.cell(3,col),'value',value))
            return h.reject(payload(path))
        h.check('Ledger blocks '+name+' without changing stored data',fault)
    def both_sides():
        path=h.variant(base,'ledger-both-sides',lambda s:setattr(s.cell(3,4),'value',100))
        return h.reject(payload(path))
    h.check('Ledger rejects debit and credit on the same posting line',both_sides)
    def wrong_balance():
        path=h.variant(base,'ledger-balance-difference',lambda s:setattr(s.cell(3,6),'value','123 DR'))
        result=h.validate(payload(path))
        assert result.get('source_diagnostics',{}).get('warnings'),result
        return {'can_import':result['can_import'],'balance_evidence':result.get('ledger_evidence'),
                'warnings':result.get('source_diagnostics',{}).get('warnings'),
                'policy_review':'Ledger balance differences warn rather than block import'}
    h.check('Ledger arithmetic difference produces an explicit warning',wrong_balance)
    for name,file,key,day,qty,amount in [
        ('Purchase Demand','2. purchase demands Test.xlsx','PD #','Date','Quantity',None),
        ('GRN','5. GRN Test.xlsx','GRN #','Receiving Date','Quantity','Amount'),
        ('Vendor Bill','6. Vendor bills Test.xlsx','Bill #','Date','Qty','Amount')]:
        def repaired(name=name,file=file,key=key,day=day,qty=qty,amount=amount):
            original=h.ROOT/'uploads'/file;_,headers,data=h.rows(original)
            key_index=headers.index(key);day_index=headers.index(day)
            footers=[i for i,r in enumerate(data,2) if not r[key_index] and not r[day_index] and any(x is not None for x in r)]
            def label(s):
                for i in footers:s.cell(i,key_index+1).value='TOTAL'
            copy=h.variant(original,'labeled-'+name.replace(' ','-'),label)
            columns=[('Document ID','Text'),('Transaction Date','Date'),('Quantity','Numeric')]
            mapping={'Document ID':key,'Transaction Date':day,'Quantity':qty}
            if amount:columns.append(('Amount','Numeric'));mapping['Amount']=amount
            t,c=h.template('Labelled '+name,columns);owner=h.client('Labelled '+name)
            p=h.stage(copy,owner,t,c,mapping,'2026-01-01','2026-12-31')
            good=[r for r in data if r[key_index] and r[day_index]]
            sample=good[0][day_index];p['date_delimiter']='-' if isinstance(sample,str) and '-' in sample else '/'
            v=h.validate(p);h.apply(p,v);actual=h.stored(owner,t)
            assert len(actual)==len(good),(len(actual),len(good))
            for field in ['Quantity','Amount']:
                if field in mapping:
                    ix=headers.index(mapping[field])
                    assert abs(sum(h.number(r[ix]) for r in good)-sum(h.number(r['row_data'][field]) for r in actual))<h.Decimal('0.00001')
            repeat=h.stage(copy,owner,t,c,mapping,'2026-01-01','2026-12-31');repeat['date_delimiter']=p['date_delimiter']
            assert h.validate(repeat)['duplicate_upload_noop']
            return {'rows':len(good),'labelled_footer_rows_in_experiment_copy':footers,'original_unchanged':True,'totals_match':True,'repeat_noop':True}
        h.check('Document '+name+' succeeds after labelling experiment-copy total',repaired)
    def intact():
        hashes=json.loads((h.PRIVATE/'original-hashes.json').read_text())
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in hashes.items())
        return {'unchanged_original_files':len(hashes)}
    h.check('Original code, sources and working SQLite intact after ledger fault tests',intact)


if __name__=='__main__':main()
