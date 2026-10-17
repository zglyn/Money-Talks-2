-- CreditPool: simulated Issuer Ledger, Operations, Reservations, Audit Events (ADR-0001, ADR-0002).
-- Mirrors src/creditpool/money.py. Amounts are integer cents (USD). Not yet run against Postgres.

create table programs (
    id bigint generated always as identity primary key,
    name text not null,
    total_budget_cents bigint not null check (total_budget_cents > 0),
    per_team_cap_cents bigint not null check (per_team_cap_cents > 0),
    expires_at timestamptz not null,
    returns_allowed boolean not null default true,
    created_at timestamptz not null default now()
);

create table teams (
    id bigint generated always as identity primary key,
    program_id bigint not null references programs (id),
    name text not null,
    eligible boolean not null default true,
    is_pool boolean not null default false,
    unique (program_id, name)
);
create unique index one_pool_per_program on teams (program_id) where is_pool;

create table operations (
    id bigint generated always as identity primary key,
    program_id bigint not null references programs (id),
    type text not null check (type in ('return', 'reallocate')),
    from_team_id bigint not null,
    to_team_id bigint not null,
    amount_cents bigint not null check (amount_cents > 0),
    idempotency_key text not null unique,
    status text not null check (
        status in ('requested', 'reserved', 'applied', 'reconciled', 'failed', 'flagged')
    ),
    agent_version text not null default 'v2' check (agent_version in ('v1', 'v2')),
    flag_reason text,
    claimed_by text,
    claimed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table issuer_ledger (
    id bigint generated always as identity primary key,
    program_id bigint not null references programs (id),
    team_id bigint not null references teams (id),
    amount_cents bigint not null check (amount_cents <> 0),
    kind text not null check (kind in ('grant', 'transfer_out', 'transfer_in')),
    operation_id bigint references operations (id),
    created_at timestamptz not null default now()
);
create index issuer_ledger_team on issuer_ledger (team_id);

create table reservations (
    id bigint generated always as identity primary key,
    operation_id bigint not null references operations (id),
    team_id bigint not null references teams (id),
    amount_cents bigint not null check (amount_cents > 0),
    status text not null check (status in ('active', 'applied', 'released', 'expired')),
    expires_at timestamptz not null,
    created_at timestamptz not null default now()
);
create unique index one_active_reservation_per_operation
    on reservations (operation_id) where status = 'active';

create table allocation_events (
    id bigint generated always as identity primary key,
    operation_id bigint not null references operations (id),
    actor text not null,
    step text not null,
    detail text not null default '',
    created_at timestamptz not null default now()
);

create function allocation_events_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'allocation_events is append-only';
end;
$$;
create trigger allocation_events_no_update before update on allocation_events
    for each row execute function allocation_events_append_only();
create trigger allocation_events_no_delete before delete on allocation_events
    for each row execute function allocation_events_append_only();
create trigger allocation_events_no_truncate before truncate on allocation_events
    for each statement execute function allocation_events_append_only();

create table agent_runs (
    id bigint generated always as identity primary key,
    operation_id bigint not null references operations (id),
    agent_version text not null check (agent_version in ('v1', 'v2')),
    outcome text not null,
    tool_calls jsonb not null default '[]'::jsonb,
    error_code text,
    prism_trace_id text,
    created_at timestamptz not null default now()
);
create index agent_runs_operation on agent_runs (operation_id);

alter table programs enable row level security;
alter table teams enable row level security;
alter table operations enable row level security;
alter table issuer_ledger enable row level security;
alter table reservations enable row level security;
alter table allocation_events enable row level security;
alter table agent_runs enable row level security;

-- Helpers ---------------------------------------------------------------------------------

create function cp_ledger_balance(p_team_id bigint) returns bigint
language sql stable as $$
    select coalesce(sum(amount_cents), 0)::bigint from issuer_ledger where team_id = p_team_id;
$$;

create function cp_program_total(p_program_id bigint) returns bigint
language sql stable as $$
    select coalesce(sum(amount_cents), 0)::bigint from issuer_ledger where program_id = p_program_id;
$$;

-- Available Balance = ledger balance minus active, unexpired Reservations.
create function available_balance(p_team_id bigint, p_now timestamptz default now())
returns bigint
language sql stable as $$
    select cp_ledger_balance(p_team_id) - coalesce((
        select sum(amount_cents) from reservations
        where team_id = p_team_id and status = 'active' and expires_at > p_now
    ), 0)::bigint;
$$;

create function cp_audit(p_operation_id bigint, p_actor text, p_step text, p_detail text)
returns void
language sql as $$
    insert into allocation_events (operation_id, actor, step, detail)
    values (p_operation_id, p_actor, p_step, coalesce(p_detail, ''));
$$;

create function cp_result(p_ok boolean, p_rule text default null, p_detail text default '')
returns jsonb
language sql immutable as $$
    select jsonb_build_object('ok', p_ok, 'rule', p_rule, 'detail', p_detail);
$$;

create function cp_set_status(p_op operations, p_to text, p_flag_reason text default null)
returns void
language plpgsql as $$
begin
    if not (
        (p_op.status = 'requested' and p_to in ('reserved', 'applied', 'flagged', 'failed'))
        or (p_op.status = 'reserved' and p_to in ('requested', 'applied', 'flagged', 'failed'))
        or (p_op.status = 'applied' and p_to in ('reconciled', 'failed'))
    ) then
        raise exception 'operation %: illegal transition % -> %', p_op.id, p_op.status, p_to;
    end if;
    update operations
    set status = p_to, flag_reason = coalesce(p_flag_reason, flag_reason), updated_at = now()
    where id = p_op.id;
end;
$$;

-- Lock the Operation row, then both Teams in id order so concurrent Operations serialize
-- without deadlocking.
create function cp_lock_operation(p_operation_id bigint) returns operations
language plpgsql as $$
declare
    v_op operations;
begin
    select * into v_op from operations where id = p_operation_id for update;
    if not found then
        raise exception 'operation % not found', p_operation_id;
    end if;
    perform 1 from teams where id in (v_op.from_team_id, v_op.to_team_id) order by id for update;
    return v_op;
end;
$$;

create function cp_flag(p_op operations, p_rule text, p_detail text, p_actor text)
returns jsonb
language plpgsql as $$
begin
    update reservations set status = 'released'
    where operation_id = p_op.id and status = 'active';
    perform cp_set_status(p_op, 'flagged', p_rule);
    perform cp_audit(p_op.id, p_actor, 'flagged',
        case when p_detail = '' then p_rule else p_rule || ': ' || p_detail end);
    return cp_result(false, p_rule, p_detail);
end;
$$;

create function cp_write_transfer(p_op operations) returns void
language plpgsql as $$
begin
    insert into issuer_ledger (program_id, team_id, amount_cents, kind, operation_id)
    select t.program_id, t.id,
           case when t.id = p_op.from_team_id then -p_op.amount_cents else p_op.amount_cents end,
           case when t.id = p_op.from_team_id then 'transfer_out' else 'transfer_in' end,
           p_op.id
    from teams t
    where t.id in (p_op.from_team_id, p_op.to_team_id);
end;
$$;

-- Admin and agent tools --------------------------------------------------------------------

create function create_program(
    p_name text, p_total_budget_cents bigint, p_per_team_cap_cents bigint,
    p_expires_at timestamptz, p_returns_allowed boolean default true
) returns bigint
language plpgsql as $$
declare
    v_id bigint;
begin
    insert into programs (name, total_budget_cents, per_team_cap_cents, expires_at, returns_allowed)
    values (p_name, p_total_budget_cents, p_per_team_cap_cents, p_expires_at, p_returns_allowed)
    returning id into v_id;
    insert into teams (program_id, name, eligible, is_pool) values (v_id, 'Program Pool', true, true);
    return v_id;
end;
$$;

create function add_team(p_program_id bigint, p_name text, p_eligible boolean default true)
returns bigint
language sql as $$
    insert into teams (program_id, name, eligible, is_pool)
    values (p_program_id, p_name, p_eligible, false)
    returning id;
$$;

create function grant_credit(p_team_id bigint, p_amount_cents bigint) returns bigint
language plpgsql as $$
declare
    v_team teams;
    v_program programs;
    v_id bigint;
begin
    if p_amount_cents <= 0 then
        raise exception 'grant amount must be positive';
    end if;
    select * into v_team from teams where id = p_team_id for update;
    if not found then
        raise exception 'team % not found', p_team_id;
    end if;
    select * into v_program from programs where id = v_team.program_id for update;
    if cp_program_total(v_program.id) + p_amount_cents > v_program.total_budget_cents then
        raise exception 'budget: grant would exceed Program total budget';
    end if;
    if not v_team.is_pool
       and cp_ledger_balance(p_team_id) + p_amount_cents > v_program.per_team_cap_cents then
        raise exception 'cap: grant would exceed per-Team cap';
    end if;
    insert into issuer_ledger (program_id, team_id, amount_cents, kind)
    values (v_program.id, p_team_id, p_amount_cents, 'grant')
    returning id into v_id;
    return v_id;
end;
$$;

-- Returns the existing Operation for a repeated Idempotency Key. A return always targets the
-- Program Pool; a Request is a reallocate whose from_team_id is the Program Pool.
create function create_operation(
    p_program_id bigint, p_type text, p_from_team_id bigint, p_to_team_id bigint,
    p_amount_cents bigint, p_idempotency_key text, p_agent_version text default 'v2'
) returns operations
language plpgsql as $$
declare
    v_op operations;
    v_to bigint := p_to_team_id;
    v_pool bigint;
begin
    select * into v_op from operations where idempotency_key = p_idempotency_key;
    if found then
        return v_op;
    end if;
    if p_type = 'return' then
        select id into v_pool from teams where program_id = p_program_id and is_pool;
        if v_pool is null then
            raise exception 'program % has no pool team', p_program_id;
        end if;
        if v_to is not null and v_to <> v_pool then
            raise exception 'a return must target the Program Pool';
        end if;
        v_to := v_pool;
    elsif p_type = 'reallocate' and v_to is null then
        raise exception 'a reallocate needs a to_team_id';
    end if;
    if p_from_team_id = v_to then
        raise exception 'from and to team must differ';
    end if;
    insert into operations (
        program_id, type, from_team_id, to_team_id, amount_cents, idempotency_key, status, agent_version
    ) values (
        p_program_id, p_type, p_from_team_id, v_to, p_amount_cents, p_idempotency_key,
        'requested', p_agent_version
    )
    on conflict (idempotency_key) do nothing
    returning * into v_op;
    if v_op.id is null then
        select * into v_op from operations where idempotency_key = p_idempotency_key;
        return v_op;
    end if;
    perform cp_audit(v_op.id, 'system', 'created', p_type);
    return v_op;
end;
$$;

create function release_expired_reservations(p_now timestamptz default now()) returns integer
language plpgsql as $$
declare
    r record;
    v_op operations;
    v_count integer := 0;
begin
    for r in
        select id, operation_id from reservations
        where status = 'active' and expires_at <= p_now
        order by id
        for update skip locked
    loop
        update reservations set status = 'expired' where id = r.id;
        select * into v_op from operations where id = r.operation_id for update;
        if v_op.status = 'reserved' then
            perform cp_set_status(v_op, 'requested');
        end if;
        perform cp_audit(r.operation_id, 'system', 'reservation_expired', '');
        v_count := v_count + 1;
    end loop;
    return v_count;
end;
$$;

create function reserve_funds(
    p_operation_id bigint, p_ttl_seconds double precision default 300, p_now timestamptz default now()
) returns jsonb
language plpgsql as $$
declare
    v_op operations;
    v_available bigint;
begin
    v_op := cp_lock_operation(p_operation_id);
    perform release_expired_reservations(p_now);
    select * into v_op from operations where id = p_operation_id;
    if v_op.status = 'reserved' then
        return cp_result(true);
    end if;
    if v_op.status <> 'requested' then
        return cp_result(false, 'bad_status', 'cannot reserve a ' || v_op.status || ' operation');
    end if;
    v_available := available_balance(v_op.from_team_id, p_now);
    if v_available < v_op.amount_cents then
        return cp_flag(v_op, 'insufficient_available',
            format('available %s < %s', v_available, v_op.amount_cents), 'system');
    end if;
    insert into reservations (operation_id, team_id, amount_cents, status, expires_at)
    values (v_op.id, v_op.from_team_id, v_op.amount_cents, 'active',
            p_now + make_interval(secs => p_ttl_seconds));
    perform cp_set_status(v_op, 'reserved');
    perform cp_audit(v_op.id, 'system', 'reserved', v_op.amount_cents::text);
    return cp_result(true);
end;
$$;

-- UNSAFE on purpose: no Reservation and no Program Rules checks. Exists to demonstrate failure.
create function apply_reallocation_v1(p_operation_id bigint) returns jsonb
language plpgsql as $$
declare
    v_op operations;
begin
    v_op := cp_lock_operation(p_operation_id);
    if v_op.status in ('applied', 'reconciled') then
        return cp_result(true);
    end if;
    if v_op.status not in ('requested', 'reserved') then
        return cp_result(false, 'bad_status', 'cannot apply a ' || v_op.status || ' operation');
    end if;
    perform cp_write_transfer(v_op);
    update reservations set status = 'released' where operation_id = v_op.id and status = 'active';
    perform cp_set_status(v_op, 'applied');
    perform cp_audit(v_op.id, 'agent-v1', 'applied', 'v1 unchecked apply');
    return cp_result(true);
end;
$$;

-- Applies only under an active Reservation and passing every Program Rule, in one transaction
-- with row locks on the Operation and both Teams. Idempotent on the Operation.
create function apply_reallocation_v2(p_operation_id bigint, p_now timestamptz default now())
returns jsonb
language plpgsql as $$
declare
    v_op operations;
    v_src teams;
    v_dst teams;
    v_program programs;
begin
    v_op := cp_lock_operation(p_operation_id);
    if v_op.status in ('applied', 'reconciled') then
        return cp_result(true);
    end if;
    if v_op.status not in ('requested', 'reserved') then
        return cp_result(false, 'bad_status', 'cannot apply a ' || v_op.status || ' operation');
    end if;

    select * into v_src from teams where id = v_op.from_team_id;
    select * into v_dst from teams where id = v_op.to_team_id;
    if v_src.id is null or v_dst.id is null then
        return cp_flag(v_op, 'unknown_team', 'team does not exist', 'system');
    end if;
    if v_src.program_id <> v_op.program_id or v_dst.program_id <> v_op.program_id then
        return cp_flag(v_op, 'program_mismatch', 'team belongs to a different Program', 'system');
    end if;
    select * into v_program from programs where id = v_op.program_id;
    if v_op.type = 'return' and not v_program.returns_allowed then
        return cp_flag(v_op, 'returns_disabled', 'Program does not allow Returns', 'system');
    end if;
    if p_now >= v_program.expires_at then
        return cp_flag(v_op, 'expired', 'Program has expired', 'system');
    end if;
    if cp_ledger_balance(v_src.id) < v_op.amount_cents then
        return cp_flag(v_op, 'insufficient_balance', 'source ledger balance too low', 'system');
    end if;
    if not v_dst.eligible then
        return cp_flag(v_op, 'ineligible', 'recipient Team is not eligible', 'system');
    end if;
    if not v_dst.is_pool
       and cp_ledger_balance(v_dst.id) + v_op.amount_cents > v_program.per_team_cap_cents then
        return cp_flag(v_op, 'cap', 'recipient would exceed per-Team cap', 'system');
    end if;
    if cp_program_total(v_op.program_id) > v_program.total_budget_cents then
        return cp_flag(v_op, 'budget', 'Program total exceeds budget', 'system');
    end if;
    if not exists (
        select 1 from reservations
        where operation_id = v_op.id and status = 'active' and team_id = v_op.from_team_id
          and amount_cents = v_op.amount_cents and expires_at > p_now
    ) then
        return cp_flag(v_op, 'no_reservation', 'no active Reservation for this Operation', 'system');
    end if;

    perform cp_write_transfer(v_op);
    update reservations set status = 'applied' where operation_id = v_op.id and status = 'active';
    perform cp_set_status(v_op, 'applied');
    perform cp_audit(v_op.id, 'system', 'applied', v_op.amount_cents::text);
    return cp_result(true);
end;
$$;

-- Verifies the Issuer Ledger, Operation, and Reservations agree. A mismatch fails the Operation.
create function reconcile_operation(p_operation_id bigint, p_now timestamptz default now())
returns jsonb
language plpgsql as $$
declare
    v_op operations;
    v_src teams;
    v_dst teams;
    v_cap bigint;
    v_found text[] := '{}';
    v_team teams;
    v_program_id bigint;
begin
    v_op := cp_lock_operation(p_operation_id);
    if v_op.status = 'reconciled' then
        return jsonb_build_object('ok', true, 'mismatches', '[]'::jsonb);
    end if;
    if v_op.status <> 'applied' then
        return jsonb_build_object('ok', false, 'mismatches', jsonb_build_array('not_applied'));
    end if;
    select * into v_src from teams where id = v_op.from_team_id;
    select * into v_dst from teams where id = v_op.to_team_id;

    if (select count(*) from issuer_ledger where operation_id = v_op.id) <> 2
       or not exists (
           select 1 from issuer_ledger
           where operation_id = v_op.id and team_id = v_src.id and amount_cents = -v_op.amount_cents)
       or not exists (
           select 1 from issuer_ledger
           where operation_id = v_op.id and team_id = v_dst.id and amount_cents = v_op.amount_cents)
    then
        v_found := v_found || 'ledger_pair';
    end if;

    if v_src.program_id <> v_op.program_id or v_dst.program_id <> v_op.program_id then
        v_found := v_found || 'cross_program';
    end if;
    foreach v_team in array array[v_src, v_dst] loop
        if cp_ledger_balance(v_team.id) < 0 then
            v_found := v_found || 'negative_balance';
        end if;
        if available_balance(v_team.id, p_now) < 0 then
            v_found := v_found || 'negative_available';
        end if;
    end loop;
    select per_team_cap_cents into v_cap from programs where id = v_dst.program_id;
    if not v_dst.is_pool and cp_ledger_balance(v_dst.id) > v_cap then
        v_found := v_found || 'over_cap';
    end if;
    for v_program_id in select distinct unnest(array[v_src.program_id, v_dst.program_id]) loop
        if cp_program_total(v_program_id) > (select total_budget_cents from programs where id = v_program_id) then
            v_found := v_found || 'over_budget';
            exit;
        end if;
    end loop;
    if exists (select 1 from reservations where operation_id = v_op.id and status = 'active') then
        v_found := v_found || 'dangling_reservation';
    end if;

    select coalesce(array_agg(distinct m), '{}') into v_found from unnest(v_found) as m;
    if cardinality(v_found) > 0 then
        perform cp_set_status(v_op, 'failed', 'reconcile_mismatch');
        perform cp_audit(v_op.id, 'system', 'reconcile_failed', array_to_string(v_found, ','));
        return jsonb_build_object('ok', false, 'mismatches', to_jsonb(v_found));
    end if;
    perform cp_set_status(v_op, 'reconciled');
    perform cp_audit(v_op.id, 'system', 'reconciled', '');
    return jsonb_build_object('ok', true, 'mismatches', '[]'::jsonb);
end;
$$;

create function flag_for_admin(p_operation_id bigint, p_reason text, p_actor text default 'agent')
returns jsonb
language plpgsql as $$
declare
    v_op operations;
begin
    v_op := cp_lock_operation(p_operation_id);
    if v_op.status not in ('requested', 'reserved') then
        return cp_result(false, 'bad_status', 'cannot flag a ' || v_op.status || ' operation');
    end if;
    perform cp_flag(v_op, p_reason, '', p_actor);
    return cp_result(true);
end;
$$;

create function write_audit_event(
    p_operation_id bigint, p_actor text, p_step text, p_detail text default ''
) returns void
language sql as $$
    select cp_audit(p_operation_id, p_actor, p_step, p_detail);
$$;
