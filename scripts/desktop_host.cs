using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("ClassCue AI")]
[assembly: AssemblyDescription("ClassCue AI Desktop")]
[assembly: AssemblyProduct("ClassCue AI")]
[assembly: AssemblyCompany("ClassCue AI")]

internal static class DesktopHost
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr AddDllDirectory(string path);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true)]
    private static extern IntPtr GetProcAddress(IntPtr library, string name);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate void SetProgramName(IntPtr name);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int PythonMain(int count, IntPtr arguments);

    private static T Export<T>(IntPtr library, string name) where T : class
    {
        IntPtr address = GetProcAddress(library, name);
        if (address == IntPtr.Zero)
            throw new InvalidOperationException("Missing Python export: " + name);
        return Marshal.GetDelegateForFunctionPointer(address, typeof(T)) as T;
    }

    private static int StartBootstrap(string project)
    {
        Process.Start(new ProcessStartInfo(
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
            "-NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File \""
                + Path.Combine(project, "scripts", "desktop_bootstrap.ps1") + "\"")
        {
            WorkingDirectory = project,
            UseShellExecute = false,
            CreateNoWindow = true
        });
        return 0;
    }

    private static bool StartLegacyControl(string project, string[] arguments)
    {
        if (arguments.Length != 3 || arguments[0] != "--desktop" || arguments[1] != "--action"
            || !(arguments[2] == "activate" || arguments[2] == "exit" || arguments[2] == "restart"))
            return false;
        foreach (string environment in new string[] { ".venv", Path.Combine(".runtime", "venv") })
        {
            string pythonw = Path.Combine(project, environment, "Scripts", "pythonw.exe");
            if (!File.Exists(pythonw)) continue;
            Process.Start(new ProcessStartInfo(pythonw,
                "\"" + Path.Combine(project, "scripts", "desktop_app.py") + "\" --action " + arguments[2])
            {
                WorkingDirectory = project,
                UseShellExecute = false,
                CreateNoWindow = true
            });
            return true;
        }
        return false;
    }

    private static int RunPython(string directory, string project, string[] arguments)
    {
        string[] runtime = File.ReadAllLines(Path.Combine(directory, "python-runtime.txt"), Encoding.UTF8);
        if (runtime.Length != 2 || !File.Exists(runtime[0]) || !File.Exists(runtime[1])
            || !String.Equals(Path.GetFileName(runtime[0]), "pythonw.exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("The desktop Python environment is unavailable.");

        string home = Path.GetDirectoryName(runtime[1]);
        // Keep the venv interpreter as sys.executable so API/ASR subprocesses
        // use their existing Python entry points instead of recursively embedding.
        AddDllDirectory(home);
        string dlls = Path.Combine(home, "DLLs");
        if (Directory.Exists(dlls)) AddDllDirectory(dlls);
        IntPtr library = LoadLibraryEx(runtime[1], IntPtr.Zero, 0x1100);
        if (library == IntPtr.Zero)
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        SetProgramName setProgramName = Export<SetProgramName>(library, "Py_SetProgramName");
        PythonMain pythonMain = Export<PythonMain>(library, "Py_Main");
        string script = arguments.Length == 1 && arguments[0] == "--activate"
            ? "desktop_activate.py" : "desktop_app.py";
        string[] argv = new string[script == "desktop_activate.py" ? 2 : arguments.Length + 1];
        argv[0] = runtime[0];
        argv[1] = Path.Combine(project, "scripts", script);
        if (script == "desktop_app.py" && arguments.Length > 1)
            Array.Copy(arguments, 1, argv, 2, arguments.Length - 1);
        IntPtr[] strings = new IntPtr[argv.Length];
        IntPtr pointers = Marshal.AllocHGlobal(IntPtr.Size * argv.Length);
        try
        {
            for (int index = 0; index < argv.Length; index++)
            {
                strings[index] = Marshal.StringToHGlobalUni(argv[index]);
                Marshal.WriteIntPtr(pointers, index * IntPtr.Size, strings[index]);
            }
            setProgramName(strings[0]);
            return pythonMain(argv.Length, pointers);
        }
        finally
        {
            foreach (IntPtr value in strings)
                if (value != IntPtr.Zero) Marshal.FreeHGlobal(value);
            Marshal.FreeHGlobal(pointers);
        }
    }

    [STAThread]
    private static int Main(string[] arguments)
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string project = Path.GetFullPath(Path.Combine(directory, "..", ".."));
        try
        {
            Directory.SetCurrentDirectory(project);
            if (arguments.Length == 0)
            {
                if (File.Exists(Path.Combine(project, ".runtime", "desktop_instance.json")))
                    return RunPython(directory, project, new string[] { "--activate" });
                return StartBootstrap(project);
            }
            if (!(arguments.Length == 1 && (arguments[0] == "--desktop" || arguments[0] == "--activate"))
                && !(arguments.Length == 3 && arguments[0] == "--desktop" && arguments[1] == "--action"
                    && (arguments[2] == "activate" || arguments[2] == "exit" || arguments[2] == "restart")))
                return 2;
            return RunPython(directory, project, arguments);
        }
        catch (Exception error)
        {
            try
            {
                if (arguments.Length == 0 || (arguments.Length == 1 && arguments[0] == "--activate"))
                    return StartBootstrap(project);
                if (StartLegacyControl(project, arguments))
                    return 0;
            }
            catch (Exception recoveryError)
            {
                error = recoveryError;
            }
            try
            {
                File.WriteAllText(Path.Combine(directory, "host-error.log"), error.ToString(), Encoding.UTF8);
            }
            catch (IOException) { }
            MessageBox.Show("ClassCue AI could not start. Please run the desktop preparation again.\n\n"
                + error.Message, "ClassCue AI", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
