using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Workbooks;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Uploads;

// Preparation produces disposable source-bound information only. P19/P20/P21
// own validation, Footing and confirmed coverage; this is never an import plan.
public sealed class UploadPreparationService(UploadStagingService staging,TenantMappingService savedMappings,
    IOptions<UploadPreparationOptions> options)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    private sealed class InvalidPreparation : Exception;
    private sealed class SizeStream(long maximum) : Stream
    {
        public override bool CanRead=>false; public override bool CanSeek=>false; public override bool CanWrite=>true;
        public override long Length=>Position; public override long Position {get;set;}
        public override void Write(byte[] buffer,int offset,int count)=>Write(buffer.AsSpan(offset,count));
        public override void Write(ReadOnlySpan<byte> buffer) {if(Position+buffer.Length>maximum) throw new InvalidPreparation();Position+=buffer.Length;}
        public override void Flush() { }
        public override int Read(byte[] buffer,int offset,int count)=>throw new NotSupportedException();
        public override long Seek(long offset,SeekOrigin origin)=>throw new NotSupportedException();
        public override void SetLength(long value)=>throw new NotSupportedException();
    }
    private static long JsonSize<T>(T value,long maximum)
    {using var stream=new SizeStream(maximum);JsonSerializer.Serialize(stream,value);return stream.Length;}

    public async Task<PreparationResult> PrepareAsync(VerifiedSession? session,int file,PreparationRequest input,CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(!Valid(input,file)) return new("invalid",Code:"invalid_preparation");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var callerCancellation=ct;
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            ct=deadline.Token;
            var saved=await savedMappings.ReadAsync(session,input.ClientId,input.TemplateId,ct);
            if(saved.Status!=MappingStatus.Success) return new(saved.Status==MappingStatus.Review?"review":"forbidden",Code:"mapping_unavailable");
            var snapshot=saved.Snapshot!;
            if(snapshot.NeedsReview) return new("review",Code:"saved_mapping_needs_review");
            if(input.MappingRevision is not null && input.MappingRevision!=snapshot.Revision
                || input.ConfigurationKey is not null && input.ConfigurationKey!=snapshot.ConfigurationKey)
                return new("conflict",Code:"preparation_context_changed");
            var result=await staging.ReadOwnedAsync(session,input.ClientId,input.TemplateId,file,input.Sha256,snapshot.ConfigurationKey,
                (path,hash,template,limits,token)=>Task.FromResult(Build(path,hash,file,template,snapshot,input,limits,options.Value.MaximumResponseBytes,token)),ct);
            if(result.Status!="success") return new(result.Status,Code:result.Code);
            // Saved mappings have their own retained revision. Check after file
            // work too; stale work cannot be returned as the new selection.
            var after=await savedMappings.ReadAsync(session,input.ClientId,input.TemplateId,ct);
            if(after.Status!=MappingStatus.Success) return new("forbidden");
            if(after.Snapshot!=null && (after.Snapshot.NeedsReview || after.Snapshot.Revision!=snapshot.Revision
                || after.Snapshot.ConfigurationKey!=snapshot.ConfigurationKey)) return new("conflict",Code:"preparation_context_changed");
            JsonSize(result.Value,options.Value.MaximumResponseBytes);
            return new("success",result.Value);
        }
        catch(OperationCanceledException) when(!callerCancellation.IsCancellationRequested && deadline.IsCancellationRequested)
        {return new("timeout",Code:"preparation_timed_out");}
        catch(Exception error) when(error is InvalidPreparation or WorkbookReadException or InvalidDataException
            or IOException or System.Xml.XmlException or ExcelDataReader.Exceptions.HeaderException or ExcelDataReader.Exceptions.InvalidPasswordException)
        {return new("invalid",Code:"invalid_preparation_source");}
        finally {requests.Release();}
    }

    private bool Valid(PreparationRequest r,int file)=>file>0 && r.ClientId>0 && r.TemplateId>0 && r.RequestId!=Guid.Empty
        && !string.IsNullOrWhiteSpace(r.Sheet) && r.Sheet.Length<=255 && r.Operation is "map" or "dates" or "rows"
        && r.Page>0 && r.Page<=1048576 && r.PageSize is >=1 and <=100
        && UploadDateParser.ValidFormat(r.DateDelimiter??"/",(r.DateOrder??"DMY").ToUpperInvariant())
        && (r.Sha256 is null || Regex.IsMatch(r.Sha256,"^[a-f0-9]{64}$",RegexOptions.CultureInvariant))
        && (r.ConfigurationKey is null || r.ConfigurationKey.Length==64) && (r.MappingRevision is null or >=0)
        && (r.Operation=="map" || r.Sha256 is not null && r.ConfigurationKey is not null && r.MappingRevision is not null)
        && (r.Mappings is null || r.Mappings.Count<=1024 && r.Mappings.All(m=>m is not null && m.ColumnId>0 && m.SourcePosition is null or >0))
        && ValidRows(r.ExcludedRows) && ValidRows(r.ForceIncludeRows);
    private bool ValidRows(IReadOnlyList<int>? rows)=>rows is null || rows.Count<=options.Value.MaximumSelectionRows
        && rows.All(n=>n is >=2 and <=1048576) && rows.Distinct().Count()==rows.Count;

    public static IReadOnlyList<PreparationHeader> Headers(IReadOnlyList<WorkbookCell> cells)
    {
        string Label(WorkbookCell c)=>c.Kind switch {
            WorkbookCellKind.Text=>c.Text??"",WorkbookCellKind.Number=>c.NumberToken??"",
            WorkbookCellKind.Boolean=>c.Boolean==true?"True":"False",
            WorkbookCellKind.DateTime=>c.DateTime!.Value.ToString("yyyy-MM-dd HH:mm:ss",CultureInfo.InvariantCulture),
            WorkbookCellKind.Time or WorkbookCellKind.Duration=>c.Time.ToString()??"",_=>""};
        var labels=cells.Select(Label).ToArray();
        return cells.Select((c,index)=>new PreparationHeader(c.Column,labels[index],string.IsNullOrWhiteSpace(labels[index])
            || labels.Count(n=>n==labels[index])>1)).ToArray();
    }

    public static IReadOnlyList<PreparedMapping> AutoMap(TemplateDefinition template,IReadOnlyList<PreparationHeader> headers)
    {
        string Normal(string name)=>Regex.Replace(name.ToLowerInvariant(),"[^a-z0-9]+","");
        string Doc(string name)=>Normal(name.Replace("#"," no ",StringComparison.Ordinal));
        var aliases=new Dictionary<string,string[]> { ["quantity"]=["qty"],["receivequantity"]=["qty"],
            ["vendor"]=["supplier"],["vendorbillno"]=["supbill"],["grnno"]=["grn"],["pono"]=["po"],
            ["rate"]=["price"],["taxamount"]=["salestax"],["unitofmeasurement"]=["unit"] };
        var chosen=new Dictionary<int,(int Position,double Score)>(); var used=new HashSet<int>();
        foreach(bool allowAlias in new[]{false,true}) foreach(var column in template.Columns)
        {
            if(chosen.ContainsKey(column.Id)) continue;
            string key=Normal(column.Name); var names=aliases.GetValueOrDefault(key,[]).ToHashSet(StringComparer.Ordinal);
            if(column.Name.Trim().Equals("Amount",StringComparison.OrdinalIgnoreCase) && column.Role=="footing") names.Add("total");
            foreach(var source in headers.Where(h=>!h.Ambiguous && !used.Contains(h.Position)))
            {
                string sourceKey=Normal(source.Label);
                bool exact=key.Length>0 && (sourceKey==key || column.Role=="document_id" && Doc(source.Label)==Doc(column.Name));
                if(exact || allowAlias && names.Contains(sourceKey))
                {chosen[column.Id]=(source.Position,exact?1:0.95);used.Add(source.Position);break;}
            }
        }
        foreach(var column in template.Columns)
        {
            if(chosen.ContainsKey(column.Id)) continue;
            string key=column.Name.Trim().ToLowerInvariant().Replace(" ",""); int? position=null; double score=0;
            foreach(var source in headers.Where(h=>!h.Ambiguous && !used.Contains(h.Position)))
            {
                string name=source.Label.Trim().ToLowerInvariant().Replace(" ","");
                if(key.Length==0 || name.Length==0 || key=="factoryunit" && name=="unit") continue;
                if(key.Contains(name,StringComparison.Ordinal) || name.Contains(key,StringComparison.Ordinal))
                {double value=(double)Math.Min(key.Length,name.Length)/Math.Max(key.Length,name.Length);if(value>score) {(position,score)=(source.Position,value);}}
            }
            if(position is null && headers.Count>template.Columns.Count)
            {
                int offset=template.Columns.ToList().IndexOf(column);
                if(offset<headers.Count && !headers[offset].Ambiguous && !used.Contains(headers[offset].Position)) (position,score)=(headers[offset].Position,0.3);
            }
            if(position is not null) {chosen[column.Id]=(position.Value,score);used.Add(position.Value);}
        }
        return template.Columns.Select(c=>new PreparedMapping(c.Id,c.StorageKey,c.Name,c.Type,c.Role,TenantTemplateService.EffectiveComparison(c,template),
            chosen.TryGetValue(c.Id,out var match)?match.Position:null,chosen.GetValueOrDefault(c.Id).Score,"auto")).ToArray();
    }

    private static PreparationOutput Build(string path,string hash,int file,TemplateDefinition template,MappingSnapshot saved,
        PreparationRequest input,WorkbookReadLimits limits,int maximumResponseBytes,CancellationToken ct)
    {
        using var rows=WorkbookReader.Read(path,".xls",input.Sheet,limits,ct).GetEnumerator();
        if(!rows.MoveNext()) throw new InvalidPreparation();
        var headers=Headers(rows.Current.Cells); var warnings=new List<string>();
        if(headers.Any(h=>h.Ambiguous)) warnings.Add("Blank or duplicate headers require an explicit source position; saved names are not guessed.");
        var mappings=AutoMap(template,headers).ToArray();
        foreach(var entry in saved.Entries)
        {
            var matches=headers.Where(h=>h.Label==entry.Header && !h.Ambiguous).ToArray();
            int target=Array.FindIndex(mappings,m=>m.ColumnId==entry.ColumnId && m.StorageKey==entry.StorageKey);
            if(target<0 || matches.Length!=1) {warnings.Add("A saved source header is absent or ambiguous; review the mapping.");continue;}
            for(int index=0;index<mappings.Length;index++)
                if(index!=target && mappings[index].SourcePosition==matches[0].Position && mappings[index].Origin=="auto")
                    mappings[index]=mappings[index] with {SourcePosition=null,Score=0};
            mappings[target]=mappings[target] with {SourcePosition=matches[0].Position,Score=1,Origin="saved"};
        }
        if(input.Mappings is not null)
        {
            if(input.Mappings.Select(m=>m.ColumnId).Distinct().Count()!=input.Mappings.Count
                || input.Mappings.Any(m=>!template.Columns.Any(c=>c.Id==m.ColumnId) || m.SourcePosition is {} p && !headers.Any(h=>h.Position==p))) throw new InvalidPreparation();
            mappings=mappings.Select(m=>m with {SourcePosition=input.Mappings.SingleOrDefault(p=>p.ColumnId==m.ColumnId)?.SourcePosition,
                Score=0,Origin="manual"}).ToArray();
        }
        if(mappings.Where(m=>m.SourcePosition is not null).GroupBy(m=>m.SourcePosition).Any(g=>g.Count()>1))
            throw new InvalidPreparation();
        string delimiter=input.DateDelimiter??"/",order=(input.DateOrder??"DMY").ToUpperInvariant();
        var excluded=(input.ExcludedRows??[]).ToHashSet(); var forced=(input.ForceIncludeRows??[]).ToHashSet();
        if(excluded.Overlaps(forced)) throw new InvalidPreparation();
        string selection=Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(new {
            file,input.ClientId,input.TemplateId,input.Sheet,hash,template.ConfigurationKey,saved.Revision,
            Mappings=mappings.Select(m=>new {m.ColumnId,m.SourcePosition}),delimiter,order,
            Excluded=excluded.Order().ToArray(),Forced=forced.Order().ToArray()})));
        var context=new PreparationContext(input.RequestId,file,input.ClientId,input.TemplateId,input.Sheet,hash,template.ConfigurationKey,saved.Revision,selection);
        if(input.Operation=="map") return new(context,headers,mappings,warnings);
        var dateMappings=mappings.Where(m=>m.Type=="Date" && m.SourcePosition is not null).ToArray();
        var samples=new List<PreparedDateSample>(); long sampleBytes=0; var counts=dateMappings.ToDictionary(m=>m.ColumnId,_=>0);
        var preview=new List<PreparedRow>(); int total=0,last=1; long previewBytes=0; DateTime? first=null,end=null; int dates=0;
        bool footing=template.Columns.Any(c=>c.Role=="footing");
        var documentDate=mappings.SingleOrDefault(m=>m.Role=="document_date");
        bool scanDates=input.Operation=="dates" && !footing && documentDate?.SourcePosition is not null;
        int key=(mappings.SingleOrDefault(m=>m.Role=="document_id")??mappings.FirstOrDefault())?.SourcePosition??0;
        while(rows.MoveNext())
        {
            ct.ThrowIfCancellationRequested(); var row=rows.Current; last=row.Number; total++;
            WorkbookCell Cell(int position)=>row.Cells.FirstOrDefault(c=>c.Column==position)??new(row.Number,position,WorkbookCellKind.Blank);
            if(input.Operation=="rows" && total>(long)(input.Page-1)*input.PageSize && preview.Count<input.PageSize)
            {
                var identity=Cell(key);
                bool flagged=key>0 && (identity.Kind==WorkbookCellKind.Blank || identity.Kind==WorkbookCellKind.Text
                    && (string.IsNullOrWhiteSpace(identity.Text)||identity.Text.Contains("total",StringComparison.OrdinalIgnoreCase)));
                var item=new PreparedRow(row.Number,row.Cells,flagged,excluded.Contains(row.Number),forced.Contains(row.Number));
                // Bound retained page bytes before retaining another large row.
                previewBytes+=JsonSize(item,maximumResponseBytes-previewBytes);
                preview.Add(item);
            }
            if(input.Operation!="dates") continue;
            foreach(var mapping in dateMappings)
            {
                var cell=Cell(mapping.SourcePosition!.Value); DateTime? parsed=null; string? error=null;
                try {parsed=UploadDateParser.Parse(cell,delimiter,order);} catch(FormatException e) {error=e.Message;}
                if(counts[mapping.ColumnId]<3 && (parsed is not null || error is not null))
                {
                    var sample=new PreparedDateSample(mapping.ColumnId,row.Number,cell,parsed?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),error);
                    sampleBytes+=JsonSize(sample,maximumResponseBytes-sampleBytes);
                    samples.Add(sample);counts[mapping.ColumnId]++;
                }
                if(scanDates && mapping.ColumnId==documentDate!.ColumnId && parsed is not null)
                {first=first is null || parsed<first?parsed:first;end=end is null || parsed>end?parsed:end;dates++;}
            }
            if(!scanDates && counts.Values.All(n=>n>=3)) break;
        }
        // Only a full row scan can validate caller row positions. Date previews
        // do not apply selection/override choices or certify import eligibility.
        if(input.Operation=="rows" && (excluded.Any(n=>n>last)||forced.Any(n=>n>last))) throw new InvalidPreparation();
        var suggestion=new PreparedDateSuggestion(footing?"await_footing_validation":documentDate is null?"configure_document_date"
            :documentDate.SourcePosition is null?"map_document_date":"ready",first?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),end?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),dates);
        return new(context,headers,mappings,warnings,input.Operation=="dates"?samples:null,input.Operation=="dates"?suggestion:null,
            input.Operation=="rows"?preview:null,input.Operation=="rows"?total:null,input.Operation=="rows"?total-excluded.Count:null);
    }
}
