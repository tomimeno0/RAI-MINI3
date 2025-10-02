# Windows Application Inventory Schema

## ERD (ASCII)
```
+-----------+        +---------------+          +-------------+
|  hosts    |1      n|    scans      |1        n|  scan_items |
+-----------+--------+---------------+----------+-------------+
| id PK     |        | id PK         |          | id PK       |
| hostname U|        | host_id FK    |          | scan_id FK  |
| ...       |        | started_at    |          | host_id FK  |
+-----------+        | completed_at  |          | app_catalog |
                    +---------------+          | package/bin |
                                                   |1
                                                   |
                                                   v
                                             +-------------+
                                             | installs    |
                                             +-------------+
                                             | id PK       |
                                             | host_id FK  |
                         +---------------+   | app_catalog |
                         | apps_catalog |1  n| package/bin |
                         +---------------+   | status      |
                         | id PK         |   +-------------+
                         | normalized    |          |
                         | publisher     |          |1
                         +---------------+          v
                                             +-------------+
                                             |security_find|
                                             +-------------+
```

## DDL SQLite

### 001_init.sql
```sql
-- Hosts discovered via scans; unique by hostname.
CREATE TABLE hosts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,          -- surrogate identifier
    hostname        TEXT NOT NULL UNIQUE,                       -- unique machine hostname
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- record creation timestamp
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP -- last modification timestamp
);

-- Catalog of logical applications, normalized by name/publisher.
CREATE TABLE apps_catalog (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,      -- surrogate identifier
    display_name        TEXT NOT NULL,                          -- original application name
    normalized_name     TEXT NOT NULL,                          -- lower/trimmed name for matching
    publisher           TEXT NOT NULL DEFAULT '',               -- normalized publisher name
    name_hash           TEXT GENERATED ALWAYS AS (              -- deterministic key for caching
        normalized_name || '::' || publisher
    ) VIRTUAL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- record creation timestamp
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- last modification timestamp
    UNIQUE (normalized_name, publisher)
);

-- UWP package metadata deduplicated globally.
CREATE TABLE packages (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,  -- surrogate identifier
    package_fullname        TEXT NOT NULL UNIQUE,               -- Windows PackageFullName
    package_name            TEXT NOT NULL,                      -- short UWP package name
    publisher               TEXT,                               -- publisher from manifest
    version                 TEXT,                               -- semantic version reported
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- record creation timestamp
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP -- last modification timestamp
);

-- Executable or shortcut binary metadata.
CREATE TABLE binaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,          -- surrogate identifier
    exe_path        TEXT NOT NULL UNIQUE,                       -- absolute executable or shortcut path
    target_path     TEXT,                                       -- resolved target for shortcuts
    icon_path       TEXT,                                       -- icon resource path
    file_hash       TEXT,                                       -- sha256 or similar fingerprint
    version         TEXT,                                       -- product/file version
    publisher       TEXT,                                       -- signer or vendor reported
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- record creation timestamp
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP -- last modification timestamp
);

-- Logical installations (current + historical) per host/app/package/binary.
CREATE TABLE installs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,      -- surrogate identifier
    host_id             INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE, -- owning host
    app_catalog_id      INTEGER NOT NULL REFERENCES apps_catalog(id) ON DELETE CASCADE, -- normalized app
    package_id          INTEGER REFERENCES packages(id) ON DELETE SET NULL, -- link to UWP package
    binary_id           INTEGER REFERENCES binaries(id) ON DELETE SET NULL, -- link to executable/shortcut
    source              TEXT NOT NULL CHECK (source IN ('uwp', 'exe', 'shortcut')), -- origin of detection
    first_seen_at       DATETIME NOT NULL,                      -- timestamp when install was first detected
    last_seen_at        DATETIME NOT NULL,                      -- last scan timestamp where it appeared
    removed_at          DATETIME,                               -- timestamp when removal was detected
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)), -- active flag for current state
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- record creation timestamp
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- last modification timestamp
    CHECK (
        (package_id IS NOT NULL AND binary_id IS NULL) OR
        (package_id IS NULL AND binary_id IS NOT NULL)
    )
);

-- Scan sessions per host.
CREATE TABLE scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,          -- surrogate identifier
    host_id         INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE, -- host scanned
    started_at      DATETIME NOT NULL,                          -- scan start timestamp
    completed_at    DATETIME,                                   -- scan completion timestamp
    total_items     INTEGER NOT NULL DEFAULT 0,                 -- number of items processed
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP -- record creation timestamp
);

-- Raw items reported during a scan.
CREATE TABLE scan_items (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,  -- surrogate identifier
    scan_id                 INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE, -- parent scan
    host_id                 INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE, -- redundant host reference for filtering
    app_catalog_id          INTEGER NOT NULL REFERENCES apps_catalog(id) ON DELETE CASCADE, -- normalized app reference
    package_id              INTEGER REFERENCES packages(id) ON DELETE SET NULL, -- package match when source=uwp
    binary_id               INTEGER REFERENCES binaries(id) ON DELETE SET NULL, -- binary match when source exe/shortcut
    source                  TEXT NOT NULL CHECK (source IN ('uwp', 'exe', 'shortcut')), -- origin reported by scanner
    raw_name                TEXT NOT NULL,                        -- raw application name from payload
    raw_version             TEXT,                                -- raw version string
    raw_publisher           TEXT,                                -- raw publisher string
    exe_path                TEXT,                                -- raw path for executables/shortcuts
    uwp_package_fullname    TEXT,                                -- raw UWP PackageFullName
    icon_path               TEXT,                                -- icon path provided by scanner
    file_hash               TEXT,                                -- SHA fingerprint from payload
    security_flag           INTEGER NOT NULL DEFAULT 0 CHECK (security_flag IN (0,1)), -- scanner security flag
    security_reason         TEXT,                                -- optional reason for flag
    scanned_at              DATETIME NOT NULL,                   -- detection timestamp from scanner
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- record creation timestamp
    item_key                TEXT GENERATED ALWAYS AS (COALESCE(exe_path, uwp_package_fullname)) STORED, -- deduplication key
    UNIQUE (scan_id, item_key)
);

-- Security findings tied to installs or specific scan items.
CREATE TABLE security_findings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,      -- surrogate identifier
    install_id          INTEGER REFERENCES installs(id) ON DELETE CASCADE, -- related install when available
    scan_item_id        INTEGER REFERENCES scan_items(id) ON DELETE CASCADE, -- originating scan item
    flag_type           TEXT NOT NULL,                          -- classifier of security issue
    flag_value          TEXT,                                   -- contextual value (path/hash)
    reason              TEXT,                                   -- human-readable explanation
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- timestamp of finding
    CHECK (
        (install_id IS NOT NULL) OR (scan_item_id IS NOT NULL)
    )
);
```

