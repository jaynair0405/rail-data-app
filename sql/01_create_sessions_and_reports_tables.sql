-- ============================================
-- Authentication Integration - Database Setup
-- ============================================
-- Run this on LOCAL MySQL first for testing
-- Then run on SERVER MySQL for production
--
-- Database: bbtro
-- ============================================

USE bbtro;

-- 1. Sessions Table (for express-session-mysql)
-- Allows both Node.js and Python to share authentication
CREATE TABLE IF NOT EXISTS sessions (
    session_id VARCHAR(128) NOT NULL PRIMARY KEY,
    expires BIGINT(20) UNSIGNED NOT NULL,
    data MEDIUMTEXT,
    INDEX expires_idx (expires)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. RTIS Reports Storage
-- Stores metadata about generated analysis reports
CREATE TABLE IF NOT EXISTS rtis_reports (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    train_number VARCHAR(10),
    lp_name VARCHAR(100),
    from_station VARCHAR(10),
    to_station VARCHAR(10),
    direction VARCHAR(10),
    analysis_date DATETIME,
    pdf_filename VARCHAR(255),
    pdf_storage_path TEXT,
    csv_filename VARCHAR(255),
    file_size_bytes BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_user (user_id),
    INDEX idx_train (train_number),
    INDEX idx_date (analysis_date),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Add RTIS Access Permission to Users Table
-- Add column if it doesn't exist (safe for existing tables)
SET @dbname = DATABASE();
SET @tablename = 'users';
SET @columnname = 'can_access_rtis';
SET @preparedStatement = (SELECT IF(
  (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE
      TABLE_SCHEMA = @dbname
      AND TABLE_NAME = @tablename
      AND COLUMN_NAME = @columnname
  ) > 0,
  'SELECT 1', -- Column exists, do nothing
  CONCAT('ALTER TABLE ', @tablename, ' ADD COLUMN ', @columnname, ' BOOLEAN DEFAULT FALSE COMMENT ''Permission to access RTIS analysis tool''')
));
PREPARE alterIfNotExists FROM @preparedStatement;
EXECUTE alterIfNotExists;
DEALLOCATE PREPARE alterIfNotExists;

-- 4. Grant RTIS access to specific users (EXAMPLES - modify as needed)
-- Uncomment and modify these lines based on your requirements:

-- Option A: Grant access to all division admins
-- UPDATE users SET can_access_rtis = TRUE WHERE div_role = 'admin' AND realm = 'division';

-- Option B: Grant access to specific offices
-- UPDATE users SET can_access_rtis = TRUE WHERE div_office_code IN ('IGP', 'CSMT', 'PUNE') AND realm = 'division';

-- Option C: Grant access to specific users by username
-- UPDATE users SET can_access_rtis = TRUE WHERE username IN ('admin', 'rtis_user1', 'rtis_user2');

-- ============================================
-- Verification Queries (run after setup)
-- ============================================

-- Check if tables were created
SELECT
    TABLE_NAME,
    ENGINE,
    TABLE_ROWS,
    CREATE_TIME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'bbtro'
    AND TABLE_NAME IN ('sessions', 'rtis_reports')
ORDER BY TABLE_NAME;

-- Check if column was added
SELECT
    COLUMN_NAME,
    COLUMN_TYPE,
    COLUMN_DEFAULT,
    IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'bbtro'
    AND TABLE_NAME = 'users'
    AND COLUMN_NAME = 'can_access_rtis';

-- Show users with RTIS access
SELECT
    id,
    username,
    full_name,
    realm,
    div_role,
    div_office_code,
    can_access_rtis
FROM users
WHERE can_access_rtis = TRUE;
