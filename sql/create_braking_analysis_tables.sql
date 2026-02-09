-- ============================================
-- Braking Pattern Analysis - Database Schema
-- ============================================
-- Purpose: Store braking pattern data for nominated halts
-- for fortnightly LP braking behavior comparison
-- ============================================

-- 1. Configuration Table: Nominated Halts
-- Stores which halts should be tracked for braking analysis
CREATE TABLE IF NOT EXISTS div_rtis_nominated_halts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    halt_code VARCHAR(20) NOT NULL,
    halt_name VARCHAR(100),
    section_type ENUM('GHAT', 'PLAIN') NOT NULL,
    section_name VARCHAR(50),
    direction VARCHAR(10),                -- UP, DN, or BOTH
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uniq_halt (halt_code, section_name, direction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Configuration table for nominated braking analysis halts';


-- 2. Braking Runs Table
-- One record per train run that has braking data at nominated halts
CREATE TABLE IF NOT EXISTS div_rtis_braking_runs (
    run_id INT AUTO_INCREMENT PRIMARY KEY,
    analysis_id INT NOT NULL,
    train_number VARCHAR(20),
    date_of_working DATE,
    lp_hrms_id VARCHAR(50),
    lp_name VARCHAR(255),
    alp_hrms_id VARCHAR(50),              -- Assistant Loco Pilot
    alp_name VARCHAR(255),
    loco_number VARCHAR(20),              -- Locomotive number
    from_station VARCHAR(10),
    to_station VARCHAR(10),
    direction VARCHAR(10),
    route VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_date (date_of_working),
    INDEX idx_lp (lp_hrms_id),
    INDEX idx_alp (alp_hrms_id),
    INDEX idx_loco (loco_number),
    INDEX idx_created (created_at),
    FOREIGN KEY (analysis_id) REFERENCES div_rtis_analyses(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Braking analysis runs linked to RTIS analyses';


-- 3. Braking Halts Table
-- Summary data for each halt within a run
CREATE TABLE IF NOT EXISTS div_rtis_braking_halts (
    halt_id INT AUTO_INCREMENT PRIMARY KEY,
    run_id INT NOT NULL,
    halt_code VARCHAR(20) NOT NULL,       -- Station code or ghat halt (T5, TKW, MHLC)
    halt_name VARCHAR(100),               -- Full name (e.g., "NNCN", "Thakurwadi")
    section_type ENUM('GHAT', 'PLAIN') NOT NULL,
    section_name VARCHAR(50),             -- e.g., "IGP-KSRA", "LNL-KJT"
    halt_distance_m FLOAT,                -- Cumulative distance at halt
    halt_time VARCHAR(20),
    speed_2400m FLOAT,
    speed_1000m FLOAT,
    speed_400m FLOAT,
    speed_100m FLOAT,

    FOREIGN KEY (run_id) REFERENCES div_rtis_braking_runs(run_id) ON DELETE CASCADE,
    INDEX idx_halt (halt_code),
    INDEX idx_section_type (section_type),
    INDEX idx_run_halt (run_id, halt_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Halt summary with speeds at key distances';


-- 4. Braking Points Table
-- Individual data points within 2400m approach window
CREATE TABLE IF NOT EXISTS div_rtis_braking_points (
    point_id INT AUTO_INCREMENT PRIMARY KEY,
    run_id INT NOT NULL,
    halt_code VARCHAR(20) NOT NULL,
    seq INT,
    distance_to_halt FLOAT,               -- 0 to 2400m
    speed FLOAT,
    time_str VARCHAR(20),

    FOREIGN KEY (run_id) REFERENCES div_rtis_braking_runs(run_id) ON DELETE CASCADE,
    INDEX idx_run_halt (run_id, halt_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Individual braking data points for Speed vs Distance charts';


-- ============================================
-- Initial Data: Nominated Halts
-- ============================================

-- LNL-KJT section (Lonavala-Karjat ghat) - UP direction only
INSERT IGNORE INTO div_rtis_nominated_halts (halt_code, halt_name, section_type, section_name, direction) VALUES
('T5', 'NNCN/Nagnath', 'GHAT', 'LNL-KJT', 'UP'),
('TKW', 'Thakurwadi', 'GHAT', 'LNL-KJT', 'UP'),
('MHLC', 'Monkey Hill Cabin', 'GHAT', 'LNL-KJT', 'UP');

-- IGP-KSRA section (Igatpuri-Kasara ghat) - UP direction only
INSERT IGNORE INTO div_rtis_nominated_halts (halt_code, halt_name, section_type, section_name, direction) VALUES
('X101', 'X101', 'GHAT', 'IGP-KSRA', 'UP'),
('X102', 'X102', 'GHAT', 'IGP-KSRA', 'UP'),
('X103', 'X103', 'GHAT', 'IGP-KSRA', 'UP');

-- Plain section halts (both directions except LNL)
INSERT IGNORE INTO div_rtis_nominated_halts (halt_code, halt_name, section_type, section_name, direction) VALUES
('DR', 'Dadar', 'PLAIN', 'CSMT-KYN', 'UP'),
('DR', 'Dadar', 'PLAIN', 'CSMT-KYN', 'DN'),
('TNA', 'Thane', 'PLAIN', 'CSMT-KYN', 'UP'),
('TNA', 'Thane', 'PLAIN', 'CSMT-KYN', 'DN'),
('KYN', 'Kalyan', 'PLAIN', 'CSMT-KYN', 'UP'),
('KYN', 'Kalyan', 'PLAIN', 'CSMT-KYN', 'DN'),
('ROHA', 'Roha', 'PLAIN', 'PNVL-RN', 'UP'),
('ROHA', 'Roha', 'PLAIN', 'PNVL-RN', 'DN'),
('LNL', 'Lonavala', 'PLAIN', 'KJT-PUNE', 'UP');


-- ============================================
-- Verification
-- ============================================
SELECT 'Braking analysis tables created:' as '';
SHOW TABLES LIKE 'div_rtis_braking%';
SHOW TABLES LIKE 'div_rtis_nominated%';

SELECT 'Nominated halts inserted:' as '';
SELECT halt_code, halt_name, section_type, section_name, direction
FROM div_rtis_nominated_halts
ORDER BY section_type, section_name, halt_code;
