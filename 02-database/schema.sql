-- Operations Hub schema (shared operational data — not per-user).

create table if not exists services (
  id serial primary key,
  name text not null unique,
  type text not null,
  status text not null,
  owner_team text not null,
  criticality text not null
);

create table if not exists incidents (
  id serial primary key,
  incident_number text not null unique,
  title text not null,
  description text not null,
  severity text not null,
  status text not null,
  service_id integer not null references services(id),
  service_name text not null,
  opened_at timestamptz not null,
  acknowledged_at timestamptz,
  resolved_at timestamptz,
  mtta_minutes real not null,
  mttr_minutes real not null,
  cost_usd real not null,
  avoidable boolean not null default false,
  root_cause_class text not null,
  impact_summary text not null,
  sla_breached boolean not null default false
);
create index if not exists incidents_opened_idx on incidents (opened_at desc);
create index if not exists incidents_sev_status_idx on incidents (severity, status);

create table if not exists alerts (
  id serial primary key,
  alert_id text not null unique,
  source text not null,
  severity text not null,
  title text not null,
  service_name text not null,
  fired_at timestamptz not null,
  resolved_at timestamptz,
  acknowledged boolean not null default false,
  related_incident_id integer,
  message text not null
);
create index if not exists alerts_fired_idx on alerts (fired_at desc);

create table if not exists tickets (
  id serial primary key,
  ticket_number text not null unique,
  source text not null,
  type text not null,
  subject text not null,
  status text not null,
  priority text not null,
  related_incident_number text,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  assignee text not null,
  description text not null,
  resolution text
);
create index if not exists tickets_created_idx on tickets (created_at desc);
create index if not exists tickets_source_idx on tickets (source);

create table if not exists runbooks (
  id serial primary key,
  name text not null,
  service_name text not null,
  version text not null,
  steps_json text not null,
  success_rate real not null,
  avg_duration_min real not null,
  last_updated date not null
);

create table if not exists runbook_executions (
  id serial primary key,
  runbook_id integer not null references runbooks(id),
  incident_number text,
  started_at timestamptz not null,
  completed_at timestamptz,
  status text not null,
  steps_completed integer not null,
  total_steps integer not null,
  executed_by text not null,
  notes text
);
create index if not exists runbook_exec_started_idx on runbook_executions (started_at desc);

create table if not exists hourly_incident_patterns (
  id serial primary key,
  hour_of_day integer not null,
  day_of_week integer not null,
  date date not null,
  incident_count integer not null,
  avg_response_min real not null,
  avg_mtta_min real not null,
  p1_count integer not null,
  service_focus text not null
);
create index if not exists hip_date_hour_idx on hourly_incident_patterns (date, hour_of_day);

create table if not exists telemetry_metrics (
  id serial primary key,
  service_name text not null,
  metric_name text not null,
  timestamp timestamptz not null,
  value real not null,
  unit text not null,
  anomaly boolean not null default false
);
create index if not exists telemetry_ts_idx on telemetry_metrics (timestamp desc);

create table if not exists pca_logs (
  id serial primary key,
  log_id text not null unique,
  service_name text not null,
  level text not null,
  message text not null,
  timestamp timestamptz not null,
  correlation_id text not null,
  upgrade_related boolean not null default false,
  host text not null
);
create index if not exists pca_logs_ts_idx on pca_logs (timestamp desc);

create table if not exists cost_events (
  id serial primary key,
  incident_number text not null,
  cost_type text not null,
  amount_usd real not null,
  description text not null,
  recorded_at timestamptz not null
);

create table if not exists sla_snapshots (
  id serial primary key,
  snapshot_at date not null unique,
  risk_score integer not null,
  health_score integer not null,
  open_p1 integer not null,
  open_p2 integer not null,
  avg_mtta_today real not null,
  next_high_risk_days integer not null,
  notes text
);

create table if not exists video_captures (
  id serial primary key,
  capture_id text not null unique,
  incident_number text,
  title text not null,
  service_name text not null,
  video_url text not null,
  duration_sec integer not null,
  captured_at timestamptz not null,
  rca_summary text not null,
  confidence real not null
);
create index if not exists video_captures_ts_idx on video_captures (captured_at desc);

create table if not exists rca_annotations (
  id serial primary key,
  capture_id text not null,
  timestamp_sec real not null,
  kind text not null,
  label text not null,
  detail text not null
);
