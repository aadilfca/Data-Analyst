"""Finish recovery of the Purchase Demand fixture without touching its original."""
import json
import run as h


def main():
    h.RESULTS.extend(json.loads((h.HERE/'results.json').read_text(encoding='utf-8')))
    def purchase_demand():
        original=h.ROOT/'uploads'/'2. purchase demands Test.xlsx'
        # The sample footer contains a date and "Total store", so it cannot
        # safely be auto-classified. Remove only that inspected footer in a copy.
        copy=h.variant(original,'purchase-demand-without-footer',lambda s:s.delete_rows(6,1))
        t,c=h.template('Purchase Demand footer removed',[('Document ID','Text'),('Transaction Date','Date'),('Quantity','Numeric')])
        cid=h.client('Purchase Demand footer removed')
        mapping={'Document ID':'PD #','Transaction Date':'Date','Quantity':'Quantity'}
        p=h.stage(copy,cid,t,c,mapping,'2026-01-01','2026-12-31');p['date_delimiter']='-'
        h.apply(p,h.validate(p));actual=h.stored(cid,t)
        assert len(actual)==4 and sum(h.number(r['row_data']['Quantity']) for r in actual)==318
        repeat=h.stage(copy,cid,t,c,mapping,'2026-01-01','2026-12-31');repeat['date_delimiter']='-'
        assert h.validate(repeat)['duplicate_upload_noop']
        return {'rows':4,'quantity':'318','repeat_noop':True,'original_unchanged':True,'experiment_copy_footer_removed':6}
    h.check('Purchase Demand imports correctly after removing ambiguous footer from copy',purchase_demand)
    def no_bypass():
        source=h.PRIVATE/'fixtures'/'invalid-debit.xlsx'
        names=['Voucher ID','Transaction Date','Account Detail','Account Name','Voucher Detail','Debit Amount','Credit Amount']
        kinds=['Text','Date','Text','Text','Text','Numeric','Numeric']
        t,c=h.template('Direct invalid import',list(zip(names,kinds)));cid=h.client('Direct invalid import')
        _,headers,_=h.rows(source)
        p=h.stage(source,cid,t,c,dict(zip(names,headers)),'2026-01-01','2026-01-31');p['skip_validation']=True
        try:
            h.upload.validate_and_import(h.upload.ImportRequest(**p),background_tasks=h.BackgroundTasks())
        except h.HTTPException as error:
            assert error.status_code==400 and not h.stored(cid,t)
        else:
            raise AssertionError('Complete report bypassed validation-plan gate')
        p.update(comparison_mode='correction',coverage_start=None,coverage_end=None)
        result=h.upload.validate_and_import(h.upload.ImportRequest(**p),background_tasks=h.BackgroundTasks())
        assert result['status']=='validation_failed' and not h.stored(cid,t),result
        return {'legacy_skip_flag_does_not_bypass_row_validation':True,'canonical_rows':0}
    h.check('Direct import cannot bypass invalid-amount validation with skip flag',no_bypass)


if __name__=='__main__':main()
