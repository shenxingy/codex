//! Covers the fork's `status_line_command` round trip through the app event loop.

use super::*;
use pretty_assertions::assert_eq;

/// The command runs locally, so a daemon disconnect must not drop its request or result;
/// either drop would leave the request pending and freeze the `custom` item.
#[cfg(unix)]
#[tokio::test]
async fn status_line_command_round_trip_survives_offline() -> Result<()> {
    let (mut app, mut events, _ops) = make_test_app_with_channels().await;
    let mut server = start_config_write_test_app_server(&app).await?;
    let mut tui = crate::tui::test_support::make_test_tui()?;
    app.chat_widget.local_settings.tui.status_line = Some(vec!["custom".to_string()]);
    app.chat_widget.local_settings.tui.status_line_command = Some(vec![
        "sh".to_string(),
        "-c".to_string(),
        "cat >/dev/null; printf 'offline ok'".to_string(),
    ]);
    app.reconnect.offline = true;

    app.chat_widget.refresh_status_line();
    let request = std::iter::from_fn(|| events.try_recv().ok())
        .find(|event| matches!(event, AppEvent::RefreshStatusLineCommand { .. }))
        .expect("custom item requests its command");
    Box::pin(app.handle_event(&mut tui, &mut server, request)).await?;

    let update = tokio::time::timeout(std::time::Duration::from_secs(/*secs*/ 10), async {
        loop {
            match events.recv().await {
                Some(event @ AppEvent::StatusLineCommandUpdated { .. }) => return event,
                Some(_) => {}
                None => panic!("app event channel closed"),
            }
        }
    })
    .await
    .expect("command result arrives while offline");
    Box::pin(app.handle_event(&mut tui, &mut server, update)).await?;

    assert_eq!(
        app.chat_widget.status_line_text(),
        Some("offline ok".to_string())
    );
    server.shutdown().await?;
    Ok(())
}
