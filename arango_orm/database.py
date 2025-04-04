"""
A wrapper around python-arango's database class.

Adds some SQLAlchemy like ORM methods to it.
"""

import logging
from copy import deepcopy
from inspect import isclass
from typing import Literal

from arango.database import StandardDatabase as ArangoDatabase
from arango.exceptions import CollectionDeleteError

# from arango.executor import DefaultExecutor
from .collections import Collection, Relation
from .event import dispatch
from .graph import Graph
from .query import Query

log = logging.getLogger(__name__)


class Database(ArangoDatabase):
    """
    Serves similar to SQLAlchemy's session object with the exception that it
    also allows creating and dropping collections etc.
    """

    def __init__(self, db: ArangoDatabase):
        """Create database instance."""
        self._db: ArangoDatabase = db
        super(Database, self).__init__(db._conn)

    #         super(Database, self).__init__(
    #             connection=connection,
    #             executor=DefaultExecutor(connection)
    # )

    def _verify_collection(self, col) -> bool:
        """
        Verifies that col is a collection class or object.
        """

        CollectionClass = None

        if isclass(col):
            CollectionClass = col
        else:
            CollectionClass = col.__class__

        if CollectionClass is Collection or issubclass(CollectionClass, Collection):
            return col.__collection__ is not None

        return False

    def _verify_relation(self, col) -> bool:
        """
        Verifies that col is a relation class or object.
        """

        CollectionClass = None

        if isclass(col):
            CollectionClass = col
        else:
            CollectionClass = col.__class__

        if CollectionClass is Relation or issubclass(CollectionClass, Relation):
            return col.__collection__ is not None

        return False

    def _entity_pre_process(self, data: dict) -> None:
        "Clean up data dict before add/update into the db."
        for k in ("_key", "_rev"):
            if k in data and data[k] is None:
                del data[k]

    def _entity_post_process(self, entity: Collection, result: dict) -> None:
        "Update entity after add/update."
        for k in (("key_", "_key"), ("rev_", "_rev")):
            if result.get(k[1], None) is not None:
                setattr(entity, k[0], result[k[1]])
                entity._dirty.remove(k[0])

    def has_collection(self, collection):
        "Confirm that the given collection class or collection name exists in the db"

        collection_name = None

        if isclass(collection) and hasattr(collection, "__collection__"):
            collection_name = collection.__collection__

        elif isinstance(collection, str):
            collection_name = collection

        assert collection_name is not None

        return self._db.has_collection(collection_name)

    def create_collection(self, collection: Collection):
        "Create a collection"

        self._verify_collection(collection)

        col_args = {}
        if "col_args" in collection._collection_config:
            col_args = collection._collection_config["col_args"]

        if self._verify_relation(collection) and "edge" not in col_args:
            col_args["edge"] = True

        col = super(Database, self).create_collection(name=collection.__collection__, **col_args)

        if "indexes" in collection._collection_config:
            for index in collection._collection_config["indexes"]:
                # index_type: Literal["hash", "fulltext", "skiplist", "geo", "persistent", "ttl"]
                # fields: list[str]
                # unique: bool
                # sparse: bool
                index_create_method_name = "add_{}_index".format(index["index_type"])

                d = deepcopy(index)
                del d["index_type"]

                # create the index
                getattr(col, index_create_method_name)(**d)

    def drop_collection(self, collection):
        "Drop a collection"
        self._verify_collection(collection)

        super(Database, self).delete_collection(name=collection.__collection__)

    def has(self, collection, key):
        """Check if the document with key exists in the given collection."""

        return self._db.collection(collection.__collection__).has(key)

    def exists(self, document):
        """
        Check if document exists in database.

        Similar to has but takes in a document object and searches
        using it's _key.
        """

        return self._db.collection(document.__collection__).has(document.key_)

    def add(self, entity: Collection, if_present: Literal["ignore", "update"] = None):
        """
        Add a record to a collection.

        :param if_present: Can be None, 'ignore' or 'update'.
            In case of None, if the document is already present then
            arango.exceptions.DocumentInsertError is raised. 'ignore' ignores
            raising the exception. 'update' updates the document if it already
            exists.
        """
        assert if_present in [None, "ignore", "update"]
        if if_present and getattr(entity, "key_", None):
            # for these cases, first check if document exists
            if self.exists(entity):
                if if_present == "ignore":
                    setattr(entity, "_db", self)
                    return entity

                elif if_present == "update":
                    return self.update(entity)

        data_json = entity.model_dump(mode="json", by_alias=True)

        self._entity_pre_process(data_json)
        dispatch(entity, "pre_add", db=self)

        collection = self._db.collection(entity.__collection__)
        setattr(entity, "_db", self)
        res = collection.insert(data_json)

        self._entity_post_process(entity, res)
        entity._dirty.clear()

        dispatch(entity, "post_add", db=self, result=entity)
        return entity

    def bulk_add(self, entity_list, only_dirty=False, **kwargs):
        """
        Add all provided documents, attaching generated _key to entities if generated
        :param entity_list: List of Collection/Relationship objects
        :return: { collection_name : {
                collection_model: Collection class,
                entity_obj_list: Collection instance,
                entity_dict_list: dict
                }
            }
        """
        collections = {}
        for entity in entity_list:
            collection_model = self._db.collection(entity.__collection__)
            data = entity.model_dump(mode="json", by_alias=True)

            if only_dirty:
                if not entity._dirty:
                    return entity

                data = {k: v for k, v in data.items() if k == "_key" or k in entity._dirty}

            # Clean data dict
            self._entity_pre_process(data)
            dispatch(entity, "pre_update", db=self)

            collection_dict = collections.get(entity.__collection__, dict())
            entity_dict_list = collection_dict.get("entity_dict_list", list())
            entity_obj_list = collection_dict.get("entity_obj_list", list())

            entity_dict_list.append(data)
            entity_obj_list.append(entity)

            collection_dict["entity_dict_list"] = entity_dict_list
            collection_dict["entity_obj_list"] = entity_obj_list
            collection_dict["collection_model"] = collection_model

            collections[entity.__collection__] = collection_dict
            setattr(entity, "_db", self)
            entity._dirty.clear()

        for collection, data in collections.items():
            collection_model = data.get("collection_model")
            entity_dict_list = data.get("entity_dict_list")
            entity_obj_list = data.get("entity_obj_list")

            res = collection_model.insert_many(entity_dict_list, **kwargs)
            for num, entity in enumerate(entity_obj_list, start=0):
                entity._dirty.clear()
                self._entity_post_process(entity, res[num])
                dispatch(entity, "post_add", db=self, result=entity)

        return collections

    def delete(self, entity: Collection, **kwargs):
        """Delete given document."""
        dispatch(entity, "pre_delete", db=self)

        collection = self._db.collection(entity.__collection__)
        collection.delete(entity.key_, **kwargs)

        dispatch(entity, "post_delete", db=self, result=entity)
        return entity

    def bulk_delete(self, entity_list, **kwargs):
        """Bulk delete utility, based on delete method. Return a list of results."""
        res = []
        for entity in entity_list:
            res.append(self.delete(entity, **kwargs))
        return res

    def update(self, entity, only_dirty=False, **kwargs):
        "Update given document"
        collection = self._db.collection(entity.__collection__)
        data = {}

        if only_dirty:
            if not entity._dirty:
                return entity
            dispatch(entity, "pre_update", db=self)  # In case of updates to fields
            data = {
                k: v
                for k, v in entity.model_dump(mode="json", by_alias=True).items()
                if k == "_key" or k in entity._dirty
            }
        else:
            dispatch(entity, "pre_update", db=self)
            data = entity.model_dump(mode="json", by_alias=True)

        setattr(entity, "_db", self)
        self._entity_pre_process(data)

        res = collection.update(data, **kwargs)

        entity._dirty.clear()
        self._entity_post_process(entity, res)
        dispatch(entity, "post_update", db=self, result=entity)

        return entity

    def bulk_update(self, entity_list, only_dirty=False, **kwargs):
        """
        Update all provided documents
        :param entity_list: List of Collection/Relationship objects
        :return: { collection_name : {
                collection_model: Collection class,
                entity_obj_list: Collection instance,
                entity_dict_list: dict
                }
            }
        """

        collections = {}
        for entity in entity_list:
            collection_model = self._db.collection(entity.__collection__)
            data = {}

            if only_dirty:
                if not entity._dirty:
                    return entity

                dispatch(entity, "pre_update", db=self)  # In case of updates to fields
                data = {
                    k: v
                    for k, v in entity.model_dump(mode="json", by_alias=True).items()
                    if k == "_key" or k in entity._dirty
                }
            else:
                dispatch(entity, "pre_update", db=self)
                data = entity.model_dump(mode="json", by_alias=True)

            # dispatch(entity, 'pre_update', db=self)
            collection_dict = collections.get(entity.__collection__, dict())
            entity_dict_list = collection_dict.get("entity_dict_list", list())
            entity_obj_list = collection_dict.get("entity_obj_list", list())

            self._entity_pre_process(data)

            entity_dict_list.append(data)
            entity_obj_list.append(entity)

            collection_dict["entity_dict_list"] = entity_dict_list
            collection_dict["entity_obj_list"] = entity_obj_list
            collection_dict["collection_model"] = collection_model

            collections[entity.__collection__] = collection_dict
            setattr(entity, "_db", self)
            # entity._dirty.clear()

        for _, data in collections.items():
            collection_model = data.get("collection_model")
            entity_dict_list = data.get("entity_dict_list")
            entity_obj_list = data.get("entity_obj_list")

            res = collection_model.update_many(entity_dict_list, **kwargs)
            for num, entity in enumerate(entity_obj_list, start=0):
                entity._dirty.clear()
                self._entity_post_process(entity, res[num])
                dispatch(entity, "post_update", db=self, result=entity)

        return collections

    def query(self, CollectionClass) -> Query:
        "Query given collection"

        return Query(CollectionClass, self)

    def create_graph(self, graph_object: Graph, **kwargs):
        """
        Create a named graph from given graph object
        Optionally can provide a list of collection names as ignore_collections
        so those collections are not created
        """

        graph_edge_definitions = []

        # Create collections manually here so we also create indices
        # defined within the collection class. If we let the create_graph
        # call create the collections, it won't create the indices
        for _, col_obj in graph_object.vertices.items():
            if (
                "ignore_collections" in kwargs
                and col_obj.__collection__ in kwargs["ignore_collections"]
            ):
                continue

            try:
                self.create_collection(col_obj)
            except Exception:
                log.warning(
                    "Error creating collection %s, it probably already exists",
                    col_obj.__collection__,
                )

        for _, rel_obj in graph_object.edges.items():
            if (
                "ignore_collections" in kwargs
                and rel_obj.__collection__ in kwargs["ignore_collections"]
            ):
                continue

            try:
                self.create_collection(rel_obj)
            except Exception:
                log.warning(
                    "Error creating edge collection %s, it probably already exists",
                    rel_obj.__collection__,
                )

        for rel_name, relation_obj in graph_object.edges.items():
            cols_from = graph_object.edge_cols_from[rel_name]
            cols_to = graph_object.edge_cols_to[rel_name]

            from_col_names = [col.__collection__ for col in cols_from]
            to_col_names = [col.__collection__ for col in cols_to]

            graph_edge_definitions.append(
                {
                    "edge_collection": relation_obj.__collection__,
                    "from_vertex_collections": from_col_names,
                    "to_vertex_collections": to_col_names,
                }
            )

        self._db.create_graph(graph_object.__graph__, graph_edge_definitions)

    def drop_graph(self, graph_object, drop_collections=True, **kwargs):
        """
        Drop a graph.

        If drop_collections is True, drop all vertices and edges
        too. Optionally can provide a list of collection names as
        ignore_collections so those collections are not dropped
        """
        self._db.delete_graph(
            graph_object.__graph__,
            ignore_missing=True,
            drop_collections=drop_collections,
        )

    def update_graph(self, graph_object: Graph, graph_info=None):
        """
        Update existing graph object by adding collections and edge collections
        that are present in graph definition but not present within the graph
        in the database.

        Note: We delete edge definitions if they no longer exist in the graph
        class but we don't drop collections
        """

        if graph_info is None:
            graph_info = self._get_graph_info(graph_object)

        # Create collections manually here so we also create indices
        # defined within the collection class. If we let the create_graph
        # call create the collections, it won't create the indices
        existing_collection_names = [c["name"] for c in self.collections()]
        for _, col_obj in graph_object.vertices.items():
            try:
                if col_obj.__collection__ in existing_collection_names:
                    log.debug("Collection %s already exists", col_obj.__collection__)
                    continue

                log.info("+ Creating collection %s", col_obj.__collection__)
                self.create_collection(col_obj)

            except Exception:
                log.warning(
                    "Error creating collection %s, it probably already exists",
                    col_obj.__collection__,
                )

        for _, rel_obj in graph_object.edges.items():
            try:
                if rel_obj.__collection__ in existing_collection_names:
                    log.debug("Collection %s already exists", rel_obj.__collection__)
                    continue

                log.info("+ Creating edge collection %s", rel_obj.__collection__)
                self.create_collection(rel_obj, edge=True)
            except Exception:
                log.warning(
                    "Error creating edge collection %s, it probably already exists",
                    rel_obj.__collection__,
                )

        existing_edges = dict(
            [(e["edge_collection"], e) for e in graph_object._graph.edge_definitions()]
        )

        for rel_name, relation in graph_object.edges.items():
            cols_from = graph_object.edge_cols_from[rel_name]
            cols_to = graph_object.edge_cols_to[rel_name]

            from_col_names = [col.__collection__ for col in cols_from]
            to_col_names = [col.__collection__ for col in cols_to]

            edge_definition = {
                "edge_collection": relation.__collection__,
                "from_vertex_collections": from_col_names,
                "to_vertex_collections": to_col_names,
            }

            # if edge does not already exist, create it
            if edge_definition["edge_collection"] not in existing_edges:
                log.info("  + creating graph edge definition: %r", edge_definition)
                graph_object._graph.create_edge_definition(**edge_definition)
            else:
                # if edge definition exists, see if it needs updating
                # compare edges
                if not self._is_same_edge(
                    edge_definition,
                    existing_edges[edge_definition["edge_collection"]],
                ):
                    # replace_edge_definition
                    log.info(
                        "  graph edge definition modified, updating:\n new: %r\n old: %r",
                        edge_definition,
                        existing_edges[edge_definition["edge_collection"]],
                    )
                    graph_object._graph.replace_edge_definition(**edge_definition)

        # Remove any edge definitions that are present in DB but not in graph definition
        graph_connections = dict(
            [(gc.relation.__collection__, gc) for gc in graph_object.graph_connections]
        )

        for edge_name, ee in existing_edges.items():
            if edge_name not in graph_connections:
                log.warning(
                    "  - dropping edge no longer present in graph definition. "
                    "Please drop the edge and vertex collections manually if you no "
                    "longer need them: \n%s",
                    ee,
                )

                graph_object._graph.delete_edge_definition(edge_name)

    def _is_same_edge(self, e1, e2):
        """
        Compare given edge dicts and return True if both dicts have same keys and values else
        return False
        """

        # {'name': 'dns_info', 'to_collections': ['domains'], 'from_collections': ['dns_records']}
        assert e1["edge_collection"] == e2["edge_collection"]

        if len(e1["to_vertex_collections"]) != len(e2["to_vertex_collections"]) or len(
            e1["from_vertex_collections"]
        ) != len(e2["from_vertex_collections"]):
            return False

        else:
            # if same length compare values
            for cname in e1["to_vertex_collections"]:
                if cname not in e2["to_vertex_collections"]:
                    return False

            for cname in e1["from_vertex_collections"]:
                if cname not in e2["from_vertex_collections"]:
                    return False

        return True

    def _get_graph_info(self, graph_obj):
        graphs_info = self.graphs()
        for gi in graphs_info:
            if gi["name"] == graph_obj.__graph__:
                return gi

        return None

    def create_all(self, db_objects):
        """
        Create all objects (collections, relations and graphs).

        Create all objects present in the db_objects list.
        """
        # Collect all graphs
        graph_objs = [obj for obj in db_objects if hasattr(obj, "__graph__")]

        for graph_obj in graph_objs:
            graph_info = self._get_graph_info(graph_obj)
            if not graph_info:
                # graph does not exist, create it
                log.info("Creating graph %s", graph_obj.__graph__)
                graph_instance = graph_obj(connection=self)
                self.create_graph(graph_instance)
            else:
                # Graph exists, determine changes and update graph accordingly
                log.debug("Graph %s already exists", graph_obj.__graph__)
                graph_instance = graph_obj(connection=self)
                self.update_graph(graph_instance, graph_info)

        exclude_collections = [c["name"] for c in self._db.collections()]

        for obj in db_objects:
            if hasattr(obj, "__bases__") and Collection in obj.__bases__:
                if obj.__collection__ not in exclude_collections:
                    log.info("Creating collection %s", obj.__collection__)
                    self.create_collection(obj)
                else:
                    log.debug("Collection %s already exists", obj.__collection__)

    def drop_all(self, db_objects):
        """
        Drop all objects (collections, relations and graphs).

        Drop all objects present in the db_objects list.
        """
        # Collect all graphs
        graph_objs = [obj for obj in db_objects if hasattr(obj, "__graph__")]

        for graph_obj in graph_objs:
            graph_info = self._get_graph_info(graph_obj)
            if graph_info:
                # graph exists, drop it
                log.info("Dropping graph %s", graph_obj.__graph__)
                graph_instance = graph_obj(connection=self)
                self.drop_graph(graph_instance)
            else:
                # Graph exists, determine changes and update graph accordingly
                log.debug("Graph %s does not exist", graph_obj.__graph__)

        for obj in db_objects:
            if hasattr(obj, "__bases__") and Collection in obj.__bases__:
                try:
                    self.drop_collection(obj)
                except CollectionDeleteError:
                    log.debug(
                        "Not deleting missing collection: %s",
                        obj.__collection__,
                    )
