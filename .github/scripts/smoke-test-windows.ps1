# Start the built app once: it opens its window, checks that the editor works
# and quits (run_editor.py --smoke-test). Exit code 0 = fine.
param([Parameter(Mandatory)] [string] $Exe)

# The app window needs the Edge WebView2 runtime, which a CI machine may lack.
$runtime = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$installed = @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$runtime",
               "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$runtime") | Where-Object { Test-Path $_ }
if (-not $installed) {
    Write-Host 'Installing the WebView2 runtime ...'
    $setup = Join-Path $env:RUNNER_TEMP 'MicrosoftEdgeWebview2Setup.exe'
    Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $setup
    Start-Process $setup -ArgumentList '/silent', '/install' -Wait
}

$app = Start-Process -FilePath $Exe -ArgumentList '--smoke-test' -PassThru
$null = $app.Handle  # keeps the exit code readable once the process has ended
if ($app.WaitForExit(240000)) {
    $code = $app.ExitCode
} else {
    Stop-Process -Id $app.Id -Force
    Write-Host 'The app did not finish within 4 minutes.'
    $code = 99
}

$log = Join-Path $env:APPDATA 'StreetviewTourEditor\editor.log'
if (Test-Path $log) { Get-Content $log -Tail 30 }
Write-Host "Smoke test exit code: $code"
exit $code
