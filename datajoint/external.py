import os
import itertools
import uuid
from collections import Mapping
from .settings import config
from .errors import DataJointError
from .hash import uuid_from_buffer, uuid_from_file
from .table import Table
from .declare import EXTERNAL_TABLE_ROOT
from . import s3 
from .utils import safe_write, safe_copy

CACHE_SUBFOLDING = (2, 2)   # (2, 2) means  "0123456789abcd" will be saved as "01/23/0123456789abcd"


def subfold(name, folds):
    """
    subfolding for external storage:   e.g.  subfold('aBCdefg', (2, 3))  -->  ['ab','cde']
    """
    return (name[:folds[0]].lower(),) + subfold(name[folds[0]:], folds[1:]) if folds else ()


class ExternalTable(Table):
    """
    The table tracking externally stored objects.
    Declare as ExternalTable(connection, database)
    """
    def __init__(self, connection, store=None, database=None):

        # copy constructor -- all QueryExpressions must provide
        if isinstance(connection, ExternalTable):
            other = connection   # the first argument is interpreted as the other object
            super().__init__(other)
            self.store = other.store
            self.spec = other.spec
            self.database = other.database
            self._connection = other._connection
            return

        # nominal constructor
        super().__init__()
        self.store = store
        self.spec = config.get_store_spec(store)
        self.database = database
        self._connection = connection
        if not self.is_declared:
            self.declare()

    @property
    def definition(self):
        return """
        # external storage tracking
        hash  : uuid    #  hash of contents (blob), of filename + contents (attach), or relative filepath (filepath)
        ---
        size      :bigint unsigned     # size of object in bytes
        filepath=null : varchar(1000)  # relative filepath used in the filepath datatype
        contents_hash=null : uuid      # used for the filepath datatype 
        timestamp=CURRENT_TIMESTAMP  :timestamp   # automatic timestamp
        """

    @property
    def table_name(self):
        return '{external_table_root}_{store}'.format(external_table_root=EXTERNAL_TABLE_ROOT, store=self.store)

    def put(self, blob):
        """
        put a binary string in external store
        """
        uuid = uuid_from_buffer(blob)
        if self.spec['protocol'] == 's3':
            s3.Folder(**self.spec).put(
                '/'.join((self.database, '/'.join(subfold(uuid.hex, self.spec['subfolding'])), uuid.hex)), blob)
        else:
            remote_file = os.path.join(os.path.join(
                self.spec['location'], self.database, *subfold(uuid.hex, self.spec['subfolding'])), uuid.hex)
            safe_write(remote_file, blob)
        # insert tracking info
        self.connection.query(
            "INSERT INTO {tab} (hash, size) VALUES (%s, {size}) ON DUPLICATE KEY "
            "UPDATE timestamp=CURRENT_TIMESTAMP".format(
                tab=self.full_table_name, size=len(blob)), args=(uuid.bytes,))
        return uuid

    def fput(self, local_filepath):
        """
        put a file identified by the path of local_filepath relative to spec['stage']
        """
        local_folder = os.path.dirname(local_filepath)
        stage_folder = os.path.join(os.path.abspath(self.spec['stage']), '')
        if not local_folder.startswith(stage_folder):
            raise DataJointError('The path {path} is not in stage {stage}'.format(
                path=local_folder, stage=stage_folder))
        relative_filepath = local_filepath[len(stage_folder):]
        uuid = uuid_from_buffer(init_string=relative_filepath)
        contents_hash = uuid_from_file(local_filepath)
        if self.spec['protocol'] == 's3':
            s3.Folder(**self.spec).fput(relative_filepath, local_filepath, contents_hash=str(contents_hash))
        else:
            remote_file = os.path.join(self.spec['location'], relative_filepath)
            safe_copy(local_filepath, remote_file)
        # insert tracking info
        self.connection.query(
            "INSERT INTO {tab} (hash, size, filepath, contents_hash) VALUES (%s, {size}, '{filepath}', %s) "
            "ON DUPLICATE KEY UPDATE timestamp=CURRENT_TIMESTAMP".format(
                tab=self.full_table_name, size=os.path.getsize(local_filepath),
                filepath=relative_filepath), args=(uuid.bytes, contents_hash.bytes))
        return uuid

    def peek(self, blob_hash, bytes_to_peek=120):
        return self.get(blob_hash, size=bytes_to_peek)

    def get(self, blob_hash, size=-1):
        """
        get an object from external store.
        :param size: max number of bytes to retrieve. If size<0, retrieve entire blob
        """
        if blob_hash is None:
            return None

        # attempt to get object from cache
        blob = None
        cache_folder = config.get('cache', None)
        blob_size = None
        if cache_folder:
            try:
                cache_path = os.path.join(cache_folder, *subfold(blob_hash.hex, CACHE_SUBFOLDING))
                cache_file = os.path.join(cache_path, blob_hash.hex)
                with open(cache_file, 'rb') as f:
                    blob = f.read(size)
            except FileNotFoundError:
                pass
            else:
                if size > 0:
                    blob_size = os.path.getsize(cache_file)

        # attempt to get object from store
        if blob is None:
            if self.spec['protocol'] == 'file':
                subfolders = os.path.join(*subfold(blob_hash.hex, self.spec['subfolding']))
                full_path = os.path.join(self.spec['location'], self.database, subfolders, blob_hash.hex)
                try:
                    with open(full_path, 'rb') as f:
                        blob = f.read(size)
                except FileNotFoundError:
                    raise DataJointError('Lost access to external blob %s.' % full_path) from None
                else:
                    if size > 0:
                        blob_size = os.path.getsize(full_path)
            elif self.spec['protocol'] == 's3':
                full_path = '/'.join(
                    (self.database,) + subfold(blob_hash.hex, self.spec['subfolding']) + (blob_hash.hex,))
                _s3 = s3.Folder(**self.spec)
                if size < 0:
                    blob = _s3.get(full_path)
                else:
                    blob = _s3.partial_get(full_path, 0, size)
                    blob_size = _s3.get_size(full_path)

            if cache_folder and size < 0:
                if not os.path.exists(cache_path):
                    os.makedirs(cache_path)
                safe_write(os.path.join(cache_path, blob_hash.hex), blob)

        return blob if size < 0 else (blob, blob_size)

    def fget(self, filepath_hash):
        """
        sync a file from external store to the local stage
        :param filepath_hash: The hash (UUID) of the relative_path
        :return: hash (UUID) of the contents of the downloaded file or Nones
        """
        if filepath_hash is not None:
            relative_filepath, contents_hash = (self & {hash: filepath_hash}).fetch1('filepath', 'contents_hash')
            local_filepath = os.path.join(os.path.abspath(self.spec['stage']), relative_filepath)
            file_exists = os.path.isfile(local_filepath) and uuid_from_file(local_filepath) == contents_hash
            if not file_exists:
                if self.spec['protocol'] == 's3':
                    contents_hash = s3.Folder(**self.spec).fget(relative_filepath, local_filepath)
                else:
                    remote_file = os.path.join(self.spec['location'], relative_filepath)
                    safe_copy(remote_file, local_filepath)
            assert isinstance(contents_hash, uuid.UUID) 
            return local_filepath, contents_hash

    @property
    def references(self):
        """
        :return: generator of referencing table names and their referencing columns
        """
        return self.connection.query("""
        SELECT concat('`', table_schema, '`.`', table_name, '`') as referencing_table, column_name
        FROM information_schema.key_column_usage
        WHERE referenced_table_name="{tab}" and referenced_table_schema="{db}"
        """.format(tab=self.table_name, db=self.database), as_dict=True)

    def delete_quick(self):
        raise DataJointError('The external table does not support delete_quick. Please use delete instead.')

    def delete(self):
        """
        Delete items that are no longer referenced.
        This operation is safe to perform at any time but may reduce performance of queries while in progress.
        """
        self.connection.query(
            "DELETE FROM `{db}`.`{tab}` WHERE ".format(tab=self.table_name, db=self.database) + (
                    " AND ".join(
                        'hash NOT IN (SELECT {column_name} FROM {referencing_table})'.format(**ref)
                        for ref in self.references) or "TRUE"))
        print('Deleted %d items' % self.connection.query("SELECT ROW_COUNT()").fetchone()[0])

    def clean(self, verbose=True):
        """
        Clean unused data in an external storage repository from unused blobs.
        This must be performed after external_table.delete() during low-usage periods to 
        reduce risks of data loss.
        """
        in_use = set(x.hex for x in self.fetch('hash'))
        if self.spec['protocol'] == 'file':
            count = itertools.count()
            print('Deleting...')
            deleted_folders = set()
            for folder, dirs, files in os.walk(
                    os.path.join(self.spec['location'], self.database), topdown=False):
                if dirs and files:
                    raise DataJointError(
                            'Invalid repository with files in non-terminal folder %s' % folder)
                dirs = set(d for d in dirs if os.path.join(folder, d) not in deleted_folders)
                if not dirs:
                    files_not_in_use = [f for f in files if f not in in_use]
                    for f in files_not_in_use:
                        filename = os.path.join(folder, f)
                        next(count)
                        if verbose:
                            print(filename)
                        os.remove(filename)
                    if len(files_not_in_use) == len(files):
                        os.rmdir(folder)
                        deleted_folders.add(folder)
            print('Deleted %d objects' % next(count))
        elif self.spec['protocol'] == 's3':
            try:
                failed_deletes = s3.Folder(database=self.database, **self.spec).clean(in_use, verbose=verbose)
                # failed_deletes are quietly ignored for now
            except TypeError:
                raise DataJointError('External store {store} configuration is incomplete.'.format(store=self.store))


class ExternalMapping(Mapping):
    """
    The external manager contains all the tables for all external stores for a given schema
    :Example:
        e = ExternalMapping(schema)
        external_table = e[store]
    """
    def __init__(self, schema):
        self.schema = schema
        self._tables = {}

    def __getitem__(self, store):
        """
        Triggers the creation of an external table.
        Should only be used when ready to save or read from external storage.
        :param store: the name of the store
        :return: the ExternalTable object for the store
        """
        if store not in self._tables:
            self._tables[store] = ExternalTable(
                connection=self.schema.connection, store=store, database=self.schema.database)
        return self._tables[store]

    def __len__(self):
        return len(self._tables)
    
    def __iter__(self):
        return iter(self._tables)
