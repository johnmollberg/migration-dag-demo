ALTER TABLE payouts ADD COLUMN statement_descriptor VARCHAR(22) NULL;

-- Legal cross-domain read: accounts is a declared ancestor of payments in
-- the DAG (payments depends_on identity, accounts), so accounts is
-- guaranteed to be fully migrated before this runs.
UPDATE payouts po
JOIN accounts a ON a.id = po.account_id
SET po.statement_descriptor = UPPER(LEFT(a.name, 22))
WHERE po.statement_descriptor IS NULL;
