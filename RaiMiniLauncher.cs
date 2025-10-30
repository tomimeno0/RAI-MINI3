using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Windows.Forms;
// Lanzador tipo bandeja del sistema para RAI-MINI.
// - Muestra un icono en el área de notificación y permite abrir la terminal o salir.
// - Inicia client.py (o un .bat de configuración) ya sea con consola visible o en modo silencioso.

namespace RaiMiniLauncher
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new LauncherContext());
        }
    }

    internal sealed class LauncherContext : ApplicationContext
    {
        private readonly string baseDir;
        private readonly string appsJsonPath;
        private readonly string clientPath;
        private readonly string batchPath;
        private readonly string iconPath;
        private readonly NotifyIcon notifyIcon;
        private readonly Icon trayIcon;
        private readonly bool ownsTrayIcon;
        private readonly ContextMenuStrip contextMenu;
        private readonly SynchronizationContext syncContext;
        private Process workerProcess;
        private bool isInteractive;
        private bool shuttingDown;
        private LaunchMode mode;

        private enum LaunchMode
        {
            Client,
            Setup
        }

        internal LauncherContext()
        {
            syncContext = SynchronizationContext.Current ?? new SynchronizationContext();
            baseDir = AppDomain.CurrentDomain.BaseDirectory;
            appsJsonPath = Path.Combine(baseDir, "apps.json");
            clientPath = Path.Combine(baseDir, "client.py");
            batchPath = Path.Combine(baseDir, "run_rai_mini.bat");
            mode = File.Exists(appsJsonPath) ? LaunchMode.Client : LaunchMode.Setup;

            iconPath = Path.Combine(baseDir, "RAI_option_A.ico");
            trayIcon = LoadTrayIcon(iconPath, out ownsTrayIcon);

            notifyIcon = new NotifyIcon
            {
                Icon = trayIcon,
                Text = "RAI esta escuchando",
                Visible = true
            };

            contextMenu = new ContextMenuStrip();
            contextMenu.Items.Add("Abrir terminal", null, OnOpenTerminal);
            contextMenu.Items.Add("Salir", null, OnExit);
            notifyIcon.ContextMenuStrip = contextMenu;

            if (mode == LaunchMode.Setup)
            {
                ShowBalloon("No se encontro apps.json. Ejecutando run_rai_mini.bat con consola visible.", ToolTipIcon.Info);
                // En modo de configuracion (sin apps.json), mostrar la consola del .bat
                StartProcess(showConsole: true);
            }
            else
            {
                ShowBalloon("RAI esta escuchando en segundo plano.", ToolTipIcon.Info);
                // En modo cliente por defecto corre en segundo plano (sin consola)
                StartProcess(showConsole: false);
            }
        }

        private void OnOpenTerminal(object sender, EventArgs e)
        {
            if (shuttingDown)
            {
                return;
            }

            if (isInteractive && workerProcess != null && !workerProcess.HasExited)
            {
                return;
            }

            StartProcess(showConsole: true); // Permite al usuario ver la consola interactiva.
        }

        private void OnExit(object sender, EventArgs e)
        {
            shuttingDown = true;
            notifyIcon.Visible = false;
            StopWorker();
            notifyIcon.Icon = null;
            contextMenu.Dispose();
            if (ownsTrayIcon)
            {
                trayIcon.Dispose();
            }
            notifyIcon.Dispose();
            Application.Exit();
        }

        private void StartProcess(bool showConsole)
        {
            if (shuttingDown)
            {
                return;
            }

            StopWorker();

            var startInfo = BuildStartInfo(showConsole);
            if (startInfo == null)
            {
                return;
            }

            try
            {
                var process = Process.Start(startInfo);
                if (process == null)
                {
                    ShowBalloon("No se pudo iniciar el proceso solicitado.", ToolTipIcon.Error);
                    return;
                }

                workerProcess = process;
                isInteractive = showConsole;
                workerProcess.EnableRaisingEvents = true;
                workerProcess.Exited += OnWorkerExited;

                if (showConsole)
                {
                    ShowBalloon("Se abrio la terminal de RAI. Cierrala para volver al modo silencioso.", ToolTipIcon.Info);
                }
            }
            catch (Exception ex)
            {
                ShowBalloon("Error al iniciar el proceso: " + ex.Message, ToolTipIcon.Error);
            }
        }

        private static Icon LoadTrayIcon(string path, out bool ownsIcon)
        {
            if (File.Exists(path))
            {
                try
                {
                    ownsIcon = true;
                    return new Icon(path);
                }
                catch
                {
                }
            }

            ownsIcon = false;
            return SystemIcons.Application;
        }

        private ProcessStartInfo BuildStartInfo(bool showConsole)
        {
            if (mode == LaunchMode.Client)
            {
                if (!File.Exists(clientPath))
                {
                    ShowBalloon("No se encontro client.py en la carpeta del proyecto.", ToolTipIcon.Error);
                    return null;
                }

                var interpreter = ResolvePython(preferConsole: showConsole);
                if (string.IsNullOrEmpty(interpreter))
                {
                    ShowBalloon("No se encontro un interprete de Python disponible.", ToolTipIcon.Error);
                    return null;
                }

                if (showConsole)
                {
                    var commandArgs = "-u " + Quote(clientPath); // -u para salida sin buffer en consola
                    var command = Quote(interpreter) + " " + commandArgs;
                    return new ProcessStartInfo
                    {
                        FileName = "cmd.exe",
                        Arguments = "/k " + Quote(command),
                        WorkingDirectory = baseDir,
                        UseShellExecute = false,
                        CreateNoWindow = false,
                        WindowStyle = ProcessWindowStyle.Normal
                    };
                }

                return new ProcessStartInfo
                {
                    FileName = interpreter,
                    Arguments = Quote(clientPath),
                    WorkingDirectory = baseDir,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                };
            }

            if (!File.Exists(batchPath))
            {
                ShowBalloon("No se encontro run_rai_mini.bat en la carpeta del proyecto.", ToolTipIcon.Error);
                return null;
            }

            var commandLine = Quote(batchPath);
            var arguments = (showConsole ? "/k " : "/c ") + Quote(commandLine); // /k deja la consola abierta, /c la cierra.

            return new ProcessStartInfo
            {
                FileName = "cmd.exe",
                Arguments = arguments,
                WorkingDirectory = baseDir,
                UseShellExecute = false,
                CreateNoWindow = !showConsole,
                WindowStyle = showConsole ? ProcessWindowStyle.Normal : ProcessWindowStyle.Hidden
            };
        }

        private string ResolvePython(bool preferConsole)
        {
            string[] candidates = preferConsole
                ? new[]
                {
                    Path.Combine(baseDir, ".venv", "Scripts", "python.exe"),
                    "python.exe",
                    "py",
                    "python"
                }
                : new[]
                {
                    Path.Combine(baseDir, ".venv", "Scripts", "pythonw.exe"),
                    Path.Combine(baseDir, ".venv", "Scripts", "python.exe"),
                    "pythonw.exe",
                    "python.exe",
                    "pyw",
                    "py",
                    "pythonw",
                    "python"
                };

            foreach (var candidate in candidates)
            {
                if (HasDirectorySeparator(candidate))
                {
                    if (File.Exists(candidate))
                    {
                        return candidate;
                    }
                }
                else
                {
                    var fromPath = FindOnPath(candidate);
                    if (!string.IsNullOrEmpty(fromPath))
                    {
                        return fromPath;
                    }
                }
            }

            return null;
        }

        private static bool HasDirectorySeparator(string value)
        {
            return value.IndexOf(Path.DirectorySeparatorChar) >= 0
                || value.IndexOf(Path.AltDirectorySeparatorChar) >= 0;
        }

        private static string FindOnPath(string command)
        {
            var pathEnv = Environment.GetEnvironmentVariable("PATH");
            if (string.IsNullOrEmpty(pathEnv))
            {
                return null;
            }

            var extension = Path.GetExtension(command);
            var hasExtension = !string.IsNullOrEmpty(extension);
            var suffixes = hasExtension ? new[] { string.Empty } : new[] { ".exe", ".bat", ".cmd", ".com" };

            foreach (var dir in pathEnv.Split(Path.PathSeparator))
            {
                if (string.IsNullOrWhiteSpace(dir))
                {
                    continue;
                }

                var trimmed = dir.Trim().Trim('"');
                foreach (var suffix in suffixes)
                {
                    var candidate = Path.Combine(trimmed, hasExtension ? command : command + suffix);
                    if (File.Exists(candidate))
                    {
                        return candidate;
                    }
                }
            }

            return null;
        }

        private void StopWorker()
        {
            var process = workerProcess;
            if (process == null)
            {
                return;
            }

            workerProcess = null;
            isInteractive = false;

            try
            {
                process.Exited -= OnWorkerExited;
            }
            catch
            {
            }

            try
            {
                if (!process.HasExited)
                {
                    process.Kill();
                    process.WaitForExit();
                }
            }
            catch
            {
            }
            finally
            {
                process.Dispose();
            }
        }

        private void OnWorkerExited(object sender, EventArgs e)
        {
            var process = sender as Process;
            int exitCode = 0;

            if (process != null)
            {
                try
                {
                    exitCode = process.ExitCode;
                }
                catch
                {
                }

                try
                {
                    process.Exited -= OnWorkerExited;
                }
                catch
                {
                }

                process.Dispose();
            }

            workerProcess = null;
            var wasInteractive = isInteractive;
            isInteractive = false;

            syncContext.Post(_ => HandleWorkerExit(exitCode, wasInteractive), null);
        }

        private void HandleWorkerExit(int exitCode, bool wasInteractive)
        {
            if (shuttingDown)
            {
                return;
            }

            if (mode == LaunchMode.Setup && File.Exists(appsJsonPath))
            {
                mode = LaunchMode.Client;
                ShowBalloon("Se genero apps.json. Iniciando client.py en modo silencioso.", ToolTipIcon.Info);
                StartProcess(showConsole: false);
                return;
            }

            if (wasInteractive)
            {
                StartProcess(showConsole: false);
                return;
            }

            if (exitCode != 0)
            {
                ShowBalloon("RAI se detuvo con codigo " + exitCode + ".", ToolTipIcon.Warning);
            }
        }

        private void ShowBalloon(string message, ToolTipIcon icon)
        {
            if (shuttingDown)
            {
                return;
            }

            notifyIcon.BalloonTipIcon = icon;
            notifyIcon.BalloonTipTitle = "RAI-MINI";
            notifyIcon.BalloonTipText = message;
            notifyIcon.ShowBalloonTip(3000);
        }

        private static string Quote(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return "\"\"";
            }

            if (value.IndexOf(' ') >= 0 || value.IndexOf('\t') >= 0)
            {
                return "\"" + value + "\"";
            }

            return value;
        }
    }
}
