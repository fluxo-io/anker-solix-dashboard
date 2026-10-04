CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS solarbank_measurements (
    id BIGSERIAL PRIMARY KEY,
    bucket_start TIMESTAMPTZ NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_updated_at TEXT,

    site_id TEXT NOT NULL,
    device_sn TEXT NOT NULL,
    device_name TEXT,
    device_model TEXT,
    data_valid BOOLEAN NOT NULL DEFAULT TRUE,

    pv_reported_total_w DOUBLE PRECISION,
    pv_calculated_total_w DOUBLE PRECISION,
    pv_1_w DOUBLE PRECISION,
    pv_2_w DOUBLE PRECISION,
    pv_3_w DOUBLE PRECISION,
    pv_4_w DOUBLE PRECISION,

    battery_soc_pct DOUBLE PRECISION,
    battery_power_w DOUBLE PRECISION,
    output_power_w DOUBLE PRECISION,
    home_load_w DOUBLE PRECISION,
    grid_power_w DOUBLE PRECISION,
    grid_import_w DOUBLE PRECISION,
    grid_export_w DOUBLE PRECISION,
    grid_to_home_w DOUBLE PRECISION,
    grid_to_battery_w DOUBLE PRECISION,

    raw_data JSONB NOT NULL,

    CONSTRAINT solarbank_measurements_slot_unique
        UNIQUE (bucket_start, site_id, device_sn)
);

CREATE INDEX IF NOT EXISTS solarbank_measurements_device_time_idx
    ON solarbank_measurements (site_id, device_sn, bucket_start DESC);

CREATE TABLE IF NOT EXISTS collector_state (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    measurements_written BIGINT NOT NULL DEFAULT 0
);

INSERT INTO collector_state (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

COMMENT ON COLUMN solarbank_measurements.battery_power_w IS
    'Positiv = Akku lädt, negativ = Akku entlädt.';

COMMENT ON COLUMN solarbank_measurements.grid_power_w IS
    'Netzbezug minus Einspeisung: positiv = Bezug, negativ = Einspeisung.';

COMMENT ON COLUMN solarbank_measurements.raw_data IS
    'Vollständiger dekodierter Geräte- und Site-Cache des Abrufs.';

INSERT INTO schema_migrations (version) VALUES (1)
ON CONFLICT (version) DO NOTHING;
