-- The month-end agent's tables. Every amount is a bigint of cents; there is no float column.
-- Each visitor's copy of the books is one row in workspaces, and everything else hangs off it
-- with ON DELETE CASCADE, so removing a copy removes all of it.

CREATE TABLE IF NOT EXISTS workspaces (
    id           uuid PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    addr_hash    text NOT NULL,
    shop_name    text NOT NULL,
    period_start date NOT NULL,
    period_end   date NOT NULL
);
CREATE INDEX IF NOT EXISTS workspaces_expires ON workspaces (expires_at);

CREATE TABLE IF NOT EXISTS accounts (
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    code         text NOT NULL,
    name         text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('asset', 'liability', 'equity', 'income', 'expense')),
    hint         text NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, code)
);

CREATE TABLE IF NOT EXISTS parties (
    id           bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('customer', 'supplier')),
    name         text NOT NULL,
    contact      text NOT NULL DEFAULT '',
    terms_days   integer NOT NULL DEFAULT 30 CHECK (terms_days BETWEEN 0 AND 120),
    UNIQUE (workspace_id, kind, name)
);

-- Credit sales to customers (invoices), credit purchases from suppliers (bills) and the
-- counter's weekly cash takings waiting to be banked (cash-ups).
CREATE TABLE IF NOT EXISTS documents (
    id           bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('invoice', 'bill', 'cashup')),
    number       text NOT NULL,
    party_id     bigint REFERENCES parties(id) ON DELETE CASCADE,
    issued_on    date NOT NULL,
    due_on       date NOT NULL,
    total_cents  bigint NOT NULL CHECK (total_cents > 0),
    paid_cents   bigint NOT NULL DEFAULT 0 CHECK (paid_cents >= 0),
    memo         text NOT NULL DEFAULT '',
    CHECK (paid_cents <= total_cents),
    UNIQUE (workspace_id, kind, number)
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id           bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    entry_date   date NOT NULL,
    memo         text NOT NULL,
    source       text NOT NULL CHECK (source IN ('opening', 'books', 'agent', 'close')),
    proposal_id  bigint,
    posted_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS journal_entries_ws ON journal_entries (workspace_id, entry_date);

CREATE TABLE IF NOT EXISTS journal_lines (
    id           bigserial PRIMARY KEY,
    entry_id     bigint NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    workspace_id uuid NOT NULL,
    account_code text NOT NULL,
    debit_cents  bigint NOT NULL DEFAULT 0 CHECK (debit_cents >= 0),
    credit_cents bigint NOT NULL DEFAULT 0 CHECK (credit_cents >= 0),
    document_id  bigint REFERENCES documents(id) ON DELETE SET NULL,
    CHECK ((debit_cents > 0) <> (credit_cents > 0)),
    FOREIGN KEY (workspace_id, account_code) REFERENCES accounts(workspace_id, code) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS journal_lines_entry ON journal_lines (entry_id);
CREATE INDEX IF NOT EXISTS journal_lines_ws_account ON journal_lines (workspace_id, account_code);

-- The ledger's one rule, held by the database itself: when a transaction commits, every
-- journal entry it touched has at least two lines and its debits equal its credits.
CREATE OR REPLACE FUNCTION journal_entry_balances() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    eid bigint;
    d bigint;
    c bigint;
    n integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        eid := OLD.entry_id;
    ELSE
        eid := NEW.entry_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = eid) THEN
        RETURN NULL;  -- the whole entry was deleted with its copy
    END IF;
    SELECT coalesce(sum(debit_cents), 0), coalesce(sum(credit_cents), 0), count(*)
      INTO d, c, n FROM journal_lines WHERE entry_id = eid;
    IF n < 2 OR d <> c THEN
        RAISE EXCEPTION 'journal entry % does not balance', eid USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS journal_lines_balance ON journal_lines;
CREATE CONSTRAINT TRIGGER journal_lines_balance
    AFTER INSERT OR UPDATE OR DELETE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_entry_balances();

-- An entry with no lines at all is caught too: a deferred trigger on the entry itself.
CREATE OR REPLACE FUNCTION journal_entry_has_lines() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM journal_entries WHERE id = NEW.id)
       AND NOT EXISTS (SELECT 1 FROM journal_lines WHERE entry_id = NEW.id) THEN
        RAISE EXCEPTION 'journal entry % has no lines', NEW.id USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS journal_entries_lines ON journal_entries;
