
import os, sys
from collections import defaultdict
import sqlite3
from .eventual import eventually

class DBError(Exception):
    pass

def get_schema(version):
    schema_fn = os.path.join(os.path.dirname(__file__),
                             "db-schemas", "v%d.sql" % version)
    return open(schema_fn, "r").read()

class ObservableDatabase:
    def __init__(self, connection):
        self.conn = connection
        self.observers = defaultdict(set)
        self.pending_notifications = []

    def subscribe(self, table, observer):
        self.observers[table].add(observer)

    def unsubscribe(self, table, observer):
        self.observers[table].remove(observer)

    # database methods

    def insert(self, sql, values, table=None):
        new_id = self.conn.execute(sql, values).lastrowid
        if table:
            c = self.conn.execute("SELECT * FROM `%s` WHERE id=?" % table,
                                  (new_id,))
            self.pending_notifications.append( (table, "insert", new_id,
                                                c.fetchone()) )
        return new_id

    def update(self, sql, values, table=None, id=None):
        self.conn.execute(sql, values)
        if table:
            c = self.conn.execute("SELECT * FROM `%s` WHERE id=?" % table,
                                  (id,))
            self.pending_notifications.append( (table, "update", id,
                                                c.fetchone()) )

    def delete(self, sql, values, table, id):
        self.conn.execute(sql, values)
        self.pending_notifications.append( (table, "delete", id, None) )

    def commit(self):
        self.conn.commit()
        for (table, action, id, new_value) in self.pending_notifications:
            for o in self.observers[table]:
                eventually(o, table, action, id, new_value)
        self.pending_notifications[:] = []

def get_db(dbfile, stderr=sys.stderr):
    """Open or create the given db file. The parent directory must exist.
    Returns the db connection object, or raises DBError.
    """

    must_create = not os.path.exists(dbfile)
    try:
        db = sqlite3.connect(dbfile)
    except (EnvironmentError, sqlite3.OperationalError), e:
        raise DBError("Unable to create/open db file %s: %s" % (dbfile, e))
    db.row_factory = sqlite3.Row

    VERSION = 1
    if must_create:
        schema = get_schema(VERSION)
        db.executescript(schema)
        db.execute("INSERT INTO version (version) VALUES (?)", (VERSION,))
        db.commit()

    try:
        version = db.execute("SELECT version FROM version").fetchone()[0]
    except sqlite3.DatabaseError, e:
        # this indicates that the file is not a compatible database format.
        # Perhaps it was created with an old version, or it might be junk.
        raise DBError("db file is unusable: %s" % e)

    if version != VERSION:
        raise DBError("Unable to handle db version %s" % version)

    return db

def make_observable_db(dbfile, stderr=sys.stderr):
    return ObservableDatabase(get_db(dbfile, stderr))
