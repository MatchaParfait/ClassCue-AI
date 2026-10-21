Option Explicit
Dim fs, shell, folder, project, action, pythonw, paths, candidate, command
Set fs = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
project = fs.GetParentFolderName(folder)
shell.CurrentDirectory = project
action = "activate"
If WScript.Arguments.Count > 0 Then action = WScript.Arguments(0)
If action <> "activate" And action <> "exit" And action <> "restart" Then WScript.Quit 1
pythonw = ""
paths = Array(project & "\.venv\Scripts\pythonw.exe", project & "\.runtime\venv\Scripts\pythonw.exe")
For Each candidate In paths
  If pythonw = "" And fs.FileExists(candidate) Then pythonw = candidate
Next
If action = "activate" Then
  If pythonw <> "" And fs.FileExists(project & "\.runtime\desktop_instance.json") Then
    command = """" & pythonw & """ """ & folder & "\desktop_activate.py"""
  Else
    command = "powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File """ & folder & "\desktop_bootstrap.ps1"""
  End If
Else
  If pythonw = "" Then WScript.Quit 0
  command = """" & pythonw & """ """ & folder & "\desktop_app.py"" --action " & action
End If
shell.Run command, 0, False
