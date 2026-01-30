-- ============================================
-- Daily Summary - Database Schema
-- ============================================

-- 1. Add BFT/BPT status columns to div_rtis_analyses
-- Check and add bft_status column
SET @exist := (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'div_rtis_analyses'
               AND COLUMN_NAME = 'bft_status');

SET @query = IF(@exist = 0,
    'ALTER TABLE div_rtis_analyses ADD COLUMN bft_status ENUM(''PASS'', ''FAIL'', ''NOT RUN'') DEFAULT ''NOT RUN''',
    'SELECT "bft_status column already exists"');

PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check and add bpt_status column
SET @exist := (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'div_rtis_analyses'
               AND COLUMN_NAME = 'bpt_status');

SET @query = IF(@exist = 0,
    'ALTER TABLE div_rtis_analyses ADD COLUMN bpt_status ENUM(''PASS'', ''FAIL'', ''NOT RUN'') DEFAULT ''NOT RUN''',
    'SELECT "bpt_status column already exists"');

PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- 2. Create daily entries table for SIM Down / NON RTIS
CREATE TABLE IF NOT EXISTS div_rtis_daily_entries (
    id INT AUTO_INCREMENT PRIMARY KEY,

    -- Date and Status
    working_date DATE NOT NULL,
    rtis_status ENUM('SIM Down', 'NON RTIS') NOT NULL,

    -- Train/Loco Details
    train_number VARCHAR(20) NOT NULL,
    loco_number VARCHAR(20) NOT NULL,
    from_station VARCHAR(50),
    to_station VARCHAR(50),
    departure_time TIME,
    arrival_time TIME,

    -- Crew Details
    lp_name VARCHAR(100),
    lp_hrms_id VARCHAR(20),
    ncli_name VARCHAR(100),
    alp_name VARCHAR(100),
    alp_hrms_id VARCHAR(20),
    ncli_alp_name VARCHAR(100),

    -- Metadata
    entered_by VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Indexes
    INDEX idx_working_date (working_date),
    INDEX idx_rtis_status (rtis_status),
    INDEX idx_loco_number (loco_number),
    INDEX idx_train_number (train_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 3. Create weekly SIM down tracking table
CREATE TABLE IF NOT EXISTS div_rtis_sim_down_weekly (
    id INT AUTO_INCREMENT PRIMARY KEY,

    -- Week Reference
    week_start_date DATE NOT NULL,
    week_end_date DATE NOT NULL,

    -- Loco Details
    loco_number VARCHAR(20) NOT NULL,
    sim_down_count INT DEFAULT 0,

    -- Dates when SIM was down (JSON array)
    sim_down_dates JSON,

    -- Report metadata
    report_generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_to_officers BOOLEAN DEFAULT FALSE,
    sent_at TIMESTAMP NULL,

    INDEX idx_week_dates (week_start_date, week_end_date),
    INDEX idx_loco_number (loco_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 4. Add ncli_alp_name column if it doesn't exist
-- Run this ALTER statement if the table already exists
ALTER TABLE div_rtis_daily_entries ADD COLUMN IF NOT EXISTS ncli_alp_name VARCHAR(100) AFTER alp_hrms_id;


-- ============================================
-- Verification
-- ============================================
SELECT 'Checking div_rtis_analyses columns:' as '';
SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
AND TABLE_NAME = 'div_rtis_analyses'
AND COLUMN_NAME IN ('bft_status', 'bpt_status');

SELECT 'Tables created:' as '';
SHOW TABLES LIKE 'div_rtis_daily%';
SHOW TABLES LIKE 'div_rtis_sim%';
