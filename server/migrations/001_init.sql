-- Hosts discovered via scans; unique by hostname.
CREATE TABLE hosts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname        TEXT NOT NULL UNIQUE,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Catalog of logical applications, normalized by name/publisher.
CREATE TABLE apps_catalog (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name        TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    publisher           TEXT NOT NULL DEFAULT '',
    name_hash           TEXT GENERATED ALWAYS AS (
        normalized_name || '::' || publisher
    ) VIRTUAL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (normalized_name, publisher)
);

-- UWP package metadata deduplicated globally.
CREATE TABLE packages (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    package_fullname        TEXT NOT NULL UNIQUE,
    package_name            TEXT NOT NULL,
    publisher               TEXT,
    version                 TEXT,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Executable or shortcut binary metadata.
CREATE TABLE binaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exe_path        TEXT NOT NULL UNIQUE,
    target_path     TEXT,
    icon_path       TEXT,
    file_hash       TEXT,
    version         TEXT,
    publisher       TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Logical installations (current + historical) per host/app/package/binary.
CREATE TABLE installs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id             INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    app_catalog_id      INTEGER NOT NULL REFERENCES apps_catalog(id) ON DELETE CASCADE,
    package_id          INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    binary_id           INTEGER REFERENCES binaries(id) ON DELETE SET NULL,
    source              TEXT NOT NULL CHECK (source IN ('uwp', 'exe', 'shortcut')),
    first_seen_at       DATETIME NOT NULL,
    last_seen_at        DATETIME NOT NULL,
    removed_at          DATETIME,
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (package_id IS NOT NULL AND binary_id IS NULL) OR
        (package_id IS NULL AND binary_id IS NOT NULL)
    )
);

-- Scan sessions per host.
CREATE TABLE scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id         INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    started_at      DATETIME NOT NULL,
    completed_at    DATETIME,
    total_items     INTEGER NOT NULL DEFAULT 0,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Raw items reported during a scan.
CREATE TABLE scan_items (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id                 INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    host_id                 INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    app_catalog_id          INTEGER NOT NULL REFERENCES apps_catalog(id) ON DELETE CASCADE,
    package_id              INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    binary_id               INTEGER REFERENCES binaries(id) ON DELETE SET NULL,
    source                  TEXT NOT NULL CHECK (source IN ('uwp', 'exe', 'shortcut')),
    raw_name                TEXT NOT NULL,
    raw_version             TEXT,
    raw_publisher           TEXT,
    exe_path                TEXT,
    uwp_package_fullname    TEXT,
    icon_path               TEXT,
    file_hash               TEXT,
    security_flag           INTEGER NOT NULL DEFAULT 0 CHECK (security_flag IN (0,1)),
    security_reason         TEXT,
    scanned_at              DATETIME NOT NULL,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    item_key                TEXT GENERATED ALWAYS AS (COALESCE(exe_path, uwp_package_fullname)) STORED,
    UNIQUE (scan_id, item_key)
);

-- Security findings tied to installs or specific scan items.
CREATE TABLE security_findings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id          INTEGER REFERENCES installs(id) ON DELETE CASCADE,
    scan_item_id        INTEGER REFERENCES scan_items(id) ON DELETE CASCADE,
    flag_type           TEXT NOT NULL,
    flag_value          TEXT,
    reason              TEXT,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (install_id IS NOT NULL) OR (scan_item_id IS NOT NULL)
    )
);