### 002_indexes.sql
```sql
CREATE INDEX idx_hosts_hostname ON hosts(hostname);
CREATE INDEX idx_apps_catalog_norm ON apps_catalog(normalized_name);
CREATE INDEX idx_apps_catalog_publisher ON apps_catalog(publisher);
CREATE UNIQUE INDEX idx_installs_host_package ON installs(host_id, package_id) WHERE package_id IS NOT NULL;
CREATE UNIQUE INDEX idx_installs_host_binary ON installs(host_id, binary_id) WHERE binary_id IS NOT NULL;
CREATE INDEX idx_installs_active ON installs(host_id, is_active) WHERE is_active = 1;
CREATE INDEX idx_binaries_publisher ON binaries(publisher);
CREATE INDEX idx_binaries_hash ON binaries(file_hash);
CREATE INDEX idx_packages_publisher ON packages(publisher);
CREATE INDEX idx_scan_items_host ON scan_items(host_id);
CREATE INDEX idx_scan_items_name ON scan_items(raw_name);
CREATE INDEX idx_scan_items_publisher ON scan_items(raw_publisher);
CREATE INDEX idx_security_findings_created ON security_findings(created_at);
```

### 003_views_triggers.sql
```sql
-- View of current active installs with metadata.
CREATE VIEW v_current_installs AS
SELECT
    i.id AS install_id,
    h.hostname,
    a.display_name,
    a.publisher,
    i.source,
    i.first_seen_at,
    i.last_seen_at,
    p.package_fullname,
    b.exe_path,
    b.file_hash,
    b.version AS binary_version,
    p.version AS package_version
FROM installs i
JOIN hosts h ON h.id = i.host_id
JOIN apps_catalog a ON a.id = i.app_catalog_id
LEFT JOIN packages p ON p.id = i.package_id
LEFT JOIN binaries b ON b.id = i.binary_id
WHERE i.is_active = 1 AND i.removed_at IS NULL;

-- View of security alerts linked to installs/items.
CREATE VIEW v_security_alerts AS
SELECT
    sf.id AS finding_id,
    sf.created_at,
    sf.flag_type,
    sf.flag_value,
    sf.reason,
    h.hostname,
    a.display_name,
    i.source,
    si.scanned_at
FROM security_findings sf
LEFT JOIN installs i ON i.id = sf.install_id
LEFT JOIN hosts h ON h.id = i.host_id
LEFT JOIN apps_catalog a ON a.id = i.app_catalog_id
LEFT JOIN scan_items si ON si.id = sf.scan_item_id;

-- View with the latest scan per host.
CREATE VIEW v_latest_scan_per_host AS
SELECT s.*
FROM scans s
JOIN (
    SELECT host_id, MAX(started_at) AS max_started
    FROM scans
    GROUP BY host_id
) latest ON latest.host_id = s.host_id AND latest.max_started = s.started_at;

-- Trigger to normalize display names and publisher on insert.
CREATE TRIGGER trg_apps_catalog_normalize_ins
AFTER INSERT ON apps_catalog
FOR EACH ROW
BEGIN
    UPDATE apps_catalog
    SET normalized_name = lower(trim(display_name)),
        publisher = trim(publisher)
    WHERE id = NEW.id;
END;

-- Trigger to enforce normalization on updates.
CREATE TRIGGER trg_apps_catalog_normalize_check
BEFORE UPDATE ON apps_catalog
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN lower(trim(NEW.display_name)) != NEW.normalized_name THEN
            RAISE(ABORT, 'normalized_name must equal lower(trim(display_name))')
    END;
    SELECT CASE
        WHEN trim(NEW.publisher) != NEW.publisher THEN
            RAISE(ABORT, 'publisher must be trimmed')
    END;
END;

-- Trigger to upsert installs when scan items arrive.
CREATE TRIGGER trg_scan_items_after_insert
AFTER INSERT ON scan_items
FOR EACH ROW
BEGIN
    -- Update existing install for packages.
    UPDATE installs
    SET last_seen_at = NEW.scanned_at,
        removed_at = NULL,
        is_active = 1,
        source = NEW.source,
        updated_at = CURRENT_TIMESTAMP
    WHERE host_id = NEW.host_id
      AND package_id = NEW.package_id
      AND NEW.package_id IS NOT NULL;

    -- Update existing install for binaries.
    UPDATE installs
    SET last_seen_at = NEW.scanned_at,
        removed_at = NULL,
        is_active = 1,
        source = NEW.source,
        updated_at = CURRENT_TIMESTAMP
    WHERE host_id = NEW.host_id
      AND binary_id = NEW.binary_id
      AND NEW.binary_id IS NOT NULL;

    -- Insert new install if package not found.
    INSERT INTO installs (host_id, app_catalog_id, package_id, binary_id, source,
                          first_seen_at, last_seen_at, removed_at, is_active)
    SELECT NEW.host_id, NEW.app_catalog_id, NEW.package_id, NULL, NEW.source,
           NEW.scanned_at, NEW.scanned_at, NULL, 1
    WHERE NEW.package_id IS NOT NULL
      AND NOT EXISTS (
            SELECT 1 FROM installs
            WHERE host_id = NEW.host_id AND package_id = NEW.package_id
        );

    -- Insert new install if binary not found.
    INSERT INTO installs (host_id, app_catalog_id, package_id, binary_id, source,
                          first_seen_at, last_seen_at, removed_at, is_active)
    SELECT NEW.host_id, NEW.app_catalog_id, NULL, NEW.binary_id, NEW.source,
           NEW.scanned_at, NEW.scanned_at, NULL, 1
    WHERE NEW.binary_id IS NOT NULL
      AND NOT EXISTS (
            SELECT 1 FROM installs
            WHERE host_id = NEW.host_id AND binary_id = NEW.binary_id
        );

    UPDATE scans SET total_items = total_items + 1 WHERE id = NEW.scan_id;

    -- Optional security heuristic for suspicious paths.
    INSERT INTO security_findings (scan_item_id, flag_type, flag_value, reason)
    SELECT NEW.id, 'path_watch', NEW.exe_path,
           'Executable located in monitored directory'
    WHERE NEW.security_flag = 0 AND NEW.exe_path IS NOT NULL AND (
        lower(NEW.exe_path) LIKE '%\\temp\\%'
        OR lower(NEW.exe_path) LIKE '%\\downloads\\%'
    );
END;

-- Trigger to convert scanner security flag into findings.
CREATE TRIGGER trg_scan_items_security_flag
AFTER INSERT ON scan_items
WHEN NEW.security_flag = 1
BEGIN
    INSERT INTO security_findings (scan_item_id, flag_type, flag_value, reason)
    VALUES (NEW.id, 'scanner_flag', COALESCE(NEW.exe_path, NEW.uwp_package_fullname), NEW.security_reason);
END;

-- Trigger to close installs not reported when scan completes.
CREATE TRIGGER trg_scans_complete_close_installs
AFTER UPDATE OF completed_at ON scans
FOR EACH ROW
WHEN NEW.completed_at IS NOT NULL
BEGIN
    UPDATE installs
    SET is_active = 0,
        removed_at = NEW.completed_at,
        updated_at = CURRENT_TIMESTAMP
    WHERE host_id = NEW.host_id
      AND is_active = 1
      AND last_seen_at < NEW.started_at
      AND NOT EXISTS (
            SELECT 1 FROM scan_items si
            WHERE si.scan_id = NEW.id
              AND si.host_id = NEW.host_id
              AND (
                    (si.package_id IS NOT NULL AND si.package_id = installs.package_id)
                 OR (si.binary_id IS NOT NULL AND si.binary_id = installs.binary_id)
              )
        );
END;
```

