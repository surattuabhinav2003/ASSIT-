# Installs CF Minutes on this laptop for the current Windows user. No administrator rights needed.
#   - needs Git and Python 3.11+ (offers to install them with winget when missing)
#   - clones the app to %LOCALAPPDATA%\Programs\CFMinutes (updates itself from then on)
#   - asks for the OpenAI key once and keeps it encrypted for this Windows user (never written to a file in plain text)
#   - adds a Desktop and Start menu shortcut
param(
    [string]$Repo = "https://github.com/surattuabhinav2003/ASSIT-.git",
    [string]$KeyFile = ""        # optional: a text file whose only content is the key (deleted after it is read)
)
$ErrorActionPreference = "Stop"
$Target = Join-Path $env:LOCALAPPDATA "Programs\CFMinutes"

function Need($cmd, $wingetId, $label) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
    Write-Host "$label is not installed."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Install $label first, then run this again." }
    $a = Read-Host "Install $label now with winget? (Y/n)"
    if ($a -match '^(n|no)$') { throw "$label is needed." }
    winget install --id $wingetId -e --accept-package-agreements --accept-source-agreements | Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$label was installed but is not on the path yet: open a new PowerShell window and run this again." }
}
Need "git" "Git.Git" "Git"
Need "python" "Python.Python.3.11" "Python 3.11"

if (Test-Path (Join-Path $Target ".git")) {
    Write-Host "CF Minutes is already installed: updating."
    git -C $Target pull --ff-only
} else {
    New-Item -ItemType Directory -Force (Split-Path $Target) | Out-Null
    git clone $Repo $Target
}
Set-Location $Target
if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

# the key
$have = .\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, '.'); from minutes import secrets; print('yes' if secrets.load_key() else 'no')"
if ($have -ne "yes") {
    $key = ""
    if ($KeyFile -and (Test-Path $KeyFile)) { $key = (Get-Content $KeyFile -Raw).Trim(); Remove-Item $KeyFile -Force }
    if (-not $key) {
        $sec = Read-Host "Paste the OpenAI key (it is not shown)" -AsSecureString
        $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
    $env:CFM_KEY = $key
    .\.venv\Scripts\python.exe -c "import os, sys; sys.path.insert(0, '.'); from minutes import secrets; secrets.save_key(os.environ['CFM_KEY'])"
    Remove-Item Env:CFM_KEY
}

# shortcuts
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))) {
    $lnk = $ws.CreateShortcut((Join-Path $dir "CF Minutes.lnk"))
    $lnk.TargetPath = Join-Path $Target ".venv\Scripts\pythonw.exe"
    $lnk.Arguments = "run.py"
    $lnk.WorkingDirectory = $Target
    $lnk.IconLocation = Join-Path $Target "ui\app.ico"
    $lnk.Description = "CF Minutes: meeting transcripts and minutes"
    $lnk.Save()
}
Write-Host ""
Write-Host "CF Minutes is installed. Open it from the Desktop or Start menu."
Write-Host "It starts recording by itself when a Teams meeting begins (Settings turns that off)."
Start-Process (Join-Path $Target ".venv\Scripts\pythonw.exe") -ArgumentList "run.py" -WorkingDirectory $Target
