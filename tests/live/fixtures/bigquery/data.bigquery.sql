-- Seed rows for the live herbarium/herbarium_sheet e2e. loan_request stays empty by design.

INSERT INTO herbarium (id, code, name) VALUES
  (1, 'ASHGROVE', 'Ashgrove'),
  (2, 'THORNFIELD', 'Thornfield');

INSERT INTO herbarium_sheet (id, herbarium_id, status, label, logged_at) VALUES
  (1, 1, 'active', 'alpha', TIMESTAMP '2026-01-01 00:00:00'),
  (2, 1, 'active', 'beta', TIMESTAMP '2026-01-02 00:00:00'),
  (3, 2, 'inactive', 'gamma', TIMESTAMP '2026-01-03 00:00:00'),
  (4, NULL, 'archived', 'delta', TIMESTAMP '2026-01-04 00:00:00');
