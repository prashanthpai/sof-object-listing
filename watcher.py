#!/usr/bin/env python

import errno
import hashlib
import logging
import os
import re
import requests
import sys
import time
from watchdog import events
from watchdog import observers
import xattr

from gluster.swift.common.utils import write_pickle
from swift.common.utils import generate_trans_id, hash_path, \
    normalize_timestamp, split_path

DISK_READ_CHUNK_SIZE = 65536
SP = 2


def get_container_listing(account, container):
    # TODO: Use swift's direct client ?
    r = requests.get('http://127.0.0.1:8080/v1/' +
                     account + '/' + container)
    return filter(None, r.text.split('\n'))


def crawl_sync(device_path):

    logging.debug("Starting initial crawl...")

    for account in os.listdir(device_path):
        if account.startswith('async_pending') or \
                account.startswith('.glusterfs'):
            continue
        account_path = os.path.join(device_path, account)
        logging.debug("\nAccount: %s" % account)
        for container in os.listdir(account_path):
            f_listing = []
            container_path = os.path.join(account_path, container)
            for root, dirs, files in os.walk(container_path):
                for name in files:
                    file_path = os.path.join(root, name)
                    obj_name = file_path[(len(container_path) + 1):]
                    f_listing.append(obj_name)
            o_listing = get_container_listing(account, container)

            to_delete = list(set(o_listing)-set(f_listing))
            to_add = list(set(f_listing)-set(o_listing))

            for i in to_add:
                update_container(os.path.join(container_path, str(i)),
                                 'PUT')

            for i in to_delete:
                update_container(os.path.join(container_path, str(i)),
                                 'DELETE')

            logging.debug("Container: %s Additions: %d Deletions: %d" %
                          (container, len(to_add), len(to_delete)))

    logging.debug("\nCrawl end.\n")


def compute_etag(fp):
    etag = hashlib.md5()
    while True:
        chunk = os.read(fp, DISK_READ_CHUNK_SIZE)
        if chunk:
            etag.update(chunk)
        else:
            break
    return etag.hexdigest()


def update_container(file_path, op, log=False):

    device_path = sys.argv[1]
    if file_path.startswith(device_path):
        obj_path = file_path[len(device_path):]

    if obj_path.startswith('/async_pending') or \
            obj_path.startswith('/.glusterfs'):
        return

    account, container, obj = split_path(obj_path, 3, 3, True)

    if op == 'PUT':
        # Check if this is temp file created by swiftonfile having
        # dot prefix dot suffix notation
        exp = re.compile('^\..*\.[a-z0-9]{32}$')
        if exp.match(obj):
            # Check again if Swift xattr exist
            try:
                xattr.getxattr(file_path, 'user.swift.metadata')
                return
            except IOError as err:
                if err.errno == errno.ENODATA:
                    pass
                return

    if log:
        logging.debug("%s %s", op, obj_path)

    # Populate data for pickle dump
    headers_out = {}

    if op == 'PUT':
        try:
            fd = os.open(file_path, os.O_RDONLY)
        except OSError as err:
            logging.error("%d %s", err.errno, err.strerror)
            return
        stat = os.fstat(fd)
        headers_out['X-Size'] = str(stat.st_size)
        headers_out['X-Timestamp'] = normalize_timestamp(stat.st_mtime)
        headers_out['X-Etag'] = str(compute_etag(fd))
        os.close(fd)
        headers_out['X-Content-Type'] = 'application/octet-stream'
    elif op == 'DELETE':
        headers_out['X-Timestamp'] = normalize_timestamp(time.time())

    headers_out['X-Trans-Id'] = generate_trans_id('')
    headers_out['User-Agent'] = 'SwiftOnFile watcher'
    headers_out['X-Backend-Storage-Policy-Index'] = str(SP)

    data = {'op': op, 'account': account, 'container': container,
            'obj': obj, 'headers': headers_out}

    # Do stuff that pickle_async_update() in Swift does
    ohash = hash_path(account, container, obj)
    device_path = '/mnt/swiftonfile/test'
    async_dir = os.path.join(device_path, 'async_pending-' + str(SP))
    write_pickle(data,
                 os.path.join(async_dir, ohash[-3:],
                              ohash + '-' + normalize_timestamp(
                              str(time.time()))))


class SwiftOnFileEventHandler(events.FileSystemEventHandler):

    def on_created(self, event):
        super(SwiftOnFileEventHandler, self).on_created(event)
        if not event.is_directory:
            print event.src_path
            update_container(event.src_path, 'PUT', log=True)

    def on_deleted(self, event):
        super(SwiftOnFileEventHandler, self).on_deleted(event)
        if not event.is_directory:
            update_container(event.src_path, 'DELETE', log=True)

if __name__ == "__main__":

    INITIAL_CRAWL = True

    if len(sys.argv) == 2:
        path = sys.argv[1]
    else:
        sys.exit("Pass volume/device name as arg.\n"
                 "Ex: %s /mnt/swiftonfile/test" % sys.argv[0])

    logging.basicConfig(level=logging.DEBUG,
                        format='%(message)s')
    logging.getLogger("requests").setLevel(logging.WARNING)

    if INITIAL_CRAWL:
        crawl_sync(path)

    event_handler = SwiftOnFileEventHandler()
    observer = observers.Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

# Things to do and serious limitations
#
# watchdog uses inotify by default on Linux
# inotify may not be able to catch file changes done by other clients accessing
# the same volume over FUSE or NFS. inotify can only inform of changes that
# occured through the vfs at the actual mountpoint being watched. If the
# changes occured outside that VFS, inotify may be of very little use.
# Possible solution: Running one instance of this script on each brick ?? And
# set inotify to watch xfs brick partition ?
#
# A PUT by Swiftonfile can be differentiated from file creation over FUSE by:
#     * Temp file name pattern - dot-prefix-dot-suffix
#     * Xattr on file - user.swift.metadata
# However, a file deletion cannot be verified whether it was done over
# SwiftOnFile or FUSE
#
# on_create() event is triggered on file open(). This currently gives wrong
# file size and checksum as we check for it before it's closed. We should
# rather catch Inotify's IN_CLOSE_WRITE event directly which indicates
# File was closed (and was open for writing).
#
# Check inotify limit: fs.inotify.max_user_watches or
#                      /proc/sys/fs/inotify/max_user_watches
#
# One pickle dump file is created for every file creation over FUSE. This
# could be too much of an overhead
#
# File modifications over FUSE are not accounted for. For example renames,
# change in file size and md5sum checksum. This matters during HEAD on
# container in terms of bytes used by container.
#
# Watch /mnt/swiftonfile recursively or one instance of this script per
# device/volume ?
