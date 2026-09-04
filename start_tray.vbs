Option Explicit

Dim shell, projectDir, pythonwPath, trayScript, command
Set shell = CreateObject("WScript.Shell")

' Block duplicate launches, including controllers started by the pre-mutex version.
' WMI is used only for this narrow process-name/command-line check; if it is unavailable,
' tray_control.py still performs the stronger named-mutex check after launch.
Function TrayControllerAlreadyRunning()
    On Error Resume Next
    Dim service, processes, process
    Set service = GetObject("winmgmts:\\.\root\cimv2")
    Set processes = service.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name = 'pythonw.exe'")
    For Each process In processes
        If InStr(1, process.CommandLine, "tray_control.py", vbTextCompare) > 0 Then
            TrayControllerAlreadyRunning = True
            Exit Function
        End If
    Next
    TrayControllerAlreadyRunning = False
End Function

If TrayControllerAlreadyRunning() Then
    WScript.Quit 0
End If

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
