-- Ninj-OS Schematics 1.6.1
-- Replace `ninjos_schematics` and `ninjos_schematic_payload_chunks` below if
-- your config uses a different table_prefix.

CREATE TABLE IF NOT EXISTS `ninjos_schematics` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `namespace` VARCHAR(64) NOT NULL,
    `name` VARCHAR(64) NOT NULL,
    `display_name` VARCHAR(128) NOT NULL,
    `description` TEXT NOT NULL,
    `author_uuid` VARCHAR(64) NOT NULL,
    `author_xuid` VARCHAR(32) NOT NULL,
    `author_name` VARCHAR(64) NOT NULL,
    `source_server` VARCHAR(96) NOT NULL,
    `source_dimension` VARCHAR(64) NOT NULL,
    `minecraft_version` VARCHAR(32) NOT NULL,
    `plugin_version` VARCHAR(32) NOT NULL,
    `format_version` SMALLINT UNSIGNED NOT NULL,
    `size_x` INT UNSIGNED NOT NULL,
    `size_y` INT UNSIGNED NOT NULL,
    `size_z` INT UNSIGNED NOT NULL,
    `block_count` BIGINT UNSIGNED NOT NULL,
    `non_air_count` BIGINT UNSIGNED NOT NULL,
    `palette_count` INT UNSIGNED NOT NULL,
    `includes_air` TINYINT(1) NOT NULL,
    `content_sha256` CHAR(64) NOT NULL,
    `compressed_bytes` BIGINT UNSIGNED NOT NULL,
    `uncompressed_bytes` BIGINT UNSIGNED NOT NULL,
    `payload` LONGBLOB NOT NULL,
    `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `updated_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    `deleted_at` TIMESTAMP(6) NULL DEFAULT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_namespace_name` (`namespace`, `name`),
    KEY `idx_namespace_updated` (`namespace`, `updated_at`),
    KEY `idx_namespace_deleted` (`namespace`, `deleted_at`),
    KEY `idx_hash` (`content_sha256`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Large compressed payloads are split into packet-safe MEDIUMBLOB rows. Existing
-- inline LONGBLOB schematics remain readable and do not need to be migrated.
CREATE TABLE IF NOT EXISTS `ninjos_schematic_payload_chunks` (
    `schematic_id` BIGINT UNSIGNED NOT NULL,
    `chunk_index` INT UNSIGNED NOT NULL,
    `chunk_bytes` INT UNSIGNED NOT NULL,
    `chunk_sha256` CHAR(64) NOT NULL,
    `payload` MEDIUMBLOB NOT NULL,
    PRIMARY KEY (`schematic_id`, `chunk_index`),
    KEY `idx_schematic_id` (`schematic_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
