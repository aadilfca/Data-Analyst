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
    private const long PreviewCacheMaximumEntryBytes=4L*1024*1024;
    private const long PreviewCacheMaximumBytes=8L*1024*1024;
    private const int PreviewCacheMaximumEntries=4;
    private static readonly TimeSpan PreviewCacheLifetime=TimeSpan.FromMinutes(10);
    private static readonly object PreviewCacheGate=new();
    private static readonly Dictionary<string,PreviewPageCache> PreviewPageCaches=new(StringComparer.Ordinal);
    private static long previewPageCacheBytes;

    private sealed class PreviewPageCache(WorkbookRow header,IReadOnlyDictionary<int,IReadOnlyList<WorkbookRow>> pages,
        int totalRows,int lastRowNumber,long bytes)
    {
        public WorkbookRow Header {get;}=header;
        public IReadOnlyDictionary<int,IReadOnlyList<WorkbookRow>> Pages {get;}=pages;
        public int TotalRows {get;}=totalRows;
        public int LastRowNumber {get;}=lastRowNumber;
        public long Bytes {get;}=bytes;
        public DateTime LastAccessUtc {get;set;}=DateTime.UtcNow;
    }

    internal sealed class InvalidPreparation : Exception;
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
    internal static long JsonSize<T>(T value,long maximum)
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
            PreviewPageCache? cachedPreview=null;
        if(input.Operation=="rows" && input.Sha256 is { } sourceHash
                && TryGetPreviewCache(PreviewCacheKey(file,sourceHash,input),out var candidate)
                && candidate.Pages.ContainsKey(input.Page)) cachedPreview=candidate;
            ct=deadline.Token;
            int responseLimit=input.Operation=="preview"?options.Value.MaximumFullPreviewBytes:options.Value.MaximumResponseBytes;
            var result=await ReadBoundAsync(session,file,input,
                (path,hash,template,snapshot,limits,token)=>Task.FromResult(Build(path,hash,file,template,snapshot,input,limits,
                    responseLimit,token,cachedPreview)),ct,cachedPreview is not null);
            if(result.Status!="success") return new(result.Status,Code:result.Code);
            long outputLimit=input.Operation=="preview" && result.Value is {PreviewComplete:false}
                ?options.Value.MaximumResponseBytes:responseLimit;
            try {JsonSize(result.Value,outputLimit);}
            catch(InvalidPreparation) when(input.Operation=="preview" && result.Value is {PreviewComplete:true,Rows:{} previewRows})
            {
                var fallback=result.Value with {Rows=previewRows.Take(input.PageSize).ToArray(),PreviewComplete=false,CachedPages=null};
                JsonSize(fallback,options.Value.MaximumResponseBytes);
                return new("success",fallback);
            }
            return new("success",result.Value);
        }
        catch(OperationCanceledException) when(!callerCancellation.IsCancellationRequested && deadline.IsCancellationRequested)
        {return new("timeout",Code:"preparation_timed_out");}
        catch(Exception error) when(error is InvalidPreparation or WorkbookReadException or InvalidDataException
            or IOException or System.Xml.XmlException or ExcelDataReader.Exceptions.HeaderException or ExcelDataReader.Exceptions.InvalidPasswordException)
        {return new("invalid",Code:"invalid_preparation_source");}
        finally {requests.Release();}
    }

    // Shared by preparation and input validation. Parsing never holds database
    // locks, and callers cannot substitute their own template or saved revision.
    internal async Task<OwnedWorkbookResult<T>> ReadBoundAsync<T>(VerifiedSession? session,int file,PreparationRequest input,
        Func<string,string,TemplateDefinition,MappingSnapshot,WorkbookReadLimits,CancellationToken,Task<T>> read,CancellationToken ct,
        bool verifiedPreviewCacheHit=false)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(!Valid(input,file)) return new("invalid",Code:"invalid_preparation");
        var result=await staging.ReadOwnedWithMappingAsync(session,input.ClientId,input.TemplateId,file,input.Sha256,
            input.MappingRevision,input.ConfigurationKey,savedMappings.ReadWithinAuthorizedContextAsync,
            verifiedPreviewCacheHit,
            (path,hash,template,snapshot,limits,token)=>read(path,hash,template,snapshot,limits,token),ct);
        // Preserve P14's review response when the stricter upload boundary
        // rejects a template before its saved mapping can be read in-transaction.
        if(result.Status=="forbidden")
        {
            var saved=await savedMappings.ReadAsync(session,input.ClientId,input.TemplateId,ct);
            if(saved.Status!=MappingStatus.Success)
                return new(saved.Status==MappingStatus.Review?"review":"forbidden",Code:"mapping_unavailable");
        }
        return result;
    }

    private bool Valid(PreparationRequest r,int file)=>file>0 && r.ClientId>0 && r.TemplateId>0 && r.RequestId!=Guid.Empty
        && !string.IsNullOrWhiteSpace(r.Sheet) && r.Sheet.Length<=255 && r.Operation is "map" or "dates" or "rows" or "preview"
        && r.Page>0 && r.Page<=1048576 && r.PageSize is >=1 and <=100 && r.PageCount is >=1 and <=8
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

    internal static PreparedSource ResolveSource(IReadOnlyList<WorkbookCell> cells,string hash,int file,
        TemplateDefinition template,MappingSnapshot saved,PreparationRequest input)
    {
        var headers=Headers(cells); var warnings=new List<string>();
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
        return new(context,template,headers,mappings,warnings);
    }

    private static PreparationOutput Build(string path,string hash,int file,TemplateDefinition template,MappingSnapshot saved,
        PreparationRequest input,WorkbookReadLimits limits,int maximumResponseBytes,CancellationToken ct,PreviewPageCache? verifiedCache=null)
    {
        var excluded=(input.ExcludedRows??[]).ToHashSet();var forced=(input.ForceIncludeRows??[]).ToHashSet();
        string cacheKey=PreviewCacheKey(file,hash,input);
        PreviewPageCache? cached=verifiedCache;
        if(cached is null && input.Operation=="rows" && TryGetPreviewCache(cacheKey,out var current)) cached=current;
        if(input.Operation=="rows" && cached is not null && cached.Pages.TryGetValue(input.Page,out var cachedRows))
        {
            var cachedSource=ResolveSource(cached.Header.Cells,hash,file,template,saved,input);
            return BuildRowsOutput(cachedSource,cachedRows,cached.TotalRows,cached.LastRowNumber,input,excluded,forced,
                maximumResponseBytes,cached.Pages);
        }

        using var rows=WorkbookReader.ReadStaged(path,".xls",input.Sheet,limits,ct).GetEnumerator();
        if(!rows.MoveNext()) throw new InvalidPreparation();
        var header=rows.Current;
        var source=ResolveSource(header.Cells,hash,file,template,saved,input);
        var (context,headers,mappings,warnings)=(source.Context,source.Headers,source.Mappings,source.Warnings);
        string delimiter=input.DateDelimiter??"/",order=(input.DateOrder??"DMY").ToUpperInvariant();
        if(input.Operation=="map") return new(context,headers,mappings,warnings);
        if(input.Operation=="rows")
            return BuildRows(rows,header,source,input,excluded,forced,maximumResponseBytes,ct,cacheKey);
        if(input.Operation=="preview")
            return BuildFullPreview(rows,header,source,input,excluded,forced,maximumResponseBytes,ct,cacheKey);

        var dateMappings=mappings.Where(m=>m.Type=="Date" && m.SourcePosition is not null).ToArray();
        var samples=new List<PreparedDateSample>(); long sampleBytes=0; var counts=dateMappings.ToDictionary(m=>m.ColumnId,_=>0);
        DateTime? first=null,end=null; int dates=0;
        bool footing=template.Columns.Any(c=>c.Role=="footing");
        var documentDate=mappings.SingleOrDefault(m=>m.Role=="document_date");
        bool scanDates=input.Operation=="dates" && !footing && documentDate?.SourcePosition is not null;
        while(rows.MoveNext())
        {
            ct.ThrowIfCancellationRequested(); var row=rows.Current;
            WorkbookCell Cell(int position)=>row.Cells.FirstOrDefault(c=>c.Column==position)??new(row.Number,position,WorkbookCellKind.Blank);
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
        var suggestion=new PreparedDateSuggestion(footing?"await_footing_validation":documentDate is null?"configure_document_date"
            :documentDate.SourcePosition is null?"map_document_date":"ready",first?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),end?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),dates);
        return new(context,headers,mappings,warnings,samples,suggestion,null,null,null);
    }

    private static PreparationOutput BuildRows(IEnumerator<WorkbookRow> rows,WorkbookRow header,PreparedSource source,
        PreparationRequest input,HashSet<int> excluded,HashSet<int> forced,int maximumResponseBytes,
        CancellationToken ct,string cacheKey)
    {
        var pages=new Dictionary<int,IReadOnlyList<WorkbookRow>>();
        long cacheBytes=0;
        bool cacheHeader=TryJsonSize(header.Cells,PreviewCacheMaximumEntryBytes,out cacheBytes);
        long headerBytes=cacheBytes;
        int cacheStartPage=Math.Max(1,input.Page-8);
        var pending=new List<WorkbookRow>(input.PageSize); int pageNumber=1,total=0,last=header.Number;
        IReadOnlyList<WorkbookRow>? requestedPage=null;
        while(rows.MoveNext())
        {
            ct.ThrowIfCancellationRequested(); var row=rows.Current; last=row.Number; total++; pending.Add(row);
            if(pending.Count==input.PageSize) SavePage();
        }
        if(pending.Count>0) SavePage();
        if(excluded.Any(n=>n>last)||forced.Any(n=>n>last)) throw new InvalidPreparation();

        if(cacheHeader && pages.Count>0)
            StorePreviewCache(cacheKey,new(header,pages,total,last,cacheBytes));
        var selected=requestedPage??Array.Empty<WorkbookRow>();
        return BuildRowsOutput(source,selected,total,last,input,excluded,forced,maximumResponseBytes,pages);

        void SavePage()
        {
            var page=pending.ToArray();
            if(pageNumber==input.Page) requestedPage=page;
            long pageBytes;
            if(cacheHeader && pageNumber>=cacheStartPage && TryJsonSize(page,PreviewCacheMaximumEntryBytes-cacheBytes,out pageBytes))
            {pages[pageNumber]=page;cacheBytes+=pageBytes;}
            else if(cacheHeader && pageNumber==input.Page)
            {
                pages.Clear(); cacheBytes=headerBytes;
                if(TryJsonSize(page,PreviewCacheMaximumEntryBytes-cacheBytes,out pageBytes))
                {pages[pageNumber]=page;cacheBytes+=pageBytes;}
            }
            pending.Clear(); pageNumber++;
        }
    }

    private static PreparationOutput BuildFullPreview(IEnumerator<WorkbookRow> rows,WorkbookRow header,PreparedSource source,
        PreparationRequest input,HashSet<int> excluded,HashSet<int> forced,int maximumResponseBytes,
        CancellationToken ct,string cacheKey)
    {
        var fullRows=new List<PreparedRow>(); var firstPage=new List<PreparedRow>(input.PageSize);
        var empty=new PreparationOutput(source.Context,source.Headers,source.Mappings,source.Warnings,
            Rows:Array.Empty<PreparedRow>(),TotalRows:0,SelectedRows:0,PreviewComplete:true);
        int key=(source.Mappings.SingleOrDefault(m=>m.Role=="document_id")??source.Mappings.FirstOrDefault())?.SourcePosition??0;
        long previewBytes=JsonSize(empty,maximumResponseBytes); bool complete=true;
        var pages=new Dictionary<int,IReadOnlyList<WorkbookRow>>();
        long cacheBytes=0; bool cacheHeader=TryJsonSize(header.Cells,PreviewCacheMaximumEntryBytes,out cacheBytes);
        long headerBytes=cacheBytes; int cacheStartPage=Math.Max(1,input.Page-8);
        var pending=new List<WorkbookRow>(input.PageSize); int pageNumber=1,total=0,last=header.Number;

        while(rows.MoveNext())
        {
            ct.ThrowIfCancellationRequested(); var row=rows.Current; total++; last=row.Number;
            var prepared=PrepareRow(row,key,excluded,forced);
            if(firstPage.Count<input.PageSize) firstPage.Add(prepared);
            if(complete)
            {
                try
                {
                    long rowBytes=JsonSize(prepared,maximumResponseBytes-previewBytes-1);
                    if(previewBytes+rowBytes+1>maximumResponseBytes) complete=false;
                    else {fullRows.Add(prepared);previewBytes+=rowBytes+1;}
                }
                catch(InvalidPreparation) {complete=false;}
                if(!complete) fullRows.Clear();
            }
            pending.Add(row);
            if(pending.Count==input.PageSize) SavePage();
        }
        if(pending.Count>0) SavePage();
        if(excluded.Any(n=>n>last)||forced.Any(n=>n>last)) throw new InvalidPreparation();
        if(cacheHeader && pages.Count>0)
            StorePreviewCache(cacheKey,new(header,pages,total,last,cacheBytes));

        if(complete)
        {
            var output=new PreparationOutput(source.Context,source.Headers,source.Mappings,source.Warnings,
                Rows:fullRows,TotalRows:total,SelectedRows:total-excluded.Count,PreviewComplete:true);
            return output;
        }
        return new(source.Context,source.Headers,source.Mappings,source.Warnings,
            Rows:firstPage,TotalRows:total,SelectedRows:total-excluded.Count,PreviewComplete:false);

        void SavePage()
        {
            var page=pending.ToArray(); long pageBytes;
            if(cacheHeader && pageNumber>=cacheStartPage
                && TryJsonSize(page,PreviewCacheMaximumEntryBytes-cacheBytes,out pageBytes))
            {pages[pageNumber]=page;cacheBytes+=pageBytes;}
            else if(cacheHeader && pageNumber==input.Page)
            {
                pages.Clear(); cacheBytes=headerBytes;
                if(TryJsonSize(page,PreviewCacheMaximumEntryBytes-cacheBytes,out pageBytes))
                {pages[pageNumber]=page;cacheBytes+=pageBytes;}
            }
            pending.Clear(); pageNumber++;
        }
    }

    private static PreparationOutput BuildRowsOutput(PreparedSource source,IReadOnlyList<WorkbookRow> rows,int total,int last,
        PreparationRequest input,HashSet<int> excluded,HashSet<int> forced,int maximumResponseBytes,
        IReadOnlyDictionary<int,IReadOnlyList<WorkbookRow>> availablePages)
    {
        if(excluded.Any(n=>n>last)||forced.Any(n=>n>last)) throw new InvalidPreparation();
        int key=(source.Mappings.SingleOrDefault(m=>m.Role=="document_id")??source.Mappings.FirstOrDefault())?.SourcePosition??0;
        var preview=new List<PreparedRow>(rows.Count); long previewBytes=0;
        foreach(var row in rows)
        {
            var item=PrepareRow(row,key,excluded,forced);
            previewBytes+=JsonSize(item,maximumResponseBytes-previewBytes); preview.Add(item);
        }
        var output=new PreparationOutput(source.Context,source.Headers,source.Mappings,source.Warnings,null,null,preview,total,total-excluded.Count);
        var included=new List<PreparationPage>();
        for(int page=input.Page+1;page<input.Page+input.PageCount;page++)
        {
            if(!availablePages.TryGetValue(page,out var pageRows)) break;
            var pageOutput=new PreparationPage(page,PrepareRows(pageRows));
            included.Add(pageOutput);
            try {JsonSize(output with {CachedPages=included},maximumResponseBytes);}
            catch(InvalidPreparation) {included.RemoveAt(included.Count-1);break;}
        }
        return included.Count==0?output:output with {CachedPages=included};

        IReadOnlyList<PreparedRow> PrepareRows(IReadOnlyList<WorkbookRow> pageRows)
        {
            var result=new List<PreparedRow>(pageRows.Count);
            foreach(var row in pageRows) result.Add(PrepareRow(row,key,excluded,forced));
            return result;
        }
    }

    private static PreparedRow PrepareRow(WorkbookRow row,int key,HashSet<int> excluded,HashSet<int> forced)
    {
        var identity=row.Cells.FirstOrDefault(c=>c.Column==key)??new(row.Number,key,WorkbookCellKind.Blank);
        bool flagged=key>0 && (identity.Kind==WorkbookCellKind.Blank || identity.Kind==WorkbookCellKind.Text
            && (string.IsNullOrWhiteSpace(identity.Text)||identity.Text.Contains("total",StringComparison.OrdinalIgnoreCase)));
        return new(row.Number,row.Cells,flagged,excluded.Contains(row.Number),forced.Contains(row.Number));
    }

    private static string PreviewCacheKey(int file,string hash,PreparationRequest input)=>Convert.ToHexString(SHA256.HashData(
        JsonSerializer.SerializeToUtf8Bytes(new {file,input.ClientId,input.TemplateId,input.Sheet,input.PageSize,hash})));

    private static bool TryJsonSize<T>(T value,long maximum,out long size)
    {
        try {size=JsonSize(value,maximum);return true;}
        catch(InvalidPreparation) {size=0;return false;}
    }

    private static bool TryGetPreviewCache(string key,out PreviewPageCache cache)
    {
        lock(PreviewCacheGate)
        {
            RemoveExpiredPreviewCaches(DateTime.UtcNow);
            if(PreviewPageCaches.TryGetValue(key,out cache!)) {cache.LastAccessUtc=DateTime.UtcNow;return true;}
        }
        cache=null!; return false;
    }

    private static void StorePreviewCache(string key,PreviewPageCache cache)
    {
        if(cache.Bytes>PreviewCacheMaximumEntryBytes || cache.Pages.Count==0) return;
        lock(PreviewCacheGate)
        {
            RemoveExpiredPreviewCaches(DateTime.UtcNow);
            if(PreviewPageCaches.Remove(key,out var previous)) previewPageCacheBytes-=previous.Bytes;
            while(PreviewPageCaches.Count>=PreviewCacheMaximumEntries || previewPageCacheBytes+cache.Bytes>PreviewCacheMaximumBytes)
            {
                var oldest=PreviewPageCaches.MinBy(item=>item.Value.LastAccessUtc);
                if(oldest.Value is null) break;
                PreviewPageCaches.Remove(oldest.Key); previewPageCacheBytes-=oldest.Value.Bytes;
            }
            PreviewPageCaches[key]=cache; previewPageCacheBytes+=cache.Bytes;
        }
    }

    private static void RemoveExpiredPreviewCaches(DateTime now)
    {
        foreach(var item in PreviewPageCaches.Where(item=>now-item.Value.LastAccessUtc>PreviewCacheLifetime).ToArray())
        {PreviewPageCaches.Remove(item.Key);previewPageCacheBytes-=item.Value.Bytes;}
    }
}
