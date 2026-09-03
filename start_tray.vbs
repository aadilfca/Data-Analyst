Option Explicit

Dim shell, projectDir, pythonwPath, trayScript, command
Set shell = CreateObject("WScript.Shell")

projectDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonwPath = projectDir & "\venv\Scripts\pythonw.exe"
trayScript = projectDir & "\tray_control.py"

If Not CreateObject("Scripting.FileSystemObject").FileExists(pythonwPath) Then
    MsgBox "Project Python was not found:" & vbCrLf & pythonwPath, vbCritical, "Audit App Tray"
    WScript.Quit 1
End If

If Not CreateObject("Scripting.FileSystemObject").FileExists(trayScript) Then
    MsgBox "Tray controller was not found:" & vbCrLf & trayScript, vbCritical, "Audit App Tray"
    WScript.Quit 1
End If

shell.CurrentDirectory = projectDir
command = """" & pythonwPath & """ """ & trayScript & """"
shell.Run command, 0, False

Set shell = Nothing
