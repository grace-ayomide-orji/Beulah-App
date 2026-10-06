from sqlalchemy import text

from beulah_pkg.models import db


RESOURCE_BLOG_COLUMNS = {
    'resource_slug': "ADD COLUMN resource_slug VARCHAR(255) NULL",
    'resource_featured_image': "ADD COLUMN resource_featured_image VARCHAR(255) NULL",
    'resource_status': "ADD COLUMN resource_status ENUM('draft','published','suspended','unpublished') NOT NULL DEFAULT 'draft'",
    'resource_published_date': "ADD COLUMN resource_published_date DATETIME NULL",
    'resource_category': "ADD COLUMN resource_category VARCHAR(100) NULL",
    'resource_content_json': "ADD COLUMN resource_content_json LONGTEXT NULL",
}


def ensure_resource_blog_schema(app):
    with app.app_context():
        if db.engine.dialect.name != 'mysql':
            return

        with db.engine.begin() as connection:
            resources_exists = connection.execute(text("""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 'resources'
            """)).scalar()
            if not resources_exists:
                return

            connection.execute(text(
                "ALTER TABLE resources "
                "MODIFY resource_type ENUM('audio','text','slide','blog') NOT NULL"
            ))
            connection.execute(text(
                "ALTER TABLE resources MODIFY resource_body LONGTEXT NOT NULL"
            ))

            existing_columns = {
                row[0]
                for row in connection.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = DATABASE()
                      AND table_name = 'resources'
                """))
            }

            for column_name, alter_sql in RESOURCE_BLOG_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(text(f"ALTER TABLE resources {alter_sql}"))
                    if column_name == 'resource_status':
                        connection.execute(text(
                            "UPDATE resources "
                            "SET resource_status = 'published' "
                            "WHERE resource_type IN ('text','audio','slide') "
                            "AND resource_is_deleted = 0"
                        ))

            if 'resource_status' in existing_columns:
                connection.execute(text(
                    "ALTER TABLE resources "
                    "MODIFY resource_status ENUM('draft','published','suspended','unpublished') NOT NULL DEFAULT 'draft'"
                ))

            existing_indexes = {
                row[0]
                for row in connection.execute(text("""
                    SELECT index_name
                    FROM information_schema.statistics
                    WHERE table_schema = DATABASE()
                      AND table_name = 'resources'
                """))
            }

            if 'ix_resources_resource_slug' not in existing_indexes:
                connection.execute(text(
                    "CREATE UNIQUE INDEX ix_resources_resource_slug ON resources (resource_slug)"
                ))
            if 'ix_resources_resource_published_date' not in existing_indexes:
                connection.execute(text(
                    "CREATE INDEX ix_resources_resource_published_date ON resources (resource_published_date)"
                ))