## Consultas típicas
```sql
-- Último estado por host (apps activas con metadatos)
SELECT *
FROM v_current_installs
WHERE hostname = :hostname;

-- Cambios entre dos escaneos (emulación FULL OUTER JOIN en SQLite)
WITH
items_old AS (
    SELECT COALESCE(exe_path, uwp_package_fullname) AS key, raw_name, raw_version
    FROM scan_items WHERE scan_id = :old_scan_id
),
items_new AS (
    SELECT COALESCE(exe_path, uwp_package_fullname) AS key, raw_name, raw_version
    FROM scan_items WHERE scan_id = :new_scan_id
)
SELECT n.key AS match_key, n.raw_name AS app_name,
       CASE WHEN o.key IS NULL THEN 'added'
            WHEN IFNULL(o.raw_version, '') <> IFNULL(n.raw_version, '') THEN 'updated'
            ELSE 'unchanged' END AS change_type,
       o.raw_version AS old_version,
       n.raw_version AS new_version
FROM items_new n
LEFT JOIN items_old o ON o.key = n.key
UNION ALL
SELECT o.key AS match_key, o.raw_name AS app_name,
       'removed' AS change_type,
       o.raw_version AS old_version,
       NULL AS new_version
FROM items_old o
LEFT JOIN items_new n ON n.key = o.key
WHERE n.key IS NULL;
```
```sql
-- Búsqueda por nombre o publisher
SELECT *
FROM v_current_installs
WHERE display_name LIKE '%' || :term || '%'
   OR publisher LIKE '%' || :term || '%';

-- Top publishers por host
SELECT hostname, publisher, COUNT(*) AS installs
FROM v_current_installs
GROUP BY hostname, publisher
ORDER BY installs DESC
LIMIT 10;

-- Apps con security_flag en la última semana
SELECT sf.finding_id, sf.hostname, sf.display_name, sf.reason, sf.created_at
FROM v_security_alerts sf
WHERE sf.created_at >= datetime('now', '-7 days');
```

