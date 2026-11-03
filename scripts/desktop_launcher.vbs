Option Explicit
Dim fs, shell, folder, project, action, pythonw, paths, candidate, command, host, windowStyle
Set fs = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
project = fs.GetParentFolderName(folder)
shell.CurrentDirectory = project
action = "activate"
If WScript.Arguments.Count > 0 Then action = WScript.Arguments(0)
If action <> "activate" And action <> "exit" And action <> "restart" Then WScript.Quit 1
pythonw = ""
windowStyle = 0
host = project & "\.runtime\desktop-host\ClassCue-AI.exe"
paths = Array(project & "\.venv\Scripts\pythonw.exe", project & "\.runtime\venv\Scripts\pythonw.exe")
For Each candidate In paths
  If pythonw = "" And fs.FileExists(candidate) Then pythonw = candidate
Next
If action = "activate" Then
  If fs.FileExists(host) And fs.FileExists(project & "\.runtime\desktop_instance.json") Then
    command = """" & host & """ --activate"
    windowStyle = 1
  ElseIf pythonw <> "" And fs.FileExists(project & "\.runtime\desktop_instance.json") Then
    command = """" & pythonw & """ """ & folder & "\desktop_activate.py"""
  Else
    command = "powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File """ & folder & "\desktop_bootstrap.ps1"""
  End If
Else
  If fs.FileExists(host) Then
    command = """" & host & """ --desktop --action " & action
    windowStyle = 1
  Else
    If pythonw = "" Then WScript.Quit 0
    command = """" & pythonw & """ """ & folder & "\desktop_app.py"" --action " & action
  End If
End If
shell.Run command, windowStyle, False
