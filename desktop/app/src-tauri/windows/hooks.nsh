; Tauri NSIS installer hooks (bundle.windows.nsis.installerHooks).

!macro NSIS_HOOK_PREUNINSTALL
  ; A real uninstall signs this device out the way the app's Sign out link
  ; does: it revokes the device's proxy key, forgets the saved session in
  ; Windows Credential Manager and restores connected editors. Without it a
  ; reinstall signed straight back in as the previous account. Upgrades
  ; ($UpdateMode = 1) keep the user signed in.
  ${If} $UpdateMode <> 1
    ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --uninstall-cleanup'
  ${EndIf}
!macroend
