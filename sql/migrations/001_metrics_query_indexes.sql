-- migrate: no-transaction
-- Indexes tuned for process metrics endpoints.

create index concurrently if not exists idx_telemetry_event_utc_time
  on telemetry (event, utc_time desc);

create index concurrently if not exists idx_telemetry_pc_utc_time
  on telemetry (utc_time desc)
  where event = 'process_completed' and trace is not null;

create index concurrently if not exists idx_telemetry_pc_process
  on telemetry ((trace->>'process'))
  where event = 'process_completed' and trace is not null;

create index concurrently if not exists idx_telemetry_pc_status
  on telemetry ((trace->>'status'))
  where event = 'process_completed' and trace is not null;

create index concurrently if not exists idx_telemetry_pc_attempt
  on telemetry ((trace->>'attempt'))
  where event = 'process_completed' and trace is not null;

create index concurrently if not exists idx_telemetry_pc_exit
  on telemetry ((trace->>'exit'))
  where event = 'process_completed' and trace is not null;
