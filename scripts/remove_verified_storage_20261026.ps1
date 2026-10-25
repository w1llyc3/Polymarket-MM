param([switch]$Execute)
$ErrorActionPreference='Stop'
$nbaRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$nbaDataRoot=(Resolve-Path -LiteralPath (Join-Path $nbaRoot 'data')).Path
$nbaPrefix=$nbaDataRoot.TrimEnd('\')+'\'
$nbaArchiveRoot=Join-Path $nbaDataRoot 'archives\storage-cleanup-20261026'
$nbaManifestPath=Join-Path $nbaArchiveRoot 'manifest.json'
$nbaManifest=Get-Content -LiteralPath $nbaManifestPath -Raw | ConvertFrom-Json
$nbaExpected=@(
 'unattended/2026-10-22/discovery.jsonl',
 'unattended/2026-10-23/discovery.jsonl',
 'unattended/2026-10-24/discovery.jsonl',
 'day-studies/2026-10-21/5676016/REDECODED-L0.incomplete-1792596914015170600',
 'day-studies/2026-10-21/5676016/REDECODED-L1.incomplete-1792597090581723400',
 'day-studies/2026-10-21/5676016/REDECODED-L1.incomplete-1792597171506635100',
 'day-studies/2026-10-21/5676021/REDECODED-L0.incomplete-1792597640646027000',
 'day-studies/2026-10-21/5680507/REDECODED-L0.incomplete-1792597710562281000'
)
if($nbaManifest.root -ne $nbaDataRoot -or $nbaManifest.archives.Count -ne $nbaExpected.Count){throw 'Wrong/incomplete archive manifest'}
if(@(Compare-Object $nbaExpected @($nbaManifest.archives | ForEach-Object {$_.source})).Count){throw 'Candidate scope changed'}
function Get-CheckedNbaPath([string]$Relative){
 $nbaResolved=(Resolve-Path -LiteralPath (Join-Path $nbaDataRoot $Relative)).Path
 if(-not $nbaResolved.StartsWith($nbaPrefix,[System.StringComparison]::OrdinalIgnoreCase)){throw 'Target escaped NBA data directory'}
 $nbaCheck=Get-Item -LiteralPath $nbaResolved -Force
 while($nbaCheck.FullName -ne $nbaDataRoot){
  if($nbaCheck.Attributes -band [System.IO.FileAttributes]::ReparsePoint){throw 'Reparse point rejected'}
  $nbaCheck=Get-Item -LiteralPath (Split-Path -Parent $nbaCheck.FullName) -Force
 }
 return $nbaResolved
}
function Get-NbaSha256([string]$Path,[System.IO.Stream]$LockedStream=$null){
 $nbaStream=if($null -ne $LockedStream){$LockedStream.Position=0;$LockedStream}else{[System.IO.File]::OpenRead($Path)}
 $nbaHasher=[System.Security.Cryptography.SHA256]::Create()
 try{return [BitConverter]::ToString($nbaHasher.ComputeHash($nbaStream)).Replace('-','').ToLowerInvariant()}
 finally{$nbaHasher.Dispose();if($null -eq $LockedStream){$nbaStream.Dispose()}}
}
$nbaLocks=@{}
try{
 foreach($nbaItem in $nbaManifest.archives){
  if($nbaItem.originals_removed){throw 'Already cleaned; this operation does not repeat deletion'}
  if($nbaItem.verification -ne 'FULL_DECOMPRESSION_SIZE_AND_SHA256_MATCH'){throw 'Archive not fully verified'}
  $nbaTarget=Get-CheckedNbaPath $nbaItem.source
  $nbaArchive=Get-CheckedNbaPath $nbaItem.archive
  if(-not $nbaArchive.StartsWith($nbaArchiveRoot+'\',[System.StringComparison]::OrdinalIgnoreCase)){throw 'Unexpected archive location'}
  if($nbaItem.source.EndsWith('discovery.jsonl')){
   $nbaDayFolder=Split-Path -Parent $nbaTarget
   $nbaState=Get-Content -LiteralPath (Join-Path $nbaDayFolder 'state.json') -Raw | ConvertFrom-Json
   if($nbaState.scheduler_status -ne 'COMPLETE'){throw 'Scheduler not complete'}
   $nbaLockPath=Join-Path $nbaDayFolder 'scheduler.lock'
  }else{$nbaLockPath=Join-Path $nbaTarget 'paper.sqlite.writer.lock'}
  $nbaLock=[System.IO.File]::Open($nbaLockPath,[System.IO.FileMode]::Open,[System.IO.FileAccess]::ReadWrite,[System.IO.FileShare]::ReadWrite)
  try{$nbaLock.Lock(0,1)}catch{$nbaLock.Dispose();throw}
  $nbaLocks[$nbaItem.source]=$nbaLock
  if((Get-NbaSha256 $nbaArchive) -ne $nbaItem.archive_sha256){throw 'Archive hash changed'}
  $nbaActual=if(Test-Path -LiteralPath $nbaTarget -PathType Container){@(Get-ChildItem -LiteralPath $nbaTarget -File -Recurse -Force | ForEach-Object {$_.FullName})}else{@($nbaTarget)}
  $nbaListed=@($nbaItem.files | ForEach-Object {Get-CheckedNbaPath $_.path})
  if(@(Compare-Object $nbaActual $nbaListed).Count){throw 'Source file list changed'}
  foreach($nbaFile in $nbaItem.files){
   $nbaFilePath=Get-CheckedNbaPath $nbaFile.path
   if($nbaFilePath -ne $nbaTarget -and -not $nbaFilePath.StartsWith($nbaTarget+'\',[System.StringComparison]::OrdinalIgnoreCase)){throw 'Member escaped candidate'}
   $nbaFileHash=if($nbaFilePath -eq $nbaLockPath){Get-NbaSha256 $nbaFilePath $nbaLock}else{Get-NbaSha256 $nbaFilePath}
   if((Get-Item -LiteralPath $nbaFilePath).Length -ne $nbaFile.bytes -or $nbaFileHash -ne $nbaFile.sha256){throw 'Original changed after archive verification'}
  }
  [pscustomobject]@{stage='VERIFIED_FOR_REMOVAL';source=$nbaItem.source;source_bytes=$nbaItem.source_bytes;archive_bytes=$nbaItem.archive_bytes} | ConvertTo-Json -Compress
 }
 if(-not $Execute){Write-Output 'Dry run only. No originals removed.';return}
 foreach($nbaItem in $nbaManifest.archives){
  # Re-resolve the absolute target immediately before any recursive operation.
  $nbaTarget=Get-CheckedNbaPath $nbaItem.source
  if($nbaItem.source.EndsWith('discovery.jsonl')){
   Remove-Item -LiteralPath $nbaTarget -Force
  }else{
   # Keep the writer lock until all archived data files have been removed.
   foreach($nbaFile in $nbaItem.files){
    if(-not $nbaFile.path.EndsWith('/paper.sqlite.writer.lock')){Remove-Item -LiteralPath (Get-CheckedNbaPath $nbaFile.path) -Force}
   }
   $nbaLocks[$nbaItem.source].Dispose();$nbaLocks.Remove($nbaItem.source)
   $nbaRemaining=@(Get-ChildItem -LiteralPath $nbaTarget -File -Recurse -Force)
   if(@($nbaRemaining | Where-Object {$_.FullName -ne (Join-Path $nbaTarget 'paper.sqlite.writer.lock')}).Count){throw 'New files appeared; leave remaining directory intact'}
   $nbaTarget=Get-CheckedNbaPath $nbaItem.source
   Remove-Item -LiteralPath $nbaTarget -Recurse -Force
  }
  $nbaItem.originals_removed=$true
  $nbaManifest | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath ($nbaManifestPath+'.tmp') -Encoding utf8
  Move-Item -LiteralPath ($nbaManifestPath+'.tmp') -Destination $nbaManifestPath -Force
  [pscustomobject]@{stage='ORIGINAL_REMOVED_ARCHIVE_RETAINED';source=$nbaItem.source;at=(Get-Date -Format o)} | ConvertTo-Json -Compress
 }
}finally{foreach($nbaLock in $nbaLocks.Values){$nbaLock.Dispose()}}