## Política de UPSERTs
- **Deduplicación**
  - Ejecutables/atajos: resolver/crear `binaries` por `exe_path` (y `target_path` para shortcuts). `installs` asegura unicidad con `idx_installs_host_binary`.
  - UWP: resolver/crear `packages` por `package_fullname`. `installs` usa `idx_installs_host_package`.
- **Shortcut vs ejecutable real**: si `source='shortcut'` trae `target_path`, intentar mapear a un `binary` existente (`exe_path = target_path`). Si se encuentra, reutilizar `binary_id`; de lo contrario se almacena como binario independiente con `target_path` rellenado. `installs.source` conserva el origen reportado.
- **Actualización de metadatos**: para `packages` y `binaries`, usar `INSERT ... ON CONFLICT DO UPDATE` para refrescar `version`, `publisher`, `file_hash`, `icon_path`. Sólo se sobrescriben campos cuando llega un valor no nulo (`COALESCE` preserva existentes).
- **Normalización de catálogo**: `apps_catalog` se resuelve por `normalized_name` (`lower(trim(name))`) y `publisher`. Si no existe, se inserta; de lo contrario se reutiliza `id`.
- **Installs**: el trigger `trg_scan_items_after_insert` hace SCD Tipo 2: reactiva instalación existente (actualiza `last_seen_at`, limpia `removed_at`) o inserta nueva con `first_seen_at = scanned_at`.
- **Ítems omitidos**: cuando se finaliza el escaneo (`completed_at` distinto de NULL) se dispara `trg_scans_complete_close_installs`, marcando `is_active = 0` y `removed_at = completed_at` para instalaciones no reportadas.

