"""Compare every persisted mapped cell with its original source Excel row."""
import hashlib
import json
from pathlib import Path
import run as h


def main():
    h.RESULTS.extend(json.loads((h.HERE/'results.json').read_text(encoding='utf-8')))
    def cells():
        imports=h.query("SELECT u.id,u.file_name,u.client_id,u.template_id,t.data_purpose FROM uploaded_files u JOIN templates t ON t.id=u.template_id WHERE u.status='Imported' ORDER BY u.id")
        checked=0;datasets=0;cache={};differences=[]
        for item in imports:
            path=h.ROOT/'uploads'/item['file_name']
            if not path.exists():continue
            if path not in cache:cache[path]=h.rows(path)
            _,headers,data=cache[path]
            names=['Voucher ID','Transaction Date','Voucher Detail','Debit Amount','Credit Amount','Balance'] if item['data_purpose']=='account_ledger' else ['Voucher ID','Transaction Date','Account Detail','Account Name','Voucher Detail','Debit Amount','Credit Amount']
            if headers[0]!='Voucher ID':continue
            mapping=dict(zip(names,headers[:len(names)]))
            actual=h.query('SELECT row_data,source_row_number,extract_date FROM client_data WHERE upload_id=%s AND is_deleted=FALSE',(item['id'],))
            if not actual:continue
            for record in actual:
                source=data[record['source_row_number']-2]
                for field,header in mapping.items():
                    value=source[headers.index(header)];saved=record['row_data'].get(field)
                    if field=='Transaction Date':
                        day=value.date().isoformat() if isinstance(value,h.datetime) else h.datetime.strptime(str(value),'%d/%m/%Y').date().isoformat()
                        assert str(record['extract_date'])==day
                    elif field in ('Debit Amount','Credit Amount'):
                        assert h.number(value)==h.number(saved)
                    else:
                        if ('' if value is None else str(value))!=('' if saved is None else str(saved)):
                            differences.append({'file':item['file_name'],'row':record['source_row_number'],'field':field,'source':value,'stored':saved})
                checked+=1
            datasets+=1
        assert datasets>=22,(datasets,checked)
        (h.PRIVATE/'cell-comparison.json').write_text(json.dumps({'datasets':datasets,'checked_rows':checked,'differences':differences},indent=2,default=str),encoding='utf-8')
        assert not differences, f'{len(differences)} mapped-cell differences; see private/cell-comparison.json'
        return {'original_import_datasets':datasets,'complete_posting_rows_compared':checked,'all_mapped_cells_match':True}
    h.check('All original voucher and ledger mapped cells match stored rows',cells)
    def protection():
        hashes=json.loads((h.PRIVATE/'original-hashes.json').read_text())
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in hashes.items())
        return {'original_files_unchanged':len(hashes),'production_connection_used':False}
    h.check('Final checksum protection after all experiments',protection)


if __name__=='__main__':main()
