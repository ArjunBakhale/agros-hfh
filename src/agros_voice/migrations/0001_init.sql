-- Agros voice outreach: baseline schema.
-- Tenant isolation: every tenant-scoped table has RLS keyed on the transaction-local
-- setting app.tenant_id, set only by agros_voice.db.with_tenant(). Missing setting => zero rows.
-- The application connects as the non-owner role agros_app, so RLS always applies.

create extension if not exists pgcrypto;

create or replace function app_current_tenant() returns uuid
language sql stable as $$
  select nullif(current_setting('app.tenant_id', true), '')::uuid
$$;

create or replace function prevent_tenant_id_change() returns trigger
language plpgsql as $$
begin
  if new.tenant_id is distinct from old.tenant_id then
    raise exception 'tenant_id is immutable';
  end if;
  return new;
end $$;

create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

create or replace function reject_mutation() returns trigger
language plpgsql as $$
begin
  raise exception '% is append-only', tg_table_name;
end $$;

create table tenants (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz not null default now()
);

create table users (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  email text not null,
  password_hash text not null,
  role text not null check (role in ('admin','manager','cta','viewer')),
  display_name text,
  disabled boolean not null default false,
  created_at timestamptz not null default now(),
  unique (tenant_id, id),
  unique (email)
);

create table voice_campaigns (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  name text not null,
  status text not null default 'draft'
    check (status in ('draft','active','paused','completed','archived')),
  script_id text not null,
  max_attempts integer not null default 1 check (max_attempts between 1 and 10),
  call_window_start time not null default '09:00',
  call_window_end time not null default '19:00',
  timezone text not null default 'America/Detroit',
  is_fixture boolean not null default true,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, id),
  foreign key (tenant_id, created_by) references users (tenant_id, id) on delete restrict
);

create table voice_contacts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  campaign_id uuid not null,
  contact_token text not null,
  phone_e164 text not null check (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  language text not null default 'en',
  age_band text,
  eligibility_flags jsonb not null default '{}'::jsonb,
  is_fixture boolean not null default true,
  do_not_call boolean not null default false,
  status text not null default 'pending'
    check (status in ('pending','queued','calling','screened','interested','handoff_sent',
                      'scheduling_requested','scheduled','declined','unreachable','needs_human','closed')),
  final_disposition text,
  attempt_count integer not null default 0,
  last_attempt_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, id),
  unique (tenant_id, campaign_id, contact_token),
  foreign key (tenant_id, campaign_id) references voice_campaigns (tenant_id, id) on delete restrict
);

create table voice_call_attempts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  campaign_id uuid not null,
  contact_id uuid not null,
  script_id text not null,
  script_version integer not null,
  provider text not null check (provider in ('fixture','livekit')),
  provider_call_id text,
  fixture_persona text,
  status text not null default 'queued'
    check (status in ('queued','calling','completed','failed','cancelled','timed_out')),
  dialogue_state text not null default 'queued',
  answers jsonb not null default '{}'::jsonb,
  disposition text,
  failure_reason text,
  requested_by uuid,
  idempotency_key text not null,
  started_at timestamptz,
  ended_at timestamptz,
  duration_seconds integer,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, id),
  unique (tenant_id, idempotency_key),
  unique (provider, provider_call_id),
  foreign key (tenant_id, campaign_id) references voice_campaigns (tenant_id, id) on delete restrict,
  foreign key (tenant_id, contact_id) references voice_contacts (tenant_id, id) on delete restrict,
  foreign key (tenant_id, requested_by) references users (tenant_id, id) on delete restrict
);

create table voice_call_events (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  call_attempt_id uuid not null,
  provider_event_id text not null,
  event_type text not null
    check (event_type in ('call.started','turn.completed','call.ended','call.failed')),
  seq integer,
  occurred_at timestamptz not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (tenant_id, id),
  unique (tenant_id, call_attempt_id, provider_event_id),
  foreign key (tenant_id, call_attempt_id) references voice_call_attempts (tenant_id, id) on delete restrict
);

-- Webhooks that cannot be correlated to exactly one attempt are quarantined, never applied.
create table voice_webhook_quarantine (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  provider_call_id text,
  reason text not null,
  payload_sha256 text not null,
  received_at timestamptz not null default now()
);

create table voice_handoffs (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  contact_id uuid not null,
  call_attempt_id uuid,
  kind text not null check (kind in ('coordinator_followup','human_review','warm_transfer','econsent_link')),
  reason text not null,
  status text not null default 'open' check (status in ('open','in_progress','completed','cancelled')),
  preferred_window text,
  resolved_by uuid,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, id),
  unique (tenant_id, call_attempt_id, kind),
  foreign key (tenant_id, contact_id) references voice_contacts (tenant_id, id) on delete restrict,
  foreign key (tenant_id, call_attempt_id) references voice_call_attempts (tenant_id, id) on delete restrict,
  foreign key (tenant_id, resolved_by) references users (tenant_id, id) on delete restrict,
  check ((status in ('completed','cancelled')) = (resolved_by is not null and resolved_at is not null))
);

