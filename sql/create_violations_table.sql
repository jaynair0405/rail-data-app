-- ============================================
-- Daily Speed Violations Report - Database Schema
-- ============================================

-- 1. Create the violations table
-- This table stores speed violations detected during RTIS analysis

CREATE TABLE IF NOT EXISTS div_rtis_violations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    analysis_id INT NOT NULL,
    working_date DATE,
    train_number VARCHAR(20),
    loco_number VARCHAR(20),

    -- LP Details
    lp_name VARCHAR(100),
    lp_hrms_id VARCHAR(20),
    ncli_name VARCHAR(100),

    -- ALP Details
    alp_name VARCHAR(100),
    alp_hrms_id VARCHAR(20),
    ncli_alp_name VARCHAR(100),

    -- Violation Details
    violation_type ENUM('1000m_zone_b', '500m_zone_a', 'ghat') NOT NULL,
    speed DECIMAL(5,1) NOT NULL,
    threshold DECIMAL(5,1) NOT NULL,
    halt_station VARCHAR(50),
    zone VARCHAR(20),

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Indexes for faster querying
    INDEX idx_working_date (working_date),
    INDEX idx_violation_type (violation_type),
    INDEX idx_analysis_id (analysis_id),
    INDEX idx_lp_hrms_id (lp_hrms_id),

    -- Foreign key to analyses table
    FOREIGN KEY (analysis_id) REFERENCES div_rtis_analyses(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 2. Add ALP columns to existing analyses table (if not already present)
-- Run these ALTER statements if the columns don't exist

-- Check and add alp_hrms_id column
SET @exist := (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'div_rtis_analyses'
               AND COLUMN_NAME = 'alp_hrms_id');

SET @query = IF(@exist = 0,
    'ALTER TABLE div_rtis_analyses ADD COLUMN alp_hrms_id VARCHAR(20) AFTER ncli_name',
    'SELECT "alp_hrms_id column already exists"');

PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check and add alp_name column
SET @exist := (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'div_rtis_analyses'
               AND COLUMN_NAME = 'alp_name');

SET @query = IF(@exist = 0,
    'ALTER TABLE div_rtis_analyses ADD COLUMN alp_name VARCHAR(100) AFTER alp_hrms_id',
    'SELECT "alp_name column already exists"');

PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check and add ncli_alp_name column
SET @exist := (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'div_rtis_analyses'
               AND COLUMN_NAME = 'ncli_alp_name');

SET @query = IF(@exist = 0,
    'ALTER TABLE div_rtis_analyses ADD COLUMN ncli_alp_name VARCHAR(100) AFTER alp_name',
    'SELECT "ncli_alp_name column already exists"');

PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- ============================================
-- SIMPLIFIED VERSION (if the above doesn't work)
-- ============================================
-- If the dynamic SQL above doesn't work in your MySQL version,
-- use these direct ALTER statements instead (they will fail silently if columns exist):

-- ALTER TABLE div_rtis_analyses ADD COLUMN alp_hrms_id VARCHAR(20) AFTER ncli_name;
-- ALTER TABLE div_rtis_analyses ADD COLUMN alp_name VARCHAR(100) AFTER alp_hrms_id;
-- ALTER TABLE div_rtis_analyses ADD COLUMN ncli_alp_name VARCHAR(100) AFTER alp_name;


-- ============================================
-- Sample Queries for Reports
-- ============================================

-- Get all violations for a specific date
-- SELECT * FROM div_rtis_violations WHERE DATE(working_date) = '2024-01-25' ORDER BY violation_type;

-- Get violation counts by type for a date
-- SELECT violation_type, COUNT(*) as count
-- FROM div_rtis_violations
-- WHERE DATE(working_date) = '2024-01-25'
-- GROUP BY violation_type;

-- Get violations by LP
-- SELECT lp_name, lp_hrms_id, COUNT(*) as violation_count
-- FROM div_rtis_violations
-- WHERE working_date BETWEEN '2024-01-01' AND '2024-01-31'
-- GROUP BY lp_hrms_id, lp_name
-- ORDER BY violation_count DESC;
