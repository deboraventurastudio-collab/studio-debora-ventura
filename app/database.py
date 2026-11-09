"""Database adapter: SQLite locally and PostgreSQL when DATABASE_URL is configured."""
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "studio.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))


class _PgCursor:
    def __init__(self, cursor, inserted=False):
        self._cursor = cursor
        self._inserted = inserted
        self._lastrowid_read = False

    @property
    def lastrowid(self):
        if not self._inserted or self._lastrowid_read:
            return None
        self._lastrowid_read = True
        row = self._cursor.fetchone()
        return row["id"] if row else None

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        # Match sqlite3.Cursor behavior: allow direct iteration over query results.
        return iter(self._cursor.fetchall())


class _PgConnection:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()

    @staticmethod
    def _sql(sql):
        sql = sql.strip()
        if sql.upper() == "BEGIN IMMEDIATE":
            sql = "BEGIN"
        sql = sql.replace("?", "%s")
        if USE_POSTGRES:
            sql = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", sql, flags=re.I)
            sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.I)
            sql = re.sub(r"TEXT(\s+NOT NULL)?\s+DEFAULT\s+CURRENT_TIMESTAMP",
                         lambda m: "TEXT" + (m.group(1) or "") + " DEFAULT (CURRENT_TIMESTAMP::text)", sql, flags=re.I)
        return sql

    def execute(self, sql, params=()):
        sql = self._sql(sql)
        inserted = bool(re.match(r"(?is)^INSERT\s+INTO\b", sql)) and "RETURNING" not in sql.upper()
        if inserted:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self.raw.cursor()
        cur.execute(sql, tuple(params or ()))
        return _PgCursor(cur, inserted=inserted)

    def executescript(self, script):
        # The schema contains ordinary DDL/DML statements, with no procedural blocks.
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()


class _StringAgg:
    def __init__(self):
        self.values = []
        self.separator = ", "
    def step(self, value, separator):
        if value is not None:
            self.values.append(str(value))
        if separator is not None:
            self.separator = str(separator)
    def finalize(self):
        return self.separator.join(self.values) if self.values else None


def _connect():
    app_env = os.getenv("APP_ENV", "local").strip().lower()
    if app_env == "production" and not USE_POSTGRES:
        raise RuntimeError("Banco permanente não configurado. Defina DATABASE_URL do PostgreSQL no ambiente de produção; o SQLite temporário foi bloqueado para proteger seus cadastros.")
    if USE_POSTGRES:
        try:
            import psycopg2
            from psycopg2.extras import DictCursor
        except ImportError as exc:
            raise RuntimeError("Driver PostgreSQL ausente. Instale psycopg2-binary.") from exc
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        raw = psycopg2.connect(url, cursor_factory=DictCursor, connect_timeout=15)
        return _PgConnection(raw)

    db_path = Path(os.getenv("STUDIO_DB_PATH", str(DEFAULT_DB)))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.create_aggregate("STRING_AGG", 2, _StringAgg)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_column(conn, table, column, definition):
    if USE_POSTGRES:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,)
        ).fetchall()
        existing = {row["column_name"] for row in rows}
    else:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _postgres_schema(script):
    script = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", script, flags=re.I)
    script = re.sub(r"\bREAL\b", "DOUBLE PRECISION", script, flags=re.I)
    script = re.sub(r"TEXT(\s+NOT NULL)?\s+DEFAULT\s+CURRENT_TIMESTAMP",
                    lambda m: "TEXT" + (m.group(1) or "") + " DEFAULT (CURRENT_TIMESTAMP::text)", script, flags=re.I)
    return script


