using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.TrialBalance;

internal static class TrialBalanceStorageWriter
{
    internal sealed record AccountRow(string account_code,string source_account_code,string? account_name,
        string? parent_account_code,int level,string classification,bool manually_overridden,int sort_order,
        string? source_account_type,string? opening_dr,string? opening_cr,string? period_dr,string? period_cr,
        string? closing_dr,string? closing_cr,Dictionary<string,object?> raw_row_data,int excel_row);

    internal static AccountRow[] Rows(TrialBalanceReview review,TrialBalanceInput input,Dictionary<int,string> originals,CancellationToken ct)
    {
        var sources=review.SourceRows.ToDictionary(r=>r.ExcelRow);
        var positions=input.Mapping.Where(p=>p.Value is not null).ToDictionary(p=>p.Key,p=>review.Headers.Single(h=>h.Label==p.Value).Position);
        var result=new List<AccountRow>();
        foreach(var account in review.Accounts)
        {
            ct.ThrowIfCancellationRequested();var source=sources[account.ExcelRow];
            WorkbookCell Cell(string role)=>TrialBalanceParser.Cell(source,positions[role]);
            string? Amount(string role)=>Cell(role).Kind==WorkbookCellKind.Blank||string.IsNullOrWhiteSpace(TrialBalanceParser.Text(Cell(role)))?null:account.Amounts[role];
            var raw=new Dictionary<string,object?>();
            foreach(var pair in positions) raw[input.Mapping[pair.Key]!]=Value(TrialBalanceParser.Cell(source,pair.Value));
            result.Add(new(account.AccountCode,originals[account.ExcelRow],account.AccountName,account.ParentAccountCode,
                account.AccountCode.Split('-').Length,account.Classification,input.ClassificationOverrides?.ContainsKey(account.ExcelRow)==true,
                account.ExcelRow-2,positions.ContainsKey("type_column")?TrialBalanceParser.Text(Cell("type_column")):null,
                Amount("opening_dr_column"),Amount("opening_cr_column"),Amount("period_dr_column"),Amount("period_cr_column"),
                Amount("closing_dr_column"),Amount("closing_cr_column"),raw,account.ExcelRow));
        }
        var rows=result.ToArray();TrialBalanceBudget.Size(rows,33554432);return rows;
    }
    private static object? Value(WorkbookCell cell)=>cell.Kind switch {
        WorkbookCellKind.Blank=>null,WorkbookCellKind.Number=>JsonSerializer.Deserialize<JsonElement>(cell.NumberToken!),
        WorkbookCellKind.Boolean=>cell.Boolean,WorkbookCellKind.Error=>cell.Error,WorkbookCellKind.DateTime=>cell.DateTime?.ToString("yyyy-MM-dd HH:mm:ss",CultureInfo.InvariantCulture),
        WorkbookCellKind.Time or WorkbookCellKind.Duration=>cell.Time?.ToString(),_=>cell.Text};

