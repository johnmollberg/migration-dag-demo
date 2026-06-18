ALTER TABLE users ADD COLUMN last_payment_at TIMESTAMP NULL;

-- Backfill from payment history. This is the bug this repo exists to catch:
-- identity is the ROOT of the DAG, and payments is a descendant. At identity's
-- position in the bootstrap order the payments table does not exist yet.
UPDATE users u
JOIN (
    SELECT account_ref.user_id, MAX(p.created_at) AS last_paid_at
    FROM payments p
    JOIN accounts account_ref ON account_ref.id = p.account_id
    WHERE p.status = 'settled'
    GROUP BY account_ref.user_id
) latest ON latest.user_id = u.id
SET u.last_payment_at = latest.last_paid_at;