CREATE CONSTRAINT TRIGGER journal_entries_lines
    AFTER INSERT ON journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_entry_has_lines();

-- Bank statements the owner gave the agent, and their lines.
CREATE TABLE IF NOT EXISTS statements (
    id                bigserial PRIMARY KEY,
    workspace_id      uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    file_name         text NOT NULL,
    sha256            text NOT NULL,
    received_at       timestamptz NOT NULL DEFAULT now(),
    period_start      date,
    period_end        date,
    opening_cents     bigint,
    closing_cents     bigint
);

CREATE TABLE IF NOT EXISTS statement_lines (
    id            bigserial PRIMARY KEY,
    statement_id  bigint NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    line_no       integer NOT NULL,
    line_date     date NOT NULL,
    description   text NOT NULL,
    reference     text NOT NULL DEFAULT '',
    amount_cents  bigint NOT NULL CHECK (amount_cents <> 0),
    balance_cents bigint,
    status        text NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'matched', 'suggested', 'flagged', 'posted', 'set_aside')),
    note          text NOT NULL DEFAULT '',
    UNIQUE (statement_id, line_no)
);
CREATE INDEX IF NOT EXISTS statement_lines_ws ON statement_lines (workspace_id, line_date);

-- Everything the agent wants to change waits here until the owner approves or rejects it.
CREATE TABLE IF NOT EXISTS proposals (
    id           bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    job_id       bigint,
    kind         text NOT NULL CHECK (kind IN ('record', 'match', 'flag', 'followup')),
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected', 'posted')),
    title        text NOT NULL,
    detail       text NOT NULL DEFAULT '',
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    decided_at   timestamptz,
    posted_at    timestamptz
);
CREATE INDEX IF NOT EXISTS proposals_ws ON proposals (workspace_id, status, id);

-- The month-end job and the steps it streams to the browser.
CREATE TABLE IF NOT EXISTS jobs (
    id            bigserial PRIMARY KEY,
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('close_month', 'post_approved')),
    status        text NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed')),
    statement_id  bigint REFERENCES statements(id) ON DELETE SET NULL,
    parent_id     bigint REFERENCES jobs(id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    summary       jsonb
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status, id);

CREATE TABLE IF NOT EXISTS job_events (
    id       bigserial PRIMARY KEY,
    job_id   bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    step     integer NOT NULL CHECK (step BETWEEN 0 AND 5),
    kind     text NOT NULL,
    message  text NOT NULL,
    data     jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events (job_id, id);

-- Who did what, when: the owner, the agent or the system.
CREATE TABLE IF NOT EXISTS audit_log (
    id           bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    at           timestamptz NOT NULL DEFAULT now(),
    actor        text NOT NULL CHECK (actor IN ('owner', 'agent', 'system')),
    action       text NOT NULL,
    object_type  text NOT NULL DEFAULT '',
    object_id    text NOT NULL DEFAULT '',
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_ws ON audit_log (workspace_id, id);

-- The agent's conversation with the owner, so a reload keeps it.
CREATE TABLE IF NOT EXISTS messages (
    id           bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    at           timestamptz NOT NULL DEFAULT now(),
    role         text NOT NULL CHECK (role IN ('owner', 'agent')),
    body         text NOT NULL,
    via          text NOT NULL DEFAULT 'text' CHECK (via IN ('text', 'voice')),
    proposal_id  bigint
);
CREATE INDEX IF NOT EXISTS messages_ws ON messages (workspace_id, id);

-- Durable limits: one counter per bucket and window. A bucket names what is counted and a
-- keyed hash of the caller's address, never the address itself.
CREATE TABLE IF NOT EXISTS rate_counters (
    bucket       text NOT NULL,
    window_start timestamptz NOT NULL,
    n            integer NOT NULL,
    PRIMARY KEY (bucket, window_start)
);
CREATE INDEX IF NOT EXISTS rate_counters_window ON rate_counters (window_start);

CREATE TABLE IF NOT EXISTS schema_version (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