create table audit_events (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  seq bigint not null check (seq > 0),
  actor_type text not null check (actor_type in ('user','system','provider')),
  actor_id uuid,
  action text not null,
  object_type text not null,
  object_id uuid,
  payload jsonb not null default '{}'::jsonb,
  prev_hash text,
  hash text not null,
  created_at timestamptz not null default now(),
  unique (tenant_id, seq),
  check ((seq = 1) = (prev_hash is null))
);

create or replace function audit_events_enforce_chain() returns trigger
language plpgsql as $$
declare
  last_seq bigint;
  last_hash text;
begin
  select seq, hash into last_seq, last_hash from audit_events
   where tenant_id = new.tenant_id order by seq desc limit 1;
  if last_seq is null then
    if new.seq <> 1 then raise exception 'audit chain must start at seq 1'; end if;
  elsif new.seq <> last_seq + 1 or new.prev_hash is distinct from last_hash then
    raise exception 'audit chain fork rejected';
  end if;
  return new;
end $$;

create trigger audit_events_chain before insert on audit_events
  for each row execute function audit_events_enforce_chain();
create trigger audit_events_append_only before update or delete on audit_events
  for each row execute function reject_mutation();
create trigger voice_call_events_append_only before update or delete on voice_call_events
  for each row execute function reject_mutation();

-- Simple Postgres job queue (pg-boss style): SKIP LOCKED claims, retry, dead letter.
create table jobs (
  id bigserial primary key,
  queue text not null,
  tenant_id uuid not null references tenants(id) on delete restrict,
  payload jsonb not null,
  status text not null default 'pending' check (status in ('pending','active','done','dead')),
  attempts integer not null default 0,
  max_attempts integer not null default 3,
  run_after timestamptz not null default now(),
  locked_until timestamptz,
  last_error text,
  singleton_key text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index jobs_singleton on jobs (queue, singleton_key) where singleton_key is not null;
create index jobs_due on jobs (queue, status, run_after);

do $$
declare t text;
begin
  foreach t in array array['users','voice_campaigns','voice_contacts','voice_call_attempts',
                           'voice_call_events','voice_handoffs','audit_events','jobs']
  loop
    execute format('create trigger %I_tenant_immutable before update on %I
                    for each row execute function prevent_tenant_id_change()', t, t);
  end loop;
  foreach t in array array['voice_campaigns','voice_contacts','voice_call_attempts','voice_handoffs','jobs']
  loop
    execute format('create trigger %I_updated_at before update on %I
                    for each row execute function set_updated_at()', t, t);
  end loop;
  foreach t in array array['users','voice_campaigns','voice_contacts','voice_call_attempts',
                           'voice_call_events','voice_handoffs','audit_events']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('create policy tenant_isolation on %I using (tenant_id = app_current_tenant())
                    with check (tenant_id = app_current_tenant())', t);
  end loop;
end $$;

alter table tenants enable row level security;
create policy tenant_self on tenants using (id = app_current_tenant());

create index voice_contacts_campaign_status on voice_contacts (tenant_id, campaign_id, status);
create index voice_attempts_contact on voice_call_attempts (tenant_id, contact_id, created_at desc);
create index voice_attempts_calling on voice_call_attempts (status, updated_at) where status = 'calling';
create index voice_handoffs_open on voice_handoffs (tenant_id, status) where status = 'open';

-- Narrow cross-tenant entry points (SECURITY DEFINER; the owner is not subject to RLS).
create or replace function auth_lookup_user(p_email text)
returns table (id uuid, tenant_id uuid, password_hash text, role text, disabled boolean)
language sql stable security definer set search_path = public as $$
  select id, tenant_id, password_hash, role, disabled from users where email = lower(p_email)
$$;

create or replace function resolve_call_attempt(p_provider text, p_provider_call_id text)
returns table (tenant_id uuid, attempt_id uuid)
language sql stable security definer set search_path = public as $$
  select tenant_id, id from voice_call_attempts
   where provider = p_provider and provider_call_id = p_provider_call_id
$$;

create or replace function sweep_stuck_attempts(p_older_than interval)
returns table (tenant_id uuid, attempt_id uuid)
language sql stable security definer set search_path = public as $$
  select tenant_id, id from voice_call_attempts
   where status in ('calling','queued') and updated_at < now() - p_older_than
$$;
