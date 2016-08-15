# -*- encoding: utf-8 -*-

from backy2.logging import logger
from backy2.locking import Locking
from backy2.locking import setprocname, find_other_procs
from backy2.utils import grouper
from urllib import parse
import datetime
import importlib
import math
import random
import time
import sys


def blocks_from_hints(hints, block_size):
    """ Helper method """
    blocks = set()
    for offset, length, exists in hints:
        start_block = math.floor(offset / block_size)
        end_block = math.ceil((offset + length) / block_size)
        for i in range(start_block, end_block):
            blocks.add(i)
    return blocks


class LockError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)


class Backy():
    """
    """

    def __init__(self, meta_backend, data_backend, config, block_size=None,
            hash_function=None, lock_dir=None, process_name='backy2'):
        if block_size is None:
            block_size = 1024*4096  # 4MB
        if hash_function is None:
            import hashlib
            hash_function = hashlib.sha512
        self.meta_backend = meta_backend
        self.data_backend = data_backend
        self.config = config
        self.block_size = block_size
        self.hash_function = hash_function
        self.locking = Locking(lock_dir)
        self.process_name = process_name

        if setprocname(process_name) != 0:
            raise RuntimeError('Unable to set process name')

        if not self.locking.lock('backy'):
            raise LockError('A backy is running which requires exclusive access.')
        self.locking.unlock('backy')


    def _prepare_version(self, name, size_bytes, from_version_uid=None):
        """ Prepares the metadata for a new version.
        If from_version_uid is given, this is taken as the base, otherwise
        a pure sparse version is created.
        """
        if from_version_uid:
            old_version = self.meta_backend.get_version(from_version_uid)  # raise if not exists
            if not old_version.valid:
                raise RuntimeError('You cannot base on an invalid version.')
            old_blocks = self.meta_backend.get_blocks_by_version(from_version_uid)
        else:
            old_blocks = None
        size = math.ceil(size_bytes / self.block_size)
        # we always start with invalid versions, then validate them after backup
        version_uid = self.meta_backend.set_version(name, size, size_bytes, 0)
        if not self.locking.lock(version_uid):
            raise LockError('Version {} is locked.'.format(version_uid))
        for id in range(size):
            if old_blocks:
                try:
                    old_block = old_blocks[id]
                except IndexError:
                    uid = None
                    checksum = None
                    block_size = self.block_size
                    valid = 1
                else:
                    assert old_block.id == id
                    uid = old_block.uid
                    checksum = old_block.checksum
                    block_size = old_block.size
                    valid = old_block.valid
            else:
                uid = None
                checksum = None
                block_size = self.block_size
                valid = 1

            # the last block can differ in size, so let's check
            _offset = id * self.block_size
            new_block_size = min(self.block_size, size_bytes - _offset)
            if new_block_size != block_size:
                # last block changed, so set back all info
                block_size = new_block_size
                uid = None
                checksum = None
                valid = 1

            self.meta_backend.set_block(
                id,
                version_uid,
                uid,
                checksum,
                block_size,
                valid,
                _commit=False,
                _upsert=False)
        self.meta_backend._commit()
        #logger.info('New version: {}'.format(version_uid))
        self.locking.unlock(version_uid)
        return version_uid


    def ls(self):
        versions = self.meta_backend.get_versions()
        return versions


    def ls_version(self, version_uid):
        # don't lock here, this is not really error-prone.
        blocks = self.meta_backend.get_blocks_by_version(version_uid)
        return blocks


    def stats(self, version_uid=None, limit=None):
        stats = self.meta_backend.get_stats(version_uid, limit)
        return stats


    def get_io_by_source(self, source):
        res = parse.urlparse(source)
        if res.params or res.query or res.fragment:
            raise ValueError('Invalid URL.')
        scheme = res.scheme
        if not scheme:
            raise ValueError('Invalid URL. You must provide the type (e.g. file://)')
        # import io with name == scheme
        # and pass config section io_<scheme>
        IOLib = importlib.import_module('backy2.io.{}'.format(scheme))
        config = self.config(section='io_{}'.format(scheme))
        return IOLib.IO(
                config=config,
                block_size=self.block_size,
                hash_function=self.hash_function,
                )


    def scrub(self, version_uid, source=None, percentile=100):
        """ Returns a boolean (state). If False, there were errors, if True
        all was ok
        """
        if not self.locking.lock(version_uid):
            raise LockError('Version {} is locked.'.format(version_uid))
        self.meta_backend.get_version(version_uid)  # raise if version not exists
        blocks = self.meta_backend.get_blocks_by_version(version_uid)
        if source:
            io = self.get_io_by_source(source)
            io.open_r(source)

        state = True
        for block in blocks:
            if block.uid:
                if percentile < 100 and random.randint(1, 100) > percentile:
                    logger.debug('Scrub of block {} (UID {}) skipped (percentile is {}).'.format(
                        block.id,
                        block.uid,
                        percentile,
                        ))
                    continue
                try:
                    data = self.data_backend.read(block.uid)
                except FileNotFoundError as e:
                    logger.error('Blob not found: {}'.format(str(e)))
                    self.meta_backend.set_blocks_invalid(block.uid, block.checksum)
                    state = False
                    continue
                if len(data) != block.size:
                    logger.error('Blob has wrong size: {} is: {} should be: {}'.format(
                        block.uid,
                        len(data),
                        block.size,
                        ))
                    self.meta_backend.set_blocks_invalid(block.uid, block.checksum)
                    state = False
                    continue
                data_checksum = self.hash_function(data).hexdigest()
                if data_checksum != block.checksum:
                    logger.error('Checksum mismatch during scrub for block '
                        '{} (UID {}) (is: {} should-be: {}).'.format(
                            block.id,
                            block.uid,
                            data_checksum,
                            block.checksum,
                            ))
                    self.meta_backend.set_blocks_invalid(block.uid, block.checksum)
                    state = False
                    continue
                else:
                    if source:
                        source_data = io.read(block, sync=True)
                        if source_data != data:
                            logger.error('Source data has changed for block {} '
                                '(UID {}) (is: {} should-be: {}). NOT setting '
                                'this block invalid, because the source looks '
                                'wrong.'.format(
                                    block.id,
                                    block.uid,
                                    self.hash_function(source_data).hexdigest(),
                                    data_checksum,
                                    ))
                            state = False
                            # We are not setting the block invalid here because
                            # when the block is there AND the checksum is good,
                            # then the source is invalid.
                    logger.debug('Scrub of block {} (UID {}) ok.'.format(
                        block.id,
                        block.uid,
                        ))
            else:
                logger.debug('Scrub of block {} (UID {}) skipped (sparse).'.format(
                    block.id,
                    block.uid,
                    ))
        if state == True:
            self.meta_backend.set_version_valid(version_uid)
        else:
            # version is set invalid by set_blocks_invalid.
            logger.error('Marked version invalid because it has errors: {}'.format(version_uid))
        if source:
            io.close()  # wait for all io

        self.locking.unlock(version_uid)
        return state


    def restore(self, version_uid, target, sparse=False, force=False):
        if not self.locking.lock(version_uid):
            raise LockError('Version {} is locked.'.format(version_uid))

        version = self.meta_backend.get_version(version_uid)  # raise if version not exists
        blocks = self.meta_backend.get_blocks_by_version(version_uid)

        io = self.get_io_by_source(target)
        io.open_w(target, version.size_bytes, force)

        for block in blocks:
            if block.uid:
                data = self.data_backend.read(block.uid)
                assert len(data) == block.size
                data_checksum = self.hash_function(data).hexdigest()
                io.write(block, data)
                if data_checksum != block.checksum:
                    logger.error('Checksum mismatch during restore for block '
                        '{} (is: {} should-be: {}, block-valid: {}). Block '
                        'restored is invalid. Continuing.'.format(
                            block.id,
                            data_checksum,
                            block.checksum,
                            block.valid,
                            ))
                    self.meta_backend.set_blocks_invalid(block.uid, block.checksum)
                else:
                    logger.debug('Restored block {} successfully ({} bytes).'.format(
                        block.id,
                        block.size,
                        ))
            elif not sparse:
                io.write(block, b'\0'*block.size)
                logger.debug('Restored sparse block {} successfully ({} bytes).'.format(
                    block.id,
                    block.size,
                    ))
            else:
                logger.debug('Ignored sparse block {}.'.format(
                    block.id,
                    ))
        self.locking.unlock(version_uid)


    def rm(self, version_uid, force=True, disallow_rm_when_younger_than_days=0):
        if not self.locking.lock(version_uid):
            raise LockError('Version {} is locked.'.format(version_uid))
        version = self.meta_backend.get_version(version_uid)
        if not force:
            # check if disallow_rm_when_younger_than_days allows deletion
            age_days = (datetime.datetime.now() - version.date).days
            if disallow_rm_when_younger_than_days > age_days:
                raise LockError('Version {} is too young. Will not delete.'.format(version_uid))

        num_blocks = self.meta_backend.rm_version(version_uid)
        logger.info('Removed backup version {} with {} blocks.'.format(
            version_uid,
            num_blocks,
            ))
        self.locking.unlock(version_uid)


    def backup(self, name, source, hints, from_version):
        """ Create a backup from source.
        If hints are given, they must be tuples of (offset, length, exists)
        where offset and length are integers and exists is a boolean. Then, only
        data within hints will be backed up.
        Otherwise, the backup reads source and looks if checksums match with
        the target.
        """
        stats = {
                'version_size_bytes': 0,
                'version_size_blocks': 0,
                'bytes_read': 0,
                'blocks_read': 0,
                'bytes_written': 0,
                'blocks_written': 0,
                'bytes_found_dedup': 0,
                'blocks_found_dedup': 0,
                'bytes_sparse': 0,
                'blocks_sparse': 0,
                'start_time': time.time(),
            }
        io = self.get_io_by_source(source)
        io.open_r(source)
        source_size = io.size()

        size = math.ceil(source_size / self.block_size)
        stats['version_size_bytes'] = source_size
        stats['version_size_blocks'] = size

        # Sanity check: check hints for validity, i.e. too high offsets, ...
        if hints:
            max_offset = max([h[0]+h[1] for h in hints])
            if max_offset > source_size:
                raise ValueError('Hints have higher offsets than source file.')

        if hints:
            sparse_blocks = blocks_from_hints([hint for hint in hints if not hint[2]], self.block_size)
            read_blocks = blocks_from_hints([hint for hint in hints if hint[2]], self.block_size)
        else:
            sparse_blocks = []
            read_blocks = range(size)
        sparse_blocks = set(sparse_blocks)
        read_blocks = set(read_blocks)

        try:
            version_uid = self._prepare_version(name, source_size, from_version)
        except RuntimeError as e:
            logger.error(str(e))
            logger.error('Backy exiting.')
            # TODO: Don't exit here, exit in Commands
            exit(4)
        except LockError as e:
            logger.error(str(e))
            logger.error('Backy exiting.')
            # TODO: Don't exit here, exit in Commands
            exit(99)
        if not self.locking.lock(version_uid):
            logger.error('Version {} is locked.'.format(version_uid))
            logger.error('Backy exiting.')
            # TODO: Don't exit here, exit in Commands
            exit(99)

        blocks = self.meta_backend.get_blocks_by_version(version_uid)

        if from_version and hints:
            # SANITY CHECK:
            # Check some blocks outside of hints if they are the same in the
            # from_version backup and in the current backup. If they
            # don't, either hints are wrong (e.g. from a wrong snapshot diff)
            # or source doesn't match. In any case, the resulting backup won't
            # be good.
            logger.info('Starting sanity check with 1% of the blocks. Reading...')
            ignore_blocks = list(set(range(size)) - read_blocks - sparse_blocks)
            random.shuffle(ignore_blocks)
            num_check_blocks = 10
            # 50% from the start
            check_block_ids = ignore_blocks[:num_check_blocks//2]
            # and 50% from random locations
            check_block_ids = set(check_block_ids + random.sample(ignore_blocks, num_check_blocks//2))
            num_reading = 0
            for block in blocks:
                if block.id in check_block_ids and block.uid:  # no uid = sparse block in backup. Can't check.
                    io.read(block)
                    num_reading += 1
            for i in range(num_reading):
                # this is source file data
                source_block, source_data, source_data_checksum = io.get()
                # check metadata checksum with the newly read one
                if source_block.checksum != source_data_checksum:
                    logger.error("Source and backup don't match in regions outside of the hints.")
                    logger.error("Looks like the hints don't match or the source is different.")
                    logger.error("Found wrong source data at block {}: offset {} with max. length {}".format(
                        source_block.id,
                        source_block.id * self.block_size,
                        self.block_size
                        ))
                    # remove version
                    self.meta_backend.rm_version(version_uid)
                    sys.exit(5)
            logger.info('Finished sanity check. Checked {} blocks {}.'.format(num_reading, check_block_ids))

        read_jobs = 0
        for block in blocks:
            if block.id in read_blocks or not block.valid:
                io.read(block.deref())  # adds a read job.
                read_jobs += 1
            elif block.id in sparse_blocks:
                # This "elif" is very important. Because if the block is in read_blocks
                # AND sparse_blocks, it *must* be read.
                self.meta_backend.set_block(block.id, version_uid, None, None, block.size, valid=1, _commit=False)
                stats['blocks_sparse'] += 1
                stats['bytes_sparse'] += block.size
                logger.debug('Skipping block (sparse) {}'.format(block.id))
            else:
                #self.meta_backend.set_block(block.id, version_uid, block.uid, block.checksum, block.size, valid=1, _commit=False)
                logger.debug('Keeping block {}'.format(block.id))

        # now use the readers and write
        done_jobs = 0
        for i in range(read_jobs):
            block, data, data_checksum = io.get()

            stats['blocks_read'] += 1
            stats['bytes_read'] += len(data)

            # dedup
            existing_block = self.meta_backend.get_block_by_checksum(data_checksum)
            if existing_block and existing_block.size == len(data):
                self.meta_backend.set_block(block.id, version_uid, existing_block.uid, data_checksum, len(data), valid=1, _commit=False)
                stats['blocks_found_dedup'] += 1
                stats['bytes_found_dedup'] += len(data)
                logger.debug('Found existing block for id {} with uid {})'.format
                        (block.id, existing_block.uid))
            else:
                block_uid = self.data_backend.save(data)
                self.meta_backend.set_block(block.id, version_uid, block_uid, data_checksum, len(data), valid=1, _commit=False)
                stats['blocks_written'] += 1
                stats['bytes_written'] += len(data)
                logger.debug('Wrote block {} (checksum {}...)'.format(block.id, data_checksum[:16]))
            done_jobs += 1

        io.close()  # wait for all readers
        self.data_backend.close()  # wait for all writers
        if read_jobs != done_jobs:
            logger.error('backy broke somewhere. Backup is invalid.')
            sys.exit(3)

        self.meta_backend.set_version_valid(version_uid)
        self.meta_backend.set_stats(
            version_uid=version_uid,
            version_name=name,
            version_size_bytes=stats['version_size_bytes'],
            version_size_blocks=stats['version_size_blocks'],
            bytes_read=stats['bytes_read'],
            blocks_read=stats['blocks_read'],
            bytes_written=stats['bytes_written'],
            blocks_written=stats['blocks_written'],
            bytes_found_dedup=stats['bytes_found_dedup'],
            blocks_found_dedup=stats['blocks_found_dedup'],
            bytes_sparse=stats['bytes_sparse'],
            blocks_sparse=stats['blocks_sparse'],
            duration_seconds=int(time.time() - stats['start_time']),
            )
        logger.info('New version: {}'.format(version_uid))
        self.locking.unlock(version_uid)
        return version_uid


    def cleanup_fast(self, dt=3600):
        """ Delete unreferenced blob UIDs """
        if not self.locking.lock('backy-cleanup-fast'):
            raise LockError('Another backy cleanup is running.')

        for uid_list in self.meta_backend.get_delete_candidates(dt):
            logger.debug('Cleanup-fast: Deleting UIDs from data backend: {}'.format(uid_list))
            no_del_uids = []
            no_del_uids = self.data_backend.rm_many(uid_list)
            if no_del_uids:
                logger.info('Cleanup-fast: Unable to delete these UIDs from data backend: {}'.format(uid_list))
        self.locking.unlock('backy-cleanup-fast')


    def cleanup_full(self, prefix=None):
        """ Delete unreferenced blob UIDs starting with <prefix> """
        # in this mode, we compare all existing uids in data and meta.
        # make sure, no other backy will start
        if not self.locking.lock('backy'):
            self.locking.unlock('backy')
            raise LockError('Other backy instances are running.')
        # make sure, no other backy is running
        if len(find_other_procs(self.process_name)) > 1:
            raise LockError('Other backy instances are running.')
        active_blob_uids = set(self.data_backend.get_all_blob_uids(prefix))
        active_block_uids = set(self.meta_backend.get_all_block_uids(prefix))
        delete_candidates = active_blob_uids.difference(active_block_uids)
        for delete_candidate in delete_candidates:
            logger.debug('Cleanup: Removing UID {}'.format(delete_candidate))
            try:
                self.data_backend.rm(delete_candidate)
            except FileNotFoundError:
                continue
        logger.info('Cleanup: Removed {} blobs'.format(len(delete_candidates)))
        self.locking.unlock('backy')


    def close(self):
        self.meta_backend.close()
        self.data_backend.close()


    def export(self, version_uid, f):
        self.meta_backend.export(version_uid, f)
        return f


    def import_(self, f):
        self.meta_backend.import_(f)


