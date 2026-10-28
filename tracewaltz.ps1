param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot.Replace('\','/')
if ($projectPath -notmatch '^([A-Za-z]):/(.*)$') { throw '项目必须位于本地盘符目录' }
$linuxProject = '/mnt/' + $Matches[1].ToLowerInvariant() + '/' + $Matches[2]
[string[]]$converted = @(foreach ($argument in $Arguments) {
    $argumentPath = $argument
    if ($argument.Contains('\') -and (Test-Path -LiteralPath $argument)) {
        $argumentPath = (Resolve-Path -LiteralPath $argument).Path
    }
    if ($argumentPath -match '^([A-Za-z]):[\\/](.*)$') {
        '/mnt/' + $Matches[1].ToLowerInvariant() + '/' + $Matches[2].Replace('\','/')
    } else { $argument }
})
& wsl.exe -d Ubuntu-24.04 --cd $linuxProject -- bash tools/with-linux-tools.sh bash tools/linux-env.sh .runtime/linux-python/bin/python -m evotrace_bridge @converted
exit $LASTEXITCODE
