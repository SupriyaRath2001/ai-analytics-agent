# Start, stop or check the analytics-agent VM on Google Cloud. For Windows.
#
# Easiest: double-click scripts\vm-start.cmd, vm-stop.cmd or vm-status.cmd.
# The .cmd files run this script even when Windows blocks PowerShell scripts.
#
# From PowerShell, in the project folder:
#   .\scripts\vm-status.cmd
#   .\scripts\vm-start.cmd -Zone us-central1-a      (another zone, -Project or -Vm)
#
# It uses your gcloud sign-in and the project from `gcloud config set project`.
# The defaults match scripts/gcloud-setup.sh.

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action,
    [string]$Project = "",
    [string]$Zone = "asia-south1-a",
    [string]$Vm = "analytics-agent"
)

# If an API is off, fail with a clear message instead of a hidden yes/no question.
$env:CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API = "false"

function Exit-WithError([string]$Message) {
    Write-Host ""
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# Runs gcloud without questions. Returns Ok (True/False), Out (the answer) and Err (error text).
function Invoke-Gcloud {
    $lines = & $Gcloud @args --quiet 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    $out = $lines | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }
    $err = $lines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }
    [pscustomobject]@{
        Ok  = $ok
        Out = (($out | ForEach-Object { "$_" }) -join "`n").Trim()
        Err = (($err | ForEach-Object { "$_" }) -join "`n").Trim()
    }
}

# Turns a gcloud error into a short message that says what to do.
function Exit-WithGcloudError([string]$Text) {
    if ($Text -match "auth login|auth tokens|active account") {
        Exit-WithError "You are not signed in to Google Cloud. Run: gcloud.cmd auth login"
    }
    if ($Text -match "billing") {
        Exit-WithError "Billing is off for project $Project. Turn it on in the Cloud Console (Billing), then try again."
    }
    if ($Text -match "has not been used|SERVICE_DISABLED") {
        Exit-WithError "The Compute Engine API is off in project $Project, so there is no VM yet. Create it first with scripts/gcloud-setup.sh."
    }
    if ($Text -match "permission") {
        Exit-WithError "Account '$Account' may not manage VMs in project $Project. Sign in as the project owner (gcloud.cmd auth login), or ask the owner for the role 'Compute Instance Admin (v1)'."
    }
    if ($Text -match "was not found|notFound") {
        Exit-WithError "There is no VM named '$Vm' in zone $Zone of project $Project. Create it with scripts/gcloud-setup.sh. To list your VMs: gcloud.cmd compute instances list"
    }
    Exit-WithError "gcloud said:`n$Text"
}

# Asks Google for the VM's state and IP address, for example "RUNNING  34.100.1.2".
function Get-VmInfo {
    Invoke-Gcloud compute instances describe $Vm "--zone=$Zone" "--project=$Project" `
        "--format=value(status,networkInterfaces[0].accessConfigs[0].natIP)"
}

# 1. Find gcloud. Use gcloud.cmd, because Windows may block gcloud.ps1.
$Gcloud = (Get-Command gcloud.cmd -ErrorAction SilentlyContinue).Source
if (-not $Gcloud) {
    $Gcloud = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
}
if (-not (Test-Path $Gcloud)) {
    Exit-WithError "gcloud is not installed. Install it with: winget install --id Google.CloudSDK -e"
}

# 2. Pick the project and see who is signed in.
if (-not $Project) {
    $Project = (Invoke-Gcloud config get-value project).Out
}
if (-not $Project) {
    Exit-WithError "No project is set. Run: gcloud.cmd config set project YOUR_PROJECT_ID"
}
$Account = (Invoke-Gcloud config get-value account).Out

# 3. Read the VM's state. If the sign-in is missing or expired, sign in and try once more.
$info = Get-VmInfo
if (-not $info.Ok -and $info.Err -match "auth login|auth tokens|active account") {
    Write-Host "Your Google Cloud sign-in is missing or expired. A sign-in page opens in your browser."
    & $Gcloud auth login
    $Account = (Invoke-Gcloud config get-value account).Out
    $info = Get-VmInfo
}
if (-not $info.Ok) {
    Exit-WithGcloudError $info.Err
}

$State, $Ip = $info.Out -split "\s+"
$Words = @{ RUNNING = "running"; TERMINATED = "stopped"; STOPPING = "stopping"; STAGING = "starting"; PROVISIONING = "starting" }
Write-Host "VM '$Vm' in project $Project (zone $Zone), signed in as $Account"
Write-Host "State: $State ($($Words[$State]))"

# 4. Do what was asked.
if ($Action -eq "start") {
    if ($State -eq "RUNNING") {
        Write-Host "It is already running." -ForegroundColor Green
    }
    elseif ($State -eq "TERMINATED") {
        Write-Host "Starting the VM. This takes about 30 seconds..."
        $result = Invoke-Gcloud compute instances start $Vm "--zone=$Zone" "--project=$Project"
        if (-not $result.Ok) { Exit-WithGcloudError $result.Err }
        Write-Host "Started. The app starts by itself in 1 to 2 minutes." -ForegroundColor Green
        Write-Host "It costs money while it runs. Stop it with scripts\vm-stop.cmd when you are done."
    }
    else {
        Exit-WithError "The VM is busy ($State). Wait a minute, then try again."
    }
}
elseif ($Action -eq "stop") {
    if ($State -eq "TERMINATED") {
        Write-Host "It is already stopped." -ForegroundColor Green
    }
    elseif ($State -eq "RUNNING") {
        Write-Host "Stopping the VM. This can take up to 2 minutes..."
        $result = Invoke-Gcloud compute instances stop $Vm "--zone=$Zone" "--project=$Project"
        if (-not $result.Ok) { Exit-WithGcloudError $result.Err }
        Write-Host "Stopped. The website is off until you start the VM again." -ForegroundColor Green
        Write-Host "While stopped, you still pay a little for the disk and the IP address."
    }
    else {
        Exit-WithError "The VM is busy ($State). Wait a minute, then try again."
    }
}

if ($Ip -and $Action -ne "stop") {
    Write-Host "IP address: $Ip (it stays the same after a restart)"
    Write-Host ("With sslip.io, the website is: https://" + ($Ip -replace "\.", "-") + ".sslip.io")
}
