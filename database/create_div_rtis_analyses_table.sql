-- ============================================================
-- Table: div_rtis_analyses
-- Purpose: Store RTIS analysis records with full metadata
-- Created: 2025-12-22
-- ============================================================

CREATE TABLE IF NOT EXISTS div_rtis_analyses (
    -- Primary Key
    id INT AUTO_INCREMENT PRIMARY KEY,

    -- User Information
    user_id INT NOT NULL,
    lp_hrms_id VARCHAR(50) DEFAULT NULL,
    lp_name VARCHAR(255) DEFAULT NULL,
    ncli_id VARCHAR(50) DEFAULT NULL,
    ncli_name VARCHAR(255) DEFAULT NULL,
    analyst_id VARCHAR(50) DEFAULT NULL,
    analyst_name VARCHAR(255) DEFAULT NULL,

    -- Analysis Dates
    analysis_date DATE NOT NULL COMMENT 'Date when analysis was performed',
    working_date DATE DEFAULT NULL COMMENT 'Date of actual train run',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Train & Route Information
    train_number VARCHAR(20) DEFAULT NULL,
    from_station VARCHAR(10) DEFAULT NULL,
    to_station VARCHAR(10) DEFAULT NULL,
    direction VARCHAR(10) DEFAULT NULL COMMENT 'UP/DOWN/BOTH',
    route VARCHAR(50) DEFAULT NULL COMMENT 'e.g., KJM-PUNE, KYN-IGP',

    -- Loco & Coach Information
    loco_number VARCHAR(20) DEFAULT NULL,
    coach_type VARCHAR(50) DEFAULT NULL COMMENT 'e.g., BOXNHL, FREIGHT',
    load_type VARCHAR(50) DEFAULT NULL COMMENT 'e.g., Loaded, Empty',
    brake_position VARCHAR(20) DEFAULT NULL COMMENT 'e.g., A9, G',

    -- File Information
    csv_filename VARCHAR(255) NOT NULL,
    csv_file_size INT DEFAULT NULL COMMENT 'Size in bytes',
    pdf_filename VARCHAR(255) DEFAULT NULL COMMENT 'Generated PDF report filename',

    -- Analysis Results (Summary Metrics)
    total_distance DECIMAL(10, 3) DEFAULT NULL COMMENT 'Total distance in km',
    total_duration INT DEFAULT NULL COMMENT 'Total duration in seconds',
    max_speed DECIMAL(6, 2) DEFAULT NULL COMMENT 'Maximum speed in km/h',
    avg_speed DECIMAL(6, 2) DEFAULT NULL COMMENT 'Average speed in km/h',
    halt_count INT DEFAULT 0 COMMENT 'Number of halts detected',
    braking_events_count INT DEFAULT 0 COMMENT 'Number of braking events analyzed',

    -- Additional Data
    notes TEXT DEFAULT NULL COMMENT 'User notes or remarks',
    metadata JSON DEFAULT NULL COMMENT 'Additional JSON metadata (criteria, sections, etc.)',

    -- Indexes for Performance
    INDEX idx_user_id (user_id),
    INDEX idx_analysis_date (analysis_date),
    INDEX idx_working_date (working_date),
    INDEX idx_lp_hrms_id (lp_hrms_id),
    INDEX idx_train_number (train_number),
    INDEX idx_loco_number (loco_number),
    INDEX idx_route (route),
    INDEX idx_from_to (from_station, to_station),
    INDEX idx_created_at (created_at),

    -- Unique constraint for race protection (one analysis per trip)
    CONSTRAINT uniq_analysis_trip
        UNIQUE (working_date, train_number, from_station, to_station, direction, route, loco_number),

    -- Foreign Key Constraint
    CONSTRAINT fk_user_id FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='RTIS Analysis Records - stores complete analysis metadata and results';
