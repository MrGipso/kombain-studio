' Комбайн Studio — тихий запуск десктопной версии.
'
' Запускает start_desktop.bat со скрытым окном, поэтому на экране остаётся
' только окно самого приложения, без чёрной консоли. Об ошибках запуска
' launch.py сообщает системным окном (переменная PIPELINE_DESKTOP_SILENT).
'
' Для отладки с видимой консолью запускайте start_desktop.bat напрямую.

Dim shell, fso, here
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

shell.Environment("PROCESS").Item("PIPELINE_DESKTOP_SILENT") = "1"

' 0 = скрытое окно, False = не ждать завершения.
shell.Run """" & here & "\start_desktop.bat""", 0, False
