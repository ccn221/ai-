# 打成单文件 exe（Windows）。用之前先 pip install pyinstaller
param(
    [string]$Python = "python",
    [string]$Name = "MemoryMap"
)
$ErrorActionPreference = "Stop"
$project = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$entry = Join-Path $project "src\memory_map.py"

& $Python -m PyInstaller --noconfirm --onefile --windowed --name $Name `
    --distpath (Join-Path $project "dist") --workpath (Join-Path $project "build") `
    --specpath $project $entry

if ($LASTEXITCODE -ne 0) { throw "打包失败" }
Write-Output (Join-Path $project ("dist\" + $Name + ".exe"))
