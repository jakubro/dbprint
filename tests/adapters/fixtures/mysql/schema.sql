-- MySQL adapter test substrate schema (wire-compatible with MariaDB + Oracle MySQL).
-- Exercises AUTO_INCREMENT, ENUM, JSON, a secondary index, and table/column comments.

CREATE TABLE herbarium_sheet (
  id INT PRIMARY KEY AUTO_INCREMENT,
  status ENUM('active', 'inactive', 'archived') NOT NULL,
  payload JSON NULL,
  label VARCHAR(64) NOT NULL COMMENT 'human-facing specimen label',
  KEY herbarium_sheet_label_idx (label)
) COMMENT='Herbarium sheet catalog';
