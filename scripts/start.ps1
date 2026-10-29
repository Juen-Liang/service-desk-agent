$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
if (Test-Path (Join-Path $data 'processes.json')) { throw '已有启动记录；请先运行 scripts/stop.ps1，避免重复启动。' }
$commands = @(
 @{name='demo';exe=$python;args='-m uvicorn deskflow.demo:app --host 127.0.0.1 --port 8012';cwd=(Join-Path $root 'backend')},
 @{name='api';exe=$python;args='-m uvicorn deskflow.api:app --host 127.0.0.1 --port 8011';cwd=(Join-Path $root 'backend')},
 @{name='worker';exe=$python;args='-m deskflow.worker';cwd=(Join-Path $root 'backend')},
 @{name='web';exe=(Get-Command node).Source;args=('"'+(Join-Path $root 'frontend\node_modules\vite\bin\vite.js')+'" --host 127.0.0.1 --port 5178');cwd=(Join-Path $root 'frontend')}
)
$records = @()
foreach ($command in $commands) {
 $process = Start-Process -FilePath $command.exe -ArgumentList $command.args -WorkingDirectory $command.cwd -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $data ($command.name+'.out.log')) -RedirectStandardError (Join-Path $data ($command.name+'.err.log'))
 $records += @{id=$process.Id;start=$process.StartTime.ToUniversalTime().ToString('o');name=$command.name;exe=$command.exe}
 $records | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $data 'processes.json')
}
Write-Output 'DeskFlow: http://127.0.0.1:5178 | 登录凭据: data/demo-credentials.json'
