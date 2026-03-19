-- ============================================================
-- ARIA — Seed Data: Bangalore Delivery Zones
-- Runs automatically on first postgres container start.
-- 20 real Bangalore zones with approximate centroids.
-- Sister zones = static seed; agent overrides at runtime.
-- ============================================================

INSERT INTO zones (id, name, city, centroid_lat, centroid_lng, area_km2) VALUES
  ('a1000000-0000-0000-0000-000000000001', 'Koramangala 4B',     'Bangalore', 12.9352,  77.6245, 2.1),
  ('a1000000-0000-0000-0000-000000000002', 'Koramangala 1B',     'Bangalore', 12.9279,  77.6271, 1.8),
  ('a1000000-0000-0000-0000-000000000003', 'HSR Layout',         'Bangalore', 12.9116,  77.6389, 4.2),
  ('a1000000-0000-0000-0000-000000000004', 'Indiranagar',        'Bangalore', 12.9784,  77.6408, 3.1),
  ('a1000000-0000-0000-0000-000000000005', 'BTM Layout',         'Bangalore', 12.9165,  77.6101, 3.4),
  ('a1000000-0000-0000-0000-000000000006', 'Marathahalli',       'Bangalore', 12.9591,  77.6972, 5.2),
  ('a1000000-0000-0000-0000-000000000007', 'Whitefield',         'Bangalore', 12.9698,  77.7499, 8.1),
  ('a1000000-0000-0000-0000-000000000008', 'Electronic City',    'Bangalore', 12.8399,  77.6770, 6.3),
  ('a1000000-0000-0000-0000-000000000009', 'Bellandur',          'Bangalore', 12.9257,  77.6763, 3.8),
  ('a1000000-0000-0000-0000-000000000010', 'Sarjapur Road',      'Bangalore', 12.9061,  77.6871, 4.5),
  ('a1000000-0000-0000-0000-000000000011', 'Jayanagar',          'Bangalore', 12.9250,  77.5938, 3.2),
  ('a1000000-0000-0000-0000-000000000012', 'JP Nagar',           'Bangalore', 12.9102,  77.5833, 4.1),
  ('a1000000-0000-0000-0000-000000000013', 'Bannerghatta Road',  'Bangalore', 12.8782,  77.5975, 5.0),
  ('a1000000-0000-0000-0000-000000000014', 'Yelahanka',          'Bangalore', 13.1004,  77.5963, 7.2),
  ('a1000000-0000-0000-0000-000000000015', 'Hebbal',             'Bangalore', 13.0350,  77.5970, 4.4),
  ('a1000000-0000-0000-0000-000000000016', 'Rajajinagar',        'Bangalore', 12.9906,  77.5530, 3.6),
  ('a1000000-0000-0000-0000-000000000017', 'Malleswaram',        'Bangalore', 13.0025,  77.5701, 2.9),
  ('a1000000-0000-0000-0000-000000000018', 'Banashankari',       'Bangalore', 12.9256,  77.5468, 3.7),
  ('a1000000-0000-0000-0000-000000000019', 'Yeshwantpur',        'Bangalore', 13.0236,  77.5396, 4.0),
  ('a1000000-0000-0000-0000-000000000020', 'MG Road',            'Bangalore', 12.9747,  77.6074, 1.5)
ON CONFLICT DO NOTHING;

-- Static sister zone seeds — within ~5–7km of each other
-- Format: zone gets its 2-3 nearest high-density neighbours
UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000002'::uuid,
  'a1000000-0000-0000-0000-000000000003'::uuid,
  'a1000000-0000-0000-0000-000000000005'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000001';  -- Koramangala 4B

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000001'::uuid,
  'a1000000-0000-0000-0000-000000000005'::uuid,
  'a1000000-0000-0000-0000-000000000020'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000002';  -- Koramangala 1B

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000001'::uuid,
  'a1000000-0000-0000-0000-000000000005'::uuid,
  'a1000000-0000-0000-0000-000000000009'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000003';  -- HSR Layout

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000020'::uuid,
  'a1000000-0000-0000-0000-000000000006'::uuid,
  'a1000000-0000-0000-0000-000000000001'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000004';  -- Indiranagar

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000001'::uuid,
  'a1000000-0000-0000-0000-000000000002'::uuid,
  'a1000000-0000-0000-0000-000000000011'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000005';  -- BTM Layout

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000007'::uuid,
  'a1000000-0000-0000-0000-000000000009'::uuid,
  'a1000000-0000-0000-0000-000000000004'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000006';  -- Marathahalli

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000006'::uuid,
  'a1000000-0000-0000-0000-000000000010'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000007';  -- Whitefield (distant — smaller sister list)

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000010'::uuid,
  'a1000000-0000-0000-0000-000000000013'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000008';  -- Electronic City

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000006'::uuid,
  'a1000000-0000-0000-0000-000000000003'::uuid,
  'a1000000-0000-0000-0000-000000000010'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000009';  -- Bellandur

UPDATE zones SET sister_zone_ids = ARRAY[
  'a1000000-0000-0000-0000-000000000009'::uuid,
  'a1000000-0000-0000-0000-000000000008'::uuid,
  'a1000000-0000-0000-0000-000000000003'::uuid
] WHERE id = 'a1000000-0000-0000-0000-000000000010';  -- Sarjapur Road
