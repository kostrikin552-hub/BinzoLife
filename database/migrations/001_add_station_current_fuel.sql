-- ============================================================
-- Миграция: Добавление радара наличия и очередей
-- ============================================================

-- Таблица актуального состояния топлива на АЗС
CREATE TABLE IF NOT EXISTS station_current_fuel (
    station_id BIGINT NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    fuel_type VARCHAR(16) NOT NULL,
    price NUMERIC(10, 2),
    availability VARCHAR(16) NOT NULL DEFAULT 'unknown',  -- 'available', 'limited', 'unavailable', 'unknown'
    queue_level VARCHAR(16) DEFAULT 'unknown',            -- 'low', 'medium', 'high', 'unknown'
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source VARCHAR(32) NOT NULL,                          -- 'gdebenz', 'benzuber', 'tatneft', 'gpn', 'user'
    confidence NUMERIC(5, 2) NOT NULL DEFAULT 0,
    PRIMARY KEY (station_id, fuel_type)
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_station_fuel_avail ON station_current_fuel (station_id, fuel_type, availability);
CREATE INDEX IF NOT EXISTS idx_station_fuel_observed ON station_current_fuel (observed_at DESC);

-- Таблица для внешних ID источников (чтобы не дублировать станции)
CREATE TABLE IF NOT EXISTS station_external_ids (
    station_id BIGINT NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    source VARCHAR(32) NOT NULL,
    external_id VARCHAR(100) NOT NULL,
    PRIMARY KEY (station_id, source)
);
CREATE INDEX IF NOT EXISTS idx_external_id_lookup ON station_external_ids (source, external_id);

-- Комментарии
COMMENT ON TABLE station_current_fuel IS 'Актуальное состояние топлива на АЗС из внешних источников';
COMMENT ON COLUMN station_current_fuel.availability IS 'available, limited, unavailable, unknown';
COMMENT ON COLUMN station_current_fuel.queue_level IS 'low, medium, high, unknown';
