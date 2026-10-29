$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$recordPath = Join-Path $root 'data\processes.json'
if (!(Test-Path -LiteralPath $recordPath)) { Write-Output '没有 DeskFlow 启动记录。'; exit }
$records = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
foreach ($record in $records) {
 $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
 if ($process -and $process.StartTime.ToUniversalTime().ToString('o') -eq $record.start) {
  $actual = Get-CimInstance Win32_Process -Filter ('ProcessId = '+$record.id)
  if ($actual.ExecutablePath -eq $record.exe) { Stop-Process -Id $record.id }
 }
}
Remove-Item -LiteralPath $recordPath
Write-Output '已停止本启动脚本创建的 DeskFlow 进程。'
