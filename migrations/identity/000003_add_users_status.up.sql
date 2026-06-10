ALTER TABLE users
    ADD COLUMN status ENUM ('active', 'suspended', 'deleted') NOT NULL DEFAULT 'active',
    ADD INDEX idx_users_status (status);
