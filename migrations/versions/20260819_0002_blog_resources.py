"""add blog resource fields

Revision ID: 20260819_0002
Revises: 88e01695750a
Create Date: 2026-08-19 19:30:00.000000

"""
from alembic import op
from sqlalchemy import inspect, text


revision = '20260819_0002'
down_revision = '88e01695750a'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()

    op.execute("ALTER TABLE resources MODIFY resource_type ENUM('audio','text','slide','blog') NOT NULL")
    op.execute("ALTER TABLE resources MODIFY resource_body LONGTEXT NOT NULL")

    if connection.dialect.name == 'mysql':
        existing_columns = {
            row[0]
            for row in connection.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = 'resources'
            """))
        }
        existing_indexes = {
            row[0]
            for row in connection.execute(text("""
                SELECT index_name
                FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                  AND table_name = 'resources'
            """))
        }
    else:
        inspector = inspect(connection)
        existing_columns = {column['name'] for column in inspector.get_columns('resources')}
        existing_indexes = {index['name'] for index in inspector.get_indexes('resources')}

    columns = {
        'resource_slug': "ADD COLUMN resource_slug VARCHAR(255) NULL",
        'resource_featured_image': "ADD COLUMN resource_featured_image VARCHAR(255) NULL",
        'resource_status': "ADD COLUMN resource_status ENUM('draft','published','suspended','unpublished') NOT NULL DEFAULT 'draft'",
        'resource_published_date': "ADD COLUMN resource_published_date DATETIME NULL",
        'resource_category': "ADD COLUMN resource_category VARCHAR(100) NULL",
        'resource_content_json': "ADD COLUMN resource_content_json LONGTEXT NULL",
    }

    for column_name, alter_sql in columns.items():
        if column_name not in existing_columns:
            op.execute(f"ALTER TABLE resources {alter_sql}")

    op.execute(
        "ALTER TABLE resources "
        "MODIFY resource_status ENUM('draft','published','suspended','unpublished') "
        "NOT NULL DEFAULT 'draft'"
    )

    if 'ix_resources_resource_slug' not in existing_indexes:
        op.execute("CREATE UNIQUE INDEX ix_resources_resource_slug ON resources (resource_slug)")
    if 'ix_resources_resource_published_date' not in existing_indexes:
        op.execute("CREATE INDEX ix_resources_resource_published_date ON resources (resource_published_date)")


def downgrade():
    with op.batch_alter_table('resources', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_resources_resource_published_date'))
        batch_op.drop_index(batch_op.f('ix_resources_resource_slug'))
        batch_op.drop_column('resource_content_json')
        batch_op.drop_column('resource_category')
        batch_op.drop_column('resource_published_date')
        batch_op.drop_column('resource_status')
        batch_op.drop_column('resource_featured_image')
        batch_op.drop_column('resource_slug')

    op.execute("ALTER TABLE resources MODIFY resource_type ENUM('audio','text','slide') NOT NULL")
