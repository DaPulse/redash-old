try:
    import snowflake.connector

    enabled = True
except ImportError:
    enabled = False

from redash.query_runner import BaseQueryRunner, register
from redash.query_runner import (
    TYPE_STRING,
    TYPE_DATE,
    TYPE_DATETIME,
    TYPE_INTEGER,
    TYPE_FLOAT,
    TYPE_BOOLEAN,
)
from redash.utils import json_dumps, json_loads
import re

TYPES_MAP = {
    0: TYPE_INTEGER,
    1: TYPE_FLOAT,
    2: TYPE_STRING,
    3: TYPE_DATE,
    4: TYPE_DATETIME,
    5: TYPE_STRING,
    6: TYPE_DATETIME,
    7: TYPE_DATETIME,
    8: TYPE_DATETIME,
    13: TYPE_BOOLEAN,
}


def _query_restrictions(query):
    query_without_comments = ''
    for line in query.split('\n'):
        if line.startswith('--'):
            continue  # skip comments
        query_without_comments += ' ' + line  # creates one line query
    query = query_without_comments
    query = query.lower()
    # replace multiple spaces with one space
    query = re.sub(' +', ' ', query)
    # get rid of prefix like bigbrain. or final.
    query = re.sub('bigbrain.', '', re.sub('final.', '', re.sub('raw.', '', query)))
    occurrences = re.findall(" from events ", query) + re.findall(" join events ", query)
    # print("num of occurrences : ", len(occurrences))
    if len(occurrences) > 1:
        return False, f'Querying events table multiple times is forbidden.The query contains {len(occurrences)} occurrences of the table events.'

    if occurrences:
        if query.find("create_at") + query.find("ingestion_time") == -2:
            return False, 'Querying events table should always be with time constraint (by created_at for ' \
                          'FINAL.events & ingestion_time for RAW.events) '
        return True, ''


class Snowflake(BaseQueryRunner):
    noop_query = "SELECT 1"

    @classmethod
    def configuration_schema(cls):
        return {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "user": {"type": "string"},
                "password": {"type": "string"},
                "warehouse": {"type": "string"},
                "database": {"type": "string"},
                "region": {"type": "string", "default": "us-west"},
                "lower_case_columns": {
                    "type": "boolean",
                    "title": "Lower Case Column Names in Results",
                    "default": False,
                },
                "host": {"type": "string"},
            },
            "order": [
                "account",
                "user",
                "password",
                "warehouse",
                "database",
                "region",
                "host",
            ],
            "required": ["user", "password", "account", "database", "warehouse"],
            "secret": ["password"],
            "extra_options": [
                "host",
            ],
        }

    @classmethod
    def enabled(cls):
        return enabled

    @classmethod
    def determine_type(cls, data_type, scale):
        t = TYPES_MAP.get(data_type, None)
        if t == TYPE_INTEGER and scale > 0:
            return TYPE_FLOAT
        return t

    def _get_connection(self):
        region = self.configuration.get("region")
        account = self.configuration["account"]

        # for us-west we don't need to pass a region (and if we do, it fails to connect)
        if region == "us-west":
            region = None

        if self.configuration.__contains__("host"):
            host = self.configuration.get("host")
        else:
            if region:
                host = "{}.{}.snowflakecomputing.com".format(account, region)
            else:
                host = "{}.snowflakecomputing.com".format(account)

        connection = snowflake.connector.connect(
            user=self.configuration["user"],
            password=self.configuration["password"],
            account=account,
            region=region,
            host=host,
        )

        return connection

    def _column_name(self, column_name):
        if self.configuration.get("lower_case_columns", False):
            return column_name.lower()

        return column_name

    def _parse_results(self, cursor):
        columns = self.fetch_columns(
            [
                (self._column_name(i[0]), self.determine_type(i[1], i[5]))
                for i in cursor.description
            ]
        )
        rows = [
            dict(zip((column["name"] for column in columns), row)) for row in cursor
        ]

        data = {"columns": columns, "rows": rows}
        return data

    def run_query(self, query, user, query_id=None):
        connection = self._get_connection()
        cursor = connection.cursor()

        condition, message = _query_restrictions(query)

        if not condition:
            return None, message

        try:
            cursor.execute("USE WAREHOUSE {}".format(self.configuration["warehouse"]))
            cursor.execute("USE {}".format(self.configuration["database"]))

            user_id = "redash" if user is None else user.email
            query_id = str(query_id) if query_id else ''
            query += "-- REDASH USER: " + user_id + " QUERY ID: " + query_id

            cursor.execute(query)

            data = self._parse_results(cursor)
            error = None
            json_data = json_dumps(data)
        finally:
            cursor.close()
            connection.close()

        return json_data, error

    def _run_query_without_warehouse(self, query):
        connection = self._get_connection()
        cursor = connection.cursor()

        try:
            cursor.execute("USE {}".format(self.configuration["database"]))

            cursor.execute(query)

            data = self._parse_results(cursor)
            error = None
        finally:
            cursor.close()
            connection.close()

        return data, error

    def _database_name_includes_schema(self):
        return "." in self.configuration.get("database")

    def get_schema(self, get_stats=False):
        if self._database_name_includes_schema():
            query = "SHOW COLUMNS"
        else:
            query = "SHOW COLUMNS IN DATABASE"

        results, error = self._run_query_without_warehouse(query)

        if error is not None:
            raise Exception("Failed getting schema.")

        schema = {}
        for row in results["rows"]:
            if row["kind"] == "COLUMN":
                table_name = "{}.{}".format(row["schema_name"], row["table_name"])

                if table_name not in schema:
                    schema[table_name] = {"name": table_name, "columns": []}

                schema[table_name]["columns"].append(row["column_name"])

        return list(schema.values())


register(Snowflake)