## Plan de migraciones
- `001_init.sql`: crear tablas base y restricciones.
- `002_indexes.sql`: añadir índices y constraints únicos parciales.
- `003_views_triggers.sql`: definir vistas y triggers (normalización, upsert, cierre de instalaciones, hallazgos de seguridad).

### Notas PostgreSQL
- Cambiar `INTEGER PRIMARY KEY AUTOINCREMENT` por `GENERATED BY DEFAULT AS IDENTITY`.
- Reemplazar `datetime('now', '-7 days')` con `CURRENT_TIMESTAMP - INTERVAL '7 days'`.
- Triggers deben escribirse en PL/pgSQL usando `NEW.column :=` asignaciones.
- Partial indexes idénticos (`CREATE UNIQUE INDEX ... WHERE ...`).
- Aprovechar `citext` para `hostname`, `display_name`, `publisher` si se desea comparaciones case-insensitive.
- `name_hash` puede ser columna generada `stored` para soportar índices hash.

## Compatibilidad con Flask
- **Validación de payload**
  - `host`: string no vacío.
  - Cada elemento en `apps`: requiere `name`, `source`, `scanned_at` ISO8601.
  - Si `source IN ('exe','shortcut')` → `exe_path` obligatorio.
  - Si `source = 'uwp'` → `uwp_package_fullname` obligatorio.
  - Campos opcionales: `version`, `publisher`, `icon_path`, `hash`, `security_flag`, `reason`.
- **DAO / Endpoints sugeridos**
  1. `create_scan(host)`
     ```sql
     INSERT INTO hosts(hostname) VALUES (?)
     ON CONFLICT(hostname) DO NOTHING;

     INSERT INTO scans(host_id, started_at)
     VALUES ((SELECT id FROM hosts WHERE hostname = ?), CURRENT_TIMESTAMP);
     ```
  2. `upsert_scan_item(scan_id, item)`
     - Resolver `apps_catalog`:
       ```sql
       INSERT INTO apps_catalog(display_name, normalized_name, publisher)
       VALUES (:display_name, lower(trim(:display_name)), COALESCE(:publisher, ''))
       ON CONFLICT(normalized_name, publisher) DO UPDATE SET display_name = excluded.display_name,
           updated_at = CURRENT_TIMESTAMP;
       ```
     - Resolver `packages` o `binaries` con `ON CONFLICT DO UPDATE` (ver ejemplo en sección performance).
     - Insertar en `scan_items` con ids resueltos.
  3. `finalize_scan(scan_id)`
     ```sql
     UPDATE scans SET completed_at = CURRENT_TIMESTAMP WHERE id = :scan_id;
     ```
