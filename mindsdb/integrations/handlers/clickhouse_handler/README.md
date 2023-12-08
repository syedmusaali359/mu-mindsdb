# ClickHouse Handler

This is the implementation of the ClickHouse handler for MindsDB.

## ClickHouse

ClickHouse is a column-oriented database management system (DBMS) for online analytical processing of queries (OLAP). https://clickhouse.com/docs/en/intro/



## Implementation
This handler was implemented using the standard `clickhouse-sqlalchemy` library https://clickhouse-sqlalchemy.readthedocs.io/en/latest/.
Please install it before using this handler:

```
pip install clickhouse-sqlalchemy
```

## Usage

To connect to ClickHouse use add `engine=clickhouse` to the CREATE DATABASE statement as:

```sql
CREATE DATABASE clic
WITH ENGINE = "clickhouse",
PARAMETERS = {
   "host": "127.0.0.1",
    "port": "9000",
    "user": "root",
    "password": "mypass",
     "database": "test_data"
    }
```

Now, you can use this established connection to query your database as follows,

```sql
SELECT * FROM clic.test_data.table
```