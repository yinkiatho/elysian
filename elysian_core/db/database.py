import os

from dotenv import load_dotenv
from peewee import Model, PostgresqlDatabase
from playhouse.shortcuts import ReconnectMixin

load_dotenv()


class ReconnectPostgresqlDatabase(ReconnectMixin, PostgresqlDatabase):
    pass


DATABASE = ReconnectPostgresqlDatabase(
    os.getenv("POSTGRES_DATABASE"),
    user=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST"),
    port=os.getenv("POSTGRES_PORT"),
    autoconnect=True,
)


class BaseModel(Model):
    class Meta:
        database = DATABASE