- **Batch insert**: usar transacción por escaneo; preparar statements y usar `executemany` para `scan_items`. Aplicar `PRAGMA journal_mode=WAL;` y `PRAGMA synchronous=NORMAL;` al abrir la conexión SQLite (`server/apps.sqlite`).

## Estrategia de performance
- Índices compuestos clave: `idx_installs_host_package`, `idx_installs_host_binary`, `idx_apps_catalog_norm`, `idx_apps_catalog_publisher`.
- Mantener PRAGMAs: `journal_mode = WAL`, `synchronous = NORMAL`, `foreign_keys = ON`.
- Reutilizar conexiones en Flask con `g`/`app.app_context()` y transacciones por request `/apps/scan`.
- Resolver IDs de catálogo con caché en memoria por request para reducir lecturas repetidas.

## Dataset de ejemplo
```sql
-- Hosts
INSERT INTO hosts (hostname) VALUES ('PC-JUAN'), ('PC-TOMI');

-- Apps catalog (se normalizarán vía trigger)
INSERT INTO apps_catalog (display_name, normalized_name, publisher)
VALUES ('Photo Viewer', 'Photo Viewer', 'Contoso'),
       ('Secure Editor', 'Secure Editor', 'Fabrikam'),
       ('Quick Notes', 'Quick Notes', 'Wingtip');

-- Packages (UWP)
INSERT INTO packages (package_fullname, package_name, publisher, version)
VALUES ('Contoso.PhotoViewer_1.0.0.0_x64__8wekyb3d8bbwe', 'Contoso.PhotoViewer', 'Contoso', '1.0.0.0');

-- Binaries (exe + shortcut apuntando al mismo exe)
INSERT INTO binaries (exe_path, target_path, icon_path, file_hash, version, publisher)
VALUES ('C:\\Program Files\\SecureEditor\\editor.exe', NULL, 'C:\\Program Files\\SecureEditor\\editor.ico', 'sha256:abc', '5.1.2', 'Fabrikam'),
       ('C:\\Users\\Juan\\Desktop\\SecureEditor.lnk', 'C:\\Program Files\\SecureEditor\\editor.exe', NULL, NULL, NULL, 'Fabrikam'),
       ('C:\\Program Files\\QuickNotes\\notes.exe', NULL, 'C:\\Program Files\\QuickNotes\\notes.ico', 'sha256:def', '3.0.0', 'Wingtip');

-- Scans
INSERT INTO scans (host_id, started_at, completed_at)
VALUES (1, '2024-05-01T10:00:00', '2024-05-01T10:05:00'),
       (2, '2024-05-01T11:00:00', '2024-05-01T11:03:00');

-- Scan items host PC-JUAN
INSERT INTO scan_items (scan_id, host_id, app_catalog_id, package_id, binary_id, source, raw_name, raw_version, raw_publisher, exe_path, uwp_package_fullname, icon_path, file_hash, security_flag, security_reason, scanned_at)
VALUES (1, 1, 1, 1, NULL, 'uwp', 'Photo Viewer', '1.0.0.0', 'Contoso', NULL, 'Contoso.PhotoViewer_1.0.0.0_x64__8wekyb3d8bbwe', NULL, NULL, 0, NULL, '2024-05-01T10:00:15');

INSERT INTO scan_items (scan_id, host_id, app_catalog_id, package_id, binary_id, source, raw_name, raw_version, raw_publisher, exe_path, uwp_package_fullname, icon_path, file_hash, security_flag, security_reason, scanned_at)
VALUES (1, 1, 2, NULL, 1, 'exe', 'Secure Editor', '5.1.2', 'Fabrikam', 'C:\\Program Files\\SecureEditor\\editor.exe', NULL, 'C:\\Program Files\\SecureEditor\\editor.ico', 'sha256:abc', 0, NULL, '2024-05-01T10:01:00');

INSERT INTO scan_items (scan_id, host_id, app_catalog_id, package_id, binary_id, source, raw_name, raw_version, raw_publisher, exe_path, uwp_package_fullname, icon_path, file_hash, security_flag, security_reason, scanned_at)
VALUES (1, 1, 2, NULL, 2, 'shortcut', 'Secure Editor Shortcut', NULL, 'Fabrikam', 'C:\\Users\\Juan\\Desktop\\SecureEditor.lnk', NULL, NULL, NULL, 1, 'Shortcut in desktop flagged', '2024-05-01T10:01:30');

-- Scan items host PC-TOMI
INSERT INTO scan_items (scan_id, host_id, app_catalog_id, package_id, binary_id, source, raw_name, raw_version, raw_publisher, exe_path, uwp_package_fullname, icon_path, file_hash, security_flag, security_reason, scanned_at)
VALUES (2, 2, 2, NULL, 1, 'exe', 'Secure Editor', '5.1.2', 'Fabrikam', 'C:\\Program Files\\SecureEditor\\editor.exe', NULL, 'C:\\Program Files\\SecureEditor\\editor.ico', 'sha256:abc', 0, NULL, '2024-05-01T11:00:20');

INSERT INTO scan_items (scan_id, host_id, app_catalog_id, package_id, binary_id, source, raw_name, raw_version, raw_publisher, exe_path, uwp_package_fullname, icon_path, file_hash, security_flag, security_reason, scanned_at)
VALUES (2, 2, 3, NULL, 3, 'exe', 'Quick Notes', '3.0.0', 'Wingtip', 'C:\\Program Files\\QuickNotes\\notes.exe', NULL, 'C:\\Program Files\\QuickNotes\\notes.ico', 'sha256:def', 0, NULL, '2024-05-01T11:01:10');
```

