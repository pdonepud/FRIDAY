use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};

// ========== SIDECAR CONFIG ==========
// The FastAPI server (server/api.py) runs at 127.0.0.1:8765. On app launch we
// probe that port — if something is already listening (e.g. the user launched
// `python -m server.api` in a terminal), we skip our own spawn to avoid a
// duplicate. If the port is free, we spawn `python -m server.api --no-reload`
// as a child process and remember its handle so we can kill it on app exit.
const SERVER_PORT: u16 = 8765;
const SERVER_PROBE_TIMEOUT_MS: u64 = 250;

/// State managed by Tauri and cleaned up in the RunEvent handler.
///
/// `child` holds the FastAPI subprocess when we spawned it. `we_spawned_it` is
/// a separate flag rather than just `child.is_some()` so a failed spawn (which
/// leaves child = None) is distinguishable from "user already had the server
/// running" — the shutdown path skips kill only when we did not spawn.
struct PythonServer {
    child: Mutex<Option<Child>>,
    we_spawned_it: bool,
}

fn is_server_running() -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], SERVER_PORT));
    TcpStream::connect_timeout(&addr, Duration::from_millis(SERVER_PROBE_TIMEOUT_MS)).is_ok()
}

/// The project root — where `server/` lives. At compile time
/// `CARGO_MANIFEST_DIR` is `<repo>/ui/src-tauri`, so the project root is two
/// levels up. `canonicalize` collapses the `..` for a cleaner log line; if it
/// fails (e.g. the path was moved after build), we fall back to the joined
/// form which still resolves at process spawn time.
fn project_root() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let joined = manifest_dir.join("..").join("..");
    joined.canonicalize().unwrap_or(joined)
}

fn spawn_python_server() -> std::io::Result<Child> {
    let cwd = project_root();
    log::info!(
        "[server-autostart] spawning `python -m server.api --no-reload` from {}",
        cwd.display()
    );
    Command::new("python")
        .args(["-m", "server.api", "--no-reload"])
        .current_dir(&cwd)
        // Inherit stdio so uvicorn's logs land in the Tauri terminal — makes
        // server errors visible during dev without a second window.
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
}

fn shutdown_python_server(state: &PythonServer) {
    if !state.we_spawned_it {
        log::info!("[server-autostart] not our server, leaving it running");
        return;
    }
    let mut guard = match state.child.lock() {
        Ok(g) => g,
        Err(poisoned) => {
            log::warn!("[server-autostart] child mutex was poisoned — recovering");
            poisoned.into_inner()
        }
    };
    if let Some(mut child) = guard.take() {
        let pid = child.id();
        log::info!("[server-autostart] shutting down python server (pid {})", pid);
        if let Err(err) = child.kill() {
            log::warn!("[server-autostart] child.kill() failed: {}", err);
        }
        match child.wait() {
            Ok(status) => log::info!(
                "[server-autostart] python server (pid {}) exited with {:?}",
                pid,
                status
            ),
            Err(err) => log::warn!("[server-autostart] child.wait() failed: {}", err),
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // === Phase 14: auto-start FastAPI server ===
            let server_state = if is_server_running() {
                log::info!(
                    "[server-autostart] detected existing server on 127.0.0.1:{} — skipping spawn",
                    SERVER_PORT
                );
                PythonServer {
                    child: Mutex::new(None),
                    we_spawned_it: false,
                }
            } else {
                match spawn_python_server() {
                    Ok(child) => {
                        log::info!(
                            "[server-autostart] spawned python server (pid {})",
                            child.id()
                        );
                        PythonServer {
                            child: Mutex::new(Some(child)),
                            we_spawned_it: true,
                        }
                    }
                    Err(err) => {
                        // Don't abort app launch — the frontend health gate will
                        // show a "server not responding" message on the boot
                        // screen so the user has visible feedback.
                        log::error!(
                            "[server-autostart] failed to spawn python server: {}. \
                             Frontend will report the server as unreachable.",
                            err
                        );
                        PythonServer {
                            child: Mutex::new(None),
                            we_spawned_it: false,
                        }
                    }
                }
            };
            app.manage(server_state);

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        // Fires on ExitRequested (user closed last window / app.exit()) and on
        // Exit (app loop finished). `guard.take()` inside makes double-fire safe
        // — the second call finds None and returns cleanly.
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            let state = app_handle.state::<PythonServer>();
            shutdown_python_server(&state);
        }
    });
}
