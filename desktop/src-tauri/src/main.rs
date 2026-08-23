// DataOps Studio desktop shell (Tauri v2, Windows and Linux).
//
// The shell owns only the window, first-run password form, health polling and
// bounded shutdown. Runtime provisioning and service orchestration remain in
// install/start/stop scripts, which are the single contract authority.
#![cfg_attr(
    all(target_os = "windows", not(debug_assertions)),
    windows_subsystem = "windows"
)]

#[cfg(not(any(target_os = "windows", target_os = "linux")))]
compile_error!("the DataOps Studio desktop shell supports only Windows and Linux");

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};

const API_PORT: u16 = 8020;
const START_TIMEOUT: Duration = Duration::from_secs(900);
const STOP_TIMEOUT: Duration = Duration::from_secs(45);
const FORCE_TERM_TIMEOUT: Duration = Duration::from_secs(5);

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

struct Backend(Mutex<Option<Child>>);

fn executable_parent() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(|parent| parent.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn bundle_root(app: &AppHandle) -> PathBuf {
    std::env::var_os("DATAOPS_BUNDLE_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| default_bundle_root(app))
}

#[cfg(target_os = "windows")]
fn default_bundle_root(_app: &AppHandle) -> PathBuf {
    executable_parent()
}

#[cfg(target_os = "linux")]
fn default_bundle_root(app: &AppHandle) -> PathBuf {
    app.path()
        .resource_dir()
        .unwrap_or_else(|_| executable_parent())
}

fn state_root(app: &AppHandle) -> PathBuf {
    std::env::var_os("DATAOPS_STATE_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| default_state_root(app))
}

#[cfg(target_os = "windows")]
fn default_state_root(app: &AppHandle) -> PathBuf {
    bundle_root(app)
}

#[cfg(target_os = "linux")]
fn default_state_root(_app: &AppHandle) -> PathBuf {
    if let Some(path) = std::env::var_os("XDG_DATA_HOME") {
        return PathBuf::from(path).join("dataops-studio");
    }
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".local/share/dataops-studio")
}

fn dataops_home(app: &AppHandle) -> PathBuf {
    std::env::var_os("DATAOPS_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| state_root(app).join("home"))
}

fn script_command(app: &AppHandle, stem: &str) -> Command {
    platform_script_command(bundle_root(app), state_root(app), stem)
}

#[cfg(target_os = "windows")]
fn platform_script_command(bundle: PathBuf, _state: PathBuf, stem: &str) -> Command {
    let mut command = Command::new("powershell.exe");
    command
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-File")
        .arg(bundle.join(format!("{stem}.ps1")))
        .current_dir(&bundle);
    hide_command_window(&mut command);
    command
}

#[cfg(target_os = "linux")]
fn platform_script_command(bundle: PathBuf, state: PathBuf, stem: &str) -> Command {
    use std::os::unix::process::CommandExt;

    let mut command = Command::new("/bin/bash");
    command
        .arg(bundle.join(format!("{stem}.sh")))
        .current_dir(&bundle)
        .env("DATAOPS_STATE_ROOT", state)
        .process_group(0);
    command
}

#[cfg(target_os = "windows")]
fn hide_command_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;

    command.creation_flags(CREATE_NO_WINDOW);
}

fn app_url() -> String {
    format!("http://127.0.0.1:{API_PORT}/")
}

/// Minimal HTTP probe: a 200 response from /healthz means the backend is ready.
fn health_ok() -> bool {
    let addr = match format!("127.0.0.1:{API_PORT}").parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(800)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1500)));
    let request =
        format!("GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{API_PORT}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);
    response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")
}

#[cfg(target_os = "windows")]
fn venv_python(state: &std::path::Path) -> PathBuf {
    state.join(".venv/Scripts/python.exe")
}

#[cfg(target_os = "linux")]
fn venv_python(state: &std::path::Path) -> PathBuf {
    state.join(".venv/bin/python")
}

#[tauri::command]
fn check_state(app: AppHandle) -> String {
    if health_ok() {
        return "running".into();
    }
    let installed = venv_python(&state_root(&app)).exists();
    let initialized = dataops_home(&app)
        .join("config/.bundle-initialized")
        .exists();
    if installed && initialized {
        "installed".into()
    } else {
        "first-run".into()
    }
}