## Tests / Mini-spec
```sql
-- 1) Primer scan crea instalaciones activas
SELECT COUNT(*) AS active_installs_host1
FROM installs
WHERE host_id = 1 AND is_active = 1;
-- Esperado: 3 (Photo Viewer UWP + Secure Editor exe + shortcut)

-- 2) Segundo scan actualiza versión
INSERT INTO scans (host_id, started_at) VALUES (1, '2024-06-01T09:00:00');
INSERT INTO scan_items (scan_id, host_id, app_catalog_id, package_id, binary_id, source, raw_name, raw_version, raw_publisher, exe_path, uwp_package_fullname, icon_path, file_hash, security_flag, security_reason, scanned_at)
VALUES (3, 1, 2, NULL, 1, 'exe', 'Secure Editor', '5.2.0', 'Fabrikam', 'C:\\Program Files\\SecureEditor\\editor.exe', NULL, 'C:\\Program Files\\SecureEditor\\editor.ico', 'sha256:xyz', 0, NULL, '2024-06-01T09:02:00');
UPDATE scans SET completed_at = '2024-06-01T09:05:00' WHERE id = 3;
SELECT last_seen_at, removed_at, is_active FROM installs WHERE host_id = 1 AND binary_id = 1;
-- Esperado: last_seen_at='2024-06-01T09:02:00', removed_at NULL, is_active=1.

-- 3) Tercer scan omite app => debe quedar inactiva
INSERT INTO scans (host_id, started_at) VALUES (1, '2024-07-01T09:00:00');
-- No se inserta scan_item para Secure Editor
UPDATE scans SET completed_at = '2024-07-01T09:05:00' WHERE id = 4;
SELECT is_active, removed_at FROM installs WHERE host_id = 1 AND binary_id = 1;
-- Esperado: is_active=0 y removed_at='2024-07-01T09:05:00'.

-- 4) Vistas clave
SELECT * FROM v_current_installs WHERE hostname = 'PC-JUAN';
SELECT * FROM v_security_alerts WHERE hostname = 'PC-JUAN';
SELECT * FROM v_latest_scan_per_host;
```
