CREATE TABLE payouts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    account_id BIGINT UNSIGNED NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    status ENUM ('requested', 'in_transit', 'paid', 'failed') NOT NULL DEFAULT 'requested',
    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TIMESTAMP NULL,
    PRIMARY KEY (id),
    KEY idx_payouts_account (account_id),
    CONSTRAINT fk_payouts_account FOREIGN KEY (account_id) REFERENCES accounts (id)
) ENGINE=InnoDB;
