"""
Monetdb database backend for Django.

Requires Monetdb: and requires the MonetSQLdb package which comes with it.
"""

import re

try:
    import monetdb
    import monetdb.sql as Database
    DatabaseError = monetdb.monetdb_exceptions.DatabaseError
    IntegrityError = monetdb.monetdb_exceptions.IntegrityError
    NotSupportedError = monetdb.monetdb_exceptions.NotSupportedError
except ImportError, e:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Error loading MonetSQLdb module: %s" % e)

import monetdb
import monetdb.sql
import monetdb.sql.converters as django_conversions
#from MonetSQLdb.constants import FIELD_TYPE, FLAG

from django.db.backends import *
from django.db.backends.monetdb.client import DatabaseClient
from django.db.backends.monetdb.creation import DatabaseCreation
from django.db.backends.monetdb.introspection import DatabaseIntrospection
#from django.db.backends.monetdb.validation import DatabaseValidation

# Raise exceptions for database warnings if DEBUG is on
from django.conf import settings

# MySQLdb-1.2.1 supports the Python boolean type, and only uses datetime
# module for time-related columns; older versions could have used mx.DateTime
# or strings if there were no datetime module. However, MySQLdb still returns
# TIME columns as timedelta -- they are more like timedelta in terms of actual
# behavior as they are signed and include days -- and Django expects time, so
# we still need to override that.
#django_conversions = conversions.copy()

# This should match the numerical portion of the version numbers (we can treat
# versions like 5.0.24 and 5.0.24a as the same). Based on the list of version
# at http://dev.mysql.com/doc/refman/4.1/en/news.html and
# http://dev.mysql.com/doc/refman/5.0/en/news.html .
server_version_re = re.compile(r'(\d{1,2})\.(\d{1,2})\.(\d{1,2})')

# MySQLdb-1.2.1 and newer automatically makes use of SHOW WARNINGS on
# MySQL-4.1 and newer, so the MysqlDebugWrapper is unnecessary. Since the
# point is to raise Warnings as exceptions, this can be done with the Python
# warning module, and this is setup when the connection is created, and the
# standard util.CursorDebugWrapper can be used. Also, using sql_mode
# TRADITIONAL will automatically cause most warnings to be treated as errors.

class CursorWrapper(Database.cursors.Cursor):
    """
    A thin wrapper around MySQLdb's normal cursor class so that we can catch
    particular exception instances and reraise them with the right types.

    Implemented as a wrapper, rather than a subclass, so that we aren't stuck
    to the particular underlying representation returned by Connection.cursor().
    """
    def __init__(self, cursor):
        self.cursor = cursor
        self.__iter__ = cursor.__iter__

    def execute(self, sql, params=()):
        return self.cursor.execute(sql, params)

    def executemany(self, sql, param_list):
        return self.cursor.executemany(sql, param_list)

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return self.__dict__[attr]
        else:
            return getattr(self.cursor, attr)

class DatabaseFeatures(BaseDatabaseFeatures):
    empty_fetchmany_value = []
    update_can_self_select = False
    related_fields_match_type = True

class DatabaseOperations(BaseDatabaseOperations):
    def date_extract_sql(self, lookup_type, field_name):
        # http://dev.mysql.com/doc/mysql/en/date-and-time-functions.html
        return "EXTRACT(%s FROM %s)" % (lookup_type.upper(), field_name)

    def date_trunc_sql(self, lookup_type, field_name):
        fields = ['year', 'month', 'day', 'hour', 'minute', 'second']
        format = ('%%Y-', '%%m', '-%%d', ' %%H:', '%%i', ':%%s') # Use double percents to escape.
        format_def = ('0000-', '01', '-01', ' 00:', '00', ':00')
        try:
            i = fields.index(lookup_type) + 1
        except ValueError:
            sql = field_name
        else:
            format_str = ''.join([f for f in format[:i]] + [f for f in format_def[i:]])
            sql = "CAST(DATE_FORMAT(%s, '%s') AS DATETIME)" % (field_name, format_str)
        return sql

    def drop_foreignkey_sql(self):
        return "DROP FOREIGN KEY"

    def fulltext_search_sql(self, field_name):
        return 'MATCH (%s) AGAINST (%%s IN BOOLEAN MODE)' % field_name

    def no_limit_value(self):
        # 2**64 - 1, as recommended by the MySQL documentation
        return 18446744073709551615L

    def quote_name(self, name):
        if name.startswith('"') and name.endswith('"'):
            return name # Quoting once is enough.
        return name
        #return '"%s"' % name

    def random_function_sql(self):
        return 'RAND()'

    def sql_flush(self, style, tables, sequences):
        # NB: The generated SQL below is specific to MySQL
        # 'TRUNCATE x;', 'TRUNCATE y;', 'TRUNCATE z;'... style SQL statements
        # to clear all tables of all data
        if tables:
            sql = ['SET FOREIGN_KEY_CHECKS = 0;']
            for table in tables:
                sql.append('%s %s;' % (style.SQL_KEYWORD('TRUNCATE'), style.SQL_FIELD(self.quote_name(table))))
            sql.append('SET FOREIGN_KEY_CHECKS = 1;')

            # 'ALTER TABLE table AUTO_INCREMENT = 1;'... style SQL statements
            # to reset sequence indices
            sql.extend(["%s %s %s %s %s;" % \
                (style.SQL_KEYWORD('ALTER'),
                 style.SQL_KEYWORD('TABLE'),
                 style.SQL_TABLE(self.quote_name(sequence['table'])),
                 style.SQL_KEYWORD('AUTO_INCREMENT'),
                 style.SQL_FIELD('= 1'),
                ) for sequence in sequences])
            return sql
        else:
            return []

    def value_to_db_datetime(self, value):
        if value is None:
            return None
        
        # MySQL doesn't support tz-aware datetimes
        if value.tzinfo is not None:
            raise ValueError("MySQL backend does not support timezone-aware datetimes.")

        # MySQL doesn't support microseconds
        return unicode(value.replace(microsecond=0))

    def value_to_db_time(self, value):
        if value is None:
            return None
            
        # MySQL doesn't support tz-aware datetimes
        if value.tzinfo is not None:
            raise ValueError("MySQL backend does not support timezone-aware datetimes.")
        
        # MySQL doesn't support microseconds
        return unicode(value.replace(microsecond=0))

    def year_lookup_bounds(self, value):
        # Again, no microseconds
        first = '%s-01-01 00:00:00'
        second = '%s-12-31 23:59:59.99'
        return [first % value, second % value]