#[tauri::command]
fn start_backend(
    app: AppHandle,
    state: State<'_, Backend>,
    admin_password: Option<String>,
) -> Result<(), String> {
    if health_ok() {
        let _ = app.emit("backend-ready", app_url());
        return Ok(());
    }
    {
        let mut guard = state.0.lock().map_err(|error| error.to_string())?;
        if guard.is_some() {
            return Ok(());
        }
        let state = state_root(&app);
        std::fs::create_dir_all(&state)
            .map_err(|error| format!("cannot create state directory: {error}"))?;
        let mut command = script_command(&app, "start");
        if let Some(password) = admin_password.filter(|password| !password.is_empty()) {
            command.env("DATAOPS_ADMIN_PASSWORD", password);
        }
        let log = std::fs::File::create(state.join("gui-start.log"))
            .map_err(|error| format!("cannot create gui-start.log: {error}"))?;
        let log_error = log.try_clone().map_err(|error| error.to_string())?;
        command.stdout(log).stderr(log_error);
        let child = command
            .spawn()
            .map_err(|error| format!("cannot start backend: {error}"))?;
        *guard = Some(child);
    }

    std::thread::spawn(move || {
        let started = Instant::now();
        let deadline = started + START_TIMEOUT;
        loop {
            if health_ok() {
                let _ = app.emit("backend-ready", app_url());
                return;
            }
            if Instant::now() > deadline {
                let _ = app.emit(
                    "backend-failed",
                    "启动超时。请查看用户数据目录中的 gui-start.log。".to_string(),
                );
                return;
            }
            let _ = app.emit("backend-progress", started.elapsed().as_secs());
            std::thread::sleep(Duration::from_millis(1200));
        }
    });
    Ok(())
}

fn wait_for_child(child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) if Instant::now() >= deadline => return false,
            Ok(None) => std::thread::sleep(Duration::from_millis(250)),
            Err(_) => return true,
        }
    }
}

#[cfg(target_os = "windows")]
fn terminate_process_tree(mut child: Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }
    let mut kill = Command::new("taskkill");
    kill.args(["/PID", &child.id().to_string(), "/T", "/F"]);
    hide_command_window(&mut kill);
    let tree_killed = kill.status().is_ok_and(|status| status.success());
    if !tree_killed {
        let _ = child.kill();
    }
    let _ = wait_for_child(&mut child, FORCE_TERM_TIMEOUT);
}

#[cfg(target_os = "linux")]
fn signal_process_group(child: &Child, signal: libc::c_int) {
    let process_group = -(child.id() as libc::pid_t);
    // SAFETY: the child was spawned with process_group(0), so its PID is the
    // dedicated group ID. A negative PID deliberately targets that group only.
    unsafe {
        libc::kill(process_group, signal);
    }
}

#[cfg(target_os = "linux")]
fn terminate_process_tree(mut child: Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }
    signal_process_group(&child, libc::SIGTERM);
    if !wait_for_child(&mut child, FORCE_TERM_TIMEOUT) {
        signal_process_group(&child, libc::SIGKILL);
        let _ = child.wait();
    }
}

/// Ask the launcher to stop cleanly, wait at most 45 seconds, then kill only
/// the dedicated script process tree/group before exiting the desktop shell.
fn shutdown_and_exit(app: AppHandle) {
    std::thread::spawn(move || {
        if let Ok(mut stop) = script_command(&app, "stop").spawn() {
            if !wait_for_child(&mut stop, STOP_TIMEOUT) {
                terminate_process_tree(stop);
            }
        }
        if let Some(state) = app.try_state::<Backend>() {
            if let Ok(mut guard) = state.0.lock() {
                if let Some(child) = guard.take() {
                    terminate_process_tree(child);
                }
            }
        }
        app.exit(0);
    });
}

fn main() {
    tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![check_state, start_backend])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                shutdown_and_exit(window.app_handle().clone());
            }
        })
        .run(tauri::generate_context!())
        .expect("DataOps Studio desktop shell failed");
}