    internal static async Task Precision(PostgresTransactionContext db,AccountRow[] rows,Dictionary<string,string> types,CancellationToken ct)
    {
        // Compare source decimal meaning with PostgreSQL's actual stored-value
        // rendering. Blanks remain NULL, distinct from a supplied numeric zero.
        string predicates=string.Join(" OR ",TrialBalanceStorageStore.AmountColumns.Select(c=>
            $"((r->>'{c}')::numeric IS DISTINCT FROM (((r->>'{c}')::{types[c]})::text)::numeric)"));
        await using var command=db.CreateCommand($"SELECT EXISTS(SELECT 1 FROM jsonb_array_elements($1::jsonb) r WHERE {predicates})");
        command.Parameters.AddWithValue(JsonSerializer.Serialize(rows));
        if((bool)(await command.ExecuteScalarAsync(ct))!) throw new TrialBalanceStorageRejected("review","tb_storage_precision_review");
    }
    internal static async Task<int> Import(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,
        TrialBalancePeriod period,TrialBalanceStorageSource source,TrialBalanceReview review,Guid operation,
        AccountRow[] rows,TrialBalanceStorageSnapshot snapshot,CancellationToken ct)
    {
        await Precision(db,rows,snapshot.AmountTypes,ct);
        await Remove(db,snapshot,ct);
        await TrialBalanceAssignmentStore.CarryAsync(db,actor,client,review,rows.ToDictionary(r=>r.excel_row,r=>r.source_account_code),ct);
        int upload;
        await using(var command=db.CreateCommand("""
            INSERT INTO public.trial_balance_uploads(client_id,as_of_date,file_name,status,row_count,period_start,period_end,period_type)
            VALUES($1,$2::date,$3,'pending',0,$4::date,$2::date,$5) RETURNING id
            """))
        {command.Parameters.AddWithValue(client);command.Parameters.AddWithValue(period.End);command.Parameters.AddWithValue(source.FileName);command.Parameters.AddWithValue(period.Start);command.Parameters.AddWithValue(period.Type);upload=(int)(await command.ExecuteScalarAsync(ct))!;}
        await using(var set=db.CreateCommand("UPDATE public.platform_trial_balance_operations SET result_upload_id=$1 WHERE operation_id=$2"))
        {set.Parameters.AddWithValue(upload);set.Parameters.AddWithValue(operation);await set.ExecuteNonQueryAsync(ct);}
        await using(var own=db.CreateCommand("INSERT INTO public.platform_trial_balance_uploads(upload_id,tenant_id,user_id,client_id,operation_id,source_hash,review_hash) VALUES($1,$2,$3,$4,$5,$6,$7)"))
        {own.Parameters.AddWithValue(upload);own.Parameters.AddWithValue(actor.Tenant);own.Parameters.AddWithValue(actor.User);own.Parameters.AddWithValue(client);own.Parameters.AddWithValue(operation);own.Parameters.AddWithValue(review.Sha256);own.Parameters.AddWithValue(review.InputFingerprint);await own.ExecuteNonQueryAsync(ct);}
        // Populate the canonical record using its actual column types. The JSON
        // payload contains mapped source cells only, never a second row store.
        var columns=new[]{"account_code","source_account_code","account_name","parent_account_code","level","classification","manually_overridden","sort_order","source_account_type"}
            .Concat(TrialBalanceStorageStore.AmountColumns).Append("raw_row_data").ToArray();
        string names=string.Join(',',columns.Select(TrialBalanceStorageStore.Quote));
        string values=string.Join(',',columns.Select(c=>"r."+TrialBalanceStorageStore.Quote(c)));
        var accountIds=new Dictionary<string,int>(StringComparer.Ordinal);
        await using(var insert=db.CreateCommand($"INSERT INTO public.trial_balance_accounts(upload_id,{names},category_id) SELECT $1,{values},COALESCE(e.category_id,o.category_id) FROM jsonb_populate_recordset(NULL::public.trial_balance_accounts,$2::jsonb) r LEFT JOIN public.account_category_assignments e ON e.client_id=$3 AND e.account_code=r.account_code LEFT JOIN public.account_category_assignments o ON o.client_id=$3 AND o.account_code=r.source_account_code RETURNING id,account_code"))
        {
            insert.Parameters.AddWithValue(upload);insert.Parameters.AddWithValue(JsonSerializer.Serialize(rows));insert.Parameters.AddWithValue(client);
            await using var reader=await insert.ExecuteReaderAsync(ct);while(await reader.ReadAsync(ct)) accountIds.Add(reader.GetString(1),reader.GetInt32(0));
        }
        if(accountIds.Count!=rows.Length) throw new TrialBalanceStorageRejected("conflict","tb_stored_count_mismatch");
        var adjustments=review.RollforwardDifferences.Select(d=>new {account_id=accountIds[d.AccountCode!],source_excel_row=d.ExcelRow,
            expected_closing_net=d.ExpectedClosingNet,original_closing_net=d.OriginalClosingNet,difference_net=d.DifferenceNet,
            closing_dr_adjustment=d.Amounts["closing_dr_column"],closing_cr_adjustment=d.Amounts["closing_cr_column"]}).ToArray();
        if(adjustments.Length>0)
        {
            await using var insert=db.CreateCommand("""
                INSERT INTO public.trial_balance_rollforward_adjustments(upload_id,account_id,source_excel_row,expected_closing_net,original_closing_net,difference_net,closing_dr_adjustment,closing_cr_adjustment)
                SELECT $1,r.account_id,r.source_excel_row,r.expected_closing_net,r.original_closing_net,r.difference_net,r.closing_dr_adjustment,r.closing_cr_adjustment
                FROM jsonb_populate_recordset(NULL::public.trial_balance_rollforward_adjustments,$2::jsonb) r
                """);insert.Parameters.AddWithValue(upload);insert.Parameters.AddWithValue(JsonSerializer.Serialize(adjustments));
            if(await insert.ExecuteNonQueryAsync(ct)!=adjustments.Length) throw new TrialBalanceStorageRejected("conflict","tb_stored_count_mismatch");
        }
        var controls=new {total_excel_row=review.TotalExcelRow,no_total_row=review.NoTotalRow,
            source_total_rows=review.TotalExcelRow.HasValue?new[]{review.TotalExcelRow.Value}:Array.Empty<int>(),
            source_total_comparison=review.SourceTotalComparison.Select(c=>new {field=c.Field,source_total=c.SourceTotal,leaf_total=c.LeafTotal,difference=c.Difference,within_tolerance=c.WithinTolerance}),
            checks=review.Checks.Select(c=>new {key=c.Key,status=c.Status,affected_rows=c.AffectedRows}),
            global_adjustments=review.GlobalAdjustments.Select(GlobalEntry),
            account_classifications=review.Accounts.OrderBy(a=>a.AccountCode,StringComparer.Ordinal).Select(a=>new[]{a.AccountCode,a.Classification}),global_adjustments_automatic=review.GlobalAdjustments.Count>0,
            erp_unbalanced=review.ErpUnbalanced,draft_reconciliation=JsonSerializer.SerializeToElement(review.DraftReconciliation,TrialBalanceStorageJson.Options)};
        await using(var complete=db.CreateCommand("UPDATE public.trial_balance_uploads SET status='parsed',row_count=$1,validation_controls=$2::jsonb WHERE id=$3"))
        {complete.Parameters.AddWithValue(rows.Length);complete.Parameters.AddWithValue(JsonSerializer.Serialize(controls));complete.Parameters.AddWithValue(upload);if(await complete.ExecuteNonQueryAsync(ct)!=1) throw new TrialBalanceStorageRejected("conflict","tb_stored_count_mismatch");}
        // Read back the canonical amounts and blankness before committing.
        var expected=rows.ToDictionary(r=>r.account_code,StringComparer.Ordinal);int count=0;
        await using(var verify=db.CreateCommand("SELECT account_code,"+string.Join(',',TrialBalanceStorageStore.AmountColumns.Select(c=>TrialBalanceStorageStore.Quote(c)+"::text"))+" FROM public.trial_balance_accounts WHERE upload_id=$1"))
        {
            verify.Parameters.AddWithValue(upload);await using var reader=await verify.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                var row=expected[reader.GetString(0)];var amounts=new[]{row.opening_dr,row.opening_cr,row.period_dr,row.period_cr,row.closing_dr,row.closing_cr};count++;
                for(int i=0;i<6;i++)
                {string? actual=reader.IsDBNull(i+1)?null:reader.GetString(i+1);if(actual is null&&amounts[i] is null)continue;
                    if(actual is null||amounts[i] is null||AuditPlatform.Api.Uploads.UploadExactAmount.Parse(actual).CompareTo(AuditPlatform.Api.Uploads.UploadExactAmount.Parse(amounts[i]!))!=0) throw new TrialBalanceStorageRejected("review","tb_stored_amount_mismatch");}
            }
        }
        if(count!=rows.Length) throw new TrialBalanceStorageRejected("conflict","tb_stored_count_mismatch");
        return upload;
    }
    internal static async Task Remove(PostgresTransactionContext db,TrialBalanceStorageSnapshot snapshot,CancellationToken ct)
    {
        foreach(string table in TrialBalanceStorageStore.DeleteTables.Where(snapshot.Tables.Contains))
        {
            await using var command=db.CreateCommand($"DELETE FROM public.{TrialBalanceStorageStore.Quote(table)} WHERE {(table=="trial_balance_uploads"?"id":"upload_id")}=ANY($1)");
            command.Parameters.AddWithValue(snapshot.UploadIds);await command.ExecuteNonQueryAsync(ct);
        }
    }
    private static Dictionary<string,object?> GlobalEntry(TrialBalanceDifference difference)
    {
        var entry=difference.Amounts.ToDictionary(p=>p.Key.Replace("_column","_adjustment",StringComparison.Ordinal),p=>(object?)p.Value);
        entry["account_name"]=difference.AccountName;entry["global_adjustment"]=true;return entry;
    }
}
