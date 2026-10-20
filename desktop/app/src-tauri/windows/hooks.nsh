; Tauri NSIS installer hooks (bundle.windows.nsis.installerHooks).

!macro NSIS_HOOK_PREUNINSTALL
  ; A real uninstall signs this device out the way the app's Sign out link
  ; does: it revokes the device's proxy key, forgets the saved session in
  ; Windows Credential Manager and restores connected editors. Without it a
  ; reinstall signed straight back in as the previous account. Upgrades
  ; ($UpdateMode = 1) keep the user signed in.
  ${If} $UpdateMode <> 1
    ; Stop starting the tray app at sign-in (claude_code::set_autostart): the
    ; Startup shortcut, and the Run value that older installs wrote.
    Delete "$APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\ContextShrink.lnk"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "ContextShrink"
    ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --uninstall-cleanup'
    ; The helpers the Connect step copied to the shared folder. Only these
    ; files and folders; the bin folder is removed only when it is empty.
    Delete "$LOCALAPPDATA\ContextShrink\bin\contextshrink-status.exe"
    Delete "$LOCALAPPDATA\ContextShrink\bin\claude-code-launcher.exe"
    RMDir "$LOCALAPPDATA\ContextShrink\bin"
    RMDir /r "$LOCALAPPDATA\ContextShrink\claude-code\plugin-marketplace"
  ${EndIf}
!macroend
