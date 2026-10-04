ALTER TABLE solarbank_measurements
    ADD COLUMN IF NOT EXISTS grid_to_home_w DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS grid_to_battery_w DOUBLE PRECISION;

INSERT INTO schema_migrations (version) VALUES (2)
ON CONFLICT (version) DO NOTHING;