def init_db():
    with _connect() as conn:
        schema = """
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL DEFAULT 60,
            price REAL NOT NULL DEFAULT 0,
            estimated_cost REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT DEFAULT '',
            cost_price REAL NOT NULL DEFAULT 0,
            sale_price REAL NOT NULL DEFAULT 0,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            min_stock INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES clients(id),
            service_id INTEGER NOT NULL REFERENCES services(id),
            starts_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Agendado',
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES clients(id),
            total REAL NOT NULL DEFAULT 0,
            payment_method TEXT NOT NULL DEFAULT 'Pendente',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            item_type TEXT NOT NULL,
            item_id INTEGER,
            description TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_price REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            movement_type TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            note TEXT DEFAULT '',
            sale_id INTEGER REFERENCES sales(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            amount REAL NOT NULL,
            payment_method TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
        if USE_POSTGRES:
            conn.executescript(_postgres_schema(schema))
        else:
            conn.executescript(schema)
        _add_column(conn, "appointments", "total_amount", "REAL NOT NULL DEFAULT 0")
        _add_column(conn, "appointments", "amount_paid", "REAL NOT NULL DEFAULT 0")
        _add_column(conn, "appointments", "payment_method", "TEXT NOT NULL DEFAULT 'Pendente'")
        _add_column(conn, "appointments", "billing_status", "TEXT NOT NULL DEFAULT 'Pendente'")
        _add_column(conn, "appointments", "due_date", "TEXT DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS appointment_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
            service_id INTEGER NOT NULL REFERENCES services(id),
            description TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_price REAL NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0
        )""")
        conn.execute("""INSERT INTO appointment_items(appointment_id,service_id,description,quantity,unit_price,line_total)
            SELECT a.id,a.service_id,sv.name,1,sv.price,sv.price FROM appointments a
            JOIN services sv ON sv.id=a.service_id
            WHERE NOT EXISTS (SELECT 1 FROM appointment_items ai WHERE ai.appointment_id=a.id)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS cash_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cash_type TEXT NOT NULL CHECK(cash_type IN ('servicos','produtos')),
            direction TEXT NOT NULL CHECK(direction IN ('Entrada','Saída')),
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            payment_method TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Recebido',
            due_date TEXT DEFAULT '',
            appointment_id INTEGER REFERENCES appointments(id),
            sale_id INTEGER REFERENCES sales(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        _add_column(conn, "sales", "amount_paid", "REAL NOT NULL DEFAULT 0")
        _add_column(conn, "sales", "status", "TEXT NOT NULL DEFAULT 'Pendente'")
        _add_column(conn, "sales", "notes", "TEXT DEFAULT ''")
        _add_column(conn, "sales", "discount", "REAL NOT NULL DEFAULT 0")
        _add_column(conn, "sales", "discount_type", "TEXT NOT NULL DEFAULT 'valor'")
        _add_column(conn, "sales", "appointment_id", "INTEGER REFERENCES appointments(id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS sale_installments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            installment_no INTEGER NOT NULL,
            amount REAL NOT NULL,
            due_date TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Pendente',
            paid_at TEXT DEFAULT ''
        )""")
        _add_column(conn, "sale_installments", "amount_paid", "REAL NOT NULL DEFAULT 0")
        conn.execute("""UPDATE sales SET amount_paid=total, status='Pago'
                        WHERE amount_paid=0 AND payment_method NOT IN ('Pendente','') AND total>0""")
        conn.execute("""INSERT INTO cash_movements
            (cash_type,direction,amount,description,payment_method,status,sale_id)
            SELECT CASE WHEN si.item_type='produto' THEN 'produtos' ELSE 'servicos' END,
                   'Entrada',s.amount_paid,'Recebimento histórico — venda #'||s.id,
                   s.payment_method,'Recebido',s.id
            FROM sales s JOIN sale_items si ON si.sale_id=s.id
            WHERE s.amount_paid>0
              AND NOT EXISTS (SELECT 1 FROM cash_movements cm
                              WHERE cm.sale_id=s.id AND cm.status='Recebido' AND cm.direction='Entrada')""")
        conn.execute("""INSERT INTO cash_movements
            (cash_type,direction,amount,description,payment_method,status,due_date,sale_id)
            SELECT CASE WHEN si.item_type='produto' THEN 'produtos' ELSE 'servicos' END,
                   'Entrada',s.total-s.amount_paid,'Saldo histórico pendente — venda #'||s.id,
                   'Faturado','Pendente','',s.id
            FROM sales s JOIN sale_items si ON si.sale_id=s.id
            WHERE s.total>s.amount_paid
              AND NOT EXISTS (SELECT 1 FROM cash_movements cm
                              WHERE cm.sale_id=s.id AND cm.status='Pendente' AND cm.direction='Entrada')""")
        conn.commit()


def query(sql, params=()):
    with _connect() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql, params=()):
    with _connect() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def transaction(callback):
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = callback(conn)
        conn.commit()
        return result
