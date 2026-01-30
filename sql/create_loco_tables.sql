-- ============================================
-- CR Loco Management - Database Schema
-- ============================================

-- 1. Create sheds master table
CREATE TABLE IF NOT EXISTS div_cr_sheds (
    id INT AUTO_INCREMENT PRIMARY KEY,
    shed_code VARCHAR(10) NOT NULL UNIQUE,
    shed_name VARCHAR(100),
    zone VARCHAR(20) DEFAULT 'CR',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Insert default CR sheds
INSERT INTO div_cr_sheds (shed_code, shed_name) VALUES
('AQE', 'Ajni Electric Loco Shed'),
('BSLL', 'Bhusawal Electric Loco Shed'),
('DNDE', 'Daund Electric Loco Shed'),
('KYNE', 'Kalyan Electric Loco Shed'),
('PADX', 'Pune Electric Loco Shed')
ON DUPLICATE KEY UPDATE shed_name = VALUES(shed_name);


-- 2. Create locos master table
CREATE TABLE IF NOT EXISTS div_cr_locos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    loco_number VARCHAR(20) NOT NULL UNIQUE,
    loco_type VARCHAR(20),
    current_shed VARCHAR(20),
    status ENUM('Active', 'Transferred Out', 'Condemned') DEFAULT 'Active',
    commission_date DATE,
    remarks VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_shed (current_shed),
    INDEX idx_status (status),
    INDEX idx_loco_type (loco_type),
    INDEX idx_loco_number (loco_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 3. Create loco transfer history table
CREATE TABLE IF NOT EXISTS div_cr_loco_transfers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    loco_number VARCHAR(20) NOT NULL,
    from_shed VARCHAR(20),
    to_shed VARCHAR(20),
    transfer_date DATE,
    transfer_type ENUM('Internal', 'Incoming', 'Outgoing') DEFAULT 'Internal',
    remarks VARCHAR(255),
    created_by VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_loco (loco_number),
    INDEX idx_date (transfer_date),
    INDEX idx_from_shed (from_shed),
    INDEX idx_to_shed (to_shed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================
-- Verification Queries
-- ============================================

-- Check sheds
-- SELECT * FROM div_cr_sheds;

-- Check locos count by shed
-- SELECT current_shed, COUNT(*) as count FROM div_cr_locos GROUP BY current_shed;

-- Check locos count by type
-- SELECT loco_type, COUNT(*) as count FROM div_cr_locos GROUP BY loco_type ORDER BY count DESC;
