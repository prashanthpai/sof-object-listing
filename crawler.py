#!/usr/bin/env python

import hashlib
import logging
import os
import re
import requests
import sys
import time
from subprocess import call

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
        # Skip over dirs on mountpoint that are not account names
        if account.startswith('async_pending') or \
                account.startswith('.glusterfs') or \
                not os.path.isdir(os.path.join(device_path, account)):
            continue
        account_path = os.path.join(device_path, account)
        logging.debug("\nAccount: %s" % account)
        for container in os.listdir(account_path):
            f_listing = []  # Actual files on mount point
            container_path = os.path.join(account_path, container)
            for root, dirs, files in os.walk(container_path):
                for name in files:
                    file_path = os.path.join(root, name)
                    obj_name = file_path[(len(container_path) + 1):]
                    f_listing.append(obj_name)
            # Objects listing from container DB
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
        # Strip /mnt/swiftonfile/test
        obj_path = file_path[len(device_path):]

    account, container, obj = split_path(obj_path, 3, 3, True)

    if op == 'PUT':
        # Check if this is temp file created by swiftonfile having
        # dot prefix dot suffix notation
        exp = re.compile('^\..*\.[a-z0-9]{32}$')
        if exp.match(obj):
            return

    if log:
        logging.debug("%s %s", op, obj_path)

    # Populate data for pickle dump
    headers_out = {}

    if op == 'PUT':
        try:
            # FS operations - use same fd
            fd = os.open(file_path, os.O_RDONLY)
            stat = os.fstat(fd)
            headers_out['X-Etag'] = str(compute_etag(fd))
        except OSError as err:
            logging.error("%d %s", err.errno, err.strerror)
            return
        finally:
            os.close(fd)
        headers_out['X-Size'] = str(stat.st_size)
        headers_out['X-Timestamp'] = normalize_timestamp(stat.st_mtime)
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
    async_dir = os.path.join(device_path, 'async_pending-' + str(SP))
    write_pickle(data,
                 os.path.join(async_dir, ohash[-3:],
                              ohash + '-' + normalize_timestamp(
                              str(time.time()))))


if __name__ == "__main__":

    if len(sys.argv) == 2:
        path = sys.argv[1]
    else:
        sys.exit("Pass volume/device name as arg.\n"
                 "Ex: %s /mnt/swiftonfile/test" % sys.argv[0])

    logging.basicConfig(level=logging.DEBUG,
                        format='%(message)s')
    logging.getLogger("requests").setLevel(logging.WARNING)

    while True:
        crawl_sync(path)
        call(['swift-object-updater', '-o', '/etc/swift/object-server/5.conf'])
        time.sleep(60)
