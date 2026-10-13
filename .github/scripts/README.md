# Private Neom build logs

The active manual workflow captures shell output into `RUNNER_TEMP/neom-private-logs`.
GitHub receives stage outcomes only. It does not print the captured build/extraction/
packing/upload details or publish them as workflow artifacts.

The workflow checks the decoded rclone configuration, remote name, token structure,
and Drive authentication before starting extraction. Diagnostics retain rclone stderr
in `drive-check.log` rather than suppressing it.

The final `always()` step sends a ZIP to the configured `TELEGRAM_OWNER_ID` on success
or failure. It runs before credential cleanup, independently of the engine checkout
or build outcome. The owner chat must have a positive ID and `getChat` must confirm
that it is that exact private chat. No group, channel, builder, or secondary recipient
is used. Existing bot and owner secrets are reused.

The ZIP includes `full-build.log`, per-stage logs and durations, step outcomes,
build state, partition filesystems and sizes, super group usage, and packaging output.
Known credentials and OAuth fields are redacted. The actual rclone configuration is
never included. Files over 40 MiB are delivered as numbered ZIP parts without losing
log content. Concatenate those parts in order to restore the ZIP.

Delivery uses Telegram `sendDocument`, checks the API response, and retries transient
network/server/rate-limit errors. Ordinary stage failures preserve their exit code;
the final reporting step still runs. A forced runner shutdown cannot run final steps.

Run the focused tests with `python3 -m unittest discover -s tests -v`.
