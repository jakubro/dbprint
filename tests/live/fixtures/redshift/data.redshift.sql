-- Seed rows for the live herbarium/herbarium_sheet e2e. loan_request stays empty by design.

INSERT INTO herbarium (id, code, name) VALUES
  (1, 'ASHGROVE', 'Ashgrove'),
  (2, 'THORNFIELD', 'Thornfield');

INSERT INTO herbarium_sheet (id, herbarium_id, status, label, logged_at) VALUES
  (1, 1, 'active',   'alpha', '2026-01-01 00:00:00'),
  (2, 1, 'active',   'beta',  '2026-01-02 00:00:00'),
  (3, 2, 'inactive', 'gamma', '2026-01-03 00:00:00'),
  (4, NULL, 'archived', 'delta', '2026-01-04 00:00:00');

-- (trial_id, reading_no) is unique across all 6 rows; seed_count is constant, so it prunes out
-- of the grain search entirely and cannot compete with the intended pair.
INSERT INTO germination_reading (trial_id, reading_no, seed_count) VALUES
  (1, 1, 100),
  (1, 2, 100),
  (2, 1, 100),
  (2, 2, 100),
  (3, 1, 100),
  (3, 2, 100);
