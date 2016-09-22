"""
A wrapper around python-arango's database class adding some SQLAlchemy like ORM methods to it.
"""
import logging
from inspect import isclass

from arango.database import Database as ArangoDatabase
from .collections import CollectionBase

log = logging.getLogger(__name__)


class Query(object):
    """
    Class used for querying records from an arangodb collection using a database connection
    """

    def __init__(self, CollectionClass, db=None):

        self._db = db
        self._CollectionClass = CollectionClass
        self._bind_vars = {'@collection': self._CollectionClass.__collection__}
        self._filter_conditions = []
        self._sort_columns = []
        self._limit = None
        self._limit_start_record = 0

    def count(self):
        "Return collection count"

        return self._db.collection(self._CollectionClass.__collection__).count()

    def by_key(self, key, **kwargs):
        "Return a single document using it's key"

        doc_dict = self._db.collection(self._CollectionClass.__collection__).get(key, **kwargs)
        log.warning(doc_dict)
        return self._CollectionClass._load(doc_dict)

    def filter(self, condition, _or=False, **kwargs):
        """
        Filter the results based on given condition. By default filter conditions are joined
        by AND operator if this method is called multiple times. If you want to use the OR operator
        then specify _or=True
        """

        joiner = None
        if len(self._filter_conditions) > 0:
            joiner = 'OR' if _or else 'AND'

        self._filter_conditions.append(dict(condition=condition, joiner=joiner))
        self._bind_vars.update(kwargs)

        return self

    def sort(self, col_name):
        "Add a sort condition, sorting order of ASC or DESC can be provided after col_name and a space"

        self._sort_columns.append(col_name)

        return self

    def limit(self, num_records, start_from=0):

        assert isinstance(num_records, int)
        assert isinstance(start_from, int)

        self._limit = num_records
        self._limit_start_record = start_from

        return self

    def _make_aql(self):
        "Make AQL statement from filter, sort and limit expressions"

        # Order => FILTER, SORT, LIMIT
        aql = 'FOR rec IN @@collection\n'

        # Process filter conditions

        for fc in self._filter_conditions:
            line = ""
            if fc['joiner'] is None:
                line = "FILTER "
            else:
                line = fc['joiner'] + ' '

            line += 'rec.' + fc['condition']
            aql += line + ' '

        # Process Sort
        if self._sort_columns:
            aql += '\n SORT'

            for sc in self._sort_columns:
                aql += ' rec.' + sc + ','

            aql = aql[:-1]

        # Process Limit
        if self._limit:
            aql += "\n LIMIT {}, {} ".format(self._limit_start_record, self._limit)

        return aql

    def update(self, **kwargs):
        pass

    def delete(self):
        pass

    def all(self):
        "Return all records considering current filter conditions (if any)"

        aql = self._make_aql()

        aql += '\n RETURN rec'
        print(aql)

        results = self._db.aql.execute(aql, bind_vars=self._bind_vars)
        ret = []

        for rec in results:
            ret.append(self._CollectionClass._load(rec))

        return ret

    def aql(self, query, **kwargs):
        """
        Return results based on given AQL query. bind_vars already contains @@collection param.
        Query should always refer to the current collection using @collection
        """

        if 'bind_vars' in kwargs:
            kwargs['bind_vars']['@collection'] = self._bind_vars['@collection']
        else:
            kwargs['bind_vars'] = {'@collection': self._bind_vars['@collection']}

        return [self._CollectionClass._load(rec) for rec in self._db.aql.execute(query, **kwargs)]