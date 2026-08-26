-- Seed rows for the live herbarium_sheet e2e. Multiple rows make the volatile
-- AUTO_INCREMENT=<N> table-option counter appear in SHOW CREATE TABLE.

INSERT INTO herbarium_sheet (status, payload, label) VALUES
  ('active',   '{"k": 1}', 'alpha'),
  ('active',   NULL,       'beta'),
  ('inactive', '{"k": 2}', 'gamma'),
  ('archived', NULL,       'delta');
