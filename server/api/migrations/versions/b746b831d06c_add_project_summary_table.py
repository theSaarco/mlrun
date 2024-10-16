# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""add project summary table

Revision ID: b746b831d06c
Revises: cfe2a22173fc
Create Date: 2024-07-09 11:27:28.255723

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "b746b831d06c"
down_revision = "cfe2a22173fc"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "project_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "project", sa.String(length=255, collation="utf8mb3_bin"), nullable=False
        ),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("updated", mysql.DATETIME(timezone=True, fsp=3), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project", name="_project_summaries_uc"),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("project_summaries")
    # ### end Alembic commands ###
