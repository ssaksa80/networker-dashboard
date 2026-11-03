# Unit tests for the installer's firewall rule. It opens the port on Domain and
# Private networks only, unless -FirewallProfile says otherwise, and warns about
# each connected network the rule leaves closed (review S4's firewall fix). The
# firewall cmdlets are injected, so nothing here touches this machine's rules.
# Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\firewall.test.ps1
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }
function Net([string]$Name, [string]$Category, [string]$Alias = 'Ethernet0') {
  [pscustomobject]@{ Name = $Name; InterfaceAlias = $Alias; NetworkCategory = $Category }
}

# ---- Resolve-NwdashFirewallProfile: what -FirewallProfile accepts ----
Assert ($script:NwdashFirewallDefault -eq 'Domain,Private') 'the default is Domain and Private networks'
Assert ((Resolve-NwdashFirewallProfile 'Domain,Private') -eq 'Domain,Private') 'Domain,Private as typed'
Assert ((Resolve-NwdashFirewallProfile 'Domain Private') -eq 'Domain,Private') 'Domain Private, as Setup-NWDash.cmd hands Domain,Private on'
Assert ((Resolve-NwdashFirewallProfile ' private, DOMAIN ') -eq 'Domain,Private') 'any case, any order, spaces after commas'
Assert ((Resolve-NwdashFirewallProfile 'Public') -eq 'Public') 'a single profile'
Assert ((Resolve-NwdashFirewallProfile 'Domain,Private,Public') -eq 'Domain,Private,Public') 'all three'
Assert ((Resolve-NwdashFirewallProfile 'any') -eq 'Any') 'Any opens every network'
foreach ($bad in @('Home', 'Domain,Home', 'Any,Public', ',', '')) {
  Assert ((Resolve-NwdashFirewallProfile $bad) -eq '') "'$bad' is refused, not guessed"
}

# ---- Get-NwdashFirewallGap: which connected networks stay closed ----
$nets = @((Net 'domain.example' 'DomainAuthenticated'), (Net 'lab' 'Private' 'Ethernet1'), (Net 'Network 3' 'Public' 'Ethernet2'))
$gaps = @(Get-NwdashFirewallGap -Networks $nets)
Assert ($gaps.Count -eq 1 -and $gaps[0].Name -eq 'Network 3' -and $gaps[0].Category -eq 'Public' -and $gaps[0].InterfaceAlias -eq 'Ethernet2') 'the default leaves only the Public network closed'
Assert (@(Get-NwdashFirewallGap -NetworkProfile 'Any' -Networks $nets).Count -eq 0) 'Any leaves nothing closed'
Assert (@(Get-NwdashFirewallGap -NetworkProfile 'Domain,Private,Public' -Networks $nets).Count -eq 0) 'all three leave nothing closed'
$gaps = @(Get-NwdashFirewallGap -NetworkProfile 'Public' -Networks $nets)
Assert ((@($gaps | ForEach-Object { $_.Category }) -join ',') -eq 'Domain,Private') 'DomainAuthenticated is the Domain profile'
Assert (@(Get-NwdashFirewallGap -Networks @()).Count -eq 0) 'no connected network: nothing to say'
$gap = [pscustomobject]@{ Name = 'Network 3'; InterfaceAlias = 'Ethernet2'; Category = 'Public' }
Assert ((Format-NwdashFirewallGap -Gap $gap -Port 8443) -match "network 'Network 3' on Ethernet2 is classified Public.*port 8443.*-FirewallProfile Any") 'the warning names the network, the port and the way out'

# ---- Set-NwdashFirewall, with the cmdlets injected ----
function Invoke-Firewall([hashtable]$Options) {
  $script:rules = @(); $script:removed = 0
  $remove = { $script:removed++ }
  $new = { param($p, $prof) $script:rules += "$p $prof" }
  $out = & { Set-NwdashFirewall -RemoveRules $remove -NewRule $new @Options } 3>&1 6>&1 | Out-String
  return $out
}
$out = Invoke-Firewall @{ Port = 8443; GetNetworks = { $nets } }
Assert ($script:removed -eq 1 -and ($script:rules -join '|') -eq '8443 Domain,Private') 'no -NetworkProfile: the group is replaced with one Domain,Private rule'
Assert ($out -match 'opened inbound TCP 8443 on Domain,Private networks') 'the log says where it is open'
Assert ($out -match "network 'Network 3' on Ethernet2 is classified Public") 'and warns about the Public network it is not open on'
Assert (-not ($out -match "'lab'|domain\.example")) 'but not about the networks it covers'

$out = Invoke-Firewall @{ Port = 9443; NetworkProfile = 'Any'; GetNetworks = { $nets } }
Assert (($script:rules -join '|') -eq '9443 Any') '-FirewallProfile Any reaches the rule'
Assert (-not ($out -match 'classified')) 'and no network is left closed'

$out = Invoke-Firewall @{ Port = 8443; GetNetworks = { throw 'no network service' } }
Assert (($script:rules -join '|') -eq '8443 Domain,Private' -and -not ($out -match 'classified')) 'networks that cannot be listed: the rule still goes in, no warning'

$script:rules = @()
$out = & { Set-NwdashFirewall -Port 8443 -RemoveRules { } -NewRule { throw 'Access is denied' } -GetNetworks { $nets } } 3>&1 6>&1 | Out-String
Assert ($out -match 'could not configure the firewall \(Access is denied\)\. Open inbound TCP 8443 manually') 'a rule that cannot be made is a warning, as before'
Assert (-not ($out -match 'classified')) 'and no network warning follows it'

# ---- the installer passes the profile through ----
$install = Get-Content -Raw "$PSScriptRoot\..\install.ps1"
Assert ($install.Contains('Set-NwdashFirewall -Port $Port -NetworkProfile $fwProfile')) 'install.ps1 hands the resolved profile to the rule'
Assert ($install.Contains('-Profile Any') -eq $false) 'install.ps1 never opens every network by itself'
$common = Get-Content -Raw "$PSScriptRoot\..\lib\common.ps1"
Assert (-not $common.Contains('-Profile Any')) 'nor does common.ps1'

Write-Host 'firewall: all tests passed'