class DatabaseWrapper(BaseDatabaseWrapper):

    operators = {
        'exact': '= %s',
        'iexact': 'LIKE %s',
        'contains': 'LIKE BINARY %s',
        'icontains': 'LIKE %s',
        'regex': 'REGEXP BINARY %s',
        'iregex': 'REGEXP %s',
        'gt': '> %s',
        'gte': '>= %s',
        'lt': '< %s',
        'lte': '<= %s',
        'startswith': 'LIKE BINARY %s',
        'endswith': 'LIKE BINARY %s',
        'istartswith': 'LIKE %s',
        'iendswith': 'LIKE %s',
    }

    def __init__(self, *args, **kwargs):
        super(DatabaseWrapper, self).__init__(*args, **kwargs)

        self.server_version = None

        self.features = DatabaseFeatures()
        self.ops = DatabaseOperations()
        self.client = DatabaseClient(self)
        self.creation = DatabaseCreation(self)
        self.introspection = DatabaseIntrospection(self)
        try:
            self.validation = BaseDatabaseValidation()
        except TypeError:
            self.validation = BaseDatabaseValidation(self)

    def _valid_connection(self):
        if self.connection is not None:
            return True
            try:
                self.connection.ping()
                return True
            except DatabaseError:
                self.connection.close()
                self.connection = None
        return False

    def _cursor(self):
        if self.connection is None:
            settings_dict = self.settings_dict
            if 'NAME' in settings_dict.keys() \
               and 'DATABASE_NAME' not in settings_dict.keys():
                settings_dict['DATABASE_NAME'] = settings_dict['NAME']
            if 'OPTIONS' in settings_dict.keys() \
               and 'DATABASE_OPTIONS' not in settings_dict.keys():
                settings_dict['DATABASE_OPTIONS'] = settings_dict['OPTIONS']

            if not settings_dict['DATABASE_NAME']:
                from django.core.exceptions import ImproperlyConfigured
                raise ImproperlyConfigured, "Please fill out DATABASE_NAME in the settings module before using the database."
            kwargs = {
                'database': settings_dict['DATABASE_NAME'],
                #'detect_types': Database.PARSE_DECLTYPES | Database.PARSE_COLNAMES,
            }
            kwargs.update(settings_dict['DATABASE_OPTIONS'])
            self.connection = Database.connect(**kwargs)
            # Register extract, date_trunc, and regexp functions.
        return self.connection.cursor() #factory=MonetdbCursorWrapper)

    def _DEPRECATED_cursor(self, settings):
        if not self._valid_connection():
            kwargs = {
                'conv': django_conversions,
                'charset': 'utf8',
                'use_unicode': True,
            }
            if settings.DATABASE_USER:
                kwargs['user'] = settings.DATABASE_USER
            if settings.DATABASE_NAME:
                kwargs['db'] = settings.DATABASE_NAME
            if settings.DATABASE_PASSWORD:
                kwargs['passwd'] = settings.DATABASE_PASSWORD
            if settings.DATABASE_HOST.startswith('/'):
                kwargs['unix_socket'] = settings.DATABASE_HOST
            elif settings.DATABASE_HOST:
                kwargs['host'] = settings.DATABASE_HOST
            if settings.DATABASE_PORT:
                kwargs['port'] = int(settings.DATABASE_PORT)
            kwargs.update(self.options)
            self.connection = Database.connect(**kwargs)
        cursor = CursorWrapper(self.connection.cursor())
        return cursor

    def _commit(self):
        return

    def _rollback(self):
        return
        try:
            BaseDatabaseWrapper._rollback(self)
        except NotSupportedError:
            pass

    def get_server_version(self):
        if not self.server_version:
            if not self._valid_connection():
                self.cursor()
            m = server_version_re.match(self.connection.get_server_info())
            if not m:
                raise Exception('Unable to determine MySQL version from version string %r' % self.connection.get_server_info())
            self.server_version = tuple([int(x) for x in m.groups()])
        return self.server_version
