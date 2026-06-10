CREATE TABLE journal_lines (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    ledger_entry_id BIGINT UNSIGNED NOT NULL,
    line_no INT NOT NULL,
    memo VARCHAR(255) NULL,
    amount_minor BIGINT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_journal_lines_entry_line (ledger_entry_id, line_no),
    CONSTRAINT fk_journal_lines_entry FOREIGN KEY (ledger_entry_id) REFERENCES ledger_entries (id) ON DELETE CASCADE
) ENGINE=InnoDB;
