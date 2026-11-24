//! Covers the fork's command-backed `custom` status-line item (`[tui] status_line_command`).

use super::*;
use codex_config::types::BuiltinStatusLine;
use codex_config::types::StatusLineProviderId;
use codex_config::types::StatusLineSelection;
use pretty_assertions::assert_eq;
use std::time::Duration;

fn command_requests(
    rx: &mut tokio::sync::mpsc::UnboundedReceiver<AppEvent>,
) -> Vec<(u64, Vec<String>, String)> {
    std::iter::from_fn(|| rx.try_recv().ok())
        .filter_map(|event| match event {
            AppEvent::RefreshStatusLineCommand {
                request_id,
                command,
                stdin_json,
            } => Some((request_id, command, stdin_json)),
            _ => None,
        })
        .collect()
}

fn configure_custom(chat: &mut ChatWidget, command: Option<&[&str]>) {
    chat.local_settings.tui.status_line = Some(vec!["custom".to_string()]);
    chat.local_settings.tui.status_line_command =
        command.map(|argv| argv.iter().map(ToString::to_string).collect());
}

fn select_mcp_provider(chat: &mut ChatWidget) {
    chat.thread_id = Some(ThreadId::new());
    chat.local_settings.tui.status_line_provider =
        Some(Box::new(StatusLineSelection::Mcp(StatusLineProviderId {
            server: "fixture".into(),
            tool: "line".into(),
        })));
}

#[tokio::test]
async fn custom_item_without_a_command_is_silent() {
    let (mut chat, mut rx, _ops) = make_chatwidget_manual(/*model_override*/ None).await;
    configure_custom(&mut chat, /*command*/ None);

    chat.refresh_status_line();
    chat.refresh_status_line_if_command_due();

    assert_eq!(status_line_text(&chat), None);
    let events: Vec<_> = std::iter::from_fn(|| rx.try_recv().ok()).collect();
    assert!(
        !events
            .iter()
            .any(|event| matches!(event, AppEvent::RefreshStatusLineCommand { .. })),
        "no command configured, nothing to run"
    );
    assert!(
        !events
            .iter()
            .any(|event| matches!(event, AppEvent::InsertHistoryCell(_))),
        "custom is a known item and must not warn"
    );
}

#[tokio::test]
async fn command_is_not_run_unless_custom_is_selected() {
    let (mut chat, mut rx, _ops) = make_chatwidget_manual(/*model_override*/ None).await;
    chat.local_settings.tui.status_line = Some(vec!["run-state".to_string()]);
    chat.local_settings.tui.status_line_command = Some(vec!["true".to_string()]);

    chat.refresh_status_line();
    chat.refresh_status_line_if_command_due();

    assert!(command_requests(&mut rx).is_empty());
}

#[tokio::test]
async fn selected_custom_item_requests_the_command_with_a_session_snapshot() {
    let (mut chat, mut rx, _ops) = make_chatwidget_manual(/*model_override*/ None).await;
    configure_custom(&mut chat, Some(&["printf", "hi"]));

    chat.refresh_status_line();

    let [(request_id, command, stdin_json)] = command_requests(&mut rx).try_into().unwrap();
    assert_eq!(command, vec!["printf".to_string(), "hi".to_string()]);
    assert_eq!(
        chat.status_line_command_pending_request_id,
        Some(request_id)
    );
    let snapshot: serde_json::Value = serde_json::from_str(&stdin_json).unwrap();
    assert_eq!(
        snapshot,
        serde_json::json!({
            "cwd": chat.status_line_workspace_cwd().display().to_string(),
            "git_branch": null,
            "model": chat.status_model_display_name(),
        })
    );

    // One run at a time: a pending request blocks the next one.
    chat.refresh_status_line_if_command_due();
    assert!(command_requests(&mut rx).is_empty());
}

#[tokio::test]
async fn command_result_updates_the_footer_and_failures_keep_the_last_value() {
    let (mut chat, mut rx, _ops) = make_chatwidget_manual(/*model_override*/ None).await;
    configure_custom(&mut chat, Some(&["true"]));
    chat.refresh_status_line();
    let [(first, _, _)] = command_requests(&mut rx).try_into().unwrap();

    assert!(chat.set_status_line_command_output(first, Ok(Some("Clade main".to_string()))));
    assert_eq!(status_line_text(&chat), Some("Clade main".to_string()));
    assert!(
        !chat.set_status_line_command_output(first, Ok(Some("stale".to_string()))),
        "a result for a request that is no longer pending is ignored"
    );

    chat.status_line_command_last_requested_at = None;
    chat.refresh_status_line_if_command_due();
    let [(second, _, _)] = command_requests(&mut rx).try_into().unwrap();
    assert!(chat.set_status_line_command_output(second, Err("timed out".to_string())));
    assert_eq!(status_line_text(&chat), Some("Clade main".to_string()));
}

#[tokio::test]
async fn mcp_provider_takes_precedence_over_the_command() {
    let (mut chat, mut rx, _ops) = make_chatwidget_manual(/*model_override*/ None).await;
    configure_custom(&mut chat, Some(&["true"]));
    select_mcp_provider(&mut chat);

    // Feature off: the provider is ignored and the built-in footer, `custom` included, runs.
    chat.refresh_status_line();
    assert_eq!(command_requests(&mut rx).len(), 1);

    // Feature on: the provider owns the footer, so the command is not run.
    chat.status_line_command_pending_request_id = None;
    chat.status_line_command_last_requested_at = None;
    chat.statusline_feature_enabled = true;
    chat.refresh_status_line();
    chat.refresh_status_line_if_command_due();
    assert!(command_requests(&mut rx).is_empty());

    // An explicit built-in selection hands the footer, and the command, back.
    chat.local_settings.tui.status_line_provider = Some(Box::new(StatusLineSelection::Builtin(
        BuiltinStatusLine::Default,
    )));
    chat.refresh_status_line();
    assert_eq!(command_requests(&mut rx).len(), 1);
}

#[tokio::test]
async fn lost_command_request_is_retried() {
    let (mut chat, mut rx, _ops) = make_chatwidget_manual(/*model_override*/ None).await;
    configure_custom(&mut chat, Some(&["true"]));
    chat.refresh_status_line();
    let [(lost, _, _)] = command_requests(&mut rx).try_into().unwrap();

    chat.refresh_status_line_if_command_due();
    assert!(
        command_requests(&mut rx).is_empty(),
        "still within the wait"
    );

    chat.status_line_command_last_requested_at =
        Instant::now().checked_sub(Duration::from_secs(/*secs*/ 31));
    chat.refresh_status_line_if_command_due();
    let [(retry, _, _)] = command_requests(&mut rx).try_into().unwrap();
    assert_ne!(retry, lost);
    assert!(!chat.set_status_line_command_output(lost, Ok(Some("late".to_string()))));
    assert!(chat.set_status_line_command_output(retry, Ok(Some("fresh".to_string()))));
    assert_eq!(status_line_text(&chat), Some("fresh".to_string()));
}
