-- Oracle-MySQL live e2e schema. Diverges from the MariaDB substrate where the
-- native JSON type matters: Oracle MySQL stores JSON as a first-class type
-- (DATA_TYPE='json'), so the json classification is exercised end-to-end here.

DROP TABLE IF EXISTS herbarium_sheet;

CREATE TABLE herbarium_sheet (
  id INT PRIMARY KEY AUTO_INCREMENT,
  status ENUM('active', 'inactive', 'archived') NOT NULL,
  payload JSON NULL,
  label VARCHAR(64) NOT NULL COMMENT 'human-facing specimen label',
  KEY herbarium_sheet_label_idx (label)
) COMMENT='Herbarium sheet catalog';
