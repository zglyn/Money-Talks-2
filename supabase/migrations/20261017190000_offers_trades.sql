-- Offers and Trades. Credit still moves only as a reallocate Operation (ADR-0002, ADR-0003).
-- Settlement is recorded here and is not an issuer_ledger kind.

create table offers (
    id bigint generated always as identity primary key,
    program_id bigint not null references programs (id),
    team_id bigint not null references teams (id),
    amount_cents bigint not null check (amount_cents > 0),
    settlement_ask_cents bigint not null check (settlement_ask_cents >= 0),
    note text not null default '',
    status text not null check (status in ('open', 'cancelled', 'accepted')),
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table trades (
    id bigint generated always as identity primary key,
    offer_id bigint not null unique references offers (id),
    program_id bigint not null references programs (id),
    from_team_id bigint not null references teams (id),
    to_team_id bigint not null references teams (id),
    amount_cents bigint not null check (amount_cents > 0),
    settlement_ask_cents bigint not null check (settlement_ask_cents >= 0),
    settlement_status text not null check (settlement_status in ('simulated', 'external_paid')),
    note text not null default '',
    operation_id bigint not null unique references operations (id),
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (from_team_id <> to_team_id)
);

alter table allocation_events alter column operation_id drop not null;
alter table allocation_events add column offer_id bigint references offers (id);
alter table allocation_events add column trade_id bigint references trades (id);

alter table offers enable row level security;
alter table trades enable row level security;
