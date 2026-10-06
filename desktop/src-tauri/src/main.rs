// OrgForge desktop: starts the bundled OrgForge backend and shows its dashboard.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;

use tauri::{Manager, RunEvent, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct Backend(Mutex<Option<CommandChild>>);

fn show_status(app: &tauri::AppHandle, message: &str) {
    if let Some(window) = app.get_webview_window("main") {
        let text = serde_json::to_string(message).unwrap_or_default();
        let script = format!(
            "(() => {{ const s = document.getElementById('status'); if (s) {{ s.textContent = {text}; \
             s.className = 'error'; }} const b = document.getElementById('bar'); if (b) b.remove(); }})()"
        );
        let _ = window.eval(&script);
    }
}

/// The backend prints one JSON line once it is listening; only a 127.0.0.1 address is accepted.
fn dashboard_url(line: &str) -> Option<Url> {
    let value: serde_json::Value = serde_json::from_str(line.trim()).ok()?;
    let url = Url::parse(value.get("url")?.as_str()?).ok()?;
    (url.scheme() == "http" && url.host_str() == Some("127.0.0.1")).then_some(url)
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let (mut events, child) = app
                .shell()
                .sidecar("orgforge-server")?
                .env("ORGFORGE_EXIT_WITH_PARENT", "1")
                .spawn()?;
            app.manage(Backend(Mutex::new(Some(child))));
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut ready = false;
                let mut errors = String::new();
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(bytes) if !ready => {
                            let text = String::from_utf8_lossy(&bytes).to_string();
                            if let Some(url) = text.lines().find_map(dashboard_url) {
                                if let Some(window) = handle.get_webview_window("main") {
                                    ready = window.navigate(url).is_ok();
                                }
                            }
                        }
                        CommandEvent::Stderr(bytes) => {
                            errors.push_str(&String::from_utf8_lossy(&bytes));
                            if errors.len() > 4000 {
                                let cut = errors.len() - 4000;
                                let start = errors.char_indices().map(|(i, _)| i).find(|&i| i >= cut).unwrap_or(0);
                                errors = errors[start..].to_string();
                            }
                        }
                        CommandEvent::Terminated(_) if !ready => {
                            let tail: String = errors.lines().rev().take(6).collect::<Vec<_>>().into_iter().rev()
                                .collect::<Vec<_>>().join("\n");
                            show_status(&handle, &format!("OrgForge could not start.\n\n{tail}"));
                        }
                        CommandEvent::Error(message) if !ready => {
                            show_status(&handle, &format!("OrgForge could not start: {message}"));
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building OrgForge")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(child) = app.state::<Backend>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
