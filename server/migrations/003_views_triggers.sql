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
