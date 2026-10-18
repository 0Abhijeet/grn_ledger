-- GRN Ledger: synthetic prototype schema. 5 tables max (hard constraint).
-- Plain SQL migration, applied by scripts/migrate.py.

CREATE TABLE IF NOT EXISTS parts (
    part_id     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN
                  ('fasteners','connectors','harness_consumables','sealants')),
    uom         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS po_lines (
    id             SERIAL PRIMARY KEY,
    po_number      TEXT NOT NULL,
    line_no        INTEGER NOT NULL,
    part_id        TEXT NOT NULL REFERENCES parts(part_id),
    supplier       TEXT NOT NULL,                 -- text on purpose: no supplier table
    ordered_qty    NUMERIC(12,3) NOT NULL CHECK (ordered_qty > 0),
    unit_price_inr NUMERIC(12,2) NOT NULL CHECK (unit_price_inr >= 0),
    need_by_date   DATE NOT NULL,
    promised_date  DATE NULL,
    status         TEXT NOT NULL CHECK (status IN
                     ('pending_approval','open','partially_received','received','closed')),
    UNIQUE (po_number, line_no)
);

CREATE TABLE IF NOT EXISTS receipt_events (
    id               SERIAL PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,        -- blocks duplicate receipt attempts
    po_line_id       INTEGER NOT NULL REFERENCES po_lines(id),
    received_qty     NUMERIC(12,3) NOT NULL CHECK (received_qty >= 0),
    accepted_qty     NUMERIC(12,3) NOT NULL CHECK (accepted_qty >= 0),
    rejected_qty     NUMERIC(12,3) NOT NULL CHECK (rejected_qty >= 0),
    quarantined_qty  NUMERIC(12,3) NOT NULL CHECK (quarantined_qty >= 0),
    lot_batch        TEXT NOT NULL,
    coc_received     BOOLEAN NOT NULL,
    exception_flags  TEXT NOT NULL DEFAULT '',    -- comma list, e.g. over_delivery,missing_coc
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- every received unit must be accounted for in exactly one bucket
    CHECK (accepted_qty + rejected_qty + quarantined_qty = received_qty)
);

CREATE TABLE IF NOT EXISTS ledger_events (
    id               SERIAL PRIMARY KEY,
    part_id          TEXT NOT NULL REFERENCES parts(part_id),
    po_line_id       INTEGER NULL REFERENCES po_lines(id),
    receipt_event_id INTEGER NULL REFERENCES receipt_events(id),
    event_type       TEXT NOT NULL CHECK (event_type IN
                       ('receipt','quarantine','reject','release_from_quarantine','issue')),
    qty              NUMERIC(12,3) NOT NULL CHECK (qty > 0),   -- direction comes from event_type
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only: the database itself refuses UPDATE and DELETE on the ledger.
CREATE OR REPLACE FUNCTION ledger_events_block_mutation() RETURNS trigger AS $$
BEGIN
    -- keep percent signs out of this file: psycopg reads them as placeholders, even in comments
    RAISE EXCEPTION USING MESSAGE = 'ledger_events is append-only: ' || TG_OP || ' not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_events_no_update_delete ON ledger_events;
CREATE TRIGGER ledger_events_no_update_delete
    BEFORE UPDATE OR DELETE ON ledger_events
    FOR EACH ROW EXECUTE FUNCTION ledger_events_block_mutation();

CREATE TABLE IF NOT EXISTS invoices (
    id                     SERIAL PRIMARY KEY,
    invoice_number         TEXT NOT NULL,
    po_line_id             INTEGER NOT NULL REFERENCES po_lines(id),
    invoice_qty            NUMERIC(12,3) NOT NULL CHECK (invoice_qty > 0),
    invoice_unit_price_inr NUMERIC(12,2) NOT NULL CHECK (invoice_unit_price_inr >= 0),
    invoice_date           DATE NOT NULL,
    UNIQUE (invoice_number, po_line_id)
);
