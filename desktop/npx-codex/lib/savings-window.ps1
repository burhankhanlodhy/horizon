param(
    [Parameter(Mandatory = $true)][string]$SnapshotPath,
    [int]$OwnerPid = 0,
    [string]$PreviewPath
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$dot = ' ' + [char]0x00B7 + ' '
$form = New-Object System.Windows.Forms.Form
$form.Text = 'ContextShrink' + $dot + 'Codex savings'
$form.ClientSize = New-Object System.Drawing.Size(450, 142)
$form.MinimumSize = $form.Size
$form.MaximumSize = $form.Size
$form.MaximizeBox = $false
$form.TopMost = $true
$form.StartPosition = 'Manual'
$area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form.Location = New-Object System.Drawing.Point(($area.Right - $form.Width - 20), ($area.Bottom - $form.Height - 20))
$form.BackColor = [System.Drawing.ColorTranslator]::FromHtml('#172026')
$form.ForeColor = [System.Drawing.ColorTranslator]::FromHtml('#E6EDF3')
$form.Font = New-Object System.Drawing.Font('Segoe UI', 10)
function New-TextLabel([int]$x, [int]$y, [int]$width, [int]$height, [string]$text) {
    $label = New-Object System.Windows.Forms.Label
    $label.Location = New-Object System.Drawing.Point($x, $y)
    $label.Size = New-Object System.Drawing.Size($width, $height)
    $label.Text = $text
    $form.Controls.Add($label)
    return $label
}
$heading = New-TextLabel 16 12 418 22 'ContextShrink - Codex'
$heading.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$stats = New-TextLabel 16 40 418 28 'Loading savings...'
$stats.Font = New-Object System.Drawing.Font('Segoe UI', 13, [System.Drawing.FontStyle]::Bold)
$stats.ForeColor = [System.Drawing.ColorTranslator]::FromHtml('#72DFBD')
$scope = New-TextLabel 16 73 418 20 ('This key today (UTC)' + $dot + 'Estimated token costs')
$scope.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$scope.ForeColor = [System.Drawing.ColorTranslator]::FromHtml('#BECBD5')
$updated = New-TextLabel 16 108 285 20 ''
$updated.Font = New-Object System.Drawing.Font('Segoe UI', 8)
$updated.ForeColor = $scope.ForeColor
$pin = New-Object System.Windows.Forms.CheckBox
$pin.Text = 'Keep on top'
$pin.Checked = $true
$pin.Location = New-Object System.Drawing.Point(327, 105)
$pin.Size = New-Object System.Drawing.Size(110, 25)
$pin.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$pin.Add_CheckedChanged({ $form.TopMost = $pin.Checked })
$form.Controls.Add($pin)
function Update-Savings {
    try {
        $data = [System.IO.File]::ReadAllText($SnapshotPath) | ConvertFrom-Json
        $stamp = [DateTimeOffset]::Parse([string]$data.updatedAt, $invariant)
        if ($data.version -ne 1 -or $data.available -ne $true -or $null -eq $data.pct -or $null -eq $data.requests -or ([DateTimeOffset]::UtcNow - $stamp).TotalSeconds -gt 90) { throw 'Unavailable' }
        $percent = [double]$data.pct
        $requests = [double]$data.requests
        if ($percent -lt 0 -or $percent -gt 100 -or $requests -lt 0) { throw 'Invalid' }
        # Usage only, no dollar amounts: the plan is a flat fee.
        # One decimal under 10 percent, whole numbers from 10 up (the same text as the status line).
        $percentText = if ($percent -lt 10) { $percent.ToString('0.0', $invariant) } else { [Math]::Round($percent).ToString('0', $invariant) }
        $stats.Text = 'Usage saved ' + $percentText + '% today'
        if ($data.plan -eq 'active') { $stats.Text += $dot + 'Subscribed' }
        elseif ($null -ne $data.trialDays) { $stats.Text += $dot + 'free week ' + ([int]$data.trialDays).ToString($invariant) + 'd left' }
        $scope.Text = $requests.ToString('0', $invariant) + ' requests' + $dot + 'This computer today (UTC)'
        $updated.Text = 'ContextShrink Code' + $dot + 'Updated ' + $stamp.ToLocalTime().ToString('HH:mm:ss')
        $stats.ForeColor = [System.Drawing.ColorTranslator]::FromHtml('#72DFBD')
    } catch {
        $stats.Text = 'Savings unavailable'
        $stats.ForeColor = [System.Drawing.ColorTranslator]::FromHtml('#E3C28C')
        $scope.Text = 'Check the connection or API key.'
        $updated.Text = 'Retrying automatically' + $dot + 'Refreshes every 30 seconds'
    }
}
Update-Savings
if ($PreviewPath) {
    $form.ShowInTaskbar = $false
    $form.Location = New-Object System.Drawing.Point(-32000, -32000)
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    $image = New-Object System.Drawing.Bitmap($form.Width, $form.Height)
    $form.DrawToBitmap($image, (New-Object System.Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
    $image.Save($PreviewPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $image.Dispose()
    $form.Close()
    $form.Dispose()
    exit 0
}
# Node hides the PowerShell console with STARTUPINFO's SW_HIDE. WinForms uses
# that same initial show state, so explicitly show the form after its first
# managed show. Keep this after the offscreen preview branch.
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ContextShrinkWindowVisibility {
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr window, int command);
}
'@
$form.Add_Shown({
    [void][ContextShrinkWindowVisibility]::ShowWindow($form.Handle, 5)
    $form.Activate()
})
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    if ($OwnerPid -gt 0 -and -not (Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue)) { $form.Close(); return }
    Update-Savings
})
$timer.Start()
try { [void]$form.ShowDialog() }
finally { $timer.Stop(); $timer.Dispose(); $form.Dispose() }
