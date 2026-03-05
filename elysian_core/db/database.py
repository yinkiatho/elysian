import os

from dotenv import load_dotenv
from peewee import Model
from playhouse.shortcuts import ReconnectMixin
from playhouse.pool import PooledPostgresqlDatabase

load_dotenv()

class ReconnectPooledPostgresqlDatabase(ReconnectMixin, PooledPostgresqlDatabase):
    pass


DATABASE = ReconnectPooledPostgresqlDatabase(
    os.getenv("POSTGRES_DATABASE"),
    user=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST"),
    port=int(os.getenv("POSTGRES_PORT", 5432)),

    max_connections=20,      # max pooled connections
    stale_timeout=300,       # recycle idle connections
    autoconnect=True,
)


class BaseModel(Model):
    class Meta:
        database = DATABASE